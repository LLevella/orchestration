# Local AI Orchestration - Millennium

This directory contains the local multi-agent orchestrator used as a development
tool for Millennium AI-agent work. It is not the runtime foundation of the
travel product.

The orchestrator runs architecture, development, and investigation loops:

- `arch`: architecture proposal -> implementability review -> tech-lead gate.
- `dev`: implementation -> code review -> architecture review -> tech-lead gate.
- `inv`: root-cause hypothesis -> refutation -> independent verification.

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

Show a run:

```bash
python3 orchestrate.py show <run-id>
```
