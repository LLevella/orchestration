# Orchestrator context model & run control (Millennium copy)

Two goals, **no quality loss**:
1. **Control** — every run reports its token spend (so we can see where tokens go and prove the effect of any change).
2. **Economy** — each role gets only the context its judgment needs; agents **read source on demand** (a pointer) instead of carrying it re-inlined every iteration.

**Locked (Irina, 2026-06-19):** `MAX_THINKING_TOKENS` is **unchanged** (claude 16000 / reviewers 31999) and `max_iterations` is **unchanged**. Economy therefore comes ONLY from lean context + fewer iterations (clean first-pass scoping) + measurement — never from cutting reasoning or capping loops.

## Per-role context contract

### architecture (`arch`) — already lean
| Role (agent) | Context it gets |
|---|---|
| Author / System Architect (codex) | task + a **truncated** overview of existing arch docs (`read_existing_arch`: ≤8 files × 1500 chars) + reads the full doc on demand; plus its previous draft + the last review each iteration |
| Developer-Verifier (claude_reviewer, PRIMARY gate) · Tech-Lead · Arbiter | the **draft** (work-product) + dispute history — NOT re-inlined design docs |

### development (`dev`)
| Role (agent) | Context it gets |
|---|---|
| Developer (claude — the only editor) | task + repo scope + **pointer** to the approved architecture (materialized once to `artifacts/approved-architecture.md`; reads the sections it touches — not inlined every iteration) + a **build=closure** cadence instruction |
| Code Reviewer (claude_reviewer) | task + diff + developer summary — lean (correctness/regressions/tests; no arch needed) |
| Architect (codex) · Tech-Lead | task + diff + summary + **pointer** to the approved architecture (read on demand) — so architecture-fit review actually has the design, cheaply |
| Arbiter | diff + developer summary + disagreement history |

### investigation (`inv`) — lean
| Role (agent) | Context it gets |
|---|---|
| Investigator (claude) | problem + dynamic repo scope; reads repos/logs on demand |
| Refuter · Verifier | problem + the **hypothesis** + the debug diff (work-product) |
| Arbiter | hypothesis + disagreement history |

## Run control — token accounting
- Each step captures `usage` (normalized `{input, output, total}`) + `cost_usd`:
  - **claude** — `--output-format json` (auto-injected by the adapter); reads `.usage` + `.total_cost_usd`, parses `.result` back out as the text so verdict parsing is unaffected.
  - **deepseek** — the API `usage` block (previously parsed then discarded).
  - **codex** — the `tokens used <N>` stdout footer (total only).
  - **gemini / mock** — report nothing.
  - **Fail-safe:** any parse miss degrades to "no figure for that step", never breaks the run.
- Written per step to `events.jsonl`; aggregated by `finish()` to `usage.json` + a **Token usage** table in `transcript.md` + a one-line total in `STATUS`. `python3 orchestrate.py show <run-id>` prints totals + a by-agent breakdown.
- `cost_usd` is **claude-only** (deepseek/codex unpriced). `metered` = number of steps that carried a token figure.

## Economy levers (all quality-neutral)
- **Pointer task briefs** — reference files/sections; don't inline code maps or long docs.
- **Clean first-pass scoping** — the dominant cost lever: the draft/diff/disagreement history re-feeds the author/reviewers every cycle, and thinking + max_iterations are fixed, so iteration count *is* the multiplier. A brief that converges in 2-3 vs 8 iterations is ~3-4× cheaper.
- **Atomic dev sub-tasks** → small diffs → small reviewer prompts.
- **Build/test = closure check**, not per-iteration (instructed in the dev prompt).
- **One liveness probe** per recovered agent; don't re-probe.

## Deliberately NOT done
- Lowering `MAX_THINKING_TOKENS` (off-limits per Irina).
- Lowering `max_iterations` (kept per Irina).

This file is local to the Millennium orchestration copy and should not import
domain context from other workspaces.
