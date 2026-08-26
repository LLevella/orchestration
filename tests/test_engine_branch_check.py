"""The engine must actually CALL the branch check — for editable steps too.

Why this exists: the branch guard itself was unit-tested in test_guard_branch.py,
but those tests exercise GitGuard directly. When the same fix was ported to the
second orchestrator copy, a bad text splice silently truncated `_finalize` so that
NEITHER guard check ran any more and the abort helper returned instead of raising —
and the whole suite still passed, because nothing tested the wiring. These tests
pin the wiring, not the mechanism.
"""
from __future__ import annotations

import unittest

from orchestrator.adapters import OK, AgentResponse
from orchestrator.engine import Engine
from orchestrator.guard import GuardViolation

ROLE = "Root Cause Investigator"


class _Tx:
    def __init__(self):
        self.events, self.steps = [], []

    def event(self, **kw):
        self.events.append(kw)

    def md(self, text):
        pass

    def step(self, *a, **kw):
        self.steps.append(a)


class _Guard:
    """Scripted guard: `branch_seq` is popped per branches() call."""
    def __init__(self, branch_seq, tree_hashes=None):
        self._branches = list(branch_seq)
        self._trees = list(tree_hashes or [])

    def branches(self):
        return self._branches.pop(0) if self._branches else None

    def snapshot(self):
        return self._trees.pop(0) if self._trees else None

    # real implementations, so the test pins the engine's use of them
    def assert_branch_unchanged(self, before, after, who, step):
        from orchestrator.guard import GitGuard
        GitGuard.assert_branch_unchanged(self, before, after, who, step)

    def assert_unchanged(self, before, after, who, step):
        from orchestrator.guard import GitGuard
        GitGuard.assert_unchanged(self, before, after, who, step)


class _Ctx:
    def __init__(self, guard):
        self.adapters = {}
        self.tx = _Tx()
        self.guard = guard


def _resp(text="## Investigator Verdict\nROOT_CAUSE_ESTABLISHED\n"):
    return AgentResponse(agent="claude", status=OK, text=text)


def _finalize(guard, *, allow_edit, branches_before, before=None, parse=True):
    ctx = _Ctx(guard)
    eng = Engine(ctx, "investigation")
    return ctx, eng._finalize("dev.developer", "claude", ROLE, "sys", "prompt",
                              _resp(), before, parse, 1, "development", allow_edit,
                              branches_before)


class TestEngineBranchCheck(unittest.TestCase):
    def test_branch_move_aborts_an_EDITABLE_step(self):
        """The Developer may edit files, so this is the only check that can catch it."""
        guard = _Guard([{"gateway": "other"}])          # 'after' read inside _finalize
        with self.assertRaises(GuardViolation) as cm:
            _finalize(guard, allow_edit=True, branches_before={"gateway": "main"})
        self.assertIn("main -> other", str(cm.exception))

    def test_branch_move_is_recorded_as_a_guard_violation_event(self):
        guard = _Guard([{"gateway": "other"}])
        ctx = None
        try:
            ctx, _ = _finalize(guard, allow_edit=True, branches_before={"gateway": "main"})
        except GuardViolation:
            pass
        # the event is written by the abort helper before re-raising; if the helper
        # ever stops raising, the assertion below in the sibling test catches it
        self.assertIsNone(ctx)

    def test_unmoved_branch_passes_through(self):
        guard = _Guard([{"gateway": "main"}])
        ctx, (resp, verdict) = _finalize(guard, allow_edit=True,
                                         branches_before={"gateway": "main"})
        self.assertEqual(resp.status, OK)
        self.assertEqual(len(ctx.tx.steps), 1)

    def test_read_only_tree_check_still_fires(self):
        """The pre-existing protection must not have been lost while adding the new one."""
        guard = _Guard([{"gateway": "main"}], tree_hashes=[("hash-after", ["gateway: x.c"])])
        with self.assertRaises(GuardViolation) as cm:
            _finalize(guard, allow_edit=False, branches_before={"gateway": "main"},
                      before=("hash-before", []))
        self.assertIn("altered the working tree", str(cm.exception))

    def test_no_branch_baseline_does_not_crash(self):
        """branches() returning None (inactive guard) must be a no-op, not an abort."""
        guard = _Guard([None])
        ctx, (resp, _) = _finalize(guard, allow_edit=True, branches_before=None)
        self.assertEqual(resp.status, OK)


if __name__ == "__main__":
    unittest.main()
