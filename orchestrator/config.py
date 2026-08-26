"""Config loading and the shared RunContext.

Loads the two JSON config files, resolves paths relative to orchestration/, and
provides helpers the engine needs: role-prompt assembly (role file + optional
authoring wrapper), reading existing architecture docs as context, and capturing
the current production-code diff to feed reviewers.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# Git's per-invocation environment must be scrubbed before we run any `git -C <repo>`.
#
# WHY: GIT_DIR overrides repository discovery, so `git -C /some/repo status` with an
# inherited GIT_DIR operates on THAT gitdir with /some/repo as the worktree - proven
# 2026-08-02: it reported every file of the other repository as deleted. GIT_INDEX_FILE
# is worse, because during a `git commit` it points at the index git is still building.
#
# We inherit exactly that environment whenever the suite runs from a pre-commit hook:
# the hook runs the orchestration tests, test_workflows_mock spawns orchestrate.py as a
# subprocess, and every git call inside it then targets the commit-in-progress index
# instead of the repo it was given. The read-only guard sees the tree "change" between a
# step's before/after snapshot and aborts the run as a guard violation, so a config-only
# commit failed four times with HUMAN_REQUIRED while the same suite passed every time it
# was run by hand.
_GIT_ENV_OVERRIDES = (
    "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_INDEX_VERSION", "GIT_LITERAL_PATHSPECS",
)


def git_clean_env() -> dict:
    """A copy of os.environ with git's per-invocation overrides removed."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}


BASE = Path(__file__).resolve().parent.parent  # the orchestration/ directory

# Every workflow the orchestrator ships. Single source of truth for config
# validation, the role-file inventory, and the CLI's inspection commands, so a
# newly added chain cannot be half-registered (validated but never listed, or
# listed but never validated).
WORKFLOWS = ("architecture", "development", "investigation", "review", "implreview",
             "mrreview")

_NO_HEAD = "(no git HEAD; diff unavailable)"

# Untracked files are rendered as real new-file diffs, not just listed by name.
#
# WHY: `git diff <baseline>` never shows a file git does not track, so a run that
# ADDS files - which is most runs - used to show reviewers only a list of names
# under "# Untracked files:". On 2026-08-02 that deadlocked a run: the architect
# returned NEEDS_FIX five times in a row, each time endorsing the design but
# refusing to sign off on safety-critical shell it had never been shown, while the
# developer never pasted the contents. Two hours, no convergence possible.
#
# Caps exist because an untracked file can be a build artifact (a .deb, a test
# binary) that would otherwise blow up every reviewer prompt. Truncation is always
# announced in-band - a silently truncated diff reads as "that is the whole file".
_UNTRACKED_FILE_MAX_BYTES = 64 * 1024
_UNTRACKED_TOTAL_MAX_BYTES = 512 * 1024


# Appended to EVERY role's system prompt, in every workflow (see RunContext.load_role).
#
# WHY (Irina 2026-08-09):
#  - Language. This codebase, its docs, comments, commit messages and review artifacts
#    are English-only; the human conversation about it is not. Task briefs are routinely
#    written in Russian, and agents mirror the brief's language: a 2026-08-09 run had the
#    Architect return its whole review in Russian, which then flows into design notes and
#    code comments. The brief's language must never decide the artifact's language.
#  - Workspace isolation. Agents are launched with the workspace root as cwd, and other,
#    unrelated project workspaces live as SIBLINGS of it on disk. Nothing in the filesystem
#    stops an agent from reading them, and material from another project must never leak
#    into this one's analysis, design or code.
GLOBAL_POLICY = """
---

## Global policy (applies to every role, overrides any habit to the contrary)

**Language.** Produce every artifact in ENGLISH: code, identifiers, comments, documentation,
design notes, test names, commit messages, and your own review/verdict output. This holds
regardless of the language the task brief is written in - a brief written in another language
is still implemented and reviewed in English. Do not translate quoted log lines, existing
identifiers, or file contents; quote them verbatim.

**Workspace isolation.** Your workspace root is the directory you were launched in, and the
repositories listed in the task's scope block are the only ones in scope. Directories that sit
BESIDE your workspace root on disk are other projects: never read, search, reference, or modify
anything outside your workspace root, and never carry facts, conventions, or context from
another project into this one. If you believe something outside the scope is needed, say so in
your output and stop - do not go and read it.
"""


class ConfigError(Exception):
    """Raised with a human-readable message for bad/missing config."""


def _load_json(path: Path):
    if not path.exists():
        raise ConfigError(f"config file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: line {e.lineno} col {e.colno}: {e.msg}") from e


# The per-project layer. Everything in this repository is meant to be reusable by any
# project; what is NOT reusable - which repositories exist, where they live, what the
# domain is, which tooling and hosts the work happens on - lives in ONE file outside the
# tool's tracked content and is merged in here at load time.
#
# Resolution order (first hit wins):
#   1. $ORCH_PROJECT            - a project.json, or a directory holding one
#   2. config/project.json      - gitignored; usually a symlink to the real project layer
#   3. config/project.example.json - the shipped template, so a fresh clone still runs
#                                    `--mock` and the test suite with no setup at all
PROJECT_ENV = "ORCH_PROJECT"
PROJECT_FILE = "project.json"
PROJECT_EXAMPLE = "project.example.json"


def project_config_path() -> tuple[Path, str]:
    """Return (path, source) of the project layer to load. See PROJECT_ENV."""
    env = os.environ.get(PROJECT_ENV)
    if env:
        cand = Path(env).expanduser()
        if cand.is_dir():
            cand = cand / PROJECT_FILE
        if not cand.exists():
            raise ConfigError(f"${PROJECT_ENV} points at {cand}, which does not exist")
        return cand.resolve(), f"${PROJECT_ENV}"
    local = BASE / "config" / PROJECT_FILE
    # resolve(): config/project.json is normally a SYMLINK to a project layer kept beside
    # the project, and that layer's paths are relative to where it really lives - resolving
    # the link here is what makes "repo_root": ".." mean the project, not the tool's config/.
    if local.exists():
        return local.resolve(), f"config/{PROJECT_FILE} -> {local.resolve()}"
    return (BASE / "config" / PROJECT_EXAMPLE).resolve(), f"config/{PROJECT_EXAMPLE} (TEMPLATE)"


def load_config():
    agents = _load_json(BASE / "config" / "agents.json")
    workflows = _load_json(BASE / "config" / "workflows.json")
    project_path, project_source = project_config_path()
    project = _load_json(project_path)
    # The project layer OWNS `paths` and `guard`; workflows.json owns the workflows
    # themselves. Merging here means every consumer keeps reading workflows_cfg["paths"]
    # exactly as before and nothing else has to know the split exists.
    for key in ("paths", "guard"):
        if key in project:
            workflows[key] = {**workflows.get(key, {}), **project[key]}
    workflows["_project"] = {**project, "_path": str(project_path), "_source": project_source,
                             "_dir": str(project_path.parent)}
    _validate(agents, workflows)
    return agents, workflows


def resolve_repos(workflows_cfg):
    """Resolve the workspace repos to [(label, abspath, kind)].

    kind is 'dev' (writable by the Developer) or 'analysis' (read-only context).
    label is the configured relative path (e.g. 'service-a', 'vendor/sdk');
    abspath is resolved under paths.repo_root. Both the git
    guard and capture_diff iterate this list, so a single source of truth keeps
    the protected set and the reviewer diff in sync.
    """
    paths = workflows_cfg["paths"]
    root = (project_base(workflows_cfg) / paths["repo_root"]).resolve()
    out = []
    for rel in paths.get("dev_repos", []):
        out.append((rel, str((root / rel).resolve()), "dev"))
    for rel in paths.get("analysis_repos", []):
        out.append((rel, str((root / rel).resolve()), "analysis"))
    return out


def project_base(workflows_cfg) -> Path:
    """Directory that `paths` in the project layer are relative to.

    The project layer lives outside this repository, so its paths are resolved against
    ITS OWN directory - a project file kept beside the workspace it describes says
    `"repo_root": "."`, and moving the tool never invalidates it.
    """
    d = (workflows_cfg.get("_project") or {}).get("_dir")
    return Path(d) if d else BASE


def _read_context(base: Path, ref):
    """Read a project-context file named by the project layer, relative to it.

    A missing file is a configuration error, not a silent no-op: a run whose agents
    quietly lost the project's domain knowledge looks like a model that got worse.
    """
    if not ref:
        return ""
    f = (base / ref).expanduser()
    if not f.exists():
        raise ConfigError(f"project layer references a context file that does not exist: {f}")
    return f.read_text(encoding="utf-8").strip()


def _validate(agents, workflows):
    if "agents" not in agents:
        raise ConfigError("agents.json: missing top-level 'agents' object")
    if "paths" not in workflows:
        src = (workflows.get("_project") or {}).get("_source", "the project layer")
        raise ConfigError(f"no 'paths' object: {src} must provide one "
                          "(see config/project.example.json)")
    known = {n for n in agents["agents"] if not n.startswith("_")}
    for wf in WORKFLOWS:
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
        pbase = project_base(workflows_cfg)
        self.repo_root = (pbase / paths["repo_root"]).resolve()
        self.docs_arch = (pbase / paths.get("docs_architecture", "docs/architecture")).resolve()
        # Base roles ship WITH the tool and are the same for every project. A project may
        # point `roles_dir` at its own set, and then it owns them entirely - but the
        # supported way to add project knowledge is `context_file` / `role_context` below,
        # which leaves the base roles identical everywhere.
        self.roles_dir = ((pbase / paths["roles_dir"]).resolve() if paths.get("roles_dir")
                          else (BASE / "roles").resolve())
        self.exclude = workflows_cfg.get("guard", {}).get("exclude_prefix", BASE.name)
        self._role_cache: dict[str, str] = {}
        # Project knowledge injected into prompts: one block every role sees, plus
        # optional per-role blocks (the investigator's lab-log recipe is of no use to the
        # PM gate, and every extra paragraph costs attention).
        proj = workflows_cfg.get("_project") or {}
        self.project_context = _read_context(pbase, proj.get("context_file"))
        self.role_context = {role: _read_context(pbase, ref)
                             for role, ref in (proj.get("role_context") or {}).items()}
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
        for block, head in ((self.project_context, "Project context"),
                            (self.role_context.get(role, ""), f"Project context for {role}")):
            if block:
                text += f"\n\n---\n\n## {head}\n\n{block}"
        # Last, so it wins over anything a role file, wrapper, or project context says.
        return text + "\n" + GLOBAL_POLICY

    def roles_present(self):
        """Map each configured role to whether its file exists."""
        seen = {}
        for wf in WORKFLOWS:
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
            try:
                snippet = f.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                # skip dangling symlinks / unreadable files instead of
                # aborting the whole run
                continue
            parts.append(f"### {f.name}\n{snippet}")
        return "\n\n".join(parts) or "(docs/architecture/ is empty)"

    def _git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, env=git_clean_env())

    def _head(self, repo):
        r = self._git(repo, "rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None

    def _untracked_diff(self, repo, paths) -> str:
        """Render untracked files as new-file diffs so reviewers can actually read them.

        Uses `git diff --no-index /dev/null <path>`, which produces a normal unified
        diff for a new file and reports binaries as "Binary files ... differ" instead
        of dumping bytes. Bounded per file and in total; every omission is stated.
        """
        chunks, budget, omitted = [], _UNTRACKED_TOTAL_MAX_BYTES, []
        for rel in paths:
            if budget <= 0:
                omitted.append(rel)
                continue
            r = self._git(repo, "diff", "--no-index", "--", "/dev/null", rel)
            # --no-index exits 1 when the files differ, which is the normal case here.
            body = r.stdout
            if not body:
                omitted.append(f"{rel} (unreadable: {(r.stderr or '').strip()[:80] or 'no diff output'})")
                continue
            if len(body) > _UNTRACKED_FILE_MAX_BYTES:
                body = (body[:_UNTRACKED_FILE_MAX_BYTES]
                        + f"\n# ... TRUNCATED: {rel} is {len(r.stdout)} bytes, showing the first "
                          f"{_UNTRACKED_FILE_MAX_BYTES}. Read the file directly for the rest.\n")
            budget -= len(body)
            chunks.append(body)
        out = "".join(chunks)
        if omitted:
            out += ("\n# Untracked files NOT shown above (total budget "
                    f"{_UNTRACKED_TOTAL_MAX_BYTES} bytes exhausted, or unreadable) - "
                    "read them directly:\n" + "\n".join(f"#   {p}" for p in omitted) + "\n")
        return out

    def _repo_diff(self, repo, baseline) -> str:
        """Diff (tracked + untracked) for one repo vs its baseline, tool dir excluded.

        Untracked files are included with their CONTENTS, not merely named: a reviewer
        cannot approve code it has never seen, and `git diff <baseline>` structurally
        omits anything git does not track. See _UNTRACKED_FILE_MAX_BYTES.
        """
        if baseline is None:
            return _NO_HEAD
        excl = f":(exclude){self.exclude}"
        out = self._git(repo, "diff", baseline, "--", ".", excl).stdout
        untracked = [l for l in self._git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
                     if not l.startswith(self.exclude + "/") and l != self.exclude]
        if untracked:
            out += ("\n\n# Untracked (new) files - full contents follow as new-file diffs:\n"
                    + "\n".join(f"#   {p}" for p in untracked) + "\n\n"
                    + self._untracked_diff(repo, untracked))
        return out.strip()

    def capture_diff(self) -> str:
        """Aggregate production-code diff across all watched repos, the tool dir excluded.

        Each repo is shown under a labelled header marking it WRITABLE (a
        dev_repo) or READ-ONLY (an analysis_repo). A read-only repo with changes
        is an out-of-scope edit the reviewers must reject.

        Diff base differs by repo kind, on purpose:
          - WRITABLE (dev) repos diff against the run-start BASELINE, so the full
            developer delta is shown (committed AND uncommitted work since start).
            NOTE: includes any changes already present when the run started -
            run dev on a clean tree.
          - READ-ONLY (analysis) repos diff against their CURRENT HEAD, so only a
            developer's illicit working-tree edit (uncommitted change / untracked
            file) is surfaced. An upstream `git pull` that fast-forwards a
            read-only repo mid-run (clean tree, HEAD moved past the frozen
            baseline) is repo drift, NOT a scope violation - diffing against HEAD
            keeps that unrelated upstream work out of the reviewed change set
            instead of wrongly blocking every review until the run deadlocks.
        """
        sections = []
        for label, path, kind in self.repos:
            if kind == "dev":
                tag = "WRITABLE"
                base = self._baselines.get(label)
            else:
                tag = "READ-ONLY (analysis — any change here is OUT OF SCOPE)"
                base = self._head(path)  # current HEAD: flag working-tree edits, ignore upstream pulls
            body = self._repo_diff(path, base)
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
