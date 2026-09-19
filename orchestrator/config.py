"""Config loading and the shared RunContext.

Loads the two JSON config files, resolves paths relative to orchestration/, and
provides helpers the engine needs: role-prompt assembly (role file + optional
authoring wrapper), reading existing architecture docs as context, and capturing
the current production-code diff to feed reviewers.
"""
from __future__ import annotations

import hashlib
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
    # A dangling symlink is a MOVED tool or a moved project layer, not "no project layer".
    # Falling through to the template would be quiet and wrong: mock runs and the suite keep
    # passing, a real run then refuses to start with a guard complaint that points at the
    # repositories instead of at the link that actually broke.
    if local.is_symlink() and not local.exists():
        raise ConfigError(
            f"config/{PROJECT_FILE} is a dangling symlink -> {os.readlink(local)}\n"
            "The tool or the project layer moved. Re-point it at the project layer "
            "(an absolute link survives moving the tool), or set "
            f"${PROJECT_ENV} to the project.json.")
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


# ---- run provenance: what code produced a run ------------------------------
#
# WHY (2026-08-30): run.json recorded what a run DID and never what it ran ON. The
# tool changes between runs, so the run corpus became a mix of versions that cannot
# be told apart - the measured baseline of this project (200 runs, taken at d08d1d4)
# is comparable with any later measurement only if both name their code. Every part
# below degrades to null WITH A STATED REASON rather than to a fabricated, empty or
# plausible-looking value: an unknown version must never read as a known one, and
# capturing a version must never be able to stop a run.


def _git_read(cwd: Path, *args) -> tuple[bool, str]:
    """Run ONE read-only git command in `cwd` -> (ok, stdout | failure reason).

    Uses the same scrubbed environment as every other git call here (git_clean_env);
    an absent git binary or a non-repository directory is a reason string, not a
    raised exception."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, env=git_clean_env())
    except OSError as e:
        return False, f"git could not be run: {type(e).__name__}: {e}"
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (lines[0] if lines else f"git exited {r.returncode}")
    return True, r.stdout.strip()


def tool_version() -> dict:
    """Version of the code that is EXECUTING: the directory orchestrate.py was loaded
    from (BASE), which is usually NOT the repository being worked on.

    Dirtiness is scoped to BASE's own subtree (`-- .`), so an unrelated change
    elsewhere in a larger repository does not report the tool itself as modified.
    """
    v = {"path": str(BASE), "commit": None, "branch": None, "dirty": None,
         "unavailable": None}
    ok, head = _git_read(BASE, "rev-parse", "HEAD")
    if not ok:
        v["unavailable"] = ("the tool directory is not a git worktree, or git is "
                            f"unavailable: {head}")
        return v
    v["commit"] = head
    ok_branch, branch = _git_read(BASE, "rev-parse", "--abbrev-ref", "HEAD")
    if ok_branch:
        # A detached HEAD has no branch; say so rather than record the literal "HEAD".
        v["branch"] = "(detached HEAD)" if branch == "HEAD" else branch
    ok_dirty, porcelain = _git_read(BASE, "status", "--porcelain", "--", ".")
    if ok_dirty:
        v["dirty"] = bool(porcelain)
    if not ok_branch or not ok_dirty:
        missing, reason = (("branch", branch) if not ok_branch else ("dirtiness", porcelain))
        v["unavailable"] = f"commit recorded; {missing} could not be read: {reason}"
    return v


def project_layer_version(workflows_cfg) -> dict:
    """Identity of the per-project layer in force: its resolved path and a digest.

    The digest covers project.json AND every context file the layer injects into
    prompts, because those files ARE the project layer as a run experiences it: an
    edited context.md changes every prompt in the run while project.json's own bytes
    stay identical. Per-file digests are kept too, so a human can see WHICH file moved.
    """
    proj = workflows_cfg.get("_project") or {}
    path = proj.get("_path")
    base = project_base(workflows_cfg)
    # Every ref is relative to the project layer's own directory (see project_base),
    # and _path's parent IS that directory, so its basename is its relative name.
    refs = [Path(path).name] if path else []
    if proj.get("context_file"):
        refs.append(proj["context_file"])
    refs += [ref for ref in (proj.get("role_context") or {}).values() if ref]
    files, unreadable = {}, []
    for ref in refs:
        try:
            files[ref] = hashlib.sha256((base / ref).expanduser().read_bytes()).hexdigest()
        except OSError as e:
            files[ref] = None
            unreadable.append(f"{ref} ({type(e).__name__})")
    digest = hashlib.sha256(
        "".join(f"{ref}\0{files[ref]}\n" for ref in sorted(files)).encode()).hexdigest()
    note = None
    if not path:
        note = "no project layer is recorded in this configuration"
    elif unreadable:
        note = "digest omits unreadable file(s): " + ", ".join(unreadable)
    return {"path": path, "source": proj.get("_source"), "digest": digest,
            "files": files, "unavailable": note}


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


def _validate_agent_domains(agents):
    """Every shipped agent must declare its failure domain.

    `provider` / `availability_group` are what let the roster block the RIGHT set of
    agents when one of them fails: without them a shared account limit is rediscovered
    one alias at a time, which is how 11 of the 16 discarded corpus runs died. They are
    required in the shipped config precisely so a newly added agent cannot silently
    default to "a failure domain of its own" and re-create that walk. (A synthetic test
    adapter that declares neither still defaults both to its own name — isolation is the
    safe default for a double, but it is not acceptable as shipped configuration.)"""
    for name, cfg in agents["agents"].items():
        if name.startswith("_"):
            continue
        for field in ("provider", "availability_group"):
            if not cfg.get(field):
                raise ConfigError(
                    f"agents.json: agent '{name}' is missing '{field}'. Every shipped "
                    "agent must declare its failure domain so one account-level failure "
                    "blocks every alias that shares it (see the roster's block registry).")


def _validate_roster(wf, block, agents):
    """Structured pins, edit-capable cells, the role allowlist behind both, and the
    failure-domain separation that keeps a pin's two fallback lists honest."""
    roster = block.get("roster") or {}
    for cell, spec in block.items():
        # every step block, not only the rostered cells: a writing cell that is bound
        # statically must be edit-capable too.
        if isinstance(spec, dict) and spec.get("requires_edit"):
            agent = spec.get("agent")
            if not (agents.get(agent) or {}).get("can_edit"):
                raise ConfigError(
                    f"workflows.json: {wf}.{cell} sets requires_edit but its agent "
                    f"'{agent}' has can_edit=false. A writing cell may only be bound to "
                    "an edit-capable agent; granting edit capability is a safety-model "
                    "change, not a configuration one.")
    for cell, pin in (roster.get("pinned") or {}).items():
        if not isinstance(pin, dict):
            if pin not in agents:                       # legacy string pin
                raise ConfigError(f"workflows.json: {wf}.roster.pinned.{cell} references "
                                  f"unknown agent '{pin}'")
            continue
        primary = pin.get("primary")
        same = list(pin.get("agent_fallbacks") or [])
        cross = list(pin.get("account_provider_fallbacks") or [])
        if not primary:
            raise ConfigError(f"workflows.json: {wf}.roster.pinned.{cell} has no 'primary'")
        for label, names in (("agent_fallbacks", same),
                             ("account_provider_fallbacks", cross)):
            if len(set(names)) != len(names):
                raise ConfigError(f"workflows.json: {wf}.roster.pinned.{cell}.{label} "
                                  "repeats a candidate")
            if primary in names:
                raise ConfigError(f"workflows.json: {wf}.roster.pinned.{cell}.{label} "
                                  f"repeats the primary '{primary}'")
        both = sorted(set(same) & set(cross))
        if both:
            raise ConfigError(
                f"workflows.json: {wf}.roster.pinned.{cell} lists {both} in BOTH "
                "agent_fallbacks and account_provider_fallbacks. The two lists carry "
                "different authority — one is taken on any agent-specific loss, the "
                "other only on classified account/provider evidence and only while "
                "recording a degradation — so a candidate may sit in exactly one.")
        role = (block.get(cell) or {}).get("role") if isinstance(block.get(cell), dict) else None
        for name in [primary] + same + cross:
            if name not in agents:
                raise ConfigError(f"workflows.json: {wf}.roster.pinned.{cell} references "
                                  f"unknown agent '{name}' (known: {sorted(agents)})")
            if role and role not in (agents[name].get("roles") or []):
                raise ConfigError(
                    f"workflows.json: {wf}.roster.pinned.{cell} names '{name}', which does "
                    f"not declare the cell's role '{role}' in agents.json. A pin candidate "
                    "that cannot hold the cell is a fallback that silently does not exist.")
        # The two lists must MEAN what they are named, and naming is not evidence:
        # `agent_fallbacks` is consulted on ANY agent-specific loss, without a
        # classified account/provider trigger and without recording a degradation, so a
        # different-provider agent hidden there would cross vendor families silently —
        # exactly the assurance loss the structured pin exists to make explicit.
        pcfg = agents[primary]
        for name in same:
            if agents[name].get("provider") != pcfg.get("provider"):
                raise ConfigError(
                    f"workflows.json: {wf}.roster.pinned.{cell}.agent_fallbacks names "
                    f"'{name}' (provider '{agents[name].get('provider')}'), but the primary "
                    f"'{primary}' is provider '{pcfg.get('provider')}'. This list covers an "
                    "AGENT-specific loss and stays inside the primary's family; a "
                    "different-provider candidate belongs in account_provider_fallbacks, "
                    "which requires account/provider evidence and records what was lost.")
        # And the converse: a cross-family fallback sharing the primary's account would
        # be taken out by the very account-scoped block that unlocks the list.
        for name in cross:
            if agents[name].get("availability_group") == pcfg.get("availability_group"):
                raise ConfigError(
                    f"workflows.json: {wf}.roster.pinned.{cell}.account_provider_fallbacks "
                    f"names '{name}', which shares the primary's availability_group "
                    f"'{pcfg.get('availability_group')}'. That list is consulted only while "
                    "an account- or provider-scoped block is active, and such a block "
                    "covers every agent in the group: the fallback would be blocked by the "
                    "same failure that unlocks it.")
        if cross and not (pin.get("lost_assurance") or "").strip():
            raise ConfigError(
                f"workflows.json: {wf}.roster.pinned.{cell} allows a cross-family fallback "
                "but states no 'lost_assurance'. Crossing vendor families changes what the "
                "review is worth; the run must be able to say what was traded away.")


# Bounds of a `semantic_audit` block. Every key is a positive count or character
# budget; the block is a BUDGET, never an agent specification (the audit runs in the
# workflow's existing arbiter cell), so nothing here names or resolves an agent.
SEMANTIC_AUDIT_KEYS = ("repeat_after_cycles", "prior_objections", "prior_assessments",
                       "repeat_max_prompt_chars", "gate_max_prompt_chars",
                       "repair_max_prompt_chars", "max_calls_per_mode")


def _validate_semantic_audit(wf, block):
    """A `semantic_audit` block, if present, must carry positive integer bounds.

    Absent means DISABLED, not invalid: a workflow that predates ARCH-02 (and every
    hand-built test workflow) must load and run without making a semantic call. A
    present-but-nonsensical bound is a different thing and is rejected — a zero or
    negative prompt budget would silently produce empty evidence."""
    audit = block.get("semantic_audit")
    if audit is None:
        return
    if not isinstance(audit, dict):
        raise ConfigError(f"workflows.json: {wf}.semantic_audit must be an object "
                          "(omit it entirely to disable semantic audits)")
    for key, value in audit.items():
        if key.startswith("_"):
            continue
        if key not in SEMANTIC_AUDIT_KEYS:
            raise ConfigError(f"workflows.json: {wf}.semantic_audit has unknown key "
                              f"'{key}' (known: {sorted(SEMANTIC_AUDIT_KEYS)})")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"workflows.json: {wf}.semantic_audit.{key} must be a "
                              f"positive integer, got {value!r}")


def _validate(agents, workflows):
    if "agents" not in agents:
        raise ConfigError("agents.json: missing top-level 'agents' object")
    _validate_agent_domains(agents)
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
        _validate_semantic_audit(wf, workflows[wf])
        _validate_roster(wf, workflows[wf],
                         {n: c for n, c in agents["agents"].items() if not n.startswith("_")})


class RunContext:
    def __init__(self, agents_cfg, workflows_cfg, adapters, guard, tx, resume_cache=None,
                 human_answer=None, resume_degradations=None):
        self.agents_cfg = agents_cfg
        self.workflows = workflows_cfg
        self.adapters = adapters
        self.guard = guard
        self.tx = tx
        # Resume support: {(step_id, phase, iteration): {agent, role, verdict, text}}
        # of steps that already completed in a prior interrupted run; the engine
        # replays these instead of re-invoking the agent. Empty = normal run.
        self.resume_cache = resume_cache or {}
        # Cross-family pin fallbacks the run being resumed ALREADY used. The resumed run
        # replays the steps those agents produced, so the assurance they cost is still
        # gone: the engine seeds its ledger from this list instead of starting clean and
        # letting the resume report a review that never happened. Empty = normal run.
        self.resume_degradations = list(resume_degradations or [])
        # Set by Engine.call on every step: whether it was replayed from the journal,
        # and whether that replay drained the cache (i.e. it is the LAST journaled
        # step — the point where the resumed run actually stopped). The architecture
        # loop needs both to place a --human-answer correctly: a deferral replayed
        # from the middle of the history was already answered by an EARLIER resume,
        # and consuming today's answer there leaves the real stop point with nothing
        # to apply (Irina 2026-08-27).
        self.last_call_replayed = False
        self.last_call_final_replay = False
        # Monotonic count of steps this run really INVOKED an agent for (a replay does
        # not increment it). An iteration loop charges its budget by watching this grow,
        # so an iteration is paid for when ANY of its steps ran live — not merely when
        # its first one did (Irina 2026-08-30).
        self.live_calls = 0
        # A human decision provided via `--human-answer` when resuming a run PAUSED
        # (AWAITING_HUMAN) because the arbiter could not decide. The architecture loop
        # feeds it back as a binding ruling. None on a normal run.
        self.human_answer = human_answer
        paths = workflows_cfg["paths"]
        pbase = project_base(workflows_cfg)
        self.repo_root = (pbase / paths["repo_root"]).resolve()
        # One or more directories holding the architecture documents an author should know
        # exist. A single path stays valid; a LIST scopes the context to the lines of work
        # that matter rather than to whatever happens to sit at one directory's top level.
        _docs_arch = paths.get("docs_architecture", "docs/architecture")
        self.docs_arch = [(pbase / d).resolve()
                          for d in ([_docs_arch] if isinstance(_docs_arch, str) else _docs_arch)]
        # Base roles ship WITH the tool and are the same for every project. A project may
        # point `roles_dir` at its own set, and then it owns them entirely - but the
        # supported way to add project knowledge is `context_file` / `role_context` below,
        # which leaves the base roles identical everywhere.
        self.roles_dir = ((pbase / paths["roles_dir"]).resolve() if paths.get("roles_dir")
                          else (BASE / "roles").resolve())
        self.exclude = workflows_cfg.get("guard", {}).get("exclude_prefix", BASE.name)
        # Every role prompt this run can use, read ONCE, now (see _snapshot_prompts).
        self._role_cache: dict[str, str] = {}
        self._wrapper_cache: dict[str, str | None] = {}
        self._snapshot_prompts()
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

    # ---- run provenance ------------------------------------------------
    def _repo_versions(self) -> dict:
        """Baseline commit + branch of each watched repo, as of run start.

        The commit is the baseline capture_diff already froze in __init__ - reused, not
        re-read, so what a run reports as its repo version cannot drift from the base
        the reviewed diff was actually taken against.
        """
        out = {}
        for label, path, kind in self.repos:
            commit = self._baselines.get(label)
            entry = {"path": path, "kind": kind, "commit": commit, "branch": None,
                     "unavailable": None}
            if commit is None:
                entry["unavailable"] = ("no git HEAD at run start (not a git worktree, "
                                        "an unborn branch, or git unavailable)")
            else:
                ok, branch = _git_read(Path(path), "rev-parse", "--abbrev-ref", "HEAD")
                if ok:
                    entry["branch"] = "(detached HEAD)" if branch == "HEAD" else branch
                else:
                    entry["unavailable"] = f"branch could not be read: {branch}"
            out[label] = entry
        return out

    def versions(self) -> dict:
        """What this run is running ON: the executing tool, the project layer in force,
        and every watched repo's run-start baseline. Recorded into run.json at run
        start so a number this run produces can be attributed to the code that produced
        it - the tool changes between runs, and an un-versioned measurement cannot be
        compared with any other.

        Never raises: each part already degrades to a stated reason on its own, and the
        catch-all below is there so no defect in provenance can cost a run its work.
        """
        try:
            return {"tool": tool_version(),
                    "project_layer": project_layer_version(self.workflows),
                    "repos": self._repo_versions()}
        except Exception as e:  # noqa: BLE001 - provenance must never stop a run
            return {"unavailable": f"version capture failed: {type(e).__name__}: {e}"}

    # ---- resume -------------------------------------------------------
    def resume_lookup(self, step_id, phase, iteration):
        """Return the journaled result for this step if we are resuming and it
        already completed, else None. Consumed once so a re-used (step_id, phase,
        iteration) later in the same run runs live rather than replaying twice."""
        if not self.resume_cache:
            return None
        return self.resume_cache.pop((step_id, phase, iteration), None)

    # ---- role prompts -------------------------------------------------
    def _read_role(self, role: str) -> str:
        f = self.roles_dir / f"{role}.md"
        try:
            return f.read_text(encoding="utf-8") if f.exists() \
                else f"(ROLE FILE MISSING: {f})"
        except OSError as e:
            # Reported like a missing file - in the system prompt of the step that
            # needed it - rather than raised: reading now happens at run start, and a
            # read error there would kill the run before it has a STATUS to record.
            return f"(ROLE FILE UNREADABLE: {f}: {e})"

    def _read_wrapper(self, wrapper: str) -> str | None:
        f = BASE / "prompts" / wrapper
        try:
            return f.read_text(encoding="utf-8") if f.exists() else None
        except OSError:
            return None

    def _snapshot_prompts(self):
        """Read every role file and prompt wrapper this run can use ONCE, at run start.

        WHY (2026-08-30): load_role used to read a role file on FIRST use, so a role a
        run had not reached yet - the Tech Lead gate runs last - was read from whatever
        the file said by then. In a run whose developer edits this tool, an agent could
        rewrite the prompt of the gate that judges it, mid-run. Nothing has done this;
        nothing should be able to. agents.json / workflows.json are already read once
        into memory, and the prompts they name now are too, so a file edited mid-run
        cannot change any step of the run in flight.

        Every role of every workflow is snapshotted, not just the current one: this is a
        superset of what a run can reach, it costs one read per file, and it keeps the
        freeze independent of which branch of a state machine a run happens to take.

        A missing file is NOT reported here. It keeps the exact placeholder load_role
        has always produced, so `(ROLE FILE MISSING: ...)` still surfaces in the step
        that needed it and doctor's roles_present() still stats the disk.
        """
        for wf in WORKFLOWS:
            for spec in (self.workflows.get(wf) or {}).values():
                if not isinstance(spec, dict):
                    continue
                if spec.get("role") and spec["role"] not in self._role_cache:
                    self._role_cache[spec["role"]] = self._read_role(spec["role"])
                if spec.get("wrapper") and spec["wrapper"] not in self._wrapper_cache:
                    self._wrapper_cache[spec["wrapper"]] = self._read_wrapper(spec["wrapper"])

    def load_role(self, role: str, wrapper: str | None = None) -> str:
        # Both reads come from the run-start snapshot. The lazy fill below only ever
        # fires for a role or wrapper no workflow configures, which no step can reach.
        if role not in self._role_cache:
            self._role_cache[role] = self._read_role(role)
        text = self._role_cache[role]
        if wrapper:
            if wrapper not in self._wrapper_cache:
                self._wrapper_cache[wrapper] = self._read_wrapper(wrapper)
            wtext = self._wrapper_cache[wrapper]
            if wtext is not None:
                text = text + "\n\n---\n\n" + wtext
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
    # How much of the architecture corpus reaches an author's prompt: every document by
    # name, and the head of this many of the newest.
    ARCH_EXCERPTS = 8
    ARCH_EXCERPT_CHARS = 1500
    ARCH_INDEX_MAX = 150

    def read_existing_arch(self) -> str:
        """What architecture documents exist, plus the head of the newest few.

        An author is read-only on the repositories but CAN read files, so the useful
        thing to hand it is an INDEX of what exists — it can then go and read whatever
        the task actually touches — with enough of the recent documents to recognise the
        line of work in progress.

        Until 2026-09-19 this globbed ONE directory NON-recursively. On a project whose
        designs live one level down (`doc/architecture/<line>/*.md`) that matched none of
        them, and the single unrelated file that did sit at the top level became the whole
        of "existing architecture" — so an author correcting a C design was shown a
        proposal to rewrite that component in another language, and never saw the
        decisions log it had to extend. Recursive, multi-root and index-first fixes that.

        Newest first, by modification time: the documents of the line being worked on now
        are the ones an author needs, while alphabetical order over `ADR-00nn` /
        `ARCH-CT-*` names carries no relevance signal whatsoever.
        """
        roots = [d for d in self.docs_arch if d.exists()]
        if not roots:
            missing = ", ".join(str(d) for d in self.docs_arch)
            return f"(no existing architecture docs directory: {missing})"

        found = []
        for root in roots:
            for f in root.rglob("*.md"):
                try:
                    found.append((f.stat().st_mtime, f, root))
                except OSError:
                    # dangling symlink / unreadable file: skip it rather than abort the run
                    continue
        if not found:
            return "(the architecture docs directories hold no .md files)"
        found.sort(key=lambda row: row[0], reverse=True)

        def label(f, root):
            try:
                return str(f.relative_to(root.parent))
            except ValueError:
                return f.name

        shown = found[:self.ARCH_INDEX_MAX]
        index = "\n".join(f"- {label(f, root)}" for _, f, root in shown)
        if len(found) > len(shown):
            index += f"\n- … and {len(found) - len(shown)} more"
        parts = [f"{len(found)} documents exist, newest first. Read any of them "
                 f"by path if the task touches it:\n{index}"]

        for _, f, root in found[:self.ARCH_EXCERPTS]:
            try:
                snippet = f.read_text(encoding="utf-8", errors="replace")[:self.ARCH_EXCERPT_CHARS]
            except OSError:
                continue
            parts.append(f"### {label(f, root)}\n{snippet}")
        return "\n\n".join(parts)

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
