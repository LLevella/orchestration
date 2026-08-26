"""Config loading and the shared RunContext.

Loads the two JSON config files, resolves paths relative to orchestration/, and
provides helpers the engine needs: role-prompt assembly (role file + optional
authoring wrapper), reading existing architecture docs as context, and capturing
the current production-code diff to feed reviewers.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the orchestration/ directory

_NO_HEAD = "(no git HEAD; diff unavailable)"


class ConfigError(Exception):
    """Raised with a human-readable message for bad/missing config."""


def _load_json(path: Path):
    if not path.exists():
        raise ConfigError(f"config file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: line {e.lineno} col {e.colno}: {e.msg}") from e


def load_config():
    agents = _load_json(BASE / "config" / "agents.json")
    workflows = _load_json(BASE / "config" / "workflows.json")
    _validate(agents, workflows)
    return agents, workflows


def resolve_repos(workflows_cfg):
    """Resolve the workspace repos to [(label, abspath, kind)].

    kind is 'dev' (writable by the Developer) or 'analysis' (read-only context).
    label is the configured relative path (e.g. 'dataplane-phs-ng',
    'idu/sdk-s8000'); abspath is resolved under paths.repo_root. Both the git
    guard and capture_diff iterate this list, so a single source of truth keeps
    the protected set and the reviewer diff in sync.
    """
    paths = workflows_cfg["paths"]
    root = (BASE / paths["repo_root"]).resolve()
    out = []
    for rel in paths.get("dev_repos", []):
        out.append((rel, str((root / rel).resolve()), "dev"))
    for rel in paths.get("analysis_repos", []):
        out.append((rel, str((root / rel).resolve()), "analysis"))
    return out


def _validate(agents, workflows):
    if "agents" not in agents:
        raise ConfigError("agents.json: missing top-level 'agents' object")
    if "paths" not in workflows:
        raise ConfigError("workflows.json: missing 'paths' object")
    known = {n for n in agents["agents"] if not n.startswith("_")}
    for wf in ("architecture", "development", "investigation", "review", "implreview"):
        if wf not in workflows:
            raise ConfigError(f"workflows.json: missing '{wf}' workflow")
        if "default_mock_scenario" not in workflows[wf]:
            raise ConfigError(f"workflows.json: {wf} missing 'default_mock_scenario'")
        for key, spec in workflows[wf].items():
            if isinstance(spec, dict) and "agent" in spec and spec["agent"] not in known:
                raise ConfigError(
                    f"workflows.json: {wf}.{key} references unknown agent "
                    f"'{spec['agent']}' (known: {sorted(known)})")


class RunContext:
    def __init__(self, agents_cfg, workflows_cfg, adapters, guard, tx, resume_cache=None,
                 human_answer=None):
        self.agents_cfg = agents_cfg
        self.workflows = workflows_cfg
        self.adapters = adapters
        self.guard = guard
        self.tx = tx
        # Resume support: {(step_id, phase, iteration): {agent, role, verdict, text}}
        # of steps that already completed in a prior interrupted run; the engine
        # replays these instead of re-invoking the agent. Empty = normal run.
        self.resume_cache = resume_cache or {}
        # A human decision provided via `--human-answer` when resuming a run PAUSED
        # (AWAITING_HUMAN) because the arbiter could not decide. The architecture loop
        # feeds it back as a binding ruling. None on a normal run.
        self.human_answer = human_answer
        paths = workflows_cfg["paths"]
        self.repo_root = (BASE / paths["repo_root"]).resolve()
        self.roles_dir = (BASE / paths["roles_dir"]).resolve()
        self.docs_arch = (BASE / paths["docs_architecture"]).resolve()
        self.exclude = workflows_cfg.get("guard", {}).get("exclude_prefix", "orchestration")
        self._role_cache: dict[str, str] = {}
        # Per-repo baseline HEADs captured at run start; capture_diff compares
        # each repo's working tree against its own baseline.
        self.repos = resolve_repos(workflows_cfg)
        self._baselines = {label: self._head(path) for label, path, _ in self.repos}

    # ---- resume -------------------------------------------------------
    def resume_lookup(self, step_id, phase, iteration):
        """Return the journaled result for this step if we are resuming and it
        already completed, else None. Consumed once so a re-used (step_id, phase,
        iteration) later in the same run runs live rather than replaying twice."""
        if not self.resume_cache:
            return None
        return self.resume_cache.pop((step_id, phase, iteration), None)

    # ---- role prompts -------------------------------------------------
    def load_role(self, role: str, wrapper: str | None = None) -> str:
        if role not in self._role_cache:
            f = self.roles_dir / f"{role}.md"
            self._role_cache[role] = f.read_text(encoding="utf-8") if f.exists() \
                else f"(ROLE FILE MISSING: {f})"
        text = self._role_cache[role]
        if wrapper:
            wf = BASE / "prompts" / wrapper
            if wf.exists():
                text = text + "\n\n---\n\n" + wf.read_text(encoding="utf-8")
        return text

    def roles_present(self):
        """Map each configured role to whether its file exists."""
        seen = {}
        for wf in ("architecture", "development", "investigation", "review", "implreview"):
            for spec in self.workflows[wf].values():
                if isinstance(spec, dict) and "role" in spec:
                    r = spec["role"]
                    seen[r] = (self.roles_dir / f"{r}.md").exists()
        return seen

    # ---- context inputs ----------------------------------------------
    def read_existing_arch(self) -> str:
        if not self.docs_arch.exists():
            return "(no existing docs/architecture/ directory)"
        parts = []
        for f in sorted(self.docs_arch.glob("*.md"))[:8]:
            snippet = f.read_text(encoding="utf-8", errors="replace")[:1500]
            parts.append(f"### {f.name}\n{snippet}")
        return "\n\n".join(parts) or "(docs/architecture/ is empty)"

    def _git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True)

    def _head(self, repo):
        r = self._git(repo, "rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None

    def _repo_diff(self, repo, baseline) -> str:
        """Diff (tracked + untracked) for one repo vs its baseline, tool dir excluded."""
        if baseline is None:
            return _NO_HEAD
        excl = f":(exclude){self.exclude}"
        out = self._git(repo, "diff", baseline, "--", ".", excl).stdout
        untracked = [l for l in self._git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
                     if not l.startswith(self.exclude + "/")]
        if untracked:
            out += "\n\n# Untracked files:\n" + "\n".join(untracked)
        return out.strip()

    def capture_diff(self) -> str:
        """Aggregate production-code diff across all watched repos vs each repo's
        baseline commit, the tool dir excluded.

        Each repo is shown under a labelled header marking it WRITABLE (a
        dev_repo) or READ-ONLY (an analysis_repo). A read-only repo with changes
        is an out-of-scope edit the reviewers must reject. NOTE: includes any
        changes already present when the run started — run dev on a clean tree.
        """
        sections = []
        for label, path, kind in self.repos:
            tag = "WRITABLE" if kind == "dev" else "READ-ONLY (analysis — any change here is OUT OF SCOPE)"
            body = self._repo_diff(path, self._baselines.get(label))
            # Keep repos that changed (or can't be diffed); drop clean ones for a focused view.
            if body:
                sections.append(f"### {label} — {tag}\n{body}")
        if not sections:
            return "(no production-code changes detected)"
        return "\n\n".join(sections)

    # ---- scope description for prompts -------------------------------
    def repo_scope_prompt(self) -> str:
        """Full repo-scope block for the Developer's prompt."""
        dev = [r for r in self.repos if r[2] == "dev"]
        ana = [r for r in self.repos if r[2] == "analysis"]
        lines = ["## Repositories in this workspace",
                 f"Your working directory (and every agent's cwd) is `{self.repo_root}`. "
                 "It is not itself a git repo; each project below is a separate repo.",
                 "",
                 "WRITABLE — you MAY create/edit files here (this is the active development target):"]
        lines += [f"  - {label}/" for label, _, _ in dev] or ["  (none configured)"]
        lines += ["", "READ-ONLY — read and analyze for context, but DO NOT modify:"]
        lines += [f"  - {label}/" for label, _, _ in ana] or ["  (none configured)"]
        lines += ["",
                  "These are the ONLY repositories in scope. The workspace may also contain "
                  "OTHER repositories or directories (other tasks' or other teams' work, and the "
                  "roles/, docs/, orchestration/ trees) — those are NOT part of this task: do not "
                  "read, modify, or reference them. Create/edit files ONLY in the WRITABLE repos "
                  "above; touching a listed READ-ONLY repo is a scope violation."]
        return "\n".join(lines)

    def repo_scope_line(self) -> str:
        """One-line writable/read-only summary for reviewer prompts."""
        dev = ", ".join(r[0] for r in self.repos if r[2] == "dev") or "(none)"
        ana = ", ".join(r[0] for r in self.repos if r[2] == "analysis") or "(none)"
        return (f"Scope — the ONLY repos under review: WRITABLE: {dev}; READ-ONLY (analysis): {ana}. "
                "A change WITHIN a listed READ-ONLY repo is a violation. Anything OUTSIDE these "
                "listed repos — other repositories or directories that merely coexist in the "
                "workspace — is NOT part of this task: ignore it entirely, never inspect or flag it.")
