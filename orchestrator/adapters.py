"""Pluggable agent adapters.

One interface, several transports:
  - CliAdapter:      shells out to a CLI (claude / codex / gemini), headless.
  - ApiAdapter: API (HTTP) OR a local CLI (e.g. Ollama) - configurable,
                     neither hardcoded as the only option.
  - MockAdapter:     returns scripted fixture responses; no credentials needed.

Write permission is structural: only an agent with can_edit=True, called with
allow_edit=True, ever receives edit_args. Every other invocation gets
read_only_args. The git guard (see guard.py) independently double-checks.

Nothing here installs anything. Missing commands are detected and reported.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass

OK = "OK"
UNAVAILABLE = "UNAVAILABLE"
ERROR = "ERROR"

# DeepSeek-V4 (and some OpenAI-compatible models) emit native tool-call markup when
# they decide to "use a tool" that does not exist over this transport, e.g.
#   <｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="codebase">...
# instead of a plain-text answer. That markup carries no parseable verdict and aborts
# the run (observed 2026-06-18: deepseek refuter/architect -> HUMAN_REQUIRED). We
# (1) tell API agents up front they have no tools, and (2) strip any residual markup.
_NO_TOOLS_DIRECTIVE = (
    "\n\n[RUNTIME CONSTRAINT] You have NO tools, functions, code execution, or file "
    "access in this environment. Do NOT emit tool calls, function calls, or DSML / "
    "<｜...｜> / <tool_calls> / <invoke> markup of any kind. Return your COMPLETE answer "
    "as plain text in the message content only."
)
# Strip from the first tool-call/DSML tag to end of string (keeps any real text before
# it; collapses a markup-only reply to empty). Matches fullwidth '｜' and ASCII '|'.
_TOOL_MARKUP_RE = re.compile(
    r"<\s*[｜|/\\▁_]*\s*(?:DSML|tool[｜|▁_]*calls|invoke|function[_\s]*calls?)\b.*",
    re.IGNORECASE | re.DOTALL,
)


def _strip_tool_markup(text):
    if not text:
        return ""
    return _TOOL_MARKUP_RE.sub("", text).strip()


@dataclass
class AgentResponse:
    agent: str
    status: str
    text: str = ""
    role: str = ""
    verdict: str | None = None
    error: str = ""
    duration: float = 0.0
    # Token accounting (captured when the transport reports it; None otherwise).
    # Normalized shape: {"input": int|None, "output": int|None, "total": int|None}.
    usage: dict | None = None
    cost_usd: float | None = None  # only claude (--output-format json) reports a $ figure


class Adapter:
    def __init__(self, name: str, cfg: dict, cwd: str | None = None):
        self.name = name
        self.cfg = cfg
        # Working directory every CLI is launched in (the multi-repo workspace
        # root), so agents can read all dev/analysis repos, roles, and docs
        # regardless of where orchestrate.py was invoked from.
        self.cwd = cwd

    @property
    def can_edit(self) -> bool:
        return bool(self.cfg.get("can_edit"))

    def availability(self):  # -> (bool, reason)
        raise NotImplementedError

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900) -> AgentResponse:
        raise NotImplementedError


class CliAdapter(Adapter):
    def _base_cmd(self):
        return self.cfg.get("command") or self.cfg.get("cli_command")

    def availability(self):
        cmd = self._base_cmd()
        if not cmd:
            return False, "no command template configured"
        exe = cmd[0]
        if shutil.which(exe) is None:
            return False, f"'{exe}' not found in PATH"
        return True, "ok"

    def _build(self, system, prompt, allow_edit):
        """Return (argv, stdin_data). stdin_data is None unless cfg['stdin'] is set.

        Passing the prompt on stdin (cfg 'stdin': true) avoids argv-parsing
        pitfalls — e.g. a prompt beginning with '---' (role-file frontmatter)
        being mistaken for a CLI flag — and argv length limits.
        """
        command = list(self._base_cmd())
        sys_arg = list(self.cfg.get("system_arg") or [])
        # `claude -p` reads the prompt from stdin; force stdin for it so a large user
        # prompt (big diff + accumulated disagreement history) never overflows ARG_MAX
        # in argv (OSError [Errno 7] Argument list too long — hit on the dev arbiter 2026-06-21).
        use_stdin = bool(self.cfg.get("stdin")) or (self._base_cmd() or [""])[0] == "claude"
        has_sys_placeholder = any("{system}" in c for c in command + sys_arg)
        # If there's no way to pass a system prompt separately, fold it in.
        full_prompt = prompt if (has_sys_placeholder or sys_arg) else f"{system}\n\n{prompt}"

        def sub(x):
            return x.replace("{prompt}", full_prompt).replace("{system}", system)

        if use_stdin:
            cmd = [sub(c) for c in command if c != "{prompt}"]
            stdin_data = full_prompt
        else:
            cmd = [sub(c) for c in command]
            stdin_data = None
        if sys_arg:
            cmd += [sub(c) for c in sys_arg]
        cmd += self._extra_cli_flags(cmd)
        if allow_edit and self.can_edit:
            cmd += list(self.cfg.get("edit_args") or [])
        else:
            cmd += list(self.cfg.get("read_only_args") or [])
        return cmd, stdin_data

    def _usage_format(self):
        """How this CLI reports token usage: explicit cfg['usage_format'] if set, else
        derived from the executable — `claude` emits a JSON result object, `codex` prints
        a "tokens used <N>" stdout footer. Others (e.g. gemini) report nothing (None)."""
        fmt = self.cfg.get("usage_format")
        if fmt:
            return fmt
        exe = (self._base_cmd() or [""])[0]
        return {"claude": "claude_json", "codex": "codex_text"}.get(exe)

    def _extra_cli_flags(self, cmd):
        """Adapter-injected flags (not from the config template). Currently: make claude
        emit a JSON result object so token usage is captured; the parser reads `.result`
        back out as the text, so verdict parsing is unaffected."""
        if self._usage_format() == "claude_json" and "--output-format" not in cmd:
            return ["--output-format", "json"]
        return []

    def _parse_output(self, stdout):
        """Return (text, usage, cost_usd) from raw CLI stdout, per cfg['usage_format'].
        Fail-safe: ANY parse issue degrades to (stdout, None, None) — it never breaks a
        run, only loses that step's token figure. usage is normalized to
        {"input", "output", "total"} (None where the transport doesn't split it)."""
        fmt = self._usage_format()
        if fmt == "claude_json":
            return self._parse_claude_json(stdout)
        if fmt == "codex_text":
            return self._parse_codex_text(stdout)
        return stdout.strip(), None, None

    @staticmethod
    def _parse_claude_json(stdout):
        # `claude -p --output-format json` emits one object: {result, usage{input_tokens,
        # output_tokens,...}, total_cost_usd, ...}. text MUST be `result` so the verdict
        # parser still sees the assistant's final message.
        try:
            d = json.loads(stdout)
            if isinstance(d, dict) and "result" in d:
                u = d.get("usage") or {}
                inp, out = u.get("input_tokens"), u.get("output_tokens")
                usage = {"input": inp, "output": out,
                         "total": (inp or 0) + (out or 0)} if u else None
                return (d.get("result") or "").strip(), usage, d.get("total_cost_usd")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return stdout.strip(), None, None

    @staticmethod
    def _parse_codex_text(stdout):
        # `codex exec` prints a "tokens used\n<N>" footer; text is left as-is (the verdict
        # parser already reads codex's plain transcript today).
        m = re.search(r"tokens used[:\s]+([\d,]+)", stdout, re.IGNORECASE)
        usage = {"input": None, "output": None,
                 "total": int(m.group(1).replace(",", ""))} if m else None
        return stdout.strip(), usage, None

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        ok, reason = self.availability()
        if not ok:
            return AgentResponse(self.name, UNAVAILABLE, error=reason)
        timeout = int(self.cfg.get("timeout_s", timeout))  # per-agent override (heavy dev steps run cargo)
        cmd, stdin_data = self._build(system, prompt, allow_edit)
        run_env = None
        if self.cfg.get("env"):
            # per-agent env overlay (e.g. MAX_THINKING_TOKENS to raise reasoning effort)
            run_env = {**os.environ, **{k: str(v) for k, v in self.cfg["env"].items()}}
        t0 = time.time()
        try:
            p = subprocess.run(cmd, input=stdin_data, capture_output=True,
                               text=True, timeout=timeout, env=run_env, cwd=self.cwd)
        except Exception as e:  # noqa: BLE001 - report any launch/timeout failure
            return AgentResponse(self.name, ERROR, error=f"{type(e).__name__}: {e}",
                                 duration=time.time() - t0)
        dur = time.time() - t0
        if p.returncode != 0:
            # Keep BOTH ends of stderr. Codex echoes the ENTIRE prompt (role file + inlined
            # artifact) to stderr, which can push the real failure line (e.g. "You've hit your
            # usage limit") past a head-only truncation — then is_unavailable() misses it and the
            # run HARD-ABORTS instead of shifting the cell to a live agent. Such markers live at
            # the tail, so preserve it. (Observed 2026-07-09: a codex usage-limit at review iter 3
            # aborted the whole run because the marker was cut by the old [:4000] head slice.)
            err = (p.stderr or "").strip()
            if len(err) > 4000:
                err = err[:2000] + "\n…[stderr truncated]…\n" + err[-2000:]
            return AgentResponse(self.name, ERROR, text=p.stdout.strip(),
                                 error=err, duration=dur)
        text, usage, cost = self._parse_output(p.stdout)
        return AgentResponse(self.name, OK, text=text, duration=dur, usage=usage, cost_usd=cost)


class ApiAdapter(Adapter):
    """Generic OpenAI-compatible API agent, or a local CLI model (e.g. Ollama),
    selected by cfg['transport'] ('api' | 'cli'). Used for the arbiter (Mistral)."""

    def _transport(self):
        return self.cfg.get("transport", "api")

    def availability(self):
        if self._transport() == "cli":
            return CliAdapter(self.name, self.cfg, self.cwd).availability()
        key = os.environ.get(self.cfg.get("api_env", ""))
        if not key:
            return False, f"env {self.cfg.get('api_env')} not set"
        return True, "api key present"

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        if self._transport() == "cli":
            return CliAdapter(self.name, self.cfg, self.cwd).run(system, prompt, allow_edit, step_id, timeout)
        ok, reason = self.availability()
        if not ok:
            return AgentResponse(self.name, UNAVAILABLE, error=reason)
        key = os.environ[self.cfg["api_env"]]
        payload = {
            "model": self.cfg.get("model", "mistral-large-latest"),
            "messages": [
                {"role": "system", "content": (system or "") + _NO_TOOLS_DIRECTIVE},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        req = urllib.request.Request(
            self.cfg["api_base"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            msg = data["choices"][0]["message"]
            text = _strip_tool_markup(msg.get("content") or "")
            u = data.get("usage") or {}
            usage = {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens"),
                     "total": u.get("total_tokens")} if u else None
        except Exception as e:  # noqa: BLE001
            return AgentResponse(self.name, ERROR, error=f"{type(e).__name__}: {e}",
                                 duration=time.time() - t0)
        if not text:
            # Model returned only tool-call / DSML markup (or empty content) — no usable
            # answer. Report UNAVAILABLE so the roster shifts to a fallback agent instead
            # of aborting the whole run on an unparseable verdict.
            return AgentResponse(self.name, UNAVAILABLE,
                error="model returned no plain-text content (tool-call/DSML markup only)",
                duration=time.time() - t0)
        return AgentResponse(self.name, OK, text=text, duration=time.time() - t0, usage=usage)


class MockAdapter(Adapter):
    """Returns scripted responses keyed by step_id, advancing a per-step counter.

    Scenario format (fixtures/<workflow>/<scenario>.json):
        {"name": "...", "responses": {"<step_id>": ["text1", "text2", ...]},
         "default": {"<step_id>": "fallback text"}}
    """

    def __init__(self, name, cfg, scenario):
        super().__init__(name, cfg)
        self.scenario = scenario or {}
        self.counters: dict[str, int] = {}

    def availability(self):
        return True, "mock"

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        responses = self.scenario.get("responses", {})
        seq = responses.get(step_id)
        idx = self.counters.get(step_id, 0)
        if seq:
            text = seq[idx] if idx < len(seq) else seq[-1]
        else:
            text = self.scenario.get("default", {}).get(step_id, "")
        self.counters[step_id] = idx + 1
        return AgentResponse(self.name, OK, text=text, duration=0.0)


def build_adapters(agents_cfg: dict, mock: bool, scenario: dict | None, cwd: str | None = None):
    out = {}
    for name, cfg in agents_cfg["agents"].items():
        if name.startswith("_"):
            continue
        if mock:
            out[name] = MockAdapter(name, cfg, scenario)
        elif "api_base" in cfg or "cli_command" in cfg:
            out[name] = ApiAdapter(name, cfg, cwd)
        else:
            out[name] = CliAdapter(name, cfg, cwd)
    return out
