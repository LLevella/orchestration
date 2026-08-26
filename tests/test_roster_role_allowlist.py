"""Shift-left must respect each agent's `roles` allowlist.

Why this exists: on 2026-08-01 `claude` — the only agent with `can_edit: true` and
the only one whose `roles` list contains "Responsible Developer" — hit HTTP 429.
The roster filled cells positionally, so the developer cell shifted left through
`claude_reviewer` to `codex`, an agent that runs in a read-only sandbox and cannot
write a single file. It reported "blocked", the architect escalated, the arbiter
429'd as well, and the run ended HUMAN_REQUIRED having spent five steps on a cell
that could never have produced code. The allowlist already encoded the constraint;
`assign()` simply ignored it.

Uses synthetic agents/cells, so it is independent of the shipped config.
"""
from __future__ import annotations

import unittest

from orchestrator.roster import Roster

DEV = "Responsible Developer"
REV = "Software Engineering Code Reviewer"
ARCH = "System Architect"


class _Adapter:
    def __init__(self, cfg, live=True):
        self.cfg = cfg
        self._live = live

    def availability(self):
        return (self._live, "" if self._live else "429 rate limit")


def _roster(live: dict[str, bool]):
    """Chain mirrors production: only 'writer' may be the Developer."""
    adapters = {
        "writer":  _Adapter({"can_edit": True,  "roles": [DEV, REV, ARCH]}, live["writer"]),
        "readerA": _Adapter({"can_edit": False, "roles": [REV, ARCH]},      live["readerA"]),
        "readerB": _Adapter({"can_edit": False, "roles": [ARCH]},           live["readerB"]),
    }
    wf = {
        "roster": {"chain": ["writer", "readerA", "readerB"],
                   "cells": ["developer", "code_reviewer", "architect"]},
        "developer":     {"agent": "writer",  "role": DEV},
        "code_reviewer": {"agent": "readerA", "role": REV},
        "architect":     {"agent": "readerB", "role": ARCH},
    }
    return Roster(wf, adapters)


class TestRosterRoleAllowlist(unittest.TestCase):
    def test_all_live_reproduces_the_positional_mapping(self):
        """The fix must not disturb the normal case: cell[i] = chain[i]."""
        got = _roster({"writer": True, "readerA": True, "readerB": True}).assign()
        self.assertEqual(got, {"developer": "writer",
                               "code_reviewer": "readerA",
                               "architect": "readerB"})

    def test_developer_is_never_filled_by_an_agent_the_allowlist_forbids(self):
        """This is the 2026-08-01 failure: writer 429s, cell must go None, not to a reader."""
        got = _roster({"writer": False, "readerA": True, "readerB": True}).assign()
        self.assertIsNone(got["developer"],
                          f"developer was filled by {got['developer']!r}, which may not hold {DEV}")
        # the other cells still shift left normally
        self.assertEqual(got["code_reviewer"], "readerA")
        self.assertEqual(got["architect"], "readerB")

    def test_mark_exhausted_has_the_same_effect_as_being_unavailable(self):
        r = _roster({"writer": True, "readerA": True, "readerB": True})
        r.mark_exhausted("writer")
        self.assertIsNone(r.assign()["developer"])

    def test_a_cell_shifts_left_only_to_a_permitted_agent(self):
        """readerA may review; readerB may not, so the reviewer cell empties rather
        than being handed to an agent whose allowlist excludes the role."""
        got = _roster({"writer": False, "readerA": False, "readerB": True}).assign()
        self.assertIsNone(got["developer"])
        self.assertIsNone(got["code_reviewer"])
        self.assertEqual(got["architect"], "readerB")

    def test_agent_without_a_roles_list_holds_no_cell(self):
        """Fails CLOSED: an agent that declares no allowlist is eligible for nothing.

        The other orchestrator copy read a missing list as "may hold any cell". That is
        the same failure mode this file exists for, one level up: an agent added to
        agents.json without its `roles` list would be eligible for the editing Developer
        cell. Empty here means the run says "no live agent for role X" - loud and
        immediate - instead of handing the cell to an agent nobody vetted for it."""
        adapters = {"anything": _Adapter({"can_edit": True})}
        wf = {"roster": {"chain": ["anything"], "cells": ["developer"]},
              "developer": {"agent": "anything", "role": DEV}}
        self.assertEqual(Roster(wf, adapters).assign(), {"developer": None})

    def test_a_cell_with_no_declared_role_takes_any_agent(self):
        """The cell side stays permissive: a step block without a `role` key constrains
        nothing, so a workflow that does not declare one still fills its cells."""
        adapters = {"anything": _Adapter({})}
        wf = {"roster": {"chain": ["anything"], "cells": ["helper"]},
              "helper": {"agent": "anything"}}
        self.assertEqual(Roster(wf, adapters).assign(), {"helper": "anything"})

    def test_no_agent_is_used_for_two_cells(self):
        got = _roster({"writer": True, "readerA": False, "readerB": True}).assign()
        filled = [a for a in got.values() if a]
        self.assertEqual(len(filled), len(set(filled)))


if __name__ == "__main__":
    unittest.main()
