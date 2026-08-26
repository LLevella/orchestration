"""Review-context regressions for the development chain (Irina 2026-08-09).

Two properties, each pinned against the defect that motivated it:

  1. A re-review carries the blockers earlier gates raised in this run. Without them a
     later iteration re-read the same diff blind and passed the part it had passed
     before - 32 of 33 re-reviews after a NEEDS_FIX came back PASS. The Tech Lead's
     review header also names the agent that actually wrote the review it quotes.
  2. Every role's system prompt carries the global language + workspace-isolation policy.

NOTE: the peer code-review stage this file originally also covered (its miss telemetry
and the `gates` report over it) does not exist in this copy - the stage was removed on
2026-08-17, so nothing here measures it.

These assert on the real prompt builders, not on a model's answer.
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
    def test_architect_still_gets_the_approved_architecture(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "arch", arch_path="/run/arch.md")
        self.assertIn("/run/arch.md", p)

    def test_no_architecture_block_when_none_supplied(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "arch")
        self.assertNotIn("Approved architecture", p)

    def test_prior_blockers_are_replayed_to_the_reviewer(self):
        e = _engine()
        p = e._review_prompt("task", "diff", "summary", "arch",
                             history=["### Iteration 1 — blocked by architect\nGATE IS DEFEATABLE\n"])
        self.assertIn("GATE IS DEFEATABLE", p)
        self.assertIn("Blockers raised earlier in THIS run", p)
        # The reviewer is told to verify the fix, not just re-read the diff.
        self.assertIn("ACTUALLY fixed", p)

    def test_history_block_absent_on_the_first_iteration(self):
        e = _engine()
        self.assertNotIn("Blockers raised earlier", e._review_prompt("t", "d", "s", "arch"))

    def test_history_is_bounded(self):
        """A long run must not grow the reviewer prompt without limit."""
        e = _engine()
        hist = [f"### Iteration {i} — blocked by architect\n{'x' * 5000}\n" for i in range(1, 9)]
        block = e._review_history_block(hist)
        self.assertLessEqual(len(block), e._REVIEW_HISTORY_CHARS + 2000)
        self.assertIn("Iteration 8", block)   # newest kept
        self.assertNotIn("Iteration 1", block)  # oldest dropped

    def test_techlead_prompt_attributes_the_review_to_its_real_agent(self):
        """The header used to say "(Codex)" unconditionally, which misattributes the
        review whenever the roster shifted the architect cell to another vendor."""
        e = _engine()
        p = e._techlead_prompt("task", "diff", "summary", "AR TEXT")
        ar = e.ctx.workflows["development"]["architect"]["agent"]
        self.assertIn(f"## Architect ({ar}) review", p)


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
