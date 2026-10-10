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
import math
import os
import re
import shutil
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import failures

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


# Normalized token-accounting shape. EVERY non-null usage dict carries all six
# keys; a class this transport does not report is None — never a silent 0, and a
# reported 0 stays 0. Recording only `input_tokens` is what under-reported one
# real Claude step by 55,599x (52 stored for a true 2,891,127 input, because
# cache-creation and cache-read were dropped on the floor).
USAGE_KEYS = ("input", "input_fresh", "input_cache_creation", "input_cache_read",
              "output", "total")

# Local reason for a CLI that exits 0 and prints nothing. Kept as a constant so the
# condition is greppable from a run's failure record.
NO_ANSWER_REASON = "the transport succeeded but returned no answer (empty output)"


def _usage(**kw):
    """A normalized usage dict: every key present, unreported classes None."""
    return {k: kw.get(k) for k in USAGE_KEYS}


def _opt_int(v):
    """A token count we can trust, else None.

    Providers vary and bodies get truncated: a count arriving as a string, a
    dict, null or NaN must degrade THAT class to null — never raise. Telemetry
    is not allowed to take the orchestration path down, so every structured
    value is type-checked before any arithmetic touches it. `bool` is excluded
    deliberately: in Python it would sum as 1, but `True` is not a token count."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v)
    except (ValueError, OverflowError):   # NaN / inf
        return None


def _opt_float(v):
    """A provider-reported cost we can trust, else None (same rule as _opt_int).
    None means "this transport priced nothing" — unmetered spend, not $0.00.

    A JSON integer is unbounded while a float is not, so the conversion itself
    can overflow: a `total_cost_usd` of 10**4000 is a malformed price, not an
    expensive one, and it must degrade to null here. Raising instead would take the whole failure
    record down with it — the usage, the reason and the raw body of a step that
    really ran — which is the exact loss P2 exists to stop."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _add_opt(*vals):
    """Sum only when EVERY part is known, else None. A partial sum that looks
    like a complete total is exactly the dishonesty P2 exists to remove."""
    return None if any(v is None for v in vals) else sum(vals)


def _normalize_claude_model_usage(model_usage):
    """Claude `modelUsage` -> normalized usage, summed across model entries.

    This is the authoritative scope when present: it covers EVERY model the step
    invoked (opus + the haiku side-calls), which is the same scope as
    `total_cost_usd`. A component is summed only if every reported entry carries
    it, and the whole block is refused if any entry is unreadable, so a partial
    total can never masquerade as a complete one."""
    if not isinstance(model_usage, dict):
        return None
    entries = list(model_usage.values())
    if not entries or any(not isinstance(e, dict) for e in entries):
        # An unreadable entry makes ALL-MODEL completeness unprovable: the tokens
        # that entry would have contributed are unknown, so summing its readable
        # siblings would publish a per-model slice with the scope label of the
        # whole step. Degrade the block as a unit — the caller falls back to the
        # top-level `usage`, and if that is absent too every class stays null.
        return None

    def comp(key):
        vals = [_opt_int(e.get(key)) for e in entries]
        return None if any(v is None for v in vals) else sum(vals)

    fresh, cc = comp("inputTokens"), comp("cacheCreationInputTokens")
    cr, out = comp("cacheReadInputTokens"), comp("outputTokens")
    if all(v is None for v in (fresh, cc, cr, out)):
        return None
    inp = _add_opt(fresh, cc, cr)
    return _usage(input=inp, input_fresh=fresh, input_cache_creation=cc,
                  input_cache_read=cr, output=out, total=_add_opt(inp, out))


def _normalize_claude_usage(u):
    """Claude top-level `usage` -> normalized usage (used when `modelUsage` is
    absent). `usage.iterations` is NOT added: it is a per-turn breakdown of the
    same totals, and adding it would double-count."""
    if not isinstance(u, dict) or not u:
        return None
    fresh = _opt_int(u.get("input_tokens"))
    cc = _opt_int(u.get("cache_creation_input_tokens"))
    cr, out = _opt_int(u.get("cache_read_input_tokens")), _opt_int(u.get("output_tokens"))
    if all(v is None for v in (fresh, cc, cr, out)):
        return None
    inp = _add_opt(fresh, cc, cr)
    return _usage(input=inp, input_fresh=fresh, input_cache_creation=cc,
                  input_cache_read=cr, output=out, total=_add_opt(inp, out))


def _normalize_openai_usage(data):
    """OpenAI-compatible `usage` (gemini / deepseek) -> normalized usage. These
    APIs report aggregates only, so the cache-class breakdown stays None."""
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict) or not u:
        return None
    inp, out = _opt_int(u.get("prompt_tokens")), _opt_int(u.get("completion_tokens"))
    total = _opt_int(u.get("total_tokens"))
    if all(v is None for v in (inp, out, total)):
        return None
    return _usage(input=inp, output=out,
                  total=total if total is not None else _add_opt(inp, out))


def _openai_message_text(data):
    """(text, shape_error) for an OpenAI-compatible success body.

    Every hop is type-checked instead of being wrapped in a try/except: a provider
    variant answering with an unexpected shape must degrade to a STRUCTURED failure
    carrying the body, never raise out of the adapter and abort the run. The second
    element is a LOCAL diagnostic — it is passed to the classifier as `local_reason`,
    the last rung of the reason precedence, so the provider's own body still gets to
    speak first."""
    if not isinstance(data, dict):
        return "", "response body is not a JSON object"
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", "response body carries no non-empty 'choices' list"
    if not isinstance(choices[0], dict):
        return "", "response 'choices[0]' is not an object"
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return "", "response 'choices[0].message' is not an object"
    content = msg.get("content")
    if content is None:
        content = ""      # an explicitly empty answer, handled by the caller
    if not isinstance(content, str):
        return "", "response 'choices[0].message.content' is not a string"
    return _strip_tool_markup(content), ""


@dataclass
class ParsedOutput:
    """What an adapter could read out of a transport result, success or failure.

    `body` is the decoded structured payload (when there was one) and feeds the
    classifier; `is_error` is the provider's OWN verdict on its response, which
    can be true even on a zero exit code."""
    text: str = ""
    usage: dict | None = None
    cost_usd: float | None = None
    body: dict | None = None
    is_error: bool = False


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
    # Normalized shape: USAGE_KEYS, each int or None.
    usage: dict | None = None
    # Provider-REPORTED cost. None means "this transport prices nothing", which is
    # unmetered spend — NOT $0.00. The orchestrator keeps no price tables.
    cost_usd: float | None = None
    # Structured failure record for a non-OK response. Holds the bounded raw
    # provider body, so an error body survives for the operator without ever being
    # offered to the verdict parser as `text` (A2).
    failure: failures.FailureInfo | None = None


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


# `{config_dir}` in a CLI command template is this tool's own config directory,
# resolved from where the code lives - so agents.json can name a file shipped
# beside it (a policy, a settings file) without an absolute path that ties the
# tool to one checkout, and independently of the subprocess's working directory.
CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")


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
            return (x.replace("{prompt}", full_prompt)
                     .replace("{system}", system)
                     .replace("{config_dir}", CONFIG_DIR))

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
            cmd += [sub(c) for c in (self.cfg.get("edit_args") or [])]
        else:
            cmd += [sub(c) for c in (self.cfg.get("read_only_args") or [])]
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

    def _parse_output(self, stdout) -> ParsedOutput:
        """Read whatever this CLI's stdout carries, per cfg['usage_format'].

        Called for EVERY invocation, successful or not — a failed `claude -p` still
        emits its complete JSON result object, and returning before parsing it is
        what discarded the usage, the provider-reported cost and the real reason.
        Fail-safe: unreadable output degrades to plain text and an unreadable field
        degrades to null — every structured value is type-checked, so a provider
        variant can never raise out of here and lose a step that actually ran."""
        fmt = self._usage_format()
        if fmt == "claude_json":
            return self._parse_claude_json(stdout)
        if fmt == "codex_text":
            return self._parse_codex_text(stdout)
        return ParsedOutput(text=(stdout or "").strip())

    @staticmethod
    def _parse_claude_json(stdout):
        # `claude -p --output-format json` emits one object: {result, usage{...},
        # modelUsage{...}, total_cost_usd, is_error, api_error_status, ...}. text MUST
        # be `result` so the verdict parser still sees the assistant's final message —
        # but only when the body is NOT an error (the caller drops text in that case).
        d = failures.json_or_none(stdout)
        if d is None:
            return ParsedOutput(text=(stdout or "").strip())
        # EVERY decoded object is the body, whether or not it carries `result`. Keying
        # recognition on `result` let an `is_error: true` body that omits it fall
        # through to the plain-text branch, where the raw error JSON became the answer
        # and was offered to the verdict parser — exactly what A2 forbids.
        # modelUsage covers every model the step invoked, which is the same scope
        # as total_cost_usd; prefer it and do NOT add the top-level block again.
        usage = (_normalize_claude_model_usage(d.get("modelUsage"))
                 or _normalize_claude_usage(d.get("usage")))
        # A real boolean is the provider's own verdict and is honoured as stated. Any
        # other truthy value (a "true" string from a future shape) still fails SAFE
        # toward error: the only direction that can never turn a failure into an answer.
        is_error = bool(d.get("is_error"))
        result = d.get("result")
        # The answer is taken ONLY from a string `result` of a non-error body. A
        # structured `result` (dict/list) is not an assistant message, so it stays out
        # of `text` rather than being coerced into one.
        text = "" if is_error or not isinstance(result, str) else result.strip()
        return ParsedOutput(text=text, usage=usage,
                            cost_usd=_opt_float(d.get("total_cost_usd")), body=d,
                            is_error=is_error)

    @staticmethod
    def _parse_codex_text(stdout):
        # `codex exec` prints a "tokens used\n<N>" footer — a bare total with no
        # input/output split and no cache classes, so those stay None. text is left
        # as-is (the verdict parser already reads codex's plain transcript today).
        m = re.search(r"tokens used[:\s]+([\d,]+)", stdout or "", re.IGNORECASE)
        # A comma-only match ("tokens used: ,") is not a number: leave usage null
        # rather than raising out of the adapter over a footer we cannot read.
        digits = m.group(1).replace(",", "") if m else ""
        usage = _usage(total=int(digits)) if digits.isdigit() else None
        return ParsedOutput(text=(stdout or "").strip(), usage=usage)

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        ok, reason = self.availability()
        if not ok:
            return AgentResponse(self.name, UNAVAILABLE, error=reason,
                                 failure=failures.classify(stderr=reason,
                                                           local_kind=failures.CONFIGURATION,
                                                           local_reason=reason))
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
            detail = f"{type(e).__name__}: {e}"
            # Launch/timeout facts are known LOCALLY and outrank any text decoding.
            local_kind = None
            if isinstance(e, subprocess.TimeoutExpired):
                local_kind = failures.TIMEOUT
            elif isinstance(e, (FileNotFoundError, PermissionError, NotADirectoryError)):
                local_kind = failures.CONFIGURATION
            return AgentResponse(self.name, ERROR, error=detail,
                                 duration=time.time() - t0,
                                 failure=failures.classify(stderr=detail, local_kind=local_kind,
                                                           local_reason=detail))
        dur = time.time() - t0
        # Parse the body BEFORE applying process semantics: a non-zero exit still
        # carries a complete JSON result (usage, total_cost_usd, and the provider's
        # own reason). Returning here without parsing is defect P2 itself.
        parsed = self._parse_output(p.stdout)
        # A nominally successful transport that produced no answer at all is
        # unsuccessful too: every step consumes its agent's text (as an artifact or
        # as a verdict), so an empty answer can satisfy none of them. Left as OK it
        # spent the bounded verdict-repair budget re-prompting about nothing and then
        # failed anyway, with "no detail" as the reason. It reports the empty result
        # as the failure — it does NOT make the step recoverable (no legacy marker
        # matches, so the roster does not shift and the run stops with a real reason).
        no_answer = p.returncode == 0 and not parsed.is_error and not parsed.text
        # Unsuccessful = the process failed OR the provider said so in its own body
        # (`is_error` is true on a ZERO exit code for a Claude session limit) OR the
        # call succeeded and said nothing.
        if p.returncode != 0 or parsed.is_error or no_answer:
            # The classifier sees BOTH ends of BOTH streams. Codex echoes the ENTIRE prompt
            # (role file + inlined artifact) to stderr, which can push the real failure line
            # (e.g. "You've hit your usage limit") past a head-only truncation — then
            # is_unavailable() misses it and the run HARD-ABORTS instead of shifting the cell
            # to a live agent. Such markers live at the tail, so preserve it. (Observed
            # 2026-07-09: a codex usage-limit at review iter 3 aborted the whole run because
            # the marker was cut by the old [:4000] head slice.)
            failure = failures.classify(
                stdout=p.stdout or "", stderr=p.stderr or "", body=parsed.body,
                local_kind=failures.TRANSPORT if no_answer else None,
                local_reason=NO_ANSWER_REASON if no_answer else "")
            # text is EMPTIED so an error body can never be read as an answer or a
            # verdict; the body itself survives in failure.raw (amendment A2).
            return AgentResponse(self.name, ERROR, text="", error=failure.reason,
                                 duration=dur, usage=parsed.usage,
                                 cost_usd=parsed.cost_usd, failure=failure)
        return AgentResponse(self.name, OK, text=parsed.text, duration=dur,
                             usage=parsed.usage, cost_usd=parsed.cost_usd)


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
            return AgentResponse(self.name, UNAVAILABLE, error=reason,
                                 failure=failures.classify(stderr=reason,
                                                           local_kind=failures.CONFIGURATION,
                                                           local_reason=reason))
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
                raw = resp.read().decode("utf-8", "replace")
            data = failures.json_or_none(raw)
        except urllib.error.HTTPError as e:
            # Handled BEFORE the generic path so the error response body, the numeric
            # status and the Retry-After header all survive — urllib's str(e) alone
            # ("HTTP Error 402: Payment Required") loses the provider's message and
            # any usage the body reported.
            body_raw = ""
            try:
                body_raw = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - a body that cannot be read is not fatal
                pass
            body = failures.json_or_none(body_raw)
            # urllib's description is OURS, not the provider's. It stays classification
            # evidence (`local_evidence`) so "HTTP Error 402: Payment Required" keeps
            # driving the same legacy shift it drives today, and it is the LAST rung of
            # the reason (`local_reason`) — passed as `stderr` it outranked the response
            # body, so a plain-text "upstream is over capacity" was hidden behind
            # "HTTPError: HTTP Error 429: Too Many Requests".
            detail = f"{type(e).__name__}: {e}"
            failure = failures.classify(
                stdout=body_raw, body=body,
                http_status=getattr(e, "code", None),
                retry_after=(e.headers.get("Retry-After") if getattr(e, "headers", None) else None),
                local_evidence=detail, local_reason=detail)
            return AgentResponse(self.name, ERROR, error=failure.reason,
                                 duration=time.time() - t0,
                                 usage=_normalize_openai_usage(body), failure=failure)
        except Exception as e:  # noqa: BLE001
            detail = f"{type(e).__name__}: {e}"
            return AgentResponse(self.name, ERROR, error=detail,
                                 duration=time.time() - t0,
                                 failure=failures.classify(stderr=detail, local_reason=detail))
        dur = time.time() - t0
        usage = _normalize_openai_usage(data)
        # A structured error can also arrive inside a 200 body.
        if isinstance(data, dict) and data.get("error"):
            failure = failures.classify(stdout=raw, body=data)
            return AgentResponse(self.name, ERROR, text="", error=failure.reason,
                                 duration=dur, usage=usage, failure=failure)
        text, shape_error = _openai_message_text(data)
        if shape_error:
            # The diagnostic is LOCAL knowledge, not something the provider said, so
            # it goes in `local_reason` — passing it as `stderr` (as this once did)
            # ranked a parser message above the provider's own body and could hide
            # the real words behind "KeyError: 'choices'".
            failure = failures.classify(stdout=raw, body=data, local_reason=shape_error)
            # `error` is the classified reason (provider first, diagnostic only if the
            # body said nothing), so every consumer of this field quotes the provider.
            return AgentResponse(self.name, ERROR, text="", error=failure.reason,
                                 duration=dur, usage=usage, failure=failure)
        if not text:
            # Model returned only tool-call / DSML markup (or empty content) — no usable
            # answer. Report UNAVAILABLE so the roster shifts to a fallback agent instead
            # of aborting the whole run on an unparseable verdict.
            detail = "model returned no plain-text content (tool-call/DSML markup only)"
            return AgentResponse(self.name, UNAVAILABLE, error=detail, duration=dur,
                                 usage=usage,
                                 failure=failures.classify(stderr=detail, local_reason=detail))
        return AgentResponse(self.name, OK, text=text, duration=dur, usage=usage)


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
