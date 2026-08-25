# Local AI Orchestration - Millennium

This directory contains the local multi-agent orchestrator used as a development
tool for Millennium AI-agent work. It is not the runtime foundation of the
travel product.

The orchestrator runs architecture, development, and investigation loops:

- `arch`: architecture proposal -> implementability review -> tech-lead gate.
- `dev`: implementation -> code review -> architecture review -> tech-lead gate.
- `inv`: root-cause hypothesis -> refutation -> independent verification.
- `review`: four independent expert opinions on one merge request -> one
  consolidated list of code remarks.

## Current Context

Project context for future runs is in:

- `docs/architecture/millenium-agent-dev-context.md`
- `../projects/arch-docs/api-spec-backend-v1.10.md`
- `../projects/arch-docs/phase-0/18-meeting-summary-2026-08-05.md`
- `../projects/arch-docs/phase-0/07-decision-log.md`
- `../projects/arch-docs/phase-0/requirements-register.md`
- `tmp/next-task-agent1-phase0.md`

(Project docs moved into `../projects/arch-docs/` on 2026-07-20; task files should name
paths explicitly, workspace-root-relative, e.g. `projects/arch-docs/api-spec-backend-v1.10.md`.
Older planning files are historical context and do not override these sources.)

The current product target is AI Travel Planning Assistant, Phase 1 railway
one-way walking skeleton:

`profile -> locations/search_trains -> policy snapshot/binding -> search_cars+policy -> handoff`

plus backend-only protected `resolve-pii`.

Important invariant: backend APIs are Source of Truth. The model may parse
language and write prose, but facts and decisions come from typed backend data.
No raw PII or PCI data should enter model prompts or model-visible logs.

## CLI Setup

This copy is configured with absolute CLI paths discovered on this machine:

- Claude: `/home/irina/.nvm/versions/node/v22.23.1/bin/claude`
- Gemini: `/home/irina/.nvm/versions/node/v22.23.1/bin/gemini`
- Codex: resolved from PATH
- DeepSeek: API key from `DEEPSEEK_API_KEY` (provider of this local development
  orchestrator only; the product model selected for the first stage is GigaChat)

Check readiness:

```bash
cd /home/irina/work/millenium/orchestration
python3 orchestrate.py doctor
```

Mock runs do not need provider credentials:

```bash
python3 orchestrate.py arch --mock --task-file tmp/next-task-agent1-phase0.md
python3 orchestrate.py dev --mock --task "unit task"
python3 orchestrate.py inv --mock --task "investigate a guardrail regression"
```

## Git Guard

The configured development target is the Millennium workspace root (`..`). This
workspace is currently not a git repository, so real runs will fail closed unless
one of these is done intentionally:

- initialize/configure the actual source repository and keep `dev_repos`
  accurate; or
- run with `--allow-unguarded` for document-only/dev-tool work where that risk is
  acceptable.

Do not initialize git or change watched repos silently. The human owns that
workspace decision.

## Useful Real-Run Commands

Architecture run:

```bash
python3 orchestrate.py arch --task-file tmp/next-task-agent1-phase0.md --allow-unguarded
```

Development run with an approved architecture artifact:

```bash
python3 orchestrate.py dev --task-file <task.md> --arch <approved-architecture.md> --allow-unguarded
```

Merge-request review (the 4-expert panel):

```bash
# by GitLab MR number (branches read via `glab`, diffed locally)
python3 orchestrate.py review --mr 128 --fetch

# by refs, when glab is not authenticated (SSH alone is enough)
python3 orchestrate.py review --base origin/main --head origin/feature/x --fetch

# a patch someone handed you, no repo needed
python3 orchestrate.py review --diff-file /tmp/change.patch
```

These need no `--allow-unguarded`: the guard watches `projects/weyro-ai`, which
is a real git repo, and every reviewer is read-only.

Reviewing a repo the workflows do not watch (the monolith, a colleague's clone) is
normal — the panel is read-only — so `--repo` takes a watched repo's label OR any
git repository path, absolute or relative to the workspace root; an unwatched
target is added to the git guard for that run. Keep the path INSIDE the workspace
root, though: the global policy forbids agents to read anything beside it, so a
checkout in /tmp is a repository the reviewers will refuse to open.

For a GitLab MR without an authenticated `glab`, both of its refs are still
reachable over SSH — `refs/merge-requests/<iid>/head` is the source tip and
`refs/merge-requests/<iid>/merge^1` is the target commit:

```bash
git -C <repo> fetch origin \
    refs/merge-requests/929/head:refs/mr/929/head \
    refs/merge-requests/929/merge:refs/mr/929/merge
git -C <repo> worktree add --detach ../.mr-worktrees/mr-929 refs/mr/929/head
python3 orchestrate.py review --repo .mr-worktrees/mr-929 \
    --base $(git -C <repo> rev-parse refs/mr/929/merge^1) --head refs/mr/929/head
```

A worktree at the MR head is worth the disk: reviewers then read surrounding code
at exactly the reviewed state instead of at whatever the main checkout sits on.

Useful extras: `--repo <label-or-path>` when more than one repo is watched, `--subject`
to name the change in the document, `--task/--task-file` to tell reviewers what
to pay attention to, and `--out PATH` to drop a copy of the document next to
wherever you need it.

What it does: four experts (different model families, one emphasis each —
correctness, contract/architecture, security & privacy, tests & maintainability)
review the SAME diff independently, seeing neither each other's opinions nor a
gate; a fifth step merges their remarks into one deduplicated, severity-ranked
list, and the orchestrator writes it to
`runs/<run-id>/artifacts/mr-review.md`, with each reviewer's own opinion kept in
`artifacts/opinion-N-<agent>.md`.

The document names reviewers only as "Reviewer 1..4" and carries no run id — who
was who stays in the run directory. Terminal status `REVIEW_COMPLETE` means the
document exists, NOT that the MR is good: four REQUEST_CHANGES verdicts still
finish `REVIEW_COMPLETE`. A dead vendor costs one opinion, not the run; if
consolidation fails, the document is still written with every reviewer's remarks
unmerged and the run ends `HUMAN_REQUIRED`.

Show a run:

```bash
python3 orchestrate.py show <run-id>
```
