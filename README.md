# Local AI Orchestration (dataplane workspace)

A safe, local, multi-agent orchestrator that drives **architecture**,
**development**, and **investigation** (root-cause) loops automatically,
preserves every discussion and decision, enforces hard iteration limits, and
stops with a successful conclusion (**PASS** / **READY_FOR_HUMAN_REVIEW** /
**ROOT_CAUSE_ESTABLISHED** / **NO_DEFECT_FOUND**) or **HUMAN_REQUIRED**.

This copy is configured for the **multi-repo `dataplane/` workspace**: the
Developer actively edits the `dev_repos`, while the `analysis_repos` are
read-only context. See **[Workspace & repositories](#workspace--repositories)**.

Control flow is deterministic Python (stdlib only). All reasoning lives in the
agents and the role files under [`roles/`](roles). The orchestrator only
sequences steps, parses verdicts, enforces safety, and writes history.

## Agent ↔ role binding

Binding is controlled by `config/agents.json` (authoritative). The `model:`
field in role-file frontmatter is intentionally **ignored**, and role files are
**never modified** by this tool.

| Agent | Model | Effort / reasoning | Roles | Can edit code? |
|---|---|---|---|---|
| **Claude** | Opus 4.8 (`claude-opus-4-8`) | extended thinking `MAX_THINKING_TOKENS=16000` ("think hard" tier) | Software Engineering Developer, Responsible Developer, **Root Cause Investigator** | ✅ yes — the only writer |
| **Codex** | GPT‑5.5 | `model_reasoning_effort = xhigh` (max) | System Architect, Software Engineering Code Reviewer, **Adversarial Verifier** | ❌ read-only |
| **Gemini** | `gemini-pro-latest` | default (gemini CLI exposes no effort flag; the model reasons by default) | System Architecture Reviewer, Technical Lead, **Hypothesis Refuter** | ❌ read-only |
| **DeepSeek** | `deepseek-reasoner` | intrinsic (a reasoning model; no effort parameter is sent) | Arbitration Reviewer / Third Opinion, **Investigation Arbiter** | ❌ read-only |

Where each is configured: **Claude** — `claude` CLI session model (Opus 4.8) + env
`MAX_THINKING_TOKENS=16000` (in `config/agents.json`). **Codex** — `~/.codex/config.toml`
(`model = "gpt-5.5"`, `model_reasoning_effort = "xhigh"`). **Gemini** —
`config/agents.json` `-m gemini-pro-latest` (no reasoning-effort flag in the CLI).
**DeepSeek** — `config/agents.json` `model: deepseek-reasoner` (reasoning is built into
the model). Bindings in `agents.json` are authoritative; role-file `model:` frontmatter
is ignored.

## Workspace & repositories

The orchestration tool lives at the **workspace root** `dataplane/`, which is
**not** itself a git repo — each project under it is a separate repo. Config
(`config/workflows.json` → `paths`) declares two sets:

| Set | Repos | Developer (Claude) | Reviewers | Guard | capture_diff |
|---|---|---|---|---|---|
| `dev_repos` | `dataplane-phs-ng`, `dataplane-sdk` | **may edit** (active development) | read-only | watched | labelled **WRITABLE** |
| `analysis_repos` | `MCD4_Lite`, `ncc-core`, `idu/sdk-s8000` | read-only context | read-only | watched | labelled **READ-ONLY** |

- **`repo_root`** (`..` = `dataplane/`) is the **cwd every agent CLI is launched
  in**, so Codex/Gemini/Claude can read all repos, `roles/`, and `docs/`
  regardless of where you invoke `orchestrate.py` from.
- The **git guard** snapshots *all* watched repos (dev + analysis) around every
  read-only step; any change under a read-only agent aborts the run, with the
  delta labelled by repo. Because the root is not a git repo, the guard is
  inactive (fail-closed) unless **every** listed repo is a valid git worktree.
- **`capture_diff`** aggregates each repo's diff vs its own baseline `HEAD`,
  under a per-repo `### <repo> — WRITABLE/READ-ONLY` header. A change in a
  read-only repo is surfaced to reviewers as an out-of-scope edit to reject.
- **Out-of-list = out of scope.** Only `dev_repos` + `analysis_repos` are in
  scope. Any other repository or directory that happens to coexist in the
  workspace is NOT the orchestrator's concern: it never enters `capture_diff`,
  the guard never watches it, and reviewers are told to ignore it (not flag it).
  This is what lets you develop elsewhere in the workspace in parallel with a run.
- The Developer prompt carries an explicit writable/read-only repo list; the
  reviewer prompts carry a one-line scope summary. (Note: the guard only checks
  *read-only* steps, so keeping the Developer out of `analysis_repos` is
  enforced by the prompt scope + review, not the guard.)
- Run `python3 orchestrate.py doctor` to see the watched repos and which are
  valid git worktrees.

To retarget the workspace, edit `paths.dev_repos` / `paths.analysis_repos` in
`config/workflows.json` (paths are relative to `repo_root`).

## Quick start

```bash
cd orchestration   # at the dataplane/ workspace root

# 1) Check what's installed / configured (never installs anything)
python3 orchestrate.py doctor

# 2) Exercise the whole pipeline with NO credentials (scripted fixtures)
python3 orchestrate.py arch --mock --task "Add a health-check endpoint."
python3 orchestrate.py arch --mock --scenario escalate --task "Add a queue?"
python3 orchestrate.py dev  --mock --task "Fix off-by-one in foo()."
python3 orchestrate.py dev  --mock --scenario escalate --task "Refactor bar."
python3 orchestrate.py inv  --mock --task "Terminal drops from the RL on UH-1."
python3 orchestrate.py inv  --mock --scenario arbitrate_stands --task "ES-3 TS2 logon dropped."

# 3) Inspect a run
python3 orchestrate.py show <run-id>
```

Exit code: `0` for a successful conclusion (PASS / READY_FOR_HUMAN_REVIEW /
ROOT_CAUSE_ESTABLISHED / NO_DEFECT_FOUND), `2` for HUMAN_REQUIRED.

## Workflows

### Architecture (`arch`)
Codex authors a proposal → Gemini reviews → Codex revises (applying agreed
points, documenting disagreements) → Gemini re-reviews. Up to **10 iterations**.
If the same blocker repeats more than `trigger_after_cycles` (2) or the loop
exhausts, **DeepSeek** arbitrates for up to **10 more** iterations.
Ends **PASS** (Gemini PASS / NOT_APPLICABLE) or **HUMAN_REQUIRED**.

Proposals are written to run history only. Promotion to the repo's
`docs/architecture/` is a separate, human-approved step (see Q6 decision).

### Development (`dev`)
Claude implements (the only step allowed to edit code) → Codex reviews the diff
as Code Reviewer and as Architect every iteration. **Gemini (Tech Lead) is the
final gate**: it runs only once Code Review passes and the Architect passes /
is not-applicable / escalates — while a lower review still has blockers, Claude
fixes those first and the Tech Lead is not invoked (it never reacts to
intermediate fixes). The Developer is scope-fenced to the task (no other-phase
or unrelated edits — those are rejected in review).
Up to **10 iterations**. `DEVELOPER_NEEDS_FIX` → Claude fixes valid blockers;
`ARCHITECT_REVIEW_NEEDS_REVISION` → reviews re-run. Repeated disagreement
(> 2 cycles) triggers **DeepSeek** arbitration for up to **10 more** iterations.
Ends **READY_FOR_HUMAN_REVIEW** or **HUMAN_REQUIRED**.

> Run `dev` on a clean tree/branch: the reviewer diff is computed against each
> watched repo's baseline `HEAD`, so any pre-existing uncommitted changes in any
> `dev_repos`/`analysis_repos` appear in it.

### Investigation (`inv`)
A root-cause investigation team that does **not** ship a fix — it produces a
*proven, falsifiable* root-cause hypothesis (or a clean `NO_DEFECT_FOUND`).

1. **Investigator** (Claude) reads **all five repos on the branches named in the
   problem**, **derives each service's log location from the repos themselves**
   (systemd units, logging/ConfD config, code, `devtools` collectors) — the
   problem gives the hosts, not the paths — then reads those logs on the named
   hosts (read-only, via `devtools/estella-ops` / `hub-phs-check`), correlates
   log line → `repo/path:line`
   → mechanism → falsifier, and emits ranked hypotheses with a **per-repo
   guilty/cleared verdict** (no project is presumed innocent). It is the **only**
   role that may edit/build, and **only** in `dataplane-phs-ng` / `dataplane-sdk`,
   and only as temporary debug instrumentation to confirm/kill a hypothesis.
2. **Refuter** (Gemini) attacks every cycle and loops with the Investigator until
   it genuinely cannot break the chain (`NOT_REFUTED`); `REFUTED` /
   `INSUFFICIENT_EVIDENCE` sends it back.
3. **Verifier** (Codex) is the **final, independent gate** — it runs only once the
   Refuter passes, and must land a *new* attack the Refuter did not, hunting the
   blind spot the first two may share. `CONFIRMED` ends the run.
4. **Arbiter** (DeepSeek) is invoked only on escalation (`NEEDS_ARBITRATION`),
   repeated refutation (> `trigger_after_cycles`), or loop exhaustion.

Up to **10 iterations**, then up to **10 more** under arbitration. The
Investigator's debug diff is captured each cycle and shown to the skeptics, so a
stray edit to a read-only repo is surfaced as a disqualifying scope violation
(and the git guard independently aborts on any read-only-step write). Ends
**ROOT_CAUSE_ESTABLISHED** / **NO_DEFECT_FOUND** (success) or **HUMAN_REQUIRED**
(blocked for data/access, arbiter stop, or no convergence). The deliverable is
the hypothesis dossier in run history (`artifacts/root-cause-final.md`,
`disagreements.md`); turning it into a fix is the separate `dev` workflow.

> The problem description (`--task` / `--task-file`) is the team's only input, so
> it must name the affected repos, the **branch/commit per repo**, and the
> **hosts/servers** to read. It need **not** list log paths: the Investigator
> derives each service's log location from the repos themselves (systemd units,
> logging/ConfD config, code, `devtools` collectors) on the analysed branch — and
> a log path that differs from what the problem assumes is itself a finding.
> Branch discipline is enforced in the role: a hypothesis built on the wrong ref
> is rejected.

## Layout

```
orchestration/
├─ orchestrate.py            entrypoint (doctor / arch / dev / show)
├─ orchestrator/             config, verdicts, guard, transcript, adapters, engine
├─ config/agents.json        agent↔model↔command binding (authoritative)
├─ config/workflows.json     loop limits, step bindings, paths, guard, validation
├─ roles/<Role Name>.md      role prompts (all agent reasoning lives here)
├─ prompts/                  architecture-authoring wrapper (does not modify roles)
├─ fixtures/<wf>/<name>.json scripted mock responses (pass, escalate)
└─ runs/<run-id>/            full history (gitignored): run.json, events.jsonl,
                             transcript.md, iterations/, artifacts/, disagreements.md, STATUS
```

## Safety model

- **Only Claude/Developer edits code.** Reviewers get `read_only_args`, never
  `edit_args`. A **git guard** snapshots the working tree of *every watched repo*
  (`dev_repos` + `analysis_repos`) before/after every read-only step and
  **aborts the run** if anything changed (the delta is labelled by repo). Note
  this is *detect-and-abort* (post-hoc), not prevention — it stops the loop and
  records a `guard_violation`, but does not roll back a write.
- **Fail-closed:** a real (non-mock) run refuses to start if the git guard is
  inactive — i.e. `guard.enabled=false` or **any** configured repo is not a
  valid git worktree — so read-only agents are never run unprotected. Override
  only with `--allow-unguarded`.
- **No commit, push, delete, install, or destructive commands** anywhere. All
  subprocess calls are argv lists (no shell); the only git verbs used are
  read-only (`status`, `diff`, `rev-parse`, `ls-files`).
- **Reviewers are pure-analysis.** The orchestrator never executes build/test
  commands itself — only the Developer agent runs them, inside its own sandbox.
  The `validation` config block is reserved policy and is read by no code yet.
- **Fail-safe verdicts:** an unparseable/missing verdict is treated as
  HUMAN_REQUIRED, never a silent PASS.
- **Always-finalized history:** even an unexpected error writes a STATUS and
  records the traceback to `events.jsonl`.
- **Mock mode** runs the full state machine with zero credentials.
- **Missing CLIs / keys** are detected (`doctor`) and abort runs with a clear
  message — nothing is installed automatically.

## Wiring real agents

`codex`, `gemini`, and a DeepSeek transport are not assumed installed. Once they
are, set the exact headless command templates in `config/agents.json`
(`{prompt}` / `{system}` placeholders). DeepSeek supports either `transport:
"api"` (key in `$DEEPSEEK_API_KEY`) or `transport: "cli"` (e.g. Ollama). Run
`doctor` to confirm readiness before a real run.

> **Guard caveat:** if a real CLI writes cache/log files *inside the repo* (e.g.
> a `.codex/` dir), the guard will count that as a working-tree change under a
> read-only agent and abort. If that happens, point the tool's cache outside the
> repo, or add its path to `.gitignore` (untracked-but-ignored files don't count).

### Installed CLIs and read-only enforcement

| Agent | Install | Read-only flag applied |
|---|---|---|
| Codex | OpenAI native installer (`~/.local/bin/codex`) | `--sandbox read-only` |
| Gemini | `npm i -g @google/gemini-cli` (needs Node ≥ 20, e.g. via nvm) | `--approval-mode plan --skip-trust` |
| DeepSeek | none — official API | n/a (HTTP, no local writes) |

### Environment needed for real runs

`orchestrate.py` invokes the CLIs by bare name and inherits the shell's `PATH`
and env, so a real run must be launched from a shell where:

- `~/.local/bin` is on `PATH` (Codex), and nvm is sourced so `gemini`/Node ≥ 20
  resolve — e.g. `source ~/.nvm/nvm.sh`;
- the provider keys are exported, e.g. `source ~/.config/orchestration/deepseek.env`
  and `source ~/.config/orchestration/gemini.env` (`600` files kept **out of the
  repo**), or otherwise `export DEEPSEEK_API_KEY=…` / `export GEMINI_API_KEY=…`.

### Per-agent authentication

- **Codex** — `codex login` (ChatGPT account) or `export OPENAI_API_KEY=…`.
- **Gemini** — `export GEMINI_API_KEY=…` or run `gemini` once to log in; an
  unauthenticated `gemini -p` exits non-zero fast (no hang), so a real run just
  aborts to HUMAN_REQUIRED rather than stalling.
- **DeepSeek** — `DEEPSEEK_API_KEY` env var only; the account also needs a
  positive balance (an empty balance returns HTTP 402, handled gracefully).

To add a mock scenario, drop a `fixtures/<workflow>/<name>.json` file (see the
`pass`/`escalate` examples) and run with `--scenario <name>`.

## Not yet implemented (deliberately conservative)

- Promotion of an approved proposal into `docs/architecture/` (kept manual).
- Resume of an interrupted run.
- Reviewer read-only validation commands (scaffolded in config, disabled).
