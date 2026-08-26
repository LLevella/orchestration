"""Unit tests for Roster.assign() (orchestrator/roster.py) role-capability filtering.

Regression coverage for the 2026-08-04 bug: when the top agent in a shared chain
went EXHAUSTED, the old positional index-fill reassigned EVERY later cell one
slot early, handing the constrained `developer` cell (only `claude` has
'Responsible Developer' + can_edit) to a read-only reviewer with neither the
role nor edit capability — codex ran read-only and burned 3 wasted iterations
before HUMAN_REQUIRED. Fixed by making `assign()` skip an agent for a cell
unless the cell's declared role is in that agent's `roles` allowlist.
"""
import unittest

from orchestrator.roster import Roster, is_transient, is_unavailable


class FakeAdapter:
    def __init__(self, roles, disabled=False, live=True):
        self.cfg = {"roles": roles, "disabled": disabled}
        self._live = live

    def availability(self):
        return self._live, "" if self._live else "down"


def _wf_block(chain, cells, cell_roles, pinned=None):
    block = {"roster": {"chain": chain, "cells": cells, "pinned": pinned or {}}}
    for cell, role in cell_roles.items():
        block[cell] = {"role": role}
    return block


class TestRosterCapabilityFilter(unittest.TestCase):
    def _dev_setup(self, claude_live=True, claude_reviewer_live=True):
        """Mirrors the real `development` workflow's constrained shape: only
        `claude` can fill `developer` ('Responsible Developer' not in any other
        agent's allowlist); `claude_reviewer` and `codex` both can fill
        `code_reviewer`."""
        adapters = {
            "claude": FakeAdapter(["Responsible Developer", "Software Engineering Code Reviewer"],
                                   live=claude_live),
            "claude_reviewer": FakeAdapter(["Software Engineering Code Reviewer"],
                                            live=claude_reviewer_live),
            "codex": FakeAdapter(["System Architect", "Software Engineering Code Reviewer"]),
        }
        wf = _wf_block(
            chain=["claude", "claude_reviewer", "codex"],
            cells=["developer", "code_reviewer", "architect"],
            cell_roles={
                "developer": "Responsible Developer",
                "code_reviewer": "Software Engineering Code Reviewer",
                "architect": "System Architect",
            },
        )
        return Roster(wf, adapters)

    def test_happy_path_matches_index_order(self):
        r = self._dev_setup()
        self.assertEqual(r.assign(), {
            "developer": "claude", "code_reviewer": "claude_reviewer", "architect": "codex",
        })

    def test_top_agent_down_leaves_constrained_cell_empty_not_misassigned(self):
        """The bug: claude (only 'Responsible Developer' holder) is down. The old
        positional fill handed `developer` to claude_reviewer (incapable, no edit
        role). The fix must leave `developer` None rather than misassign it, while
        still filling the cells claude_reviewer/codex ARE capable of."""
        r = self._dev_setup(claude_live=False)
        out = r.assign()
        self.assertIsNone(out["developer"], "developer must NOT be handed to an incapable agent")
        self.assertEqual(out["code_reviewer"], "claude_reviewer")
        self.assertEqual(out["architect"], "codex")

    def test_top_two_agents_down_cascades_correctly(self):
        """claude AND claude_reviewer down: developer stays empty (codex isn't
        'Responsible Developer'-capable either); code_reviewer correctly falls to
        codex (which DOES have that role); architect ends up unfilled (only codex
        had 'System Architect' and it was consumed by code_reviewer)."""
        r = self._dev_setup(claude_live=False, claude_reviewer_live=False)
        out = r.assign()
        self.assertIsNone(out["developer"])
        self.assertEqual(out["code_reviewer"], "codex")
        self.assertIsNone(out["architect"])

    def test_cell_with_no_declared_role_is_unconstrained(self):
        adapters = {"a": FakeAdapter([]), "b": FakeAdapter([])}
        wf = _wf_block(chain=["a", "b"], cells=["x"], cell_roles={})  # no role for 'x'
        r = Roster(wf, adapters)
        self.assertEqual(r.assign()["x"], "a")

    def test_pinned_fallback_also_respects_capability(self):
        """Pinned cell's same-family Claude fallback must skip a Claude variant
        that lacks the cell's role, same as the positional path."""
        adapters = {
            "claude_reviewer": FakeAdapter(["Developer Verifier"], live=False),
            "claude_sonnet": FakeAdapter(["Adversarial Reviewer"]),  # lacks 'Developer Verifier'
            "claude_haiku": FakeAdapter(["Developer Verifier"]),
        }
        wf = _wf_block(
            chain=["claude_reviewer", "claude_sonnet", "claude_haiku"],
            cells=["developer_verifier"],
            cell_roles={"developer_verifier": "Developer Verifier"},
            pinned={"developer_verifier": "claude_reviewer"},
        )
        r = Roster(wf, adapters)
        self.assertEqual(r.assign()["developer_verifier"], "claude_haiku")

    def test_mark_exhausted_then_reassign_reflects_capability_filter(self):
        r = self._dev_setup()
        self.assertEqual(r.assign()["developer"], "claude")
        r.mark_exhausted("claude")
        self.assertIsNone(r.assign()["developer"])


class TestAvailabilityMarkers(unittest.TestCase):
    """is_unavailable() = any reason to drop+shift an agent; is_transient() = the
    momentary-blip subset that warrants ONE same-agent retry before eviction."""

    def test_transient_is_a_strict_subset_of_unavailable(self):
        # Everything transient must also be unavailable (retry, then shift if it recurs).
        for s in ("HTTP Error 503: Service Unavailable", "model overloaded",
                  "socket hang up", "fetch failed", "please try again later"):
            self.assertTrue(is_unavailable(s), s)
            self.assertTrue(is_transient(s), s)

    def test_persistent_unavailable_is_not_transient(self):
        # Quota / rate-limit / auth / billing / spending-cap persist: shift, never retry.
        for s in ("Spending cap reached resets 2:50pm", "HTTP Error 429: Too Many Requests",
                  "insufficient balance", "payment required", "invalid api key",
                  "you have reached your usage limit"):
            self.assertTrue(is_unavailable(s), s)
            self.assertFalse(is_transient(s), s)

    def test_ncc_msg_402_is_not_a_false_positive(self):
        # The NCC FL control message is literally msg=402; must not read as billing.
        self.assertFalse(is_unavailable("NCC FL control message msg=402 handled"))
        self.assertFalse(is_transient("NCC FL control message msg=402 handled"))

    def test_empty_text_is_neither(self):
        for fn in (is_unavailable, is_transient):
            self.assertFalse(fn(""))
            self.assertFalse(fn(None))


if __name__ == "__main__":
    unittest.main()
