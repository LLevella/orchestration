"""Where a `--human-answer` lands when a resumed run replays a history that already
contains an answered deferral.

Why this exists: run 20260827T034804Z replayed 25 journaled steps, spent the fresh
answer on the iteration-7 deferral that the PREVIOUS resume had already settled, and
paused again having metered nothing. The answer must survive a replayed deferral and
be applied where the run being resumed actually stopped — the last journaled step —
or, failing that, live. These tests pin that placement, not the arbiter mechanism.
"""
from __future__ import annotations

import unittest

from orchestrator.engine import Engine

DEFER = "## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n\n## Final Note\n- A human must decide.\n"
ARBITER = {"agent": "stub", "role": "Arbitration Reviewer"}


class _Tx:
    def step(self, *a, **kw):
        pass

    def md(self, text):
        pass


class _Ctx:
    """Only what the replay path of Engine.call and _arch_arbiter_step touch."""

    def __init__(self, cache, human_answer):
        self.resume_cache = dict(cache)
        self.human_answer = human_answer
        self.workflows = {"architecture": {"arbiter": ARBITER}}
        self.tx = _Tx()
        self.live_calls = 0
        self.last_call_replayed = False
        self.last_call_final_replay = False

    def resume_lookup(self, step_id, phase, iteration):
        return self.resume_cache.pop((step_id, phase, iteration), None)


def _journaled(text):
    return {"agent": "stub", "role": "Arbitration Reviewer", "verdict": None, "text": text}


class TestHumanAnswerPlacement(unittest.TestCase):
    def _arbiter(self, ctx, iteration):
        return Engine(ctx, "architecture")._arch_arbiter_step(
            "task", "draft", "review", [], ctx.human_answer, iteration)

    def test_replay_flags_mark_only_the_cache_draining_step_as_final(self):
        ctx = _Ctx({("arch.arbiter", "arbitration", 7): _journaled(DEFER),
                    ("arch.arbiter", "arbitration", 10): _journaled(DEFER)}, "decide X")
        eng = Engine(ctx, "architecture")
        eng.call("arch.arbiter", ARBITER, "p", phase="arbitration", iteration=7)
        self.assertTrue(ctx.last_call_replayed)
        self.assertFalse(ctx.last_call_final_replay)      # more history to replay
        eng.call("arch.arbiter", ARBITER, "p", phase="arbitration", iteration=10)
        self.assertTrue(ctx.last_call_replayed)
        self.assertTrue(ctx.last_call_final_replay)       # the run stopped here

    def test_answer_is_not_spent_on_a_deferral_from_the_middle_of_the_journal(self):
        ctx = _Ctx({("arch.arbiter", "arbitration", 7): _journaled(DEFER),
                    ("arch.dev_review", "architecture", 8): _journaled("later step")}, "decide X")
        kind, payload, from_human = self._arbiter(ctx, 7)
        self.assertEqual(kind, "ruling")          # the replay continues past it
        self.assertFalse(from_human)
        self.assertNotIn("decide X", payload)     # the answer was NOT consumed here
        self.assertEqual(ctx.human_answer, "decide X")

    def test_answer_is_applied_at_the_last_journaled_step(self):
        ctx = _Ctx({("arch.arbiter", "arbitration", 10): _journaled(DEFER)}, "decide X")
        kind, payload, from_human = self._arbiter(ctx, 10)
        self.assertEqual(kind, "ruling")
        self.assertTrue(from_human)
        self.assertIn("decide X", payload)


if __name__ == "__main__":
    unittest.main()
