"""Availability-driven role assignment with shift-left fallback.

Each workflow declares an ordered `chain` of agent names and an ordered list of
role `cells`. With every agent live, cell[i] = chain[i]. When an agent is not
live — its CLI is missing, it is `disabled` in config, or it returns an
EXHAUSTED-class error at runtime (usage limit / quota / repeated 503) — it drops
out and the chain shifts LEFT: the surviving agents re-fill the cells in priority
order, but ONLY an agent whose `agents.json` `roles` allowlist covers the cell's
declared role is eligible for it (checked by `_capable`) — a role-incapable agent
is skipped for that cell and tried against the next one instead. Without this, a
naive index shift after an early dropout reassigns EVERY later cell one slot
early, handing e.g. the editing Developer cell to a read-only reviewer that has
neither `can_edit` nor the role in its allowlist (observed 2026-08-04: claude
exhausted -> claude_reviewer, then codex, both misassigned to `developer` and
both structurally unable to write, burning 3 wasted iterations before
HUMAN_REQUIRED). The judge (arbiter) is filled positionally like any other cell
and is NOT reserved from the strong end (it fires only on deadlock —
Irina 2026-06-15).

`pinned` cells are held to a fixed agent and are SKIPPED by the positional
shuffle. The architecture Developer-Verifier is pinned to a Claude developer so
the developer review is always done by a real Claude developer, never displaced
to another vendor by the shift. A pin is either a plain agent name (legacy: the
pin, then the next live role-capable Claude in the chain, never elsewhere) or a
structured block naming its own candidates:

    {"primary": ..., "agent_fallbacks": [...],
     "account_provider_fallbacks": [...], "lost_assurance": "..."}

`agent_fallbacks` cover an agent-specific loss. `account_provider_fallbacks` may
cross vendor families, and ONLY on classified account- or provider-scoped
evidence (a shared spending cap, a 503 of the whole provider): an agent that is
merely missing locally, or a list of individual candidates that simply ran out,
never unlocks them — that would silently weaken the pin. A cross-family selection
is recorded as a degradation carrying `lost_assurance`, because the fallback
reviews with less than the pinned agent could. Being conditional, they are counted
apart from the selectable candidates everywhere they are reported
(`candidate_status`), so a cell never reads as "one live candidate" while it is in
fact empty and not allowed to use that candidate yet.

Resolution is recomputed on every runtime EXHAUSTED, so a mid-run dropout shifts
only the not-yet-run cells and the run continues instead of aborting. Already-run
cells never change.

That property used to rest on `exhausted` being write-once. It no longer is:
a time-boxed block (a 503, a cap with a reset) expires and the agent is
re-admitted, so eligibility both shrinks AND grows across a run. Monotonicity is
therefore stated and enforced explicitly instead of being inherited (A6):

  * WITHIN one logical engine call, eligibility only ever shrinks. Blocks expire
    and local availability is re-sampled at exactly one named point,
    `begin_call()`; while a call is still searching for a fallback nothing is
    re-admitted, so the search cannot loop back onto the selector that just
    failed and always terminates.
  * ACROSS logical calls, a re-admitted agent becomes eligible again — and gets a
    fresh once-per-agent transient-retry allowance, because that allowance is
    itself per logical call.
  * ALREADY-EXECUTED cells stay stable: `note_executed()` binds a cell to the
    agent that ran it, and `assign()` honours that binding for as long as the
    agent stays eligible. A re-admitted stronger agent therefore fills only cells
    that have not run yet; it never reclaims a cell mid-run and never makes the
    role change hands between iterations.

When a cell has no eligible agent left, `assign()` yields None for it as before —
but the engine now PAUSES the run (AWAITING_HUMAN, resumable) instead of
discarding every completed iteration.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass


def is_claude(agent: str) -> bool:
    """A Claude-family agent (opus/sonnet/haiku, any effort variant)."""
    return agent.startswith("claude")


# Substrings that mark an agent as UNAVAILABLE — drop it and shift left rather than
# hard-abort the run. "Unavailable" = the agent cannot produce output for an
# agent-specific, infrastructure reason (exhaustion, billing, auth, or transient
# server error) that a DIFFERENT-vendor fallback would not share. NOT matched:
# task/request errors (a malformed prompt fails for every agent) — those still
# abort. Matched case-insensitively against the agent's error text.
UNAVAILABLE_MARKERS = (
    # quota / rate / capacity exhaustion
    "usage limit", "rate limit", "rate-limit", "ratelimit", "quota",
    "too many requests", "429", "503", "overloaded", "over capacity",
    "high demand", "resource_exhausted", "insufficient_quota",
    "you've reached", "you have reached", "try again later", "service unavailable",
    # Claude CLI account spending cap (`claude -p` returns is_error with
    # result "Spending cap reached resets Npm"). Account-specific + time-boxed,
    # so a DIFFERENT-vendor fallback (codex/gemini/deepseek) recovers rather than
    # waiting hours for reset. Without this, a cap hit hard-aborts the run
    # (observed 2026-08-12: dev_review aborted -> HUMAN_REQUIRED mid arch-pass).
    "spending cap",
    # billing/credit exhaustion (e.g. DeepSeek HTTP 402 -> urllib "HTTP Error 402:
    # Payment Required"). Matched precisely, NOT as bare "402", because the NCC FL
    # control message is literally msg=402 and would false-positive in agent output.
    "payment required", "insufficient balance",
    # auth / credentials not configured for THIS agent (e.g. gemini run without
    # GEMINI_API_KEY sourced -> "Please set an Auth method ... GEMINI_API_KEY").
    # A different-vendor fallback has its own creds, so shifting recovers.
    "set an auth method", "gemini_api_key", "google_genai_use", "401",
    "unauthorized", "authentication failed", "invalid api key", "api key not valid",
    # transient CLI network failure (e.g. gemini-cli "TypeError: fetch failed sending
    # request") — agent-specific connectivity blip; shift-left to a fallback vendor
    # rather than hard-aborting the whole run (a run died this way 2026-06-17).
    "fetch failed", "econnreset", "enotfound", "socket hang up",
)


def is_unavailable(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in UNAVAILABLE_MARKERS)


# The subset of unavailability that is a MOMENTARY server/network blip, where an
# immediate single retry of the SAME agent is likely to succeed. Excludes quota /
# rate-limit / auth / billing / spending-cap — those persist, so a retry just wastes a
# call and shifting to a different vendor is the only recovery. Used to retry a
# transient failure ONCE before evicting a preferred agent for the whole run (observed
# 2026-08-12: gemini tech_lead hit a one-off HTTP 503 and was dropped for the run).
TRANSIENT_MARKERS = (
    "503", "service unavailable", "overloaded", "over capacity", "high demand",
    "try again later",
    "fetch failed", "econnreset", "enotfound", "socket hang up",
)


def is_transient(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in TRANSIENT_MARKERS)




# ---- failure domains and the scoped block registry ----------------------
#
# A failure has a DOMAIN: the set of agents it takes down. Before P1 every
# recognised failure evicted exactly one agent name, which is why the 11
# account-level Claude limits of the corpus were walked one alias at a time —
# claude_reviewer, then claude, then claude_sonnet, each hitting the SAME
# spending cap, each costing a call, and the run dying anyway.
SCOPE_AGENT = "agent"        # this agent alone (a wedged CLI, a missing binary)
SCOPE_ACCOUNT = "account"    # every agent sharing credentials / cap / session limit
SCOPE_PROVIDER = "provider"  # the vendor's whole service (a 503/529)

# Default re-admission delay for a blip-class block that carries no provider signal.
TRANSIENT_BLOCK_S = 60

# failure kind -> (block scope, default seconds until re-admission, honour retry_at).
#
# The kind strings mirror the vocabulary of `failures.py` (which imports THIS module,
# so it cannot be imported back here); `tests/test_roster_recovery.py` asserts every
# kind failures.py defines has a row, so a new kind cannot silently land in the
# "no block" default.
#
# `None` seconds means "for this process": the block never expires on its own and the
# agent returns only via an explicit `resume`, which starts a fresh registry. That is
# deliberate for quota/auth/billing — an account cap resets hours later, and the
# observed Claude reset prose ("resets 7:50pm") carries no date, so `retry_at` is
# null for it and no deadline is invented (parse_retry_at refuses to guess).
#
# `retry_at=False` rows ignore even a supplied deadline: authentication and billing
# do not fix themselves while the process runs, whatever a header says.
BLOCK_POLICY = {
    # a wedged CLI / a socket blip: this agent, briefly
    "timeout":         (SCOPE_AGENT, TRANSIENT_BLOCK_S, True),
    "transport":       (SCOPE_AGENT, TRANSIENT_BLOCK_S, True),
    # a missing binary or an unset key will not appear in 60 seconds
    "configuration":   (SCOPE_AGENT, None, False),
    # shared spending cap / session limit / rate limit: every alias on the account
    "quota":           (SCOPE_ACCOUNT, None, True),
    "rate_limit":      (SCOPE_ACCOUNT, None, True),
    "authentication":  (SCOPE_ACCOUNT, None, False),
    "billing":         (SCOPE_ACCOUNT, None, False),
    # the vendor is down, not the agent: 503/529, Retry-After when it sends one
    "capacity":        (SCOPE_PROVIDER, TRANSIENT_BLOCK_S, True),
    # invalid_request / unknown install NO block: a malformed prompt fails for every
    # agent, so walking the chain would only multiply the damage. They stay fatal.
}

# preflight statuses
READY = "READY"
DEGRADED = "DEGRADED"
UNASSIGNED = "UNASSIGNED"


def _epoch(stamp):
    """Epoch seconds for a `%Y-%m-%dT%H:%M:%SZ` stamp, or None.

    A stamp we cannot read yields None = "no deadline", so the block lasts the
    process. Erring the other way would re-admit an agent early on garbage input."""
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


@dataclass
class Block:
    """One active unavailability, scoped to the domain the failure really took out."""
    scope: str
    selector: str
    retry_at: str | None = None   # UTC "%Y-%m-%dT%H:%M:%SZ"; None = this process
    reason: str = ""
    kind: str = ""                # the failures.py kind that installed it
    source: str = ""              # its classification_source (evidence quality)
    agent: str = ""               # the agent whose invocation failed

    def to_dict(self) -> dict:
        return {"scope": self.scope, "selector": self.selector, "retry_at": self.retry_at,
                "reason": self.reason, "kind": self.kind, "source": self.source,
                "agent": self.agent}


@dataclass
class Pin:
    """A pinned cell's candidate policy (see the module docstring)."""
    cell: str
    primary: str
    agent_fallbacks: tuple = ()
    cross_fallbacks: tuple = ()
    lost_assurance: str = ""
    legacy: bool = False


def availability_snapshot(adapters) -> dict:
    """Call every adapter's `availability()` EXACTLY ONCE and freeze the answer.

    Preflight simulates agent, account and provider losses over the same roster
    several times per cell; re-probing inside those simulations would multiply
    `shutil.which()` / env lookups and, worse, let the answer change between two
    halves of one report. One snapshot, one truth."""
    return {name: tuple(ad.availability()) for name, ad in adapters.items()}


class Roster:
    def __init__(self, wf_block, adapters, *, snapshot=None, clock=None):
        r = wf_block.get("roster") or {}
        self.chain = list(r.get("chain") or [])
        self.cells = list(r.get("cells") or [])
        self.pinned = dict(r.get("pinned") or {})
        # isinstance: a cells entry that does not name a step block (a typo, a scalar
        # like max_iterations) must read as "no declared role", not crash the assignment.
        blocks = {cell: (wf_block.get(cell) if isinstance(wf_block.get(cell), dict) else {})
                  for cell in self.cells}
        self.roles = {cell: blk.get("role") for cell, blk in blocks.items()}
        # A cell that must WRITE (the Developer, the Investigator). Eligibility for it
        # needs `can_edit` on top of the role allowlist, so no shift and no cross-family
        # fallback can ever put a read-only agent in a writing seat.
        self.requires_edit = {cell: bool(blk.get("requires_edit"))
                              for cell, blk in blocks.items()}
        self.adapters = adapters
        self.clock = clock or time.time
        # The instance's OWN availability snapshot (A1: `assign()` stays zero-argument).
        # Taken at construction and refreshed only at the named point `begin_call()`.
        self.snapshot = dict(snapshot) if snapshot is not None else availability_snapshot(adapters)
        self.blocks: list[Block] = []
        # cell -> the agent that already executed a step in it (see the docstring).
        self.sticky: dict[str, str] = {}
        # cell -> degradation record, refilled by every assign(); the engine journals
        # the ones it has not seen yet.
        self.degradations: dict[str, dict] = {}
        self.pins = {cell: self._parse_pin(cell, spec) for cell, spec in self.pinned.items()}
        self.enabled = bool(self.chain and self.cells)

    # ---- pins ---------------------------------------------------------
    def _parse_pin(self, cell, spec) -> Pin:
        if isinstance(spec, dict):
            return Pin(cell=cell, primary=spec.get("primary"),
                       agent_fallbacks=tuple(spec.get("agent_fallbacks") or ()),
                       cross_fallbacks=tuple(spec.get("account_provider_fallbacks") or ()),
                       lost_assurance=spec.get("lost_assurance") or "")
        return Pin(cell=cell, primary=spec, legacy=True)

    def _pin_individuals(self, pin) -> list:
        """The pin's same-family candidates, in order. A legacy string pin keeps its
        historical behaviour exactly: the pin, then the live Claudes of the chain."""
        if pin.legacy:
            return [pin.primary] + [a for a in self.chain if is_claude(a) and a != pin.primary]
        return [pin.primary] + [a for a in pin.agent_fallbacks]

    # ---- failure domains ----------------------------------------------
    def _cfg(self, agent) -> dict:
        a = self.adapters.get(agent)
        return (getattr(a, "cfg", None) or {}) if a is not None else {}

    def selectors(self, agent) -> dict:
        """The three domains this agent belongs to.

        `availability_group` / `provider` are non-secret scheduling labels from
        agents.json. A synthetic test adapter that declares neither defaults both to
        its own name, so unrelated unit tests never share a failure domain by accident."""
        cfg = self._cfg(agent)
        return {SCOPE_AGENT: agent,
                SCOPE_ACCOUNT: cfg.get("availability_group") or agent,
                SCOPE_PROVIDER: cfg.get("provider") or agent}

    # ---- block registry -----------------------------------------------
    def block_for_failure(self, agent: str, failure) -> Block | None:
        """Install the block this failure justifies, and return it (None = no block).

        The DOMAIN comes from the failure's own kind and scope, not from the agent
        that happened to report it: a spending cap reported by `claude_reviewer`
        blocks the whole `anthropic-default-account` group, so the run stops trying
        its five aliases one at a time."""
        kind = getattr(failure, "kind", "") or ""
        policy = BLOCK_POLICY.get(kind)
        if policy is None or not getattr(failure, "roster_unavailable", False):
            return None
        scope, default_s, honour_retry_at = policy
        # The failure's own scope may be narrower than the kind's default (a
        # per-agent 429 classified `agent`); never widen it beyond the kind's row.
        fscope = getattr(failure, "scope", "") or ""
        if fscope == SCOPE_AGENT and scope != SCOPE_AGENT:
            scope = SCOPE_AGENT
        retry_at = getattr(failure, "retry_at", None) if honour_retry_at else None
        if retry_at is None and default_s is not None:
            retry_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(self.clock() + default_s))
        b = Block(scope=scope, selector=self.selectors(agent)[scope], retry_at=retry_at,
                  reason=(getattr(failure, "reason", "") or "")[:400], kind=kind,
                  source=getattr(failure, "classification_source", "") or "", agent=agent)
        self.blocks.append(b)
        return b

    def mark_exhausted(self, agent: str) -> None:
        """Block one agent for the rest of the process. The pre-P1 entry point, kept
        for callers that have no structured failure (and for the tests that pin the
        allowlist behaviour)."""
        self.blocks.append(Block(scope=SCOPE_AGENT, selector=agent, reason="exhausted",
                                 kind="unknown", source="legacy_text", agent=agent))

    def blocking(self, agent: str) -> Block | None:
        """The active block that makes `agent` ineligible, or None."""
        sel = self.selectors(agent)
        for b in self.blocks:
            if sel.get(b.scope) == b.selector:
                return b
        return None

    @property
    def exhausted(self) -> set:
        """Agent names currently made ineligible by an active block.

        Derived, not stored: `roster_assignment.exhausted` keeps its historical
        meaning for every existing reader, while `blocked_selectors` carries the
        scope/deadline detail that a flat list of names cannot express."""
        return {a for a in self.adapters if self.blocking(a)}

    def blocked_selectors(self) -> list:
        return [b.to_dict() for b in self.blocks]

    def _expire(self) -> list:
        """Drop blocks whose deadline has passed. Returns the re-admitted blocks."""
        now = self.clock()
        live, expired = [], []
        for b in self.blocks:
            deadline = _epoch(b.retry_at) if b.retry_at else None
            (expired if deadline is not None and deadline <= now else live).append(b)
        self.blocks = live
        return expired

    def begin_call(self) -> list:
        """The ONE refresh point (see the docstring): expire due blocks and re-sample
        local availability, once per logical engine call. Returns the re-admissions."""
        expired = self._expire()
        self.snapshot = availability_snapshot(self.adapters)
        return expired

    def note_executed(self, cell: str, agent: str) -> None:
        """Bind an already-executed cell to the agent that ran it, so a later
        re-admission cannot make the role change hands mid-run."""
        if cell and agent:
            self.sticky[cell] = agent

    # ---- eligibility ---------------------------------------------------
    def _live(self, agent: str) -> bool:
        """Configured, enabled, locally available in the frozen snapshot, unblocked."""
        a = self.adapters.get(agent)
        if a is None or a.cfg.get("disabled"):
            return False
        if self.blocking(agent):
            return False
        ok, _reason = self.snapshot.get(agent, (False, "not in the availability snapshot"))
        return bool(ok)

    def _why_not(self, agent: str) -> str:
        """Why this agent is not eligible right now — the block wins over the snapshot,
        because a blocked agent whose CLI is present would otherwise read as "ok"."""
        a = self.adapters.get(agent)
        if a is None:
            return "not configured"
        if a.cfg.get("disabled"):
            return "disabled in config"
        b = self.blocking(agent)
        if b is not None:
            return (f"blocked ({b.kind}) on {b.scope} '{b.selector}'"
                    + (f" until {b.retry_at}" if b.retry_at else " for this run")
                    + (f": {b.reason}" if b.reason else ""))
        return self.snapshot.get(agent, (False, "not in the availability snapshot"))[1]

    def _capable(self, agent: str, cell: str) -> bool:
        """Whether `agent`'s agents.json `roles` allowlist covers `cell`'s
        declared role, AND it may write when the cell writes. A cell with no declared
        role imposes no role constraint (back-compat for any workflow that doesn't
        set one).

        FAILS CLOSED on the agent side: an agent that declares no `roles` list at all
        holds NO cell, rather than being eligible for every one of them. An agent added
        to agents.json without its allowlist is then visibly unused ("no live agent for
        role X") instead of silently landing in the editing Developer cell it was never
        meant to hold. `orchestrate.py doctor` names any agent in that state."""
        a = self.adapters.get(agent)
        if a is None:
            return False
        if self.requires_edit.get(cell) and not a.cfg.get("can_edit"):
            return False
        role = self.roles.get(cell)
        if role is None:
            return True
        return role in (a.cfg.get("roles") or [])

    def _eligible(self, agent: str, cell: str) -> bool:
        return self._live(agent) and self._capable(agent, cell)

    def candidates(self, cell: str) -> list:
        """Every configured agent that MAY hold this cell, ignoring availability."""
        return self.candidate_status(cell)["candidates"]

    def candidate_status(self, cell: str) -> dict:
        """Who may hold this cell, split by whether they can be SELECTED right now.

        A cross-family pin fallback is CONDITIONAL: it becomes reachable only once an
        account- or provider-scoped block is classified against the pin's own
        same-family candidates, and only after those candidates are gone. Counting it
        as plainly "live" made preflight contradict itself — the verifier cell could
        report `NONE (no live agent)`, `live 1/3` and "none eligible now" about the
        same DeepSeek in the same breath.

        `selectable` is what `assign()` would really choose from at this instant;
        `conditional` is live and role-capable but still locked behind that evidence.
        Reported separately so a reader can tell "this cell has a fallback" from "this
        cell has a fallback it is not allowed to use yet"."""
        def usable(names):
            return [a for a in dict.fromkeys(names)
                    if a in self.adapters and self._capable(a, cell)]

        pin = self.pins.get(cell)
        if pin is None:
            capable = usable(self.chain)
            return {"candidates": capable, "unlocked": True, "conditional": [],
                    "selectable": [a for a in capable if self._eligible(a, cell)]}
        individuals = usable(self._pin_individuals(pin))
        cross = [a for a in usable(pin.cross_fallbacks) if a not in individuals]
        live_individuals = [a for a in individuals if self._eligible(a, cell)]
        live_cross = [a for a in cross if self._eligible(a, cell)]
        # `_resolve_pin` exhausts the same-family candidates first and only then
        # consults the cross-family list, so a single live individual keeps the whole
        # cross list conditional.
        unlocked = self._account_or_provider_trigger(individuals) is not None
        selectable = list(live_individuals) if live_individuals else (
            list(live_cross) if unlocked else [])
        return {"candidates": individuals + cross, "unlocked": unlocked,
                "selectable": selectable,
                "conditional": [a for a in live_cross if a not in selectable]}

    # ---- assignment -----------------------------------------------------
    def _account_or_provider_trigger(self, agents) -> Block | None:
        """The account/provider block that took these candidates out, if any.

        This is the ONLY key that unlocks a cross-family pin fallback. A locally
        missing CLI or a merely exhausted candidate list is not evidence that the
        whole vendor account is gone, and inventing that evidence would weaken the
        pin silently — exactly what the pin exists to prevent."""
        for agent in agents:
            b = self.blocking(agent)
            if b is not None and b.scope in (SCOPE_ACCOUNT, SCOPE_PROVIDER):
                return b
        return None

    def _resolve_pin(self, pin, used):
        """(agent, degradation_record_or_None) for one pinned cell."""
        individuals = self._pin_individuals(pin)
        for a in individuals:
            if a not in used and self._eligible(a, pin.cell):
                return a, None
        trigger = self._account_or_provider_trigger(individuals)
        if trigger is None:
            return None, None
        for a in pin.cross_fallbacks:
            if a not in used and self._eligible(a, pin.cell):
                return a, {
                    "kind": "cross_family_pin_fallback", "cell": pin.cell,
                    "configured_primary": pin.primary, "selected": a,
                    "trigger": {"kind": trigger.kind, "scope": trigger.scope,
                                "selector": trigger.selector, "source": trigger.source,
                                "reason": trigger.reason},
                    "lost_assurance": pin.lost_assurance,
                }
        return None, None

    def assign(self) -> dict:
        """Return {cell: agent_or_None} for this instance's availability snapshot and
        block registry. Zero-argument by contract (A1)."""
        out: dict[str, str | None] = {}
        used: set[str] = set()
        self.degradations = {}

        # 0) already-executed cells keep the agent that ran them, while it stays
        #    eligible: re-admission must never make a role change hands mid-run.
        for cell in self.cells:
            a = self.sticky.get(cell)
            if a and a not in used and self._eligible(a, cell):
                out[cell] = a
                used.add(a)

        # 1) pinned cells: the pin's own candidates, in its own order. Cross-family
        #    fallback only on classified account/provider evidence.
        for cell, pin in self.pins.items():
            if out.get(cell):
                continue
            chosen, degradation = self._resolve_pin(pin, used)
            out[cell] = chosen
            if chosen:
                used.add(chosen)
            if degradation:
                self.degradations[cell] = degradation

        # 2) positional fill for the rest: walk the chain in priority order per
        #    cell, skipping any agent that is a pin's PRIMARY or not capable of
        #    THIS cell's role. An agent skipped here (live but incapable) remains
        #    a candidate for a later cell it IS capable of.
        #
        #    Only the primary is statically reserved — matching the pre-P1 string-pin
        #    contract. A pin FALLBACK that was not selected stays available to the
        #    positional pass, so adding `deepseek` as the verifier's last resort does
        #    not quietly take DeepSeek out of the all-live architecture arbiter cell.
        reserved = {p.primary for p in self.pins.values()}
        for cell in self.cells:
            if cell in self.pins or cell in out:
                continue
            chosen = next((a for a in self.chain
                           if a not in used and a not in reserved and self._eligible(a, cell)),
                          None)
            out[cell] = chosen
            if chosen:
                used.add(chosen)
        return out

    # ---- preflight -------------------------------------------------------
    def _clone(self, extra_block=None):
        """A copy that shares the frozen snapshot and adapters, for simulation only."""
        r = Roster.__new__(Roster)
        r.__dict__.update(self.__dict__)
        r.blocks = list(self.blocks) + ([extra_block] if extra_block else [])
        r.sticky = dict(self.sticky)
        r.degradations = {}
        return r

    def _simulate(self, baseline, scope, agent):
        """Re-run the REAL allocator with one more block, and report the collateral."""
        if agent is None:
            return {"selector": None, "assigned": None, "lost": [], "changed": {},
                    "note": "cell has no current assignment"}
        selector = self.selectors(agent)[scope]
        sim = self._clone(Block(scope=scope, selector=selector, reason="preflight simulation",
                                kind="quota" if scope != SCOPE_AGENT else "transport",
                                source="local", agent=agent))
        # Simulated loss is not "the cell already ran": drop stickiness for cells whose
        # bound agent this loss takes out, so the simulation shows the real fallback.
        sim.sticky = {c: a for c, a in sim.sticky.items() if not sim.blocking(a)}
        after = sim.assign()
        return {
            "selector": selector,
            "lost": [c for c in self.cells if baseline.get(c) and not after.get(c)],
            "changed": {c: [baseline.get(c), after.get(c)]
                        for c in self.cells if baseline.get(c) != after.get(c)},
            "degradations": list(sim.degradations.values()),
            "assignment": after,
        }

    def preflight(self) -> dict:
        """The single readiness implementation, shared by run startup and `doctor`.

        For every configured cell it reports the current assignment, who MAY hold it,
        who can be SELECTED right now, who is only a conditional cross-family fallback,
        and what the deterministic allocator does when the assigned agent / its whole
        account / its whole provider goes away — including which OTHER cells that loss
        empties. No provider request is made: this is local readiness, never a claim
        about remote account health."""
        assignment = self.assign()
        cells = []
        for cell in self.cells:
            cur = assignment.get(cell)
            st = self.candidate_status(cell)
            capable = st["candidates"]
            entry = {
                "cell": cell, "role": self.roles.get(cell),
                "requires_edit": bool(self.requires_edit.get(cell)),
                "assigned": cur, "candidates": capable,
                # selectable NOW, as opposed to live-but-locked (see candidate_status)
                "live_candidates": st["selectable"],
                "conditional_candidates": st["conditional"],
                "on_agent_loss": self._simulate(assignment, SCOPE_AGENT, cur),
                "on_account_loss": self._simulate(assignment, SCOPE_ACCOUNT, cur),
                "on_provider_loss": self._simulate(assignment, SCOPE_PROVIDER, cur),
                "warnings": [],
            }
            if cur is None:
                entry["warnings"].append(
                    f"no live agent: {len(capable)} configured candidate(s), "
                    f"none selectable now"
                    + (f"; {', '.join(st['conditional'])} "
                       f"{'is' if len(st['conditional']) == 1 else 'are'} live but locked "
                       "until an account- or provider-scoped failure is classified"
                       if st["conditional"] else ""))
            if len(capable) == 1:
                entry["warnings"].append(
                    f"depth-one cell: only '{capable[0]}' may hold it"
                    + (" (it is the sole edit-capable agent for a writing cell)"
                       if entry["requires_edit"] else ""))
            for key, label in (("on_agent_loss", "the assigned agent"),
                               ("on_account_loss", "its whole account"),
                               ("on_provider_loss", "its whole provider")):
                lost = entry[key]["lost"]
                if cell in lost:
                    entry["warnings"].append(
                        f"no fallback: losing {label} empties this cell")
                collateral = [c for c in lost if c != cell]
                if collateral:
                    entry["warnings"].append(
                        f"losing {label} also empties: {', '.join(collateral)}")
            cells.append(entry)

        unassigned = [c["cell"] for c in cells if c["assigned"] is None]
        degrades = sorted({c for e in cells for k in
                           ("on_agent_loss", "on_account_loss", "on_provider_loss")
                           for c in e[k]["lost"]})
        status = UNASSIGNED if unassigned else (DEGRADED if degrades else READY)
        return {
            "status": status, "assignment": assignment, "cells": cells,
            "unassigned": unassigned, "fragile_cells": degrades,
            "blocked_selectors": self.blocked_selectors(),
            "unavailable": {a: self._why_not(a) for a in self.adapters if not self._live(a)},
        }


def format_preflight(workflow: str, report: dict) -> str:
    """The ONE renderer for a preflight report, used by both `doctor` and run startup
    (so an operator reads the same thing in both places)."""
    lines = [f"roster preflight [{workflow}]: {report['status']}"]
    for e in report["cells"]:
        seat = f"{e['cell']}{' ✎' if e['requires_edit'] else ''}"
        conditional = e.get("conditional_candidates") or []
        lines.append(f"  {seat:24s} -> {e['assigned'] or 'NONE (no live agent)'}"
                     f"   live {len(e['live_candidates'])}/{len(e['candidates'])} candidate(s)"
                     + (f" (+{len(conditional)} locked cross-family)" if conditional else "")
                     + f" [{', '.join(e['candidates']) or 'none configured'}]")
        for key, label in (("on_agent_loss", "agent"), ("on_account_loss", "account"),
                           ("on_provider_loss", "provider")):
            sim = e[key]
            if sim["selector"] is None:
                continue
            nxt = sim["assignment"].get(e["cell"])
            note = f"     if {label} '{sim['selector']}' is lost -> {nxt or 'NONE'}"
            collateral = [c for c in sim["lost"] if c != e["cell"]]
            if collateral:
                note += f"   (also empties: {', '.join(collateral)})"
            lines.append(note)
        for w in e["warnings"]:
            lines.append(f"     ! {w}")
    if report["status"] == DEGRADED:
        lines.append("  DEGRADED: the run proceeds; a cell above has no fallback for at "
                     "least one failure domain and would PAUSE (AWAITING_HUMAN, resumable) "
                     "if that domain is lost.")
    elif report["status"] == UNASSIGNED:
        lines.append("  UNASSIGNED: the run proceeds knowingly — some cells are conditional "
                     "(arbitration fires only on deadlock). An unassigned cell that is "
                     "actually reached PAUSES before any provider call.")
    return "\n".join(lines)
