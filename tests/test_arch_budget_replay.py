"""The architecture budget must charge LIVE iterations only, so a paused run stays
resumable for as many answers as a human has to give.

Why this exists: run 20260830T014616Z-architecture-fe5e replayed 28 journaled steps,
metered 0 tokens and $0.00, and paused again with the human's ruling undelivered — the
design it was resuming could never be finished. The budget was counted over EVERY
iteration, replayed ones included, so the SECOND resume reached the boundary on history
alone; the one-shot `--human-answer` allowance was granted there and applied to the next
iteration, which was itself replayed from the journal. The ruling never reached a prompt.

These tests drive the real `_run_architecture` loop over a journal left behind by an
earlier resume: the first pins that a further resume reaches LIVE steps and delivers the
answer, the second pins that the budget still bounds the live work it now counts, and the
third pins the accounting rule itself — an iteration is charged when ANY of its steps runs
live, so a journal that ends INSIDE an iteration cannot buy that iteration for free.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters import OK, AgentResponse
from orchestrator.engine import Engine
from orchestrator.transcript import Transcript

ANSWER = "Bound the ring to 4 entries."
RULING_MARK = "RULING APPLIED"

# A small workflow: 3 live author iterations + a 1-iteration human grant. Escalation is
# switched off (99) so the test measures the budget, not the arbiter tiers.
WORKFLOW = {
    "architecture": {
        "max_iterations": 3,
        "escalate_after_cycles": 99,
        "techlead_reject_before_arbiter": 99,
        "human_resume_iterations": 1,
        "arbitration": {"max_iterations": 10, "trigger_after_cycles": 99},
        "author": {"agent": "author", "role": "System Architect"},
        "developer_verifier": {"agent": "verifier", "role": "Developer Verifier"},
        "tech_lead": {"agent": "lead", "role": "Technical Lead"},
        "arbiter": {"agent": "arbiter", "role": "Arbitration Reviewer"},
    }
}


class _Guard:
    def branches(self):
        return None

    def snapshot(self):
        return None

    def assert_branch_unchanged(self, *a):
        return None

    def assert_unchanged(self, *a):
        return None


class _Agent:
    """A scripted agent that records every prompt it is REALLY sent."""

    def __init__(self, name, reply):
        self.name = name
        self._reply = reply
        self.prompts: list[str] = []

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        self.prompts.append(prompt)
        return AgentResponse(self.name, OK, text=self._reply(prompt, len(self.prompts)))

    @property
    def calls(self):
        return len(self.prompts)


def _author(prompt, n):
    # The draft echoes a binding ruling it was actually given, so a downstream
    # assertion can tell "the human's answer reached a prompt" from "it evaporated".
    mark = f"\n{RULING_MARK}: {ANSWER}" if ANSWER in prompt else ""
    return f"# Draft (live author call {n}){mark}"


def _verifier(feasible_on_ruling):
    def reply(prompt, n):
        ok = feasible_on_ruling and RULING_MARK in prompt
        return ("## Implementation Feasibility Verdict\n"
                + ("FEASIBLE" if ok else "NEEDS_REVISION") + "\n")
    return reply


class _Ctx:
    """Only what `_run_architecture` and the live path of Engine.call touch."""

    def __init__(self, tx, adapters, cache, human_answer):
        self.tx = tx
        self.adapters = adapters
        self.guard = _Guard()
        self.workflows = WORKFLOW
        self.resume_cache = dict(cache)
        self.human_answer = human_answer
        self.live_calls = 0
        self.last_call_replayed = False
        self.last_call_final_replay = False

    def resume_lookup(self, step_id, phase, iteration):
        if not self.resume_cache:
            return None
        return self.resume_cache.pop((step_id, phase, iteration), None)

    def load_role(self, role, wrapper=None):
        return f"system prompt for {role}"

    def read_existing_arch(self):
        return "(no existing architecture docs)"


def _journal_of_a_granted_resume():
    """The journal a run leaves behind AFTER its human grant has been used: the three
    budgeted iterations, the granted fourth one, and the arbiter ruling the run paused
    on ("iteration budget exhausted after the arbiter's ruling"). Resuming THIS with a
    fresh answer is the case that used to be a dead end."""
    journal = {}
    for it in (1, 2, 3, 4):
        journal[("arch.author", "architecture", it)] = {
            "agent": "author", "role": "System Architect", "verdict": None,
            "text": f"# Draft (journaled iteration {it})"}
        journal[("arch.dev_review", "architecture", it)] = {
            "agent": "verifier", "role": "Developer Verifier", "verdict": "NEEDS_REVISION",
            "text": "## Implementation Feasibility Verdict\nNEEDS_REVISION\n"}
    journal[("arch.arbiter", "arbitration", 4)] = {
        "agent": "arbiter", "role": "Arbitration Reviewer", "verdict": "NEEDS_SMALLER_FIX",
        "text": "## Arbitration Verdict\nNEEDS_SMALLER_FIX\n"}
    return journal


def _run_arch(journal, human_answer, feasible_on_ruling):
    """Run the real architecture loop against `journal`; return (status, agents)."""
    agents = {
        "author": _Agent("author", _author),
        "verifier": _Agent("verifier", _verifier(feasible_on_ruling)),
        "lead": _Agent("lead", lambda p, n: "## Tech Lead Verdict\nREADY_FOR_HUMAN_REVIEW\n"),
        "arbiter": _Agent("arbiter", lambda p, n: "## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        tx = Transcript(Path(tmp), "run-resume-2", "architecture", "task", False, None, {})
        ctx = _Ctx(tx, agents, journal, human_answer)
        status = Engine(ctx, "architecture")._run_architecture("task")
    return status, agents


class TestArchBudgetChargesLiveWorkOnly(unittest.TestCase):
    def _run(self, feasible_on_ruling):
        return _run_arch(_journal_of_a_granted_resume(), ANSWER, feasible_on_ruling)

    def test_a_second_resume_reaches_live_steps_and_delivers_the_ruling(self):
        # The dead end: 28 steps replayed, nothing sent, paused again. The replayed
        # journal must cost no budget, the run must go live past it, and the human's
        # answer must land in a prompt that is really sent.
        status, agents = self._run(feasible_on_ruling=True)
        self.assertEqual(status, "PASS", status)
        live_with_ruling = [p for p in agents["author"].prompts if ANSWER in p]
        self.assertEqual(len(live_with_ruling), 1, agents["author"].prompts)
        # 3 budgeted live iterations + the 1 the human grant bought; the 4 journaled
        # iterations were replayed and re-invoked nothing.
        self.assertEqual(agents["author"].calls, 4)
        self.assertEqual(agents["verifier"].calls, 4)
        self.assertEqual(agents["arbiter"].calls, 0)   # the answer outranks the arbiter

    def test_the_budget_still_bounds_the_live_work_it_counts(self):
        # Replay being free is not an escape hatch: a dispute that never converges still
        # stops after `max_iterations` live iterations plus the ONE bounded human grant.
        status, agents = self._run(feasible_on_ruling=False)
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["author"].calls, 4)    # 3 + human_resume_iterations, once
        self.assertEqual(agents["arbiter"].calls, 1)   # one last arbiter, then the human


def _journal_ending_inside_an_iteration():
    """A run interrupted mid-iteration: iteration 1 is journaled whole, iteration 2 only
    as far as its author. Resuming this replays the author of iteration 2 and then runs
    its developer review — and every step after it — LIVE."""
    journal = {
        ("arch.author", "architecture", 1): {
            "agent": "author", "role": "System Architect", "verdict": None,
            "text": "# Draft (journaled iteration 1)"},
        ("arch.dev_review", "architecture", 1): {
            "agent": "verifier", "role": "Developer Verifier", "verdict": "NEEDS_REVISION",
            "text": "## Implementation Feasibility Verdict\nNEEDS_REVISION\n"},
        ("arch.author", "architecture", 2): {
            "agent": "author", "role": "System Architect", "verdict": None,
            "text": "# Draft (journaled iteration 2)"},
    }
    return journal


class TestPartiallyReplayedIterationIsCharged(unittest.TestCase):
    """The budget charges an ITERATION, not an author call: 'the author was replayed' is
    not the same claim as 'nothing in this iteration ran'. At the seam where the journal
    runs out, the iteration still sends live prompts and must pay for them, or the
    configured limit would silently allow one extra iteration of real work per resume."""

    def test_an_iteration_whose_author_replayed_but_whose_review_ran_live_costs_budget(self):
        status, agents = _run_arch(_journal_ending_inside_an_iteration(),
                                   human_answer=None, feasible_on_ruling=False)
        # No answer to apply → the boundary is the last arbiter, then the human.
        self.assertEqual(status, "AWAITING_HUMAN", status)
        # max_iterations is 3. Iteration 1 replayed end to end and is free. Iteration 2
        # replayed its author but ran its developer review live: real work, charged 1.
        # That leaves 2 charged iterations (3 and 4) with a live author — NOT 3, which is
        # what author-only accounting would have allowed.
        self.assertEqual(agents["author"].calls, 2, agents["author"].prompts)
        # The live steps of the part-replayed iteration really did happen: its developer
        # review, plus one per fully live iteration.
        self.assertEqual(agents["verifier"].calls, 3)
        self.assertEqual(agents["arbiter"].calls, 1)


if __name__ == "__main__":
    unittest.main()
