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
to another vendor by the shift. If the pinned agent itself is not live, the pin
falls back to the next live Claude in the chain (same family), never elsewhere.

Resolution is recomputed on every runtime EXHAUSTED, so a mid-run dropout shifts
only the not-yet-run cells and the run continues instead of aborting. Already-run
cells never change.

That last property is NOT an invariant of the roster: it holds only because
`exhausted` is currently write-once — eviction is the sole transition, and there
is no re-admission, so eligibility can only shrink within a run. Any future
re-admission (returning an agent to service after a temporary block expires, or
on an explicit `resume`) breaks the monotonicity claim, and whatever adds it must
also state how already-executed cells stay stable.
"""
from __future__ import annotations


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


class Roster:
    def __init__(self, wf_block, adapters):
        r = wf_block.get("roster") or {}
        self.chain = list(r.get("chain") or [])
        self.cells = list(r.get("cells") or [])
        self.pinned = dict(r.get("pinned") or {})
        # isinstance: a cells entry that does not name a step block (a typo, a scalar
        # like max_iterations) must read as "no declared role", not crash the assignment.
        self.roles = {cell: (wf_block.get(cell) if isinstance(wf_block.get(cell), dict)
                             else {}).get("role")
                      for cell in self.cells}
        self.adapters = adapters
        self.exhausted: set[str] = set()
        self.enabled = bool(self.chain and self.cells)

    def mark_exhausted(self, agent: str) -> None:
        self.exhausted.add(agent)

    def _live(self, agent: str) -> bool:
        a = self.adapters.get(agent)
        if a is None:
            return False
        if a.cfg.get("disabled"):
            return False
        if agent in self.exhausted:
            return False
        ok, _ = a.availability()
        return ok

    def _capable(self, agent: str, cell: str) -> bool:
        """Whether `agent`'s agents.json `roles` allowlist covers `cell`'s
        declared role. A cell with no declared role imposes no constraint
        (back-compat for any workflow that doesn't set one).

        FAILS CLOSED on the agent side: an agent that declares no `roles` list at all
        holds NO cell, rather than being eligible for every one of them. An agent added
        to agents.json without its allowlist is then visibly unused ("no live agent for
        role X") instead of silently landing in the editing Developer cell it was never
        meant to hold. `orchestrate.py doctor` names any agent in that state."""
        role = self.roles.get(cell)
        if role is None:
            return True
        a = self.adapters.get(agent)
        if a is None:
            return False
        return role in (a.cfg.get("roles") or [])

    def assign(self) -> dict:
        """Return {cell: agent_or_None} for the current liveness snapshot."""
        out: dict[str, str | None] = {}
        used: set[str] = set()

        # 1) pinned cells: hold to the pinned agent, else the next live,
        #    role-capable same-family Claude (never a different vendor).
        for cell, pin in self.pinned.items():
            candidates = [pin] + [a for a in self.chain if is_claude(a) and a != pin]
            chosen = next((a for a in candidates
                           if self._live(a) and a not in used and self._capable(a, cell)),
                          None)
            out[cell] = chosen
            if chosen:
                used.add(chosen)

        # 2) positional fill for the rest: walk the chain in priority order per
        #    cell, skipping any agent that is pinned elsewhere or not capable of
        #    THIS cell's role. An agent skipped here (live but incapable) remains
        #    a candidate for a later cell it IS capable of.
        pinned_agents = set(self.pinned.values())
        nonpinned = [c for c in self.cells if c not in self.pinned]
        for cell in nonpinned:
            chosen = next((a for a in self.chain
                           if self._live(a) and a not in used and a not in pinned_agents
                           and self._capable(a, cell)),
                          None)
            out[cell] = chosen
            if chosen:
                used.add(chosen)
        return out
