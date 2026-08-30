# Orchestrator context model & run control

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
- Each **invocation** captures `usage` (normalized `{input, input_fresh, input_cache_creation, input_cache_read, output, total}`) + `cost_usd`. A class the transport does not report is `null`, never a silent `0`; a reported `0` stays `0`.
  - **claude** — `--output-format json` (auto-injected by the adapter); prefers `.modelUsage` (all models, same scope as `.total_cost_usd`), else the top-level `.usage` fresh/cache-creation/cache-read/output fields, plus `.total_cost_usd`. `.result` is parsed back out as the text so verdict parsing is unaffected — **unless** the body is an error, in which case the text is empty and the body is kept in the failure record instead.
  - **deepseek / gemini (API)** — the OpenAI-style `usage` block: `prompt_tokens` → `input`, `completion_tokens` → `output`, `total_tokens` → `total`; no cache-class breakdown.
  - **codex** — the `tokens used <N>` stdout footer (bare total only).
  - **mock** — reports nothing (`usage=null`).
  - **Fail-safe:** any parse miss degrades to "no figure for that invocation", never breaks the run.
- Recording only `.usage.input_tokens` under-reported one real step 55,599x (52 stored for a true 2,891,127 input) because the two cache classes were dropped; that is why every class is now stored separately.
- Written per invocation as an `agent_attempt` event in `events.jsonl` — a same-agent retry, a shift to another vendor and a verdict repair each spend tokens and are each recorded against the agent that incurred them. `finish()` aggregates those attempts into `usage.json` (`schema_version: 2`) + a **Token usage** table in `transcript.md` + a one-line total in `STATUS`. `python3 orchestrate.py show <run-id>` prints totals, input classes, coverage and a by-agent breakdown; it also still renders pre-schema-2 runs, labelled as legacy.
- **No price tables exist in this tool.** `cost_usd` is provider-reported (claude only today); an agent whose transport prices nothing is **unmetered**, not `$0.00`, and `coverage` says how many attempts reported each figure. `steps`/`metered` count distinct logical steps; `attempts` counts real invocations.

## Economy levers (all quality-neutral)
- **Pointer task briefs** — reference files/sections; don't inline code maps or long docs.
- **Clean first-pass scoping** — the dominant cost lever: the draft/diff/disagreement history re-feeds the author/reviewers every cycle, and thinking + max_iterations are fixed, so iteration count *is* the multiplier. A brief that converges in 2-3 vs 8 iterations is ~3-4× cheaper.
- **Atomic dev sub-tasks** → small diffs → small reviewer prompts.
- **Build/test = closure check**, not per-iteration (instructed in the dev prompt).
- **One liveness probe** per recovered agent; don't re-probe.

## Deliberately NOT done
- Lowering `MAX_THINKING_TOKENS` (off-limits per Irina).
- Lowering `max_iterations` (kept per Irina).

_Inspired by the Claude-only sibling copy at `andrey/orchestration` (`docs/context-model.md`, `run-chain.sh`); usage accounting is new here (that copy has none)._
