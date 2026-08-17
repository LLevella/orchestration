# Millennium AI Travel Agent - Development Context

This context pack prepares the local multi-agent orchestrator for continuing
development work on the Millennium AI travel-agent effort. The orchestrator is
development tooling only: it coordinates architecture, implementation review,
and investigation loops. It is not the runtime foundation of the product agent.

Primary source documents:

- `projects/arch-docs/api-spec-backend-v1.10.md` - backend/tool contract for the agent-facing
  facade (v1.10; supersedes v1.9, `projects/arch-docs/api-contracts-draft.md` and planning docs
  on any wire-shape conflict).
- `projects/arch-docs/phase-0/18-meeting-summary-2026-08-05.md` - decisions,
  open questions, and actions from the 05.08.2026 call.
- `projects/arch-docs/Функциональные требования..docx` - product requirements for two agents.
- `projects/arch-docs/созвон.txt` - interview/kickoff transcript with architecture decisions.
- `projects/arch-docs/dev-plan-agent1.md`, `projects/arch-docs/backend-discussion-prep.md`,
  `projects/arch-docs/kickoff-prep.md` -
  working planning artifacts already present in this workspace.

## Product Shape

Millennium wants two agents:

1. AI Travel Planning Assistant: understand a free-form travel request, clarify
   missing data, return all railway trains for the route/date, load wagons,
   availability and traveller policy, explain the results, and hand off a
   selected wagon offer to the existing booking flow.
2. AI Disruption & Rebooking Agent: react to flight disruptions, evaluate impact
   on related services, notify the passenger, find alternatives, and escalate to
   a travel manager when needed. This is not the first implementation target.

The first development target is Agent 1. The backend contract intentionally
narrows Phase 1 to a railway one-way walking skeleton, because the internal rail
API is the nearest real integration.

## Phase 1 Contract

Phase 1 is the skeleton:

- `GET /agent/v1/profile`
- railway `locations` lookup
- `POST /agent/v1/search/trains`
- a context-bound policy snapshot (producer OPEN: separate
  `policy/resolve` or bundled `search/cars`)
- `POST /agent/v1/search/cars` with wagon availability and structured policy
- optional `POST /agent/v1/policy/adjudicate`
- `POST /booking/v1/handoff`
- `POST /secure/v1/resolve-pii` as a protected backend-only dependency

The canonical flow is:

`profile -> locations/search_trains -> policy snapshot/binding ->
search_cars+policy -> handoff`

Phase 1 is one-way railway, one passenger, and one component. Non-null
`for=<passenger_token>` is a target Phase 1 shape, but live delegation stays
disabled until scope, selector and resource-specific authorization are ratified
and implemented. Round trip, approvals, reverify optimization, history,
air/hotel/transfer, visa/check, and FX are later phases. Platform order APIs are
never agent-facing.

## Non-Negotiable Invariants

- Backend APIs are the Source of Truth for facts: price, availability, policy,
  passenger identity, tenant ownership, booking state, visa facts, and history.
- The model may parse language and write user-facing prose, but must not invent
  prices, limits, availability, policy outcomes, visas, or booking status.
- User/tenant identity never comes from the model. For the first contour it is
  resolved server-side from the verified Millennium session-cookie selected on
  05.08. The exact cookie name, trusted-edge contract, expiry/revocation, and
  end-to-end implementation remain open; a separate JWT is deferred hardening.
- Every read checks ownership. Tenant isolation is mandatory.
- Structured platform PII and PCI data do not enter prompts or general logs.
  Free user text may transit the approved model under D-075, but name
  detection/removal must precede every database transaction. Until T-G23 and
  T-G23-бис pass, dialogue text is synthetic-only. Real PII is joined by
  protected backend code at handoff via `resolve-pii`.
- Money is exact decimal with scale 2 plus ISO-4217 currency. Internal
  arithmetic never uses binary float. Do not change the wire form before the
  14.08 freeze.
- Dates use the backend spec conventions. Phase 1 one-way requests send
  `dates.end: null`; non-null return date is unsupported in Phase 1.
- `empty`, `timeout`, `partial`, and `error` are different states. The agent must
  not tell the user "no options" when a source timed out.
- Policy failures and timeouts fail closed without hiding trains: no affected
  wagon becomes compliant and handoff is blocked, but the train remains visible.
- Policy never filters `search/trains`; `view_mode` is reversible presentation
  state, not a search parameter.
- Non-idempotent operations use `Idempotency-Key`; duplicate drafts and in-flight
  requests follow the backend contract exactly.
- Wagon policy/adjudicate/handoff must use
  `(resolution_id, offer_id, offer_version)`.
  Any stale resolution, stale offer, context mismatch, or price change forces the
  recovery path defined by the backend spec.
- A user-visible recommendation card is grounded by template binding from API
  fields. The model can explain, but numeric/status facts come from structured
  data.

## Transcript Decisions

Important decisions and assumptions from `projects/arch-docs/созвон.txt`:

- "Code decides, model proposes." Control flow and state transitions belong to
  deterministic code, not to the LLM.
- External actions go through typed, validated, audited tool calls. No privileged
  side path from the model to orders, profiles, or provider APIs.
- Tool permissions are state-dependent. The current state controls which tools
  can be called.
- First agents should stop where the business chooses. They should not silently
  expand toward payments or irreversible actions.
- Backend booking remains the execution owner. The agent gathers and explains a
  structured selection; the existing backend/booking flow performs authoritative
  checks and downstream booking actions.
- GigaChat was purchased for the first LLM pass; capability, structured output,
  latency, quota, and cost must be benchmarked before relying on it.
- Government-corporate customers often use email and do not enter the platform.
  Email/copilot flow is strategically important but may need a separate timeline
  because it adds async state, delayed clarification, operator review, and long
  unstructured messages.
- Incidents are expected to be handled iteratively: stop the faulty behavior,
  create a bug/task, fix, test, and return. The implementation needs kill switch,
  feature flags/canary rollback, traces, and guardrail regression tests.

## Development Guidance For Orchestrator Runs

When launching architecture or development tasks through this orchestrator:

- Include the specific task and cite the relevant source file(s).
- Keep task scope phase-bounded. If the task is Phase 1, do not implement later
  phase systems as hidden scope.
- Prefer a narrow, testable walking-skeleton slice over broad platform design.
- Treat the agent runtime and this multi-agent development orchestrator as
  separate systems.
- Require tests for behavior, guardrails, and failure modes: no invented facts,
  no agent access to booking/purchase endpoints, draft-only handoff, no raw PII
  to model/logs, ownership isolation,
  stale offer/re-resolution handling, duplicate handoff idempotency, and distinct
  empty/timeout/error responses.

## Open Decisions To Keep Visible

- Conversational user agent vs internal operator copilot for the first pilot.
- Whether real prices and exact options are shown to end users before human
  participation.
- Who owns platform core, facade, observability, Redis/session state, frontend,
  manual fallback, and KPI dashboard.
- Policy snapshot producer, six-state predicates/precedence, cars batch/partial
  semantics and the inventory-only path after resolve failure.
- Draft-only handoff delivery date, TTLs, signed-return behavior and conformance
  owner. The pilot remains synthetic until delivery and conformance.
- Exact session-cookie name, trusted-edge/bootstrap contract, expiry,
  revocation, and end-to-end implementation.
- Legal/security sign-off for the no-PII model path, logging redaction, and
  152-FZ data-residency topology.
- Segment-specific rules for self-pay over limit, especially government clients.
