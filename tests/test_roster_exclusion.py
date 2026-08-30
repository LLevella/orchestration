"""A cell this run cannot call must reserve no agent (Irina 2026-08-30).

The defect, from review run 20260830T113210Z-review-1aa9 (one-shot, no --iterate):
the roster filled ALL declared cells, so `review.reviser` — which runs only inside
the --iterate refine loop — held deepseek in reserve for a step that could not
happen. The three-lens panel, meant to be three independent heads, came out
codex + claude_reviewer + claude_sonnet: two thirds one vendor, with shared
training and shared blind spots, and nothing in the output said so.

These tests pin both halves of the fix: an impossible cell frees its agent, and a
cell that is merely CONDITIONAL (the arch/dev/inv arbiter, which fires on deadlock)
keeps its reservation — the judge has to exist at the moment a deadlock appears.
"""
import copy
import unittest
from unittest.mock import MagicMock

from orchestrator.adapters import build_adapters
from orchestrator.config import load_config
from orchestrator.engine import AgentError, AvailabilityPause, Engine, unreachable_cells
from orchestrator.roster import Roster, availability_snapshot, format_preflight


def _all_live():
    """Adapters for the shipped config with every agent live (mock transport), so the
    assertions describe the CONFIG and not the machine the suite runs on."""
    agents_cfg, wf = load_config()
    return build_adapters(agents_cfg, mock=True, scenario=None), wf


def _assign(wf_block, adapters, workflow, **mode):
    return Roster(wf_block, adapters,
                  excluded=unreachable_cells(workflow, wf_block, **mode)).assign()


class TestOneShotReviewFreesTheReviser(unittest.TestCase):
    def test_reviser_is_assigned_nobody_and_the_panel_is_four_families(self):
        adapters, wf = _all_live()
        out = _assign(wf["review"], adapters, "review", iterate=False)
        self.assertIsNone(out["reviser"],
                          "a one-shot review never calls the reviser; it must reserve nobody")
        seats = [out["reviewer_correctness"], out["reviewer_completeness"],
                 out["reviewer_risk"], out["arbiter"]]
        self.assertEqual(len(set(seats)), 4, f"panel + arbiter are not four agents: {seats}")
        self.assertEqual(seats, ["codex", "claude_reviewer", "deepseek", "gemini"],
                         "the freed agent must fall to the panel in chain order")

    def test_the_surviving_cells_keep_their_relative_order(self):
        """Excluding a cell frees its agent; it does not re-shuffle the rest. Only the
        seat that had been starved changes."""
        adapters, wf = _all_live()
        full = Roster(wf["review"], adapters).assign()
        out = _assign(wf["review"], adapters, "review", iterate=False)
        for cell in ("reviewer_correctness", "reviewer_completeness"):
            self.assertEqual(out[cell], full[cell], cell)
        self.assertEqual(out["arbiter"], full["arbiter"], "the arbiter pin is not conditional")

    def test_iterate_still_reserves_the_reviser_exactly_as_before(self):
        adapters, wf = _all_live()
        self.assertEqual(
            _assign(wf["review"], adapters, "review", iterate=True),
            {"reviewer_correctness": "codex", "reviewer_completeness": "claude_reviewer",
             "reviewer_risk": "claude_sonnet", "arbiter": "gemini", "reviser": "deepseek"})


class TestDisabledCellReservesNobody(unittest.TestCase):
    def test_enabled_false_frees_the_agent_in_a_shipped_workflow(self):
        adapters, wf = _all_live()
        block = copy.deepcopy(wf["mrreview"])
        block["moderator"]["enabled"] = False
        self.assertEqual(unreachable_cells("mrreview", block),
                         {"moderator": "disabled in config (enabled: false)"})
        out = _assign(block, adapters, "mrreview")
        self.assertIsNone(out["moderator"])
        # the panel in front of it is untouched
        self.assertEqual([out[f"expert_{n}"] for n in (1, 2, 3, 4)],
                         ["codex", "claude_reviewer", "gemini", "deepseek"])

    def test_a_disabled_cell_hands_its_agent_to_a_later_cell(self):
        """Synthetic, so the property is visible rather than incidental: with `b`
        switched off, `c` gets the agent `b` was holding."""
        class FakeAdapter:
            def __init__(self):
                self.cfg = {"roles": ["R"]}

            def availability(self):
                return True, ""

        adapters = {n: FakeAdapter() for n in ("a1", "a2", "a3")}
        block = {"roster": {"chain": ["a1", "a2", "a3"], "cells": ["a", "b", "c"]},
                 "a": {"role": "R"}, "b": {"role": "R", "enabled": False}, "c": {"role": "R"}}
        r = Roster(block, adapters, excluded=unreachable_cells("synthetic", block))
        self.assertEqual(r.assign(), {"a": "a1", "b": None, "c": "a2"})


class TestConditionalCellsKeepTheirReservation(unittest.TestCase):
    """The distinction the fix rests on: only a cell that CANNOT run is excluded, never
    one that merely MIGHT not. The arbiter is called on deadlock/escalation only, and
    stays reserved so an independent judge exists the moment a deadlock appears."""

    def test_arch_dev_inv_exclude_nothing_and_keep_the_arbiter(self):
        adapters, wf = _all_live()
        for workflow, arbiter in (("architecture", "deepseek"), ("development", "deepseek"),
                                  ("investigation", "deepseek")):
            self.assertEqual(unreachable_cells(workflow, wf[workflow]), {},
                             f"{workflow} must exclude no cell: its arbiter is conditional, "
                             "not impossible")
            out = _assign(wf[workflow], adapters, workflow)
            self.assertEqual(out["arbiter"], arbiter,
                             f"{workflow}.arbiter lost its reservation")
            self.assertEqual(len(set(out.values())), len(out),
                             f"{workflow} gave two cells the same agent: {out}")

    def test_the_review_arbiter_is_never_excluded(self):
        """It synthesises EVERY review run, one-shot included, and keeps its pin."""
        _adapters, wf = _all_live()
        for iterate in (False, True):
            self.assertNotIn("arbiter", unreachable_cells("review", wf["review"],
                                                          iterate=iterate))


class TestPreflightSeparatesUnusedFromUnavailable(unittest.TestCase):
    def test_an_excluded_cell_is_not_reported_as_missing_an_agent(self):
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        report = Roster(wf["review"], adapters, snapshot=snap,
                        excluded=unreachable_cells("review", wf["review"])).preflight()
        entry = next(e for e in report["cells"] if e["cell"] == "reviser")
        self.assertTrue(entry["excluded"])
        self.assertIsNone(entry["assigned"])
        self.assertEqual(entry["warnings"], [],
                         "depth/fallback warnings about a cell nobody calls are noise")
        self.assertNotIn("reviser", report["unassigned"],
                         "an unused cell is not an unassigned one")

    def test_the_renderer_says_not_used_in_this_mode(self):
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        r = Roster(wf["review"], adapters, snapshot=snap,
                   excluded=unreachable_cells("review", wf["review"]))
        line = next(ln for ln in format_preflight("review", r.preflight()).splitlines()
                    if ln.strip().startswith("reviser"))
        self.assertIn("not used in this mode", line)
        self.assertIn("--iterate", line)
        self.assertNotIn("no live agent", line)

    def test_a_genuinely_empty_cell_still_reads_as_no_live_agent(self):
        """The contrast that makes the distinction worth anything."""
        class DeadAdapter:
            def __init__(self):
                self.cfg = {"roles": ["R"]}

            def availability(self):
                return False, "CLI not installed"

        block = {"roster": {"chain": ["a1"], "cells": ["a"]}, "a": {"role": "R"}}
        r = Roster(block, {"a1": DeadAdapter()})
        report = r.preflight()
        self.assertEqual(report["unassigned"], ["a"])
        self.assertIn("no live agent", format_preflight("x", report))


class TestReachingAnExcludedCellFailsLoudly(unittest.TestCase):
    def test_call_refuses_an_excluded_cell_instead_of_using_a_stale_binding(self):
        adapters, wf = _all_live()
        ctx = MagicMock()
        ctx.workflows = wf
        engine = Engine(ctx, "review")
        engine.roster = Roster(wf["review"], adapters,
                               excluded=unreachable_cells("review", wf["review"]))
        # A stale static binding is exactly what must NOT be invoked.
        spec = {"role": "Review Reviser", "_cell": "reviser", "agent": "deepseek"}
        with self.assertRaises(AgentError) as cm:
            engine.call("review.reviser", spec, "prompt", phase="review", iteration=1)
        self.assertNotIsInstance(cm.exception, AvailabilityPause,
                                 "a state-machine bug is not a resumable availability pause")
        self.assertIn("reviser", str(cm.exception))
        ctx.adapters.__getitem__.assert_not_called()


if __name__ == "__main__":
    unittest.main()
