# Setup and operation

What belongs in this repository, what belongs beside your project, and how to run
the thing. If you only read one section, read [The split](#the-split) — everything
else follows from it.

## The split

| | Lives in the tool repo | Lives in the project layer |
|---|---|---|
| Engine (`orchestrate.py`, `orchestrator/`) | yes | — |
| Base role prompts (`roles/*.md`) | yes, **identical for every project** | — |
| Workflows: chains, step→role bindings, limits, rosters | yes (`config/workflows.json`) | — |
| Agent↔model↔CLI binding | yes (`config/agents.json`) | — |
| Mock fixtures, tests, docs | yes | — |
| Which repositories exist and where | — | `paths` |
| Guard on/off and its exclude prefix | — | `guard` |
| Domain knowledge (components, contracts, tooling, hosts) | — | `context_file`, `role_context` |
| Run history, task briefs, captured evidence | — | `runs_dir` and whatever else you keep there |

The test for anything you are about to commit here: **would this line make sense to
a different project?** If not, it belongs in the project layer.

```
<tool>/                        cloned once, shared by every project
├─ orchestrate.py
├─ orchestrator/
├─ roles/<Role Name>.md        base roles — never edited per project
├─ prompts/, fixtures/, tests/, docs/
└─ config/
   ├─ agents.json
   ├─ workflows.json
   ├─ project.example.json     the template you copy out
   └─ project.json             -> symlink to your project layer (gitignored)

<project>/                     one per project, outside the tool
├─ project.json                paths, guard, where the context files are
├─ context.md                  domain context appended to EVERY role
├─ roles-context/<role>.md     context only that role sees
└─ runs/                       run history, if paths.runs_dir points here
```

## Setting up a project

1. **Clone the tool** somewhere shared, or once per machine:
   `git clone <tool-repo> <tool>`   — anywhere; nothing here is project-specific.

2. **Create the project layer** next to the workspace it describes — not inside the
   tool:

   ```bash
   mkdir -p <workspace>/orchestration-project/roles-context
   cp <tool>/config/project.example.json \
      <workspace>/orchestration-project/project.json
   ```

3. **Fill in `project.json`.** Every path is relative to *this file's* directory:

   ```jsonc
   {
     "paths": {
       "repo_root": "..",                        // the multi-repo workspace root;
                                                 // also the cwd every agent CLI runs in
       "dev_repos":      ["service-a", "sdk"],   // the Developer MAY edit these
       "analysis_repos": ["upstream-x"],         // read-only context, never modified
       "docs_architecture": ["../service-a/doc/architecture"],
                                                 // one path, or several to scope the
                                                 // context to the lines of work that
                                                 // matter; each is searched recursively
       "runs_dir": "runs"                        // optional: keep run history here
                                                 // instead of inside the tool
     },
     "guard": { "enabled": true, "exclude_prefix": "orchestration" },
     "context_file": "context.md",
     "role_context": {
       "Root Cause Investigator": "roles-context/root-cause-investigator.md",
       "Responsible Developer":   "roles-context/responsible-developer.md"
     }
   }
   ```

   Every repo in `dev_repos` + `analysis_repos` must be a real git worktree, or the
   guard fails closed and real runs refuse to start.

4. **Point the tool at it** — either a symlink, which everyone using that checkout gets
   for free:

   ```bash
   ln -s /abs/path/to/<workspace>/orchestration-project/project.json \
         <tool>/config/project.json
   ```

   Make it **absolute**. A relative link is measured from `config/`, so moving the tool
   one directory breaks it — the tool then refuses to start and names the broken link,
   but you still have to go and fix it.

   Or an env var, which survives moving either side and is the better choice when one
   checkout serves several projects:

   ```bash
   export ORCH_PROJECT=<workspace>/orchestration-project/project.json   # file or dir
   ```

5. **Write the domain context.** `context.md` is prepended to no one and appended to
   everyone: what the system is, which repository holds which part, what counts as a
   contract here, and the local ground rules. `roles-context/<role>.md` is for what
   only one role needs — the investigator's log locations and build/deploy commands,
   the developer's definition of "healthy on the host". Both are appended after the
   role text and before the global policy.

   **Never edit a base role to add project knowledge.** That is what these files are
   for; a role edited per project stops being the same role everywhere.

6. **Check it:** `python3 orchestrate.py doctor`. The first lines tell you which
   project layer is in force and whether any context is wired:

   ```
   project layer  : config/project.json -> /…/orchestration-project/project.json
   project context: context_file, role_context x2
   workspace root : /…/<workspace>   (agent cwd; not itself a git repo)
   git guard      : active  enabled=True  available=True
   ```

   If it says `config/project.example.json (TEMPLATE)`, the tool did not find your
   layer: `--mock` and the tests still work, real runs do not make sense. If the link
   exists but points nowhere, the tool says so by name instead of falling back —
   that is the moved-tool case.

## Running

### Before a real run

`orchestrate.py` calls the agent CLIs by bare name and inherits your shell, so launch
from a shell where:

- `~/.local/bin` is on `PATH` (Codex) and nvm is sourced for Node ≥ 20 (Gemini):
  `source ~/.nvm/nvm.sh`
- provider keys are exported — keep them in `600` files outside every repo and source
  them **in the same shell as the run**:
  `source ~/.config/orchestration/deepseek.env; source ~/.config/orchestration/gemini.env`

A missing key is not a crash: `doctor` reports the agent MISSING, and at runtime the
roster shifts that cell left to the next live agent. It is still worth sourcing both —
a silently degraded panel is a worse review, and an arbiter that cannot be reached
ends the run as HUMAN_REQUIRED.

Then always: `python3 orchestrate.py doctor` — it probes CLI presence and keys, lists
the watched repos, and tells you whether real runs are possible at all.

### Mock first

Every workflow runs end to end with scripted fixtures, no credentials, no network:

```bash
python3 orchestrate.py arch       --mock --task "Add a health-check endpoint."
python3 orchestrate.py dev        --mock --task "Fix the off-by-one in foo()."
python3 orchestrate.py inv        --mock --task "Sessions drop after the restart."
python3 orchestrate.py review     --mock --task "Review this design." --target DESIGN.md
python3 orchestrate.py implreview --mock --task "Ready to implement?"  --target PLAN.md
python3 orchestrate.py mrreview   --mock
```

`--scenario <name>` picks a specific fixture (`doctor` lists what each workflow has);
add one by dropping `fixtures/<workflow>/<name>.json` next to the shipped ones.

### The six workflows

| Command | What it does | Ends with |
|---|---|---|
| `arch` | Architect authors, the Developer reviews for implementability, Tech Lead and Arbiter break deadlocks | `PASS` / `AWAITING_HUMAN` / `HUMAN_REQUIRED` |
| `dev` | Developer implements, then Architect → Tech Lead gates the diff; Arbiter only on a real deadlock | `READY_FOR_HUMAN_REVIEW` / `HUMAN_REQUIRED` |
| `inv` | Investigator → Refuter → independent Verifier; produces a falsifiable root-cause dossier, not a fix | `ROOT_CAUSE_ESTABLISHED` / `NO_DEFECT_FOUND` / `HUMAN_REQUIRED` |
| `review` | Three adversarial lenses over ONE artifact + an arbiter that ranks the findings | `REVIEW_COMPLETE` / `HUMAN_REQUIRED` |
| `implreview` | 6-role, 3-stage readiness gate over an implementation plan | `IMPLREVIEW_COMPLETE` / `HUMAN_REQUIRED` |
| `mrreview` | Four independent experts on one merge request + a moderator | `REVIEW_COMPLETE` / `HUMAN_REQUIRED` |

Exit code is `0` for a successful conclusion (`PASS`, `READY_FOR_HUMAN_REVIEW`,
`ROOT_CAUSE_ESTABLISHED`, `NO_DEFECT_FOUND`, `REVIEW_COMPLETE`,
`IMPLREVIEW_COMPLETE`) and `2` for anything that needs a human — `HUMAN_REQUIRED`
and the resumable `AWAITING_HUMAN` pause — so a wrapper script can branch on it.

```bash
# architecture, brief in a file (preferred: briefs get long)
python3 orchestrate.py arch --task-file TASK-arch-feature-x.md

# development against an approved design
python3 orchestrate.py dev --task-file TASK-dev-subtask-1.md \
                           --arch <repo>/doc/architecture/ARCH-feature-x.md

# investigation — the brief must name the repos, the branch/commit per repo,
# and the hosts; it need NOT name log paths
python3 orchestrate.py inv --task-file TASK-inv-symptom.md

# artifact review: one-shot critique, or refine until STRONG
python3 orchestrate.py review --task "Review for gaps" --target DESIGN.md
python3 orchestrate.py review --task "Review for gaps" --target DESIGN.md \
                              --iterate --max-iterations 3

# merge-request panel
python3 orchestrate.py mrreview --mr 174 --repo service-a --fetch
python3 orchestrate.py mrreview --base origin/main --head HEAD --repo sdk \
                                --out /tmp/remarks.md
python3 orchestrate.py mrreview --diff-file /tmp/change.patch --subject "retry policy"
```

`--task` / `--task-file` is required for every workflow except `mrreview`, whose brief
is the merge request itself.

### Long runs

A run outlives nothing: it dies with the shell that started it. For anything long,
detach it and watch the file:

```bash
nohup python3 orchestrate.py dev --task-file TASK.md > /tmp/dev-run.out 2>&1 &
tail -f /tmp/dev-run.out
```

If a run dies anyway, the Developer's edits are already in the working tree — check
`git status` in the dev repos before re-running anything.

### After a run

```bash
python3 orchestrate.py show <run-id>     # status, reason, token spend, where things are
python3 orchestrate.py resume <run-id>   # replay journaled steps, continue live
python3 orchestrate.py chains [wf]       # who holds which cell, and the fallback order
```

Each run directory holds `STATUS` (verdict + reason), `transcript.md` (the whole
conversation), `events.jsonl` (machine-readable: steps, roster shifts, guard events,
usage), `run.json`, `iterations/`, `artifacts/` and `disagreements.md`. `mrreview`
additionally writes `artifacts/mr-review.md`, the remarks document you hand to an
author.

`resume` replays every step that already completed and runs the first un-journaled one
live. It rebuilds the roster from scratch, which is also how you un-stick an agent that
was wrongly marked exhausted. An `arch` run paused as `AWAITING_HUMAN` is resumed with
your ruling: `resume <run-id> --human-answer "…"` (or `@path`).

## What the guard will stop

The guard exists because only the Developer may write code. It is *detect-and-abort*,
not prevention — it stops the loop and records the violation, it does not roll back.

| Symptom | What happened | Where to look |
|---|---|---|
| `refusing to start a real run: the git guard is inactive` | a repo in `paths` is missing or not a git worktree | `doctor` → the `NOT git repos` line |
| `Read-only agent 'X' altered the working tree` | a reviewer wrote something; the delta is labelled by repo | `events.jsonl` → `guard_violation` |
| `Agent 'X' changed the checked-out branch` | any role ran `git checkout`; the work would land on the wrong base | the event's `before -> after` detail |
| the panel keeps going after a tree change | an `mrreview` violation in a repo the run is **not** reviewing is treated as your own unrelated activity | `events.jsonl` → `guard_out_of_scope` |
| a cell silently changed vendor | the agent hit a quota/usage limit and the roster shifted left | `events.jsonl` → `roster_shift`, and read the **tail** of its detail |

A real CLI that writes caches inside a watched repo (a `.codex/` directory, say) trips
the guard the same way a misbehaving agent would. Point the cache elsewhere or ignore
the path — untracked-but-ignored files do not count.

## Adding a role

1. Write `roles/<Role Name>.md` — domain-neutral, with an output format that ends in a
   verdict block.
2. Add its verdict vocabulary to `orchestrator/verdicts.py`.
3. Bind it to a step in `config/workflows.json`, and add the cell to that workflow's
   `roster.cells`.
4. **Add the role name to the `roles` allowlist of every agent that may hold it** in
   `config/agents.json`. Miss this and the cell resolves to nobody the moment the
   roster shifts — mock runs will not catch it, `tests/test_config.py` will.
5. If the role needs project knowledge, put it in that project's `role_context`, not in
   the role file.

Then `./run-tests.sh` — the consistency test checks every workflow's roles against the
allowlists and the verdict table.
