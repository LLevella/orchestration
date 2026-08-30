# `tests/data/`

Real provider payloads captured from production runs, scrubbed of session
identifiers and copied INTO this repository so the tests have no dependency on
the run corpus (`orchestration-project/runs/` is read-only historical evidence,
not a test fixture directory).

Distinct from `fixtures/`: those are scripted *mock-workflow scenarios* and are
part of the `--mock` contract. These are inert bytes fed to a parser.

- `claude_session_limit.json` — the complete `claude -p --output-format json`
  result object from `20260828T143500Z-architecture-b343`, step `arch.dev_review`
  iteration 4. `is_error: true`, `api_error_status: 429`, and the CLI wrote
  NOTHING to stderr, so before P2 the operator saw
  `agent 'claude_reviewer' (Developer Verifier) returned ERROR: no detail`
  while this body — 2,891,127 top-level input tokens and $3.39 of
  provider-reported cost — was discarded unparsed. Only `session_id` and `uuid`
  are replaced; every token count, cost and message is verbatim.
