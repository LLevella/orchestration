"""Unit tests for the engine's verdict-repair re-prompt.

A headless agent can do the full analysis but end on a prose summary that omits
its `## <Header>` verdict block (observed: the investigator opening with
"Investigation complete." instead of "## Investigator Verdict"). Before this
repair, such a step wasted the whole iteration to HUMAN_REQUIRED. The repair
re-prompts the SAME agent (bounded) to re-emit the verdict, appends it to the
original text (analysis preserved), and only fail-safes if every attempt fails.
"""
import unittest

from orchestrator.adapters import OK, AgentResponse
from orchestrator.engine import VERDICT_REPAIR_ATTEMPTS, Engine
from orchestrator.verdicts import extract_verdict

ROLE = "Root Cause Investigator"


class _Tx:
    def __init__(self):
        self.events, self.mds = [], []

    def event(self, **kw):
        self.events.append(kw)

    def md(self, text):
        self.mds.append(text)


class _Adapter:
    """Returns scripted responses in order, one per run() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        self.calls += 1
        self.last_prompt = prompt
        return self._responses.pop(0)


class _Ctx:
    def __init__(self, adapter):
        self.adapters = {"claude": adapter}
        self.tx = _Tx()


def _engine(adapter):
    return Engine(_Ctx(adapter), "investigation")


class TestVerdictRepair(unittest.TestCase):
    def test_repair_succeeds_and_preserves_analysis(self):
        orig = AgentResponse("claude", OK,
                             text="Investigation complete. The hypothesis is ready.")
        repaired = AgentResponse("claude", OK,
                                 text="## Investigator Verdict\n"
                                      "HYPOTHESIS_READY_FOR_REFUTATION\n\n## Hypotheses\n...")
        ad = _Adapter([repaired])
        out = _engine(ad)._repair_verdict("inv.investigator", "claude", ROLE,
                                          "sys", "ORIGINAL-TASK: ER rle_decap=0", orig, allow_edit=True)
        self.assertEqual(extract_verdict(ROLE, out.text), "HYPOTHESIS_READY_FOR_REFUTATION")
        self.assertIn("Investigation complete", out.text)  # original analysis kept
        self.assertIn("ORIGINAL-TASK: ER rle_decap=0", ad.last_prompt)  # repair carries the brief
        self.assertEqual(ad.calls, 1)

    def test_repair_uses_second_attempt(self):
        orig = AgentResponse("claude", OK, text="no verdict")
        bad = AgentResponse("claude", OK, text="still missing it")
        good = AgentResponse("claude", OK,
                             text="## Investigator Verdict\nNO_DEFECT_FOUND")
        ad = _Adapter([bad, good])
        out = _engine(ad)._repair_verdict("s", "claude", ROLE, "sys", "task", orig, allow_edit=False)
        self.assertEqual(extract_verdict(ROLE, out.text), "NO_DEFECT_FOUND")
        self.assertEqual(ad.calls, 2)

    def test_repair_exhausts_then_returns_original_unparseable(self):
        orig = AgentResponse("claude", OK, text="no verdict here")
        bad = [AgentResponse("claude", OK, text="nope") for _ in range(VERDICT_REPAIR_ATTEMPTS)]
        ad = _Adapter(bad)
        out = _engine(ad)._repair_verdict("s", "claude", ROLE, "sys", "task", orig, allow_edit=True)
        self.assertIsNone(extract_verdict(ROLE, out.text))   # -> _finalize fail-safes
        self.assertEqual(ad.calls, VERDICT_REPAIR_ATTEMPTS)

    def test_repair_merges_usage_cost_duration(self):
        orig = AgentResponse("claude", OK, text="summary", duration=1.0,
                             usage={"input": 10, "output": 20, "total": 30}, cost_usd=0.5)
        repaired = AgentResponse("claude", OK,
                                 text="## Investigator Verdict\nNO_DEFECT_FOUND",
                                 duration=2.0, usage={"input": 1, "output": 2, "total": 3},
                                 cost_usd=0.1)
        out = _engine(_Adapter([repaired]))._repair_verdict("s", "claude", ROLE,
                                                            "sys", "task", orig, allow_edit=False)
        self.assertEqual(out.usage["total"], 33)
        self.assertEqual(out.duration, 3.0)
        self.assertAlmostEqual(out.cost_usd, 0.6)


if __name__ == "__main__":
    unittest.main()
