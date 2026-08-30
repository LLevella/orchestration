# Local AI Orchestration

A safe, local, multi-agent orchestrator that drives six loops automatically —
**architecture**, **development**, **investigation** (root-cause), **review** (of
an artifact), **implreview** (implementation readiness) and **mrreview** (a
merge-request panel) — preserves every discussion and decision, enforces hard
iteration limits, and stops with a successful conclusion (**PASS** /
**READY_FOR_HUMAN_REVIEW** / **ROOT_CAUSE_ESTABLISHED** / **NO_DEFECT_FOUND** /
**REVIEW_COMPLETE** / **IMPLREVIEW_COMPLETE**) or **HUMAN_REQUIRED**.

Everything in this repository is meant to be **reusable by any project**. What is
not reusable — which repositories exist and where they live, and the domain the
work is about — is one file outside it: the **[per-project layer](#the-per-project-layer)**.

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

> **Setting this up, or running it:** [`docs/SETUP.md`](docs/SETUP.md) is the
> step-by-step — what goes in the tool, what goes beside your project, how to launch
> each workflow, and what the guard will stop.

## The per-project layer

The split is the point: **this repository holds only what any project can reuse**
— the engine, the base roles, the workflows, the fixtures and the tests. One file,
kept outside it, says what THIS project is:

```jsonc
{
  "paths": {
    "repo_root": "..",                       // the multi-repo workspace root
    "dev_repos":      ["service-a", "sdk"],  // the Developer MAY edit these
    "analysis_repos": ["upstream-x"],        // read-only context, never modified
    "docs_architecture": "../docs/architecture"
  },
  "guard":   { "enabled": true, "exclude_prefix": "orchestration" },
  "context_file": "context.md",              // appended to EVERY role's prompt
  "role_context": {                          // ... and blocks only one role sees
    "Root Cause Investigator": "roles-context/root-cause-investigator.md"
  }
}
```

Paths in it are relative to **its own** directory, so it stays valid wherever the
tool is checked out. The tool finds it, in order: `$ORCH_PROJECT` (a file or a
directory), `config/project.json` (gitignored — normally a symlink to the real
one), and failing both the shipped `config/project.example.json`, which is enough
for `--mock` runs and the test suite and not enough for a real run. `doctor` prints
which one is in force.

**Base roles are identical for every project.** Domain knowledge — the components,
the contracts, the tooling and hosts, the local ground rules — belongs in
`context_file` / `role_context`, which are appended to the role prompt after the
role text and before the global policy. A project *can* replace the whole set with
`paths.roles_dir`, but then it owns them, and they drift.

| Set | Developer (Claude) | Reviewers | Guard | capture_diff |
|---|---|---|---|---|
| `dev_repos` | **may edit** (active development) | read-only | watched | labelled **WRITABLE** |
| `analysis_repos` | read-only context | read-only | watched | labelled **READ-ONLY** |

- **`repo_root`** is the **cwd every agent CLI is launched in**, so Codex/Gemini/
  Claude can read all repos, `roles/`, and `docs/` regardless of where you invoke
  `orchestrate.py` from.
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
cd <tool>   # wherever this repo is checked out

# 1) Check what's installed / configured (never installs anything)
python3 orchestrate.py doctor

# 2) Exercise the whole pipeline with NO credentials (scripted fixtures)
python3 orchestrate.py arch --mock --task "Add a health-check endpoint."
python3 orchestrate.py arch --mock --scenario escalate --task "Add a queue?"
python3 orchestrate.py dev  --mock --task "Fix off-by-one in foo()."
python3 orchestrate.py dev  --mock --scenario escalate --task "Refactor bar."
python3 orchestrate.py inv  --mock --task "Sessions drop on host X after the 14:00 restart."
python3 orchestrate.py inv  --mock --scenario arbitrate_stands --task "Requests time out on node 2."
python3 orchestrate.py review     --mock --task "Review this design." --target DESIGN.md
python3 orchestrate.py implreview --mock --task "Ready to implement?"  --target PLAN.md
python3 orchestrate.py mrreview   --mock          # canned diff: no repo, refs or network

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
   hosts (read-only, via whatever collection tooling the workspace ships),
   correlates log line → `repo/path:line`
   → mechanism → falsifier, and emits ranked hypotheses with a **per-repo
   guilty/cleared verdict** (no project is presumed innocent). It is the **only**
   role that may edit/build, and **only** in the `dev_repos`, and only as temporary
   debug instrumentation to confirm/kill a hypothesis.
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

### Review (`review`) and implementation review (`implreview`)
`review` critiques ONE ARTIFACT (a design, a plan, a document) with three
adversarial lenses — correctness, completeness, risk — plus an arbiter that ranks
the findings into a must-fix list; `--iterate` adds a reviser and re-reviews until
STRONG. `implreview` is the heavier 6-role, 3-stage readiness gate over an
implementation plan (author ↔ dev+devops peers → tech lead → PM, arbiter on
deadlock). Both take the artifact via `--target` (path or inline text) and end
**REVIEW_COMPLETE** / **IMPLREVIEW_COMPLETE** or **HUMAN_REQUIRED**.

### Merge-request review panel (`mrreview`)
Four experts review the SAME merge request **independently** — no cross-talk, no
iteration, no gate — and a moderator merges their opinions into one deduplicated,
severity-ranked list of remarks, written to `runs/<id>/artifacts/mr-review.md`
(`--out PATH` copies it anywhere you like).

```bash
python3 orchestrate.py mrreview --mr 174 --repo service-a --fetch
python3 orchestrate.py mrreview --base origin/main --head HEAD --repo sdk
python3 orchestrate.py mrreview --diff-file /tmp/change.patch --subject "retry policy"
```

The MR reaches the panel in one of three ways: `--mr <iid>` (branches read via
`glab`, diffed locally), plain `--base/--head` refs (needs nothing but git), or
`--diff-file` (needs neither git nor network). Whatever the entry point, reviewers
see the same three-dot (merge-base) diff plus the commit list and diffstat, so
commits the target branch gained meanwhile are never blamed on this MR.

Properties worth knowing, because they are deliberate:
- **The deliverable is the remarks list, not a verdict.** REQUEST_CHANGES from all
  four still ends **REVIEW_COMPLETE** — the run succeeded in producing remarks.
- **A dead vendor costs one opinion, not the run.** Two or more usable opinions
  still produce the document; one produces it unmoderated and ends HUMAN_REQUIRED.
- **The document names no model and no run** — reviewers are "Reviewer 1..4", in
  call order. Provenance lives in the run dir (`opinion-N-<agent>.md`).
- **The panel tally is computed, never asked of a model**, so the document's
  headline cannot drift from the verdicts it lists.
- **The review target need not be a watched repo.** A repo outside
  `dev_repos`/`analysis_repos` is added to the git guard for that run.
- **Unrelated edits elsewhere do not abort it.** A guard violation in a repo this
  run is not reviewing is logged as out-of-scope activity (you, working in your own
  tree during a 20-minute panel) instead of killing four paid-for reviews. A change
  in the REVIEWED repo still aborts.

## Layout

```
<tool>/                        REUSABLE — everything here works for any project
├─ orchestrate.py            entrypoint (doctor / chains / arch / dev / inv /
│                            review / implreview / mrreview / resume / show)
├─ orchestrator/             config, verdicts, guard, transcript, adapters, engine,
│                            failures (provider-neutral failure telemetry), mr
├─ config/agents.json        agent↔model↔command binding (authoritative)
├─ config/workflows.json     workflows: chains, step→role bindings, limits, rosters
├─ config/project.example.json  template for the per-project layer
├─ config/project.json       -> the real project layer (gitignored; usually a symlink)
├─ roles/<Role Name>.md      BASE role prompts — identical for every project
├─ prompts/                  architecture-authoring wrapper (does not modify roles)
├─ fixtures/<wf>/<name>.json scripted mock responses (pass, escalate)
└─ runs/<run-id>/            default run history (gitignored), unless paths.runs_dir
                             moves it out: run.json, events.jsonl, transcript.md,
                             iterations/, artifacts/, disagreements.md, STATUS,
                             usage.json (token/cost accounting, schema_version 2)

<project>/                     NOT reusable — lives with the project, never in the tool
├─ project.json              paths, guard, and where the context files are
├─ context.md                domain context appended to EVERY role
├─ roles-context/<role>.md   extra context only that role sees
└─ runs/                     run history, when paths.runs_dir points here
```

### Token and cost accounting

`usage.json` (`schema_version: 2`) and the `agent_attempt` events in
`events.jsonl` are per-**invocation**, not per logical step: a same-agent
transient retry, a shift to another vendor and a verdict repair each spend real
tokens and are each recorded with the agent that incurred them. Every input
class Claude reports — fresh, cache-creation and cache-read — is stored
separately, because a step whose input is 95% cache-read is not the same event
as one that paid for all of it fresh.

Nothing is estimated: the tool keeps **no price tables**. Sums include only the
figures a provider actually reported, and `coverage` states how many attempts
reported each one, so "no reported cost" (an unmetered transport — codex,
gemini, deepseek) stays distinguishable from a measured `0.0`. Runs recorded
before schema 2 are never rewritten; `show` labels them as legacy and says what
their numbers omit.

## Safety model

- **Only Claude/Developer edits code.** Reviewers get `read_only_args`, never
  `edit_args`. A **git guard** snapshots the working tree of *every watched repo*
  (`dev_repos` + `analysis_repos`) before/after every read-only step and
  **aborts the run** if anything changed (the delta is labelled by repo). Note
  this is *detect-and-abort* (post-hoc), not prevention — it stops the loop and
  records a `guard_violation`, but does not roll back a write.
- **No branch switching, by anyone.** Branches are recorded around **every** step,
  the Developer's editable one included, and a run aborts if a watched repo moved.
  Switching branches mutates no file, so the tree snapshot above cannot see it —
  and work that lands on the wrong base looks perfect in the diff.
- **Reviewers see new files, not just their names.** Untracked files are rendered
  as new-file diffs (bounded per file and in total, every omission stated), because
  `git diff <baseline>` structurally omits what git does not track and a reviewer
  cannot sign off on code it was never shown.
- **Our own git calls ignore an inherited `GIT_DIR`/`GIT_INDEX_FILE`.** Otherwise a
  suite run from a pre-commit hook retargets every `git -C <repo>` at the
  commit-in-progress index and the guard aborts on a tree that never changed.
- **Every role gets the global policy** (artifacts in English regardless of the
  brief's language; never read a sibling workspace) appended last, after the role
  file and any wrapper, so it wins.
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
- **An error body is never an answer.** A failed invocation gets empty
  `AgentResponse.text`, so a provider error payload cannot be read as the
  agent's reply or satisfy a gate even when it happens to contain a valid
  verdict token. The raw body is kept in the structured failure record
  (bounded head+tail) and shown to the operator instead.
- **A transport that returns nothing is a failure.** A CLI that exits 0 having
  printed nothing produced no answer, so it is recorded as one rather than
  passed on as an empty reply.
- **Richer failure telemetry changed no recovery path.** A failure is now
  *described* precisely (kind, scope, HTTP status, provider code, reset time),
  but which failures retry the same agent or shift the roster is still decided
  by the pre-existing markers — `FailureInfo.compat_*` is what the engine acts
  on, and it is the unchanged legacy verdict. Acting on the richer
  classification is failure-domain-aware recovery: a separate change.
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
