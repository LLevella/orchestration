"""Gate-quality regressions for the development review chain (Irina 2026-08-09).

Three properties, each pinned against the defect that motivated it:

  1. The Code Reviewer receives the approved architecture and the blockers earlier gates
     raised in this run — it used to get neither, so spec non-conformance was invisible to
     the first gate and a re-review after an Architect block was a blind re-read (32 of 33
     such re-reviews returned PASS).
  2. A later gate blocking a diff the Code Reviewer just passed emits a `reviewer_miss`
     event, and every development run ends with a `reviewer_miss_summary` — nothing measured
     this before, which is why a reviewer that had stopped blocking went unnoticed.
  3. Every role's system prompt carries the global language + workspace-isolation policy.

These assert on the real prompt builders and the real transcript, not on a model's answer.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.config import GLOBAL_POLICY, RunContext, load_config
from orchestrator.engine import Engine

ORCH = Path(__file__).resolve().parent.parent


def _engine():
    """Engine over a fully mocked context — only the prompt builders are exercised."""
    _, workflows = load_config()
    ctx = MagicMock()
    ctx.workflows = workflows
    ctx.repo_scope_line.return_value = "Scope — WRITABLE: repo-a; READ-ONLY: repo-b."
    return Engine(ctx, "development")


class TestReviewerContext(unittest.TestCase):
    def test_code_reviewer_gets_the_approved_architecture(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "code", arch_path="/run/arch.md")
        self.assertIn("/run/arch.md", p)
        self.assertIn("Approved architecture", p)
        # And is told non-conformance is HIS blocker, not only the Architect's.
        self.assertIn("BLOCKING", p)

    def test_architect_still_gets_the_approved_architecture(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "arch", arch_path="/run/arch.md")
        self.assertIn("/run/arch.md", p)

    def test_no_architecture_block_when_none_supplied(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "code")
        self.assertNotIn("Approved architecture", p)

    def test_prior_blockers_are_replayed_to_the_reviewer(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "code",
                             history=["### Iteration 1 — blocked by architect\nGATE IS DEFEATABLE\n"])
        self.assertIn("GATE IS DEFEATABLE", p)
        self.assertIn("Blockers raised earlier in THIS run", p)
        # The reviewer is told to verify the fix, not just re-read the diff.
        self.assertIn("ACTUALLY fixed", p)

    def test_history_block_absent_on_the_first_iteration(self):
        e = _engine()
        self.assertNotIn("Blockers raised earlier", e._review_prompt("t", "d", "s", "code"))

    def test_history_is_bounded(self):
        """A long run must not grow the reviewer prompt without limit."""
        e = _engine()
        hist = [f"### Iteration {i} — blocked by architect\n{'x' * 5000}\n" for i in range(1, 9)]
        block = e._review_history_block(hist)
        self.assertLessEqual(len(block), e._REVIEW_HISTORY_CHARS + 2000)
        self.assertIn("Iteration 8", block)   # newest kept
        self.assertNotIn("Iteration 1", block)  # oldest dropped

    def test_techlead_prompt_attributes_each_review_to_its_real_agent(self):
        e = _engine()
        p = e._techlead_prompt("task", "diff", "summary", "CR TEXT", "AR TEXT")
        cr = e.ctx.workflows["development"]["code_reviewer"]["agent"]
        ar = e.ctx.workflows["development"]["architect"]["agent"]
        self.assertIn(f"## Code Reviewer ({cr}) review", p)
        self.assertIn(f"## Architect ({ar}) review", p)


class TestReviewerMissTelemetry(unittest.TestCase):
    def test_miss_is_logged_and_counted(self):
        e = _engine()
        e._reviewer_miss("architect", "NEEDS_FIX", "the gate is defeatable", 2, "development")
        kw = e.ctx.tx.event.call_args.kwargs
        self.assertEqual(kw["kind"], "reviewer_miss")
        self.assertEqual(kw["gate"], "architect")
        self.assertEqual(kw["gate_verdict"], "NEEDS_FIX")
        self.assertEqual(kw["iteration"], 2)
        self.assertIn("defeatable", kw["blocker"])
        self.assertEqual(e._miss_count, 1)
        e._reviewer_miss("tech-lead", "DEVELOPER_NEEDS_FIX", "x", 3, "development")
        self.assertEqual(e._miss_count, 2)

    def test_blocker_text_is_truncated(self):
        e = _engine()
        e._reviewer_miss("architect", "NEEDS_FIX", "y" * 9000, 1, "development")
        self.assertLessEqual(len(e.ctx.tx.event.call_args.kwargs["blocker"]), 2000)

    def test_every_development_run_reports_a_miss_summary(self):
        e = _engine()
        e._finish("READY_FOR_HUMAN_REVIEW", "done")
        kinds = [c.kwargs.get("kind") for c in e.ctx.tx.event.call_args_list]
        self.assertIn("reviewer_miss_summary", kinds)
        summary = [c.kwargs for c in e.ctx.tx.event.call_args_list
                   if c.kwargs.get("kind") == "reviewer_miss_summary"][0]
        self.assertEqual(summary["misses"], 0)

    def test_miss_summary_counts_the_misses(self):
        e = _engine()
        e._reviewer_miss("architect", "NEEDS_FIX", "a", 1, "development")
        e._reviewer_miss("architect", "NEEDS_FIX", "b", 2, "development")
        e._finish("READY_FOR_HUMAN_REVIEW", "done")
        summary = [c.kwargs for c in e.ctx.tx.event.call_args_list
                   if c.kwargs.get("kind") == "reviewer_miss_summary"][0]
        self.assertEqual(summary["misses"], 2)

    def test_summary_is_written_once(self):
        e = _engine()
        e._finish("HUMAN_REQUIRED", "one")
        e._finish("HUMAN_REQUIRED", "two")
        n = sum(1 for c in e.ctx.tx.event.call_args_list
                if c.kwargs.get("kind") == "reviewer_miss_summary")
        self.assertEqual(n, 1)

    def test_non_development_workflows_emit_no_summary(self):
        _, workflows = load_config()
        ctx = MagicMock()
        ctx.workflows = workflows
        e = Engine(ctx, "investigation")
        e._finish("ROOT_CAUSE_ESTABLISHED", "done")
        kinds = [c.kwargs.get("kind") for c in ctx.tx.event.call_args_list]
        self.assertNotIn("reviewer_miss_summary", kinds)


class TestDisabledGate(unittest.TestCase):
    """A review cell with "enabled": false is skipped, without disturbing anything else.

    Disabled 2026-08-09 for code_reviewer (Irina). The cell deliberately STAYS in the
    roster: roster.assign() fills cells positionally from the chain, so dropping it from
    roster.cells would shift architect/tech_lead/arbiter onto different agents.
    """

    def test_code_review_cell_is_disabled_in_config(self):
        _, workflows = load_config()
        self.assertIs(workflows["development"]["code_reviewer"].get("enabled"), False)

    def test_gate_enabled_reads_the_flag(self):
        e = _engine()
        self.assertFalse(e._gate_enabled("code_reviewer"))
        self.assertTrue(e._gate_enabled("architect"))
        self.assertTrue(e._gate_enabled("tech_lead"))

    def test_disabled_cell_stays_in_the_roster(self):
        """Otherwise the positional chain silently reassigns the cells after it."""
        _, workflows = load_config()
        dev = workflows["development"]
        self.assertIn("code_reviewer", dev["roster"]["cells"])
        # And the cells after it still line up with the chain positions they had.
        self.assertEqual(dev["roster"]["cells"],
                         ["developer", "code_reviewer", "architect", "tech_lead", "arbiter"])

    def test_techlead_prompt_states_no_code_review_ran(self):
        e = _engine()
        p = e._techlead_prompt("task", "diff", "summary", None, "AR TEXT")
        self.assertIn("code-review gate is disabled", p)
        self.assertIn("do not assume one", p)
        # The Tech Lead must be told implementation risk is now its job too.
        self.assertIn("no code review ran", p)
        self.assertNotIn("Review the reviews per your Tech Lead role", p)

    def test_techlead_prompt_unchanged_when_a_review_exists(self):
        e = _engine()
        p = e._techlead_prompt("task", "diff", "summary", "CR TEXT", "AR TEXT")
        self.assertIn("CR TEXT", p)
        self.assertIn("Review the reviews per your Tech Lead role", p)
        self.assertNotIn("disabled", p)

    def test_miss_summary_reports_the_gate_is_off(self):
        e = _engine()
        e._finish("READY_FOR_HUMAN_REVIEW", "done")
        summary = [c.kwargs for c in e.ctx.tx.event.call_args_list
                   if c.kwargs.get("kind") == "reviewer_miss_summary"][0]
        self.assertIs(summary["code_review_enabled"], False)

    def test_architect_block_is_not_a_miss_when_no_gate_preceded_it(self):
        """With stage 1 off the Architect IS the first gate — blocking is its job, not a miss."""
        from orchestrator.adapters import OK, AgentResponse

        e = _engine()
        calls = []

        def fake_call(step_id, spec, prompt, **kw):
            calls.append(step_id)
            return AgentResponse("x", OK, text="blocked"), "NEEDS_FIX"

        e.call = fake_call
        verdict, _fb, gate = e._dev_staged_review("t", "d", "s", "development", 1)
        self.assertEqual((verdict, gate), ("DEVELOPER_NEEDS_FIX", "architect"))
        self.assertNotIn("dev.code_review", calls)   # stage 1 never invoked
        self.assertEqual(getattr(e, "_miss_count", 0), 0)


class TestGatesReport(unittest.TestCase):
    """`orchestrate.py gates` reconstructs miss rate from the step journal, so it works
    on runs recorded before the reviewer_miss event existed."""

    def _run_dir(self, tmp, name, rows, mock=False):
        d = Path(tmp) / name
        (d / "iterations").mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"workflow": "development", "mock": mock}),
                                    encoding="utf-8")
        with open(d / "events.jsonl", "w", encoding="utf-8") as f:
            for it, row in rows:
                for step, verdict in row.items():
                    f.write(json.dumps({"kind": "step", "step": step, "verdict": verdict,
                                        "iteration": it, "phase": "development"}) + "\n")
        return d

    def test_rows_are_grouped_by_iteration(self):
        import tempfile
        import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            d = self._run_dir(tmp, "20260101T000000Z-development-aaaa", [
                (1, {"dev.code_review": "PASS", "dev.architect": "NEEDS_FIX"}),
                (2, {"dev.code_review": "PASS", "dev.architect": "PASS",
                     "dev.tech_lead": "READY_FOR_HUMAN_REVIEW"}),
            ])
            rows = orchestrate._dev_run_gate_rows(d)
            self.assertEqual(rows[1]["dev.architect"], "NEEDS_FIX")
            self.assertEqual(len(rows), 2)

    def test_miss_counted_only_when_reviewer_passed_first(self):
        import argparse
        import io
        import contextlib
        import tempfile
        import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            # i1: reviewer PASS, architect blocks   -> a miss
            # i2: reviewer BLOCKS                   -> not a miss (it did its job)
            # i3: everything passes                 -> not a miss
            self._run_dir(tmp, "20260101T000000Z-development-aaaa", [
                (1, {"dev.code_review": "PASS", "dev.architect": "NEEDS_FIX"}),
                (2, {"dev.code_review": "NEEDS_FIX"}),
                (3, {"dev.code_review": "PASS", "dev.architect": "PASS",
                     "dev.tech_lead": "READY_FOR_HUMAN_REVIEW"}),
            ])
            old, orchestrate.RUNS_DIR = orchestrate.RUNS_DIR, Path(tmp)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    orchestrate.cmd_gates(argparse.Namespace(since=None, last=None, top=10))
                out = buf.getvalue()
            finally:
                orchestrate.RUNS_DIR = old
        self.assertIn("misses: 1 of 2 passed iterations (50%)", out)
        self.assertIn("dev.code_review", out)

    def test_mock_runs_are_excluded(self):
        """A scripted fixture run must not move the miss rate."""
        import argparse
        import io
        import contextlib
        import tempfile
        import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            self._run_dir(tmp, "20260101T000000Z-development-real", [
                (1, {"dev.code_review": "PASS", "dev.architect": "NEEDS_FIX"}),
            ])
            self._run_dir(tmp, "20260102T000000Z-development-mock", [
                (1, {"dev.code_review": "PASS", "dev.architect": "PASS",
                     "dev.tech_lead": "READY_FOR_HUMAN_REVIEW"}),
            ], mock=True)
            old, orchestrate.RUNS_DIR = orchestrate.RUNS_DIR, Path(tmp)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    orchestrate.cmd_gates(argparse.Namespace(since=None, last=None, top=10))
                out = buf.getvalue()
            finally:
                orchestrate.RUNS_DIR = old
        self.assertIn("over 1 development runs", out)
        self.assertIn("misses: 1 of 1 passed iterations (100%)", out)

    def test_escalate_is_not_a_block(self):
        """ESCALATE_TO_TECH_LEAD hands the diff on rather than rejecting it."""
        import argparse
        import io
        import contextlib
        import tempfile
        import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            self._run_dir(tmp, "20260101T000000Z-development-bbbb", [
                (1, {"dev.code_review": "PASS", "dev.architect": "ESCALATE_TO_TECH_LEAD",
                     "dev.tech_lead": "READY_FOR_HUMAN_REVIEW"}),
            ])
            old, orchestrate.RUNS_DIR = orchestrate.RUNS_DIR, Path(tmp)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    orchestrate.cmd_gates(argparse.Namespace(since=None, last=None, top=10))
                out = buf.getvalue()
            finally:
                orchestrate.RUNS_DIR = old
        self.assertIn("misses: 0 of 1 passed iterations (0%)", out)


class TestGlobalPolicy(unittest.TestCase):
    """The English-only + workspace-isolation policy reaches every role, last."""

    def setUp(self):
        agents, workflows = load_config()
        self.ctx = RunContext(agents, workflows, {}, MagicMock(), MagicMock())

    def test_policy_is_appended_to_every_configured_role(self):
        for role, present in self.ctx.roles_present().items():
            if not present:
                continue
            with self.subTest(role=role):
                self.assertIn(GLOBAL_POLICY, self.ctx.load_role(role))

    def test_policy_comes_last_so_it_wins(self):
        role = next(r for r, ok in self.ctx.roles_present().items() if ok)
        self.assertTrue(self.ctx.load_role(role).endswith(GLOBAL_POLICY))

    def test_policy_survives_a_wrapper(self):
        wrapper = "architecture_author.md"
        self.assertTrue((ORCH / "prompts" / wrapper).exists(), "wrapper fixture missing")
        text = self.ctx.load_role("System Architect", wrapper)
        self.assertTrue(text.endswith(GLOBAL_POLICY))

    def test_policy_states_both_rules(self):
        self.assertIn("ENGLISH", GLOBAL_POLICY)
        self.assertIn("Workspace isolation", GLOBAL_POLICY)
        # The isolation rule must name the sibling-directory case explicitly.
        self.assertIn("BESIDE", GLOBAL_POLICY)


if __name__ == "__main__":
    unittest.main()
