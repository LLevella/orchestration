"""Availability-driven role assignment with shift-left fallback.

Each workflow declares an ordered `chain` of agent names and an ordered list of
role `cells`. With every agent live, cell[i] = chain[i]. When an agent is not
live — its CLI is missing, it is `disabled` in config, or it returns an
EXHAUSTED-class error at runtime (usage limit / quota / repeated 503) — it drops
out and the chain shifts LEFT: the surviving agents re-fill the cells in priority
order. The judge (arbiter) is filled positionally like any other cell and is NOT
reserved from the strong end (it fires only on deadlock — Irina 2026-06-15).

`pinned` cells are held to a fixed agent and are SKIPPED by the positional
shuffle. The architecture Developer-Verifier is pinned to a Claude developer so
the developer review is always done by a real Claude developer, never displaced
to another vendor by the shift. If the pinned agent itself is not live, the pin
falls back to the next live Claude in the chain (same family), never elsewhere.

Resolution is recomputed on every runtime EXHAUSTED, so a mid-run dropout shifts
only the not-yet-run cells and the run continues instead of aborting. Already-run
cells never change: agents only move toward exhaustion during a run (never
recover), so assignment is monotonic for the cells that have already executed.
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
    # billing/credit exhaustion (e.g. DeepSeek HTTP 402 -> urllib "HTTP Error 402:
    # Payment Required"). Matched as the phrase, NOT as bare "402", so a literal "402"
    # inside agent output cannot false-positive as a usage-limit signal.
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


class Roster:
    def __init__(self, wf_block, adapters):
        r = wf_block.get("roster") or {}
        self.chain = list(r.get("chain") or [])
        self.cells = list(r.get("cells") or [])
        self.pinned = dict(r.get("pinned") or {})
        self.adapters = adapters
        self.exhausted: set[str] = set()
        self.enabled = bool(self.chain and self.cells)
        # cell -> role, so shift-left can respect each agent's `roles` allowlist.
        self.cell_roles = {c: (wf_block.get(c) or {}).get("role")
                           for c in self.cells
                           if isinstance(wf_block.get(c), dict)}

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

    def _eligible(self, agent: str, cell: str) -> bool:
        """Live AND permitted to hold this cell's role.

        `agents.json` already carries a per-agent `roles` allowlist; shift-left used
        to ignore it and fill cells positionally. Observed 2026-08-01: `claude` (the
        only agent with `can_edit: true` and the only one allowed to be Responsible
        Developer) hit HTTP 429, so the developer cell shifted left through
        `claude_reviewer` to `codex` — a read-only-sandbox agent that cannot write at
        all. It dutifully reported "blocked", the architect escalated, the arbiter
        429'd too, and the run died as HUMAN_REQUIRED after burning five steps on a
        cell that could never have produced code. Honouring the allowlist turns that
        into an immediate, accurate "no live agent for role 'Responsible Developer'".
        """
        if not self._live(agent):
            return False
        role = self.cell_roles.get(cell)
        allowed = (self.adapters[agent].cfg or {}).get("roles")
        if role and allowed and role not in allowed:
            return False
        return True

    def assign(self) -> dict:
        """Return {cell: agent_or_None} for the current liveness snapshot."""
        out: dict[str, str | None] = {}
        used: set[str] = set()

        # 1) pinned cells: hold to the pinned agent, else the next live same-family Claude.
        for cell, pin in self.pinned.items():
            candidates = [pin] + [a for a in self.chain if is_claude(a) and a != pin]
            chosen = next((a for a in candidates
                           if self._eligible(a, cell) and a not in used), None)
            out[cell] = chosen
            if chosen:
                used.add(chosen)

        # 2) fill the rest from the chain in order, skipping pinned agents and any
        #    agent not permitted to hold that cell's role. With every agent live and
        #    eligible this reproduces the previous positional mapping
        #    (cell[i] = chain[i]); it only diverges where the old code would have
        #    handed a cell to an agent the allowlist forbids.
        pinned_agents = set(self.pinned.values())
        nonpinned = [c for c in self.cells if c not in self.pinned]
        for cell in nonpinned:
            chosen = next((a for a in self.chain
                           if a not in used and a not in pinned_agents
                           and self._eligible(a, cell)), None)
            out[cell] = chosen
            if chosen:
                used.add(chosen)
        return out
