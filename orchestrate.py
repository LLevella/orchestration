#!/usr/bin/env python3
"""Local AI orchestration entrypoint.

Subcommands:
    doctor                       report CLI/key availability and readiness
    chains [arch|dev|inv]        show agent<->role chains and substitution points
    arch  --task/--task-file     run the architecture workflow
    dev   --task/--task-file     run the development workflow
    inv   --task/--task-file     run the investigation (root-cause) workflow
    review --task/--target       run the review (adversarial panel) workflow
    show  <run-id>               print a run's STATUS and transcript location

Safety: never commits, pushes, deletes, installs, or runs destructive commands.
Only the Developer (Claude) step may edit production code; the git guard enforces
this. Use --mock to exercise the full pipeline with no real AI credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.adapters import build_adapters  # noqa: E402
from orchestrator import mr as mrlib  # noqa: E402
from orchestrator.config import (BASE, WORKFLOWS, ConfigError, RunContext,  # noqa: E402
                                 load_config, project_base, resolve_repos)
from orchestrator.engine import HUMAN_REQUIRED, SUCCESS_STATUSES, Engine  # noqa: E402
from orchestrator.guard import GitGuard  # noqa: E402
from orchestrator.transcript import Transcript  # noqa: E402

# Run history defaults to the tool's own runs/ (gitignored). A project that would
# rather keep its history with the project - the tool is shared, the runs are not -
# sets paths.runs_dir in its project layer.
RUNS_DIR = BASE / "runs"


def _runs_dir(workflows_cfg):
    rel = workflows_cfg["paths"].get("runs_dir")
    return (project_base(workflows_cfg) / rel).resolve() if rel else RUNS_DIR


def _install_interrupt_handler(tx):
    """Finalize the run's STATUS if the process is killed by a signal (e.g. a
    frozen host being force-stopped, a reboot's SIGTERM, or Ctrl-C). Without this
    a killed run leaves no STATUS — ambiguous between "running" and "dead". The
    per-step journal is already written, so the run is resumable regardless; this
    just records a clean terminal state and the resume command. SIGKILL / power
    loss cannot be caught — recover those with `resume`."""
    import signal

    def _handler(signum, _frame):
        try:
            tx.finish(HUMAN_REQUIRED,
                      f"Interrupted by signal {signum} (host freeze/stop/reboot or Ctrl-C). "
                      f"Resume with: python3 orchestrate.py resume {tx.run_id}")
        finally:
            os._exit(2)  # 2 == HUMAN_REQUIRED exit code; skip further teardown

    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _handler)


def _build_guard(workflows_cfg, extra_repos=()):
    """Guard watches every configured repo (dev + analysis), plus any run-scoped repo
    the caller adds - an mrreview target outside the watched set is guarded for that run
    so a read-only reviewer that writes to it is still caught. cwd is the workspace root."""
    repos = resolve_repos(workflows_cfg)
    guard_cfg = workflows_cfg.get("guard", {})
    guard = GitGuard([(label, path) for label, path, _ in repos] + list(extra_repos),
                     guard_cfg.get("exclude_prefix", "orchestration"),
                     guard_cfg.get("enabled", True))
    repo_root = str((project_base(workflows_cfg) / workflows_cfg["paths"]["repo_root"]).resolve())
    return guard, repos, repo_root


def _read_task(args, required=True) -> str:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    if args.task:
        return args.task
    if not required:
        return ""   # an MR review's brief is the merge request itself
    sys.exit("error: provide --task \"...\" or --task-file PATH")


def _human_answer(args):
    """Resolve --human-answer for resuming a PAUSED (AWAITING_HUMAN) arch run: inline text,
    or @path to read the decision from a file. None when the flag is absent."""
    val = getattr(args, "human_answer", None)
    if not val:
        return None
    return Path(val[1:]).read_text(encoding="utf-8") if val.startswith("@") else val


def _load_scenario(workflow: str, name: str):
    f = BASE / "fixtures" / workflow / f"{name}.json"
    if not f.exists():
        avail = [p.stem for p in (BASE / "fixtures" / workflow).glob("*.json")] \
            if (BASE / "fixtures" / workflow).exists() else []
        sys.exit(f"error: mock scenario '{name}' not found for {workflow}. Available: {avail}")
    return json.loads(f.read_text(encoding="utf-8"))


def _make_run_id(workflow: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{workflow}-{os.urandom(2).hex()}"


def _resolve_arch(val):
    """Resolve an --arch value (a file path or inline text) to architecture text,
    or None if not provided."""
    if not val:
        return None
    p = Path(val)
    return p.read_text(encoding="utf-8") if p.exists() else val


def _print_watched_repos(repos, guard):
    print("\nWatched repos (dev = writable, analysis = read-only context):")
    valid = {label for label, _ in guard.valid}
    for label, path, kind in repos:
        mark = "OK " if label in valid else "NOT-GIT"
        tag = "writable (dev)" if kind == "dev" else "read-only (analysis)"
        print(f"  {mark}  {label:22s} {tag:22s} {path}")
    print()


def _print_agents(adapters):
    """List agents with availability; return whether any non-disabled one is missing."""
    print("Agents (real, non-mock):")
    any_missing = False
    for name, ad in adapters.items():
        ok, reason = ad.availability()
        edit = " [can edit code]" if ad.can_edit else " [read-only]"
        if ad.cfg.get("disabled"):
            # Disabled agents are skipped by the rosters (chain shifts left); their
            # CLI may still be present, so they do not count as "missing".
            print(f"  {name:15s} DISABLED - {ad.cfg.get('_disabled_reason', 'disabled in config')}{edit}")
            continue
        any_missing |= not ok
        # A missing `roles` allowlist is not a CLI problem, so it never shows up as
        # MISSING - but roster._capable fails closed on it, so the agent silently holds
        # no cell in any workflow. Name it here rather than let a real run be surprised.
        no_roles = "" if (ad.cfg.get("roles") or []) else \
            "  <- no `roles` in agents.json: holds NO cell in any workflow"
        print(f"  {name:15s} {'OK ' if ok else 'MISSING'} - {reason}{edit}{no_roles}")
    return any_missing


_WF_ALIASES = {"arch": "architecture", "dev": "development", "inv": "investigation",
               "rev": "review", "impl": "implreview", "mr": "mrreview"}


def _agent_model(cfg: dict) -> str:
    """Best-effort human model label for an agent: explicit `model`, else a `-m/--model`
    flag in its command, else the CLI session default."""
    if cfg.get("model"):
        return cfg["model"]
    cmd = cfg.get("command", [])
    for flag in ("-m", "--model"):
        if flag in cmd:
            i = cmd.index(flag)
            if i + 1 < len(cmd) and cmd[i + 1]:
                return cmd[i + 1]
    return "CLI session default"


def _seat_note(key: str, can_edit: bool) -> str:
    """Short, key-derived description of a step's place in the chain."""
    notes = []
    if can_edit:
        notes.append("edit-capable (only Claude may edit/build)")
    notes.append({
        "author": "authors the proposal",
        "developer": "implements",
        "investigator": "forms falsifiable hypotheses",
        "developer_verifier": "PRIMARY review · FEASIBLE is the only PASS",
        "code_reviewer": "code review (developer peer-review)",
        "architect": "architecture review",
        "refuter": "adversarial refuter (every cycle)",
        "verifier": "final independent gate (fresh attack)",
        "tech_lead": "tech lead · escalation tier-1",
        "arbiter": "independent arbiter · escalation tier-2 / deadlock only",
    }.get(key, ""))
    return " · ".join(n for n in notes if n)


def _workflow_steps(wf_cfg: dict):
    """Yield (key, spec) for the ordered chain steps (dict values carrying an 'agent'),
    skipping limits/comments. Dict insertion order == chain order."""
    for key, spec in wf_cfg.items():
        if isinstance(spec, dict) and "agent" in spec:
            yield key, spec


def _print_cast(agents: dict):
    print("Cast (agents -> model, capability, roles allow-list):")
    for name, cfg in agents.items():
        if name.startswith("_"):
            continue
        cap = "WRITER    " if cfg.get("can_edit") else "read-only "
        extra = f"  [{cfg['api_base'].split('/')[2]}]" if cfg.get("api_base") else ""
        print(f"  {name:8s} {cfg.get('transport', 'cli'):3s} {cap} {_agent_model(cfg):24s}{extra}")
        print(f"           roles: {', '.join(cfg.get('roles', []))}")
    print()


def _print_chain(wf: str, wf_cfg: dict, agents: dict):
    print(f"\n  {wf.upper()}")
    for i, (key, spec) in enumerate(_workflow_steps(wf_cfg), 1):
        cfg = agents.get(spec["agent"], {})
        edit = "✎" if cfg.get("can_edit") else " "
        print(f"    {i}. {key:18s} {edit} {spec['agent']:8s} "
              f"({_agent_model(cfg)})  →  {spec['role']}")
        note = _seat_note(key, cfg.get("can_edit", False))
        if note:
            print(f"       └─ {note}")
    ov = wf_cfg.get("_comment_TEMP_OVERRIDE")
    if ov:
        print(f"    ⚠ ACTIVE OVERRIDE (substitution): {ov}")


def cmd_chains(args):
    """Print the agent<->role bindings: the cast, each workflow's ordered chain, and the
    'substitution' points (which family fills the arbiter seat + any active override)."""
    agents_cfg, workflows_cfg = load_config()
    agents = agents_cfg["agents"]
    repo_root = str((project_base(workflows_cfg) / workflows_cfg["paths"]["repo_root"]).resolve())
    arbiter_agent = workflows_cfg.get("architecture", {}).get("arbiter", {}).get("agent", "?")
    arb_cfg = agents.get(arbiter_agent, {})

    print("== Orchestration chains & roles ==\n")
    print(f"copy (repo_root)  : {repo_root}")
    print(f"arbiter seat      : {arbiter_agent} ({arb_cfg.get('transport', '?')}, "
          f"{_agent_model(arb_cfg)})  <- swappable independent family\n")
    _print_cast(agents)

    selected = [_WF_ALIASES[args.workflow]] if getattr(args, "workflow", None) else list(WORKFLOWS)
    print("Chains (steps in order — agent  →  role):")
    for wf in selected:
        _print_chain(wf, workflows_cfg.get(wf, {}), agents)
    print()


def cmd_doctor(args):
    agents_cfg, workflows_cfg = load_config()
    guard, repos, repo_root = _build_guard(workflows_cfg)
    adapters = build_adapters(agents_cfg, mock=False, scenario=None, cwd=repo_root)

    proj = workflows_cfg.get("_project") or {}
    print("== Orchestration doctor ==\n")
    print(f"project layer  : {proj.get('_source', '(none)')}")
    if "TEMPLATE" in proj.get("_source", ""):
        print("  ^ the shipped template, NOT a real project: enough for --mock and the tests,")
        print("    not for a real run. Copy config/project.example.json out of the repo, fill it")
        print("    in, and symlink it to config/project.json (or set $ORCH_PROJECT).")
    ctx_bits = [b for b in ("context_file" if proj.get("context_file") else "",
                            f"role_context x{len(proj.get('role_context') or {})}"
                            if proj.get("role_context") else "") if b]
    print(f"project context: {', '.join(ctx_bits) if ctx_bits else '(none — base roles only)'}")
    print(f"workspace root : {repo_root}  (agent cwd; not itself a git repo)")
    print(f"git guard      : {'active' if guard.active else 'INACTIVE'}"
          f"  enabled={guard.enabled}  available={guard.available}")
    if guard.invalid:
        print(f"  NOT git repos : {guard.invalid}  <-- guard fails closed until fixed")
    _print_watched_repos(repos, guard)

    any_missing = _print_agents(adapters)

    print("\nRoles (files referenced by config):")
    ctx = RunContext(agents_cfg, workflows_cfg, adapters, guard, tx=None)
    for role, present in sorted(ctx.roles_present().items()):
        print(f"  {'OK ' if present else 'MISSING'}  {role}")

    print("\nMock scenarios:")
    for wf in WORKFLOWS:
        d = BASE / "fixtures" / wf
        names = sorted(p.stem for p in d.glob("*.json")) if d.exists() else []
        print(f"  {wf:13s}: {names}")

    print("\nReady for --mock runs: YES (no credentials required).")
    print("Ready for real runs  :",
          "NO - install/configure the MISSING agents above." if any_missing else "YES.")


_MOCK_DIFF = ("--- a/mock/service.py\n+++ b/mock/service.py\n@@ -1,3 +1,4 @@\n"
              " def handler(req):\n-    return backend.fetch(req.id)\n"
              "+    # mock change under review\n+    return backend.fetch(req.id, retry=True)\n")


def _resolve_mr(workflows_cfg, args):
    """Turn the CLI's MR arguments into the change the panel reviews.

    A --mock run with no MR arguments reviews a canned diff, so the whole chain is
    exercisable with no repo, no refs, and no network. Any explicitly named MR
    source is honoured even under --mock.
    """
    named = args.mr or args.diff_file or args.base or args.head
    if args.mock and not named:
        return mrlib.MRContext(subject="mock merge request", repo_label="(mock)",
                               repo_path=None, base=None, head=None, base_sha=None,
                               head_sha=None, diff=_MOCK_DIFF, source="file")
    try:
        return mrlib.resolve(workflows_cfg, mr=args.mr, base=args.base, head=args.head,
                             repo=args.repo, diff_file=args.diff_file,
                             fetch=args.fetch, subject=args.subject)
    except mrlib.MRResolutionError as e:
        sys.exit(f"error: {e}")


def _deliver_document(tx_dir, out_path):
    """Copy the remarks document to the path the human asked for, if any."""
    src = tx_dir / "artifacts" / "mr-review.md"
    if not src.exists():
        print("[orchestrate] no remarks document was produced.")
        return
    print(f"[orchestrate]   REMARKS     : {src}")
    if not out_path:
        return
    dest = Path(out_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        print(f"[orchestrate] WARNING: could not write --out {dest}: {e}")
        return
    print(f"[orchestrate]   copied to   : {dest}")


def _run(workflow, args):
    agents_cfg, workflows_cfg = load_config()
    task = _read_task(args, required=(workflow != "mrreview"))
    scenario = None
    scenario_name = "(real)"
    if args.mock:
        scenario_name = args.scenario or workflows_cfg[workflow]["default_mock_scenario"]
        scenario = _load_scenario(workflow, scenario_name)

    mr = _resolve_mr(workflows_cfg, args) if workflow == "mrreview" else None
    extra_guarded = ([(mr.repo_label, mr.repo_path)]
                     if mr and mr.repo_path and not mr.guard_watched else [])
    guard, _repos, repo_root = _build_guard(workflows_cfg, extra_guarded)
    if extra_guarded:
        print(f"[orchestrate] guarding review target {extra_guarded[0][0]} for this run")
    # Fail-closed: a real run must not let read-only agents touch code unprotected.
    # Mock runs perform no edits, so they are always allowed.
    if not args.mock and not guard.active and not args.allow_unguarded:
        sys.exit(
            "refusing to start a real run: the git guard is inactive "
            f"(available={guard.available}, enabled={guard.enabled}).\n"
            f"Non-git or missing watched repos: {guard.invalid or '(none)'}.\n"
            "Read-only agents (Codex/Gemini/DeepSeek) would be unprotected against "
            "writing to production code.\n"
            "Fix: ensure every dev_repos/analysis_repos path is a git repo with the "
            "guard enabled, or pass --allow-unguarded to override at your own risk."
        )
    adapters = build_adapters(agents_cfg, mock=args.mock, scenario=scenario, cwd=repo_root)
    run_id = _make_run_id(workflow)
    tx = Transcript(_runs_dir(workflows_cfg), run_id, workflow, task, args.mock, scenario_name,
                    config_snapshot=workflows_cfg[workflow])
    _install_interrupt_handler(tx)
    ctx = RunContext(agents_cfg, workflows_cfg, adapters, guard, tx,
                     human_answer=_human_answer(args))
    engine = Engine(ctx, workflow)

    arch_ref = None
    target = None
    if workflow == "development":
        arch_ref = _resolve_arch(getattr(args, "arch", None))
        # Persist the --arch argument (path/inline) so an interrupted run can be
        # resumed with the same approved architecture.
        tx.update_meta(arch_ref=getattr(args, "arch", None))
    elif workflow == "review":
        target = _resolve_arch(getattr(args, "target", None))
        # Persist --target/--iterate/--max-iterations so an interrupted review resumes identically.
        tx.update_meta(target=getattr(args, "target", None),
                       review_iterate=getattr(args, "iterate", False),
                       review_max=getattr(args, "max_iterations", None))
    elif workflow == "implreview":
        target = _resolve_arch(getattr(args, "target", None))
        # Persist --target/--max-iterations so an interrupted implreview resumes identically.
        tx.update_meta(target=getattr(args, "target", None),
                       review_max=getattr(args, "max_iterations", None))

    print(f"[orchestrate] {workflow} run {run_id} (mock={args.mock}, scenario={scenario_name})")
    if mr:
        for line in mr.header_lines():
            print(f"[orchestrate]   {line}")
    status = engine.run(task, arch_ref=arch_ref, target=target,
                        iterate=getattr(args, "iterate", False),
                        max_iters=getattr(args, "max_iterations", None), mr=mr)
    print(f"[orchestrate] FINAL STATUS: {status}")
    print(f"[orchestrate] history: {tx.dir}")
    print(f"[orchestrate]   STATUS file : {tx.dir / 'STATUS'}")
    print(f"[orchestrate]   transcript  : {tx.dir / 'transcript.md'}")
    if workflow == "mrreview":
        _deliver_document(tx.dir, getattr(args, "out", None))
    # exit codes: 0 for a successful conclusion (PASS / READY_FOR_HUMAN_REVIEW /
    # ROOT_CAUSE_ESTABLISHED / NO_DEFECT_FOUND), 2 for HUMAN_REQUIRED.
    return 0 if status in SUCCESS_STATUSES else 2


def _print_usage(run_dir):
    """Print the token-usage summary (from usage.json) if the run produced one."""
    f = run_dir / "usage.json"
    if not f.exists():
        return
    try:
        u = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    t = u.get("totals", {})
    print(f"\nToken usage (totals): in {t.get('input')} / out {t.get('output')} / "
          f"total {t.get('total')} · cost(claude) ${t.get('cost_usd', 0):.4f} · "
          f"{t.get('steps')} steps ({t.get('metered')} metered)")
    by_agent = u.get("by_agent", {})
    if by_agent:
        print("  by agent (by total tokens):")
        for a, b in sorted(by_agent.items(), key=lambda kv: -(kv[1].get("total") or 0)):
            print(f"    {a:15s} steps {b.get('steps'):>2}  total {b.get('total'):>9}  "
                  f"${b.get('cost_usd', 0):.4f}")
    print(f"  full breakdown: {f}")


def cmd_show(args):
    _agents, workflows_cfg = load_config()
    runs = _runs_dir(workflows_cfg)
    d = runs / args.run_id
    if not d.exists():
        runs = sorted(p.name for p in runs.glob("*")) if runs.exists() else []
        sys.exit(f"error: run '{args.run_id}' not found. Available: {runs}")
    status = (d / "STATUS").read_text(encoding="utf-8") if (d / "STATUS").exists() else "(no STATUS yet)"
    print(f"== {args.run_id} ==\n{status}")
    _print_usage(d)
    print(f"transcript : {d / 'transcript.md'}")
    print(f"events     : {d / 'events.jsonl'}")
    print(f"artifacts  : {d / 'artifacts'}")


_OUTPUT_MARKER = "## Agent Output\n\n"


def _load_resume_cache(src: Path) -> dict:
    """Build {(step_id, phase, iteration): {agent, role, verdict, text}} from a
    prior run's journal, so its successfully-completed steps can be replayed on
    resume. The full agent output text is recovered from each step's iteration
    file. Only steps that finished with status OK are cached — a step that errored
    or was aborted (e.g. a CLI failure or an interrupted run) must re-run live,
    never be replayed from its failed result."""
    cache = {}
    ev = src / "events.jsonl"
    if not ev.exists():
        return cache
    for line in ev.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") != "step" or e.get("status") != "OK":
            continue
        text = ""
        f = src / e.get("file", "")
        if e.get("file") and f.exists():
            body = f.read_text(encoding="utf-8")
            if _OUTPUT_MARKER in body:
                text = body.split(_OUTPUT_MARKER, 1)[1].split("\n## Error\n\n", 1)[0].rstrip()
        cache[(e["step"], e["phase"], e["iteration"])] = {
            "agent": e["agent"], "role": e["role"],
            "verdict": e.get("verdict"), "text": text,
        }
    return cache


def cmd_resume(args):
    """Resume an interrupted real run: replay its journaled steps instantly, then
    run the first un-journaled step and everything after it live. The working tree
    (including the developer's persisted edits) is reused as-is."""
    agents_cfg, workflows_cfg = load_config()
    runs_dir = _runs_dir(workflows_cfg)
    src = runs_dir / args.run_id
    if not src.exists() or not (src / "run.json").exists():
        avail = sorted(p.name for p in runs_dir.glob("*")) if runs_dir.exists() else []
        sys.exit(f"error: run '{args.run_id}' not found. Available: {avail}")
    meta = json.loads((src / "run.json").read_text(encoding="utf-8"))
    if meta.get("mock"):
        sys.exit("error: refusing to resume a mock run (re-run it with --mock instead).")
    workflow, task = meta["workflow"], meta["task"]

    guard, _repos, repo_root = _build_guard(workflows_cfg)
    if not guard.active and not args.allow_unguarded:
        sys.exit("refusing to resume: the git guard is inactive "
                 f"(invalid repos: {guard.invalid or '(none)'}). Pass --allow-unguarded to override.")

    cache = _load_resume_cache(src)
    adapters = build_adapters(agents_cfg, mock=False, scenario=None, cwd=repo_root)
    run_id = _make_run_id(workflow)
    tx = Transcript(runs_dir, run_id, workflow, task, False, f"(resume of {args.run_id})",
                    config_snapshot=workflows_cfg[workflow])
    tx.update_meta(resumed_from=args.run_id, resumed_steps=len(cache))
    _install_interrupt_handler(tx)
    ctx = RunContext(agents_cfg, workflows_cfg, adapters, guard, tx, resume_cache=cache,
                     human_answer=_human_answer(args))
    engine = Engine(ctx, workflow)

    arch_ref = None
    target = None
    if workflow == "development":
        arch_ref = _resolve_arch(getattr(args, "arch", None) or meta.get("arch_ref"))
    elif workflow in ("review", "implreview"):
        target = _resolve_arch(meta.get("target"))

    mr = None
    if workflow == "mrreview":
        # The refs are re-diffed against the CURRENT repo state: if the author pushed
        # while the run was down, the resumed panel reviews the newer code while the
        # already-journaled opinions describe the older one. Say so rather than hide it.
        spec = meta.get("mr") or {}
        if spec.get("source") == "file":
            sys.exit("error: this review ran on a patch file; re-run it with --diff-file "
                     "instead of resuming (the patch is not stored in the run).")
        try:
            mr = mrlib.resolve(workflows_cfg, mr=spec.get("mr_iid"), base=spec.get("base"),
                               head=spec.get("head"), repo=spec.get("repo_label"),
                               subject=spec.get("subject"))
        except mrlib.MRResolutionError as e:
            sys.exit(f"error: cannot re-resolve the merge request for this resume: {e}")
        print("[orchestrate] NOTE: the diff was re-read from the repository just now; "
              "if the branch moved since the interrupted run, earlier opinions describe "
              "the older code.")

    print(f"[orchestrate] RESUME {workflow} from {args.run_id} as {run_id} "
          f"({len(cache)} step(s) replayed from journal)")
    status = engine.run(task, arch_ref=arch_ref, target=target,
                        iterate=meta.get("review_iterate", False),
                        max_iters=meta.get("review_max"), mr=mr)
    print(f"[orchestrate] FINAL STATUS: {status}")
    print(f"[orchestrate] history: {tx.dir}")
    if workflow == "mrreview":
        _deliver_document(tx.dir, None)
    return 0 if status in SUCCESS_STATUSES else 2


def main():
    p = argparse.ArgumentParser(description="Local AI orchestration for this workspace.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="report CLI/key availability and readiness")

    spc = sub.add_parser("chains", help="show agent<->role chains and substitution points")
    spc.add_argument("workflow", nargs="?",
                     choices=("arch", "dev", "inv", "rev", "impl", "mr"),
                     help="limit to one workflow (default: all)")

    for name, helptext in (("arch", "run the architecture workflow"),
                           ("dev", "run the development workflow"),
                           ("inv", "run the investigation (root-cause) workflow"),
                           ("review", "run the review (adversarial panel) workflow"),
                           ("implreview", "run the implementation-readiness review (6-role, 3-stage cyclic gate)"),
                           ("mrreview", "run the merge-request review panel (4 independent experts + moderator)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--task", help="task description")
        sp.add_argument("--task-file", help="path to a task description file")
        sp.add_argument("--mock", action="store_true", help="use scripted fixtures, no real AI")
        sp.add_argument("--scenario", help="mock scenario name (default from config)")
        sp.add_argument("--allow-unguarded", action="store_true",
                        help="override the fail-closed git-guard check (unsafe)")
        if name == "arch":
            sp.add_argument("--human-answer",
                            help="human decision for an arbiter that could not decide: inline text "
                                 "or @path (mainly used with `resume` after an AWAITING_HUMAN pause)")
        if name == "dev":
            sp.add_argument("--arch", help="approved architecture: file path or inline text")
        if name == "review":
            sp.add_argument("--target",
                            help="artifact to review: file path or inline text (defaults to --task)")
            sp.add_argument("--iterate", action="store_true",
                            help="refine-to-STRONG loop: a reviser applies the arbiter's must-fix and "
                                 "the panel re-reviews until STRONG or --max-iterations (default: one-shot critique)")
            sp.add_argument("--max-iterations", type=int,
                            help="max refine iterations when --iterate (default: config max_iterations)")
        if name == "implreview":
            sp.add_argument("--target",
                            help="artifact to review: file path or inline text (defaults to --task)")
            sp.add_argument("--max-iterations", type=int,
                            help="max author-revision rounds (default: config max_iterations=5)")
        if name == "mrreview":
            sp.add_argument("--mr", help="GitLab MR iid; its branches are read via `glab` "
                                        "and diffed locally")
            sp.add_argument("--base", help="target ref of the MR (default: origin/main)")
            sp.add_argument("--head", help="source ref of the MR (default: HEAD)")
            sp.add_argument("--repo", help="repo holding the MR: a watched repo's label, or any "
                                           "git worktree path (default: the single dev repo)")
            sp.add_argument("--diff-file", help="review a patch file instead of git refs")
            sp.add_argument("--fetch", action="store_true",
                            help="`git fetch --prune origin` first (updates refs, never the tree)")
            sp.add_argument("--subject", help="how to name the change in the document")
            sp.add_argument("--out", help="also write the remarks document to this path")

    spr = sub.add_parser("resume", help="resume an interrupted run from its last completed step")
    spr.add_argument("run_id", help="the interrupted run to resume")
    spr.add_argument("--arch", help="override approved architecture (else taken from the run)")
    spr.add_argument("--human-answer",
                     help="human decision to resume a PAUSED (AWAITING_HUMAN) arch run: inline "
                          "text or @path; fed back into the loop as a binding ruling")
    spr.add_argument("--allow-unguarded", action="store_true",
                     help="override the fail-closed git-guard check (unsafe)")

    sps = sub.add_parser("show", help="print a run's status and transcript location")
    sps.add_argument("run_id")

    args = p.parse_args()
    try:
        if args.cmd == "doctor":
            cmd_doctor(args)
        elif args.cmd == "chains":
            cmd_chains(args)
        elif args.cmd == "arch":
            sys.exit(_run("architecture", args))
        elif args.cmd == "dev":
            sys.exit(_run("development", args))
        elif args.cmd == "inv":
            sys.exit(_run("investigation", args))
        elif args.cmd == "review":
            sys.exit(_run("review", args))
        elif args.cmd == "implreview":
            sys.exit(_run("implreview", args))
        elif args.cmd == "mrreview":
            sys.exit(_run("mrreview", args))
        elif args.cmd == "resume":
            sys.exit(cmd_resume(args))
        elif args.cmd == "show":
            cmd_show(args)
    except ConfigError as e:
        sys.exit(f"config error: {e}")


if __name__ == "__main__":
    main()
