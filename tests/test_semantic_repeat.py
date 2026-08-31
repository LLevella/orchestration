"""ARCH-02: the triggered semantic repeat assessment (development run 1).

The escalation counters count consecutive VERDICTS, not repeated CONTENT. An objection
restated in different words therefore read as a fresh finding and bought another
iteration on a dispute that had already happened — 17 of 64 architecture runs in the
200-run corpus burned their whole budget that way. The mechanism under test asks the
EXISTING arbiter seat whether the current blockers are the earlier ones restated, and
routes on its ruling.

What these tests defend, in order of how expensive the regression would be:

  * the ordinary path pays NOTHING — no `semantic_audit` block and no repeat mean no
    semantic call at all, and no configuration error;
  * replay never invents a paid call and never re-pays for one it already made;
  * `escalate_after_cycles` keeps precedence, and an exhausted allowance leaves the
    existing route exactly as it was;
  * nothing unparseable, insufficient, contradictory or repaired-away can pass, reset a
    counter, or drop an active blocker.

They drive the real `_run_architecture` loop over scripted agents, so a route asserted
here is the route the engine really takes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters import OK, AgentResponse
from orchestrator.config import ConfigError, load_config
from orchestrator.engine import (SEMANTIC_PHASE, SEMANTIC_REPEAT_STEP, AgentError,
                                 Engine, _ArchSemanticState, _truncate_middle)
from orchestrator.transcript import Transcript
from orchestrator.verdicts import (SemanticVerdictContract, extract_blocking_findings,
                                   extract_semantic_assessment, semantic_marker)

MARKER = semantic_marker("REPEAT")
AUDIT = {"repeat_after_cycles": 2, "prior_objections": 3, "prior_assessments": 2,
         "repeat_max_prompt_chars": 48000, "gate_max_prompt_chars": 44000,
         "repair_max_prompt_chars": 56000, "max_calls_per_mode": 3}


def workflow(*, semantic_audit=AUDIT, max_iterations=10, escalate_after_cycles=5):
    wf = {
        "max_iterations": max_iterations,
        "escalate_after_cycles": escalate_after_cycles,
        "techlead_reject_before_arbiter": 5,
        "human_resume_iterations": 1,
        "arbitration": {"max_iterations": 10, "trigger_after_cycles": 99},
        "author": {"agent": "author", "role": "Architecture Author"},
        "developer_verifier": {"agent": "verifier", "role": "Developer Verifier"},
        "tech_lead": {"agent": "lead", "role": "Technical Lead"},
        "arbiter": {"agent": "arbiter", "role": "Arbitration Reviewer"},
    }
    if semantic_audit is not None:
        wf["semantic_audit"] = dict(semantic_audit)
    return {"architecture": wf}


# ---- scripted agents ------------------------------------------------------------
def nr(gap, n=1):
    """A NEEDS_REVISION carrying ONE structured blocking gap under `### B<n>`."""
    return ("## Implementation Feasibility Verdict\nNEEDS_REVISION\n\n"
            f"## Blocking Implementation Gaps\n\n### B{n}\n- {gap}\n\n"
            "## Notes for Architect\n- advisory, not compared\n")


FEASIBLE = "## Implementation Feasibility Verdict\nFEASIBLE\n\n## Blocking Implementation Gaps\n- None.\n"
READY = "## Tech Lead Verdict\nREADY_FOR_HUMAN_REVIEW\n\n## Control Review\n- Sound.\n"


def audit(token, current="NONE", prior="NONE", evidence="cited", ruling="NONE"):
    return (f"## Semantic Consistency Verdict\n{token}\n\n"
            f"## Current Finding IDs\n{current}\n\n"
            f"## Matched Prior Finding IDs\n{prior}\n\n"
            f"## Cited Evidence\n{evidence}\n\n"
            f"## Binding Ruling\n{ruling}\n")


def same(current, prior, token="SAME_OBJECTION_REVIEWER_RIGHT", ruling="Bound the ring."):
    return audit(token, current, prior, f"{current} restates {prior}.", ruling)


class _Agent:
    """A scripted agent that records every prompt it is REALLY sent."""

    def __init__(self, name, replies):
        self.name = name
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _next(self, prompts):
        if not self.replies:
            return ""
        return self.replies[min(len(prompts) - 1, len(self.replies) - 1)]

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        self.prompts.append(prompt)
        return AgentResponse(self.name, OK, text=self._next(self.prompts))

    @property
    def calls(self):
        return len(self.prompts)


class _Arbiter:
    """ONE seat, two contracts — exactly like the real `architecture.arbiter` cell.
    Semantic prompts are the ones carrying the exact mode marker on their first line;
    everything else is ordinary arbitration."""

    def __init__(self, semantic=(), arbitration=()):
        self.name = "arbiter"
        self.semantic, self.arbitration = list(semantic), list(arbitration)
        self.semantic_prompts: list[str] = []
        self.arbitration_prompts: list[str] = []
        self.status = OK

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        if prompt.startswith(MARKER):
            self.semantic_prompts.append(prompt)
            queue, seen = self.semantic, self.semantic_prompts
        else:
            self.arbitration_prompts.append(prompt)
            queue, seen = self.arbitration, self.arbitration_prompts
        text = queue[min(len(seen) - 1, len(queue) - 1)] if queue else ""
        return AgentResponse(self.name, self.status, text=text,
                             error=None if self.status == OK else "provider is gone")

    @property
    def audits(self):
        """Logical audits, not invocations: a repair re-invokes the same seat."""
        return len([p for p in self.semantic_prompts if "STOP —" not in p])


class _Guard:
    def branches(self):
        return None

    def snapshot(self):
        return None

    def assert_branch_unchanged(self, *a):
        return None

    def assert_unchanged(self, *a):
        return None


class _Ctx:
    """Only what `_run_architecture` and the live path of Engine.call touch."""

    def __init__(self, tx, adapters, workflows, cache=None, human_answer=None):
        self.tx = tx
        self.adapters = adapters
        self.guard = _Guard()
        self.workflows = workflows
        self.resume_cache = dict(cache or {})
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


def journaled(step, phase, iteration, text, verdict=None, agent="x", role="r"):
    return {(step, phase, iteration): {"agent": agent, "role": role,
                                       "verdict": verdict, "text": text}}


def run_arch(*, verifier, semantic=(), arbitration=(), author=("# Draft",),
             lead=(READY,), wf=None, cache=None, human_answer=None):
    """Drive the real architecture loop; return (status, engine, agents)."""
    agents = {"author": _Agent("author", author),
              "verifier": _Agent("verifier", verifier),
              "lead": _Agent("lead", lead),
              "arbiter": _Arbiter(semantic, arbitration)}
    with tempfile.TemporaryDirectory() as tmp:
        tx = Transcript(Path(tmp), "run-sem", "architecture", "task", False, None, {})
        ctx = _Ctx(tx, agents, wf or workflow(), cache, human_answer)
        engine = Engine(ctx, "architecture")
        status = engine._run_architecture("the task and its acceptance criteria")
        artifacts = sorted(p.name for p in (tx.dir / "artifacts").glob("*"))
    return status, engine, agents, artifacts


# =================================================================================
class TestTheOrdinaryPathPaysNothing(unittest.TestCase):
    def test_a_workflow_without_semantic_audit_makes_no_semantic_call(self):
        # Absent block = disabled. A hand-built workflow (and every project layer that
        # predates ARCH-02) must load and run without paying for an audit it never asked
        # for — and without a configuration error, which would be worse than the cost.
        status, engine, agents, _ = run_arch(
            verifier=[nr("same gap")] * 4 + [FEASIBLE],
            wf=workflow(semantic_audit=None))
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].semantic_prompts, [])
        self.assertFalse(engine._sem.enabled)

    def test_every_other_shipped_workflow_declares_no_semantic_audit(self):
        # The capability is architecture-only; development/investigation/review/
        # implreview/mrreview behaviour must be bit-for-bit what it was.
        _agents, wf = load_config()
        for name in ("development", "investigation", "review", "implreview", "mrreview"):
            with self.subTest(workflow=name):
                self.assertNotIn("semantic_audit", wf[name])

    def test_a_first_pass_feasible_run_makes_no_semantic_call(self):
        status, _e, agents, _ = run_arch(verifier=[FEASIBLE])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].semantic_prompts, [])
        self.assertEqual(agents["arbiter"].arbitration_prompts, [])

    def test_the_first_needs_revision_is_below_the_trigger(self):
        # repeat_after_cycles is 2: one objection cannot be a repeat of anything.
        status, _e, agents, _ = run_arch(verifier=[nr("gap 1"), FEASIBLE])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits, 0)


class TestConfigValidation(unittest.TestCase):
    def test_the_shipped_architecture_block_is_accepted_and_complete(self):
        _agents, wf = load_config()
        self.assertEqual(set(wf["architecture"]["semantic_audit"]), set(AUDIT))

    def test_a_non_positive_bound_is_rejected(self):
        from orchestrator.config import _validate_semantic_audit
        for bad in (0, -1, "3", True, None):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    _validate_semantic_audit("architecture",
                                             {"semantic_audit": {"max_calls_per_mode": bad}})

    def test_an_unknown_key_is_rejected(self):
        from orchestrator.config import _validate_semantic_audit
        with self.assertRaises(ConfigError):
            _validate_semantic_audit("architecture", {"semantic_audit": {"agent": "codex"}})

    def test_a_missing_block_is_not_an_error(self):
        from orchestrator.config import _validate_semantic_audit
        self.assertIsNone(_validate_semantic_audit("architecture", {}))


class TestBlockingFindingExtraction(unittest.TestCase):
    def test_structured_gaps_get_stable_ids(self):
        text = nr("first", 1).replace("## Notes for Architect",
                                      "### B2\n- second\n\n## Notes for Architect")
        got = extract_blocking_findings(text, source="DV", iteration=3,
                                        verdict="NEEDS_REVISION")
        self.assertEqual([f.finding_id for f in got], ["DV-3-B1", "DV-3-B2"])
        self.assertFalse(any(f.synthetic for f in got))

    def test_an_unstructured_report_collapses_to_one_synthetic_b0(self):
        legacy = ("## Implementation Feasibility Verdict\nNEEDS_REVISION\n\n"
                  "## Blocking Implementation Gaps\n- the buffer overflows\n")
        got = extract_blocking_findings(legacy, source="DV", iteration=2,
                                        verdict="NEEDS_REVISION")
        self.assertEqual([f.finding_id for f in got], ["DV-2-B0"])
        self.assertTrue(got[0].synthetic)
        self.assertIn("the buffer overflows", got[0].text)

    def test_a_duplicated_number_collapses_to_one_synthetic_b0(self):
        # Python must not guess where one objection ends and the next begins.
        text = nr("first", 1).replace("## Notes for Architect",
                                      "### B1\n- second\n\n## Notes for Architect")
        got = extract_blocking_findings(text, source="DV", iteration=4,
                                        verdict="NEEDS_REVISION")
        self.assertEqual([f.finding_id for f in got], ["DV-4-B0"])

    def test_a_missing_section_falls_back_to_the_whole_report(self):
        got = extract_blocking_findings("## Implementation Feasibility Verdict\n"
                                        "NEEDS_REVISION\n\nfree prose only\n",
                                        source="DV", iteration=1, verdict="NEEDS_REVISION")
        self.assertEqual(len(got), 1)
        self.assertIn("free prose only", got[0].text)

    def test_an_approval_carries_no_findings(self):
        self.assertEqual(extract_blocking_findings(FEASIBLE, source="DV", iteration=1,
                                                   verdict="FEASIBLE"), [])

    def test_advisory_notes_are_not_part_of_the_manifest(self):
        got = extract_blocking_findings(nr("real gap"), source="DV", iteration=1,
                                        verdict="NEEDS_REVISION")
        self.assertNotIn("advisory, not compared", got[0].text)


class TestSemanticParser(unittest.TestCase):
    contract = SemanticVerdictContract(mode="REPEAT",
                                       supplied_current_ids=frozenset({"DV-2-B1"}),
                                       supplied_prior_ids=frozenset({"DV-1-B1"}))

    def parse(self, text, contract=None):
        return extract_semantic_assessment(text, contract=contract or self.contract)

    def test_a_valid_same_objection_parses(self):
        a = self.parse(same("DV-2-B1", "DV-1-B1"))
        self.assertEqual(a.verdict, "SAME_OBJECTION_REVIEWER_RIGHT")
        self.assertEqual(a.current_ids, ("DV-2-B1",))
        self.assertEqual(a.matched_prior_ids, ("DV-1-B1",))
        self.assertTrue(a.binding_ruling)

    def test_a_token_in_prose_is_not_a_ruling(self):
        self.assertIsNone(self.parse("I considered SAME_OBJECTION_REVIEWER_RIGHT but no."))

    def test_an_unknown_token_is_unparseable(self):
        self.assertIsNone(self.parse(audit("APPROVED", "DV-2-B1", "DV-1-B1")))

    def test_an_id_that_was_never_supplied_is_unparseable(self):
        self.assertIsNone(self.parse(same("DV-9-B1", "DV-1-B1")))
        self.assertIsNone(self.parse(same("DV-2-B1", "DV-9-B1")))

    def test_incomplete_coverage_is_unparseable(self):
        # One uncovered current blocker means the reviewer is asking something NEW.
        c = SemanticVerdictContract(mode="REPEAT",
                                    supplied_current_ids=frozenset({"DV-2-B1", "DV-2-B2"}),
                                    supplied_prior_ids=frozenset({"DV-1-B1"}))
        self.assertIsNone(self.parse(same("DV-2-B1", "DV-1-B1"), c))

    def test_a_missing_ruling_is_unparseable(self):
        self.assertIsNone(self.parse(audit("SAME_OBJECTION_AUTHOR_RIGHT", "DV-2-B1",
                                           "DV-1-B1", "cited", "NONE")))

    def test_missing_evidence_is_unparseable(self):
        self.assertIsNone(self.parse(audit("SAME_OBJECTION_AUTHOR_RIGHT", "DV-2-B1",
                                           "DV-1-B1", "NONE", "do it")))

    def test_an_empty_prior_list_is_unparseable_for_a_same_objection(self):
        self.assertIsNone(self.parse(audit("SAME_OBJECTION_REVIEWER_RIGHT", "DV-2-B1",
                                           "NONE", "cited", "ruling")))

    def test_distinct_needs_no_ids_or_ruling(self):
        a = self.parse(audit("DISTINCT_OR_MATERIALLY_PROGRESSING"))
        self.assertEqual(a.verdict, "DISTINCT_OR_MATERIALLY_PROGRESSING")

    def test_the_last_block_wins_so_an_appended_repair_is_the_ruling(self):
        text = (audit("INSUFFICIENT_INFORMATION")
                + "\n----- semantic repair (attempt 1) -----\n"
                + same("DV-2-B1", "DV-1-B1"))
        self.assertEqual(self.parse(text).verdict, "SAME_OBJECTION_REVIEWER_RIGHT")

    def test_an_unknown_mode_is_unparseable(self):
        c = SemanticVerdictContract(mode="NO_SUCH_MODE")
        self.assertIsNone(self.parse(audit("DISTINCT_OR_MATERIALLY_PROGRESSING"), c))

    def test_empty_text_is_unparseable(self):
        self.assertIsNone(self.parse(""))


class TestPromptBounds(unittest.TestCase):
    def _engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-b", "architecture", "task", False, None, {})
            ctx = _Ctx(tx, {}, workflow())
            engine = Engine(ctx, "architecture")
            tx.finish("PASS", "prompt-shape test only")   # release the journal handle
        engine._sem = _ArchSemanticState(engine._semantic_cfg())
        return engine

    def _manifest(self, iteration, size):
        return extract_blocking_findings(nr("X" * size), source="DV",
                                         iteration=iteration, verdict="NEEDS_REVISION")

    def test_the_dynamic_evidence_never_exceeds_the_configured_bound(self):
        engine = self._engine()
        cap = AUDIT["repeat_max_prompt_chars"]
        priors = [self._manifest(i, 40000) for i in range(1, 8)]
        records = [{"assessment": extract_semantic_assessment(
                        audit("DISTINCT_OR_MATERIALLY_PROGRESSING"),
                        contract=SemanticVerdictContract(mode="REPEAT")),
                    "text": "Y" * 30000, "iteration": i} for i in range(1, 6)]
        text, used = engine._repeat_evidence("T" * 40000, self._manifest(9, 40000),
                                             priors, "D" * 40000, records, cap)
        self.assertLessEqual(used, cap)
        self.assertIn("[TRUNCATED]", text)

    def test_only_the_configured_number_of_prior_items_is_supplied(self):
        engine = self._engine()
        priors = [self._manifest(i, 20) for i in range(1, 8)]
        records = [{"assessment": extract_semantic_assessment(
                        audit("DISTINCT_OR_MATERIALLY_PROGRESSING"),
                        contract=SemanticVerdictContract(mode="REPEAT")),
                    "text": f"assessment {i}", "iteration": i} for i in range(1, 6)]
        text, _used = engine._repeat_evidence("task", self._manifest(9, 20), priors,
                                              "delta", records,
                                              AUDIT["repeat_max_prompt_chars"])
        self.assertEqual(text.count("Prior Developer-Verifier blocking manifest -"),
                         AUDIT["prior_objections"])
        self.assertEqual(text.count("Semantic consistency assessment — architecture"),
                         AUDIT["prior_assessments"])

    def test_the_prompt_opens_with_the_exact_mode_marker(self):
        engine = self._engine()
        prompt = engine._semantic_repeat_prompt("task", self._manifest(2, 20),
                                                [self._manifest(1, 20)], "", [])
        self.assertEqual(prompt.splitlines()[0], "[[ARCH_SEMANTIC_AUDIT_V1:REPEAT]]")

    def test_evidence_is_delimited_as_untrusted(self):
        engine = self._engine()
        prompt = engine._semantic_repeat_prompt("task", self._manifest(2, 20),
                                                [self._manifest(1, 20)], "", [])
        self.assertIn("UNTRUSTED EVIDENCE", prompt)

    def test_truncation_is_head_and_tail_and_stays_within_the_limit(self):
        out = _truncate_middle("A" * 500 + "B" * 500, 200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("B"))
        self.assertIn("[TRUNCATED]", out)


class TestRepeatRouting(unittest.TestCase):
    def test_a_paraphrased_second_blocker_triggers_the_audit(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("the footprint is unbounded"),
                      nr("no budget is stated for the footprint"), FEASIBLE],
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits, 1)
        self.assertTrue(agents["arbiter"].semantic_prompts[0].startswith(MARKER))

    def test_distinct_continues_without_resetting_the_streak_and_activates_the_manifest(self):
        # Three DISTINCT rulings must NOT keep the run alive past escalate_after_cycles:
        # a semantic assessment never resets nr_streak, so the existing threshold at 5
        # is still reached, on schedule.
        status, _e, agents, _ = run_arch(
            verifier=[nr(f"gap {i}") for i in range(1, 7)],
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", f"DV-{i}-B1")
                      for i in (2, 3, 4)],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["arbiter"].audits, 3)          # streaks 2, 3, 4
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)  # streak 5
        # Each author after a plain/distinct revision receives the current blockers.
        self.assertIn("## Active Gate Findings", agents["author"].prompts[1])
        self.assertIn("DV-1-B1", agents["author"].prompts[1])

    def test_plain_sub_threshold_needs_revision_attaches_all_current_findings(self):
        # No audit is eligible at streak 1, yet the manifest is still established: the
        # active-finding contract does not depend on an audit having run.
        _status, _e, agents, _ = run_arch(verifier=[nr("gap one"), FEASIBLE])
        self.assertIn("## Active Gate Findings", agents["author"].prompts[1])
        self.assertIn("### DV-1-B1", agents["author"].prompts[1])

    def test_the_manifest_is_one_shot(self):
        # Consumed by the immediately following author; the NEXT one gets its own.
        _status, _e, agents, _ = run_arch(
            verifier=[nr("gap one"), nr("gap two"), FEASIBLE],
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1")])
        self.assertIn("### DV-1-B1", agents["author"].prompts[1])
        self.assertNotIn("### DV-1-B1", agents["author"].prompts[2])
        self.assertIn("### DV-2-B1", agents["author"].prompts[2])


class TestSameObjectionGetsOneComplianceRevision(unittest.TestCase):
    def _run(self, token):
        return run_arch(
            verifier=[nr("footprint unbounded"), nr("footprint still unbounded"), FEASIBLE],
            semantic=[same("DV-2-B1", "DV-1-B1", token=token, ruling="State the budget.")])

    def test_every_same_objection_token_supplies_ruling_and_manifest_to_author(self):
        for token in ("SAME_OBJECTION_REVIEWER_RIGHT", "SAME_OBJECTION_AUTHOR_RIGHT",
                      "SAME_OBJECTION_BOTH_PARTIALLY_RIGHT"):
            with self.subTest(token=token):
                status, _e, agents, _ = self._run(token)
                self.assertEqual(status, "PASS", status)
                author = agents["author"].prompts[2]
                self.assertIn("Semantic consistency ruling", author)
                self.assertIn("State the budget.", author)
                self.assertIn("### DV-2-B1", author)

    def test_the_compliance_verifier_is_instructed_to_apply_the_ruling(self):
        for token in ("SAME_OBJECTION_REVIEWER_RIGHT", "SAME_OBJECTION_AUTHOR_RIGHT",
                      "SAME_OBJECTION_BOTH_PARTIALLY_RIGHT"):
            with self.subTest(token=token):
                _status, _e, agents, _ = self._run(token)
                review = agents["verifier"].prompts[2]
                self.assertIn("Binding semantic consistency ruling", review)
                self.assertIn("State the budget.", review)
                self.assertIn("do not repeat a blocker the ruling rejected",
                              review.lower())

    def test_the_ruling_is_spent_after_exactly_one_revision_and_review(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), nr("g3"), FEASIBLE],
            semantic=[same("DV-2-B1", "DV-1-B1"),
                      audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-3-B1")])
        self.assertEqual(status, "PASS", status)
        self.assertNotIn("Semantic consistency ruling", agents["author"].prompts[3])
        self.assertNotIn("Binding semantic consistency ruling", agents["verifier"].prompts[3])

    def test_a_ruled_case_repeated_after_compliance_invokes_the_normal_arbiter(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), nr("g3"), FEASIBLE],
            semantic=[same("DV-2-B1", "DV-1-B1"), same("DV-3-B1", "DV-2-B1")],
            arbitration=["## Arbitration Verdict\nREVIEWER_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits, 2)
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)
        # It happened at streak 3, well before escalate_after_cycles = 5.
        self.assertEqual(agents["verifier"].calls, 4)

    def test_a_different_ruling_side_invokes_the_arbiter_with_both_assessments(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), nr("g3"), FEASIBLE],
            semantic=[same("DV-2-B1", "DV-1-B1", token="SAME_OBJECTION_REVIEWER_RIGHT"),
                      same("DV-3-B1", "DV-2-B1", token="SAME_OBJECTION_AUTHOR_RIGHT")],
            arbitration=["## Arbitration Verdict\nBOTH_PARTIALLY_RIGHT\n\nSplit it.\n"])
        self.assertEqual(status, "PASS", status)
        history = agents["arbiter"].arbitration_prompts[0]
        self.assertIn("SAME_OBJECTION_REVIEWER_RIGHT", history)
        self.assertIn("SAME_OBJECTION_AUTHOR_RIGHT", history)

    def test_self_contradiction_invokes_the_arbiter_with_the_evidence(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("SELF_CONTRADICTION", "DV-2-B1", "DV-1-B1")],
            arbitration=["## Arbitration Verdict\nREVIEWER_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)
        self.assertIn("SELF_CONTRADICTION", agents["arbiter"].arbitration_prompts[0])


class TestRulingsSupersedeTheManifest(unittest.TestCase):
    def test_a_full_arbiter_ruling_clears_the_manifest_and_marks_the_review_historical(self):
        # The author is now complying with the ARBITER, not accepting the gate's
        # blockers. Presenting the gate report as live feedback would hand it two
        # binding instructions at once.
        _status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("NEEDS_HUMAN_DECISION", "DV-2-B1")],
            arbitration=["## Arbitration Verdict\nREVIEWER_IS_RIGHT\n\nBound the ring.\n"])
        after = agents["author"].prompts[2]
        self.assertIn("## Historical Gate Evidence (inactive)", after)
        self.assertNotIn("## Developer review to address", after)
        self.assertNotIn("## Active Gate Findings", after)
        self.assertIn("Bound the ring.", after)

    def test_a_human_ruling_clears_the_manifest_too(self):
        _status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("NEEDS_HUMAN_DECISION", "DV-2-B1")],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"],
            human_answer="Take the four-entry ring.")
        after = agents["author"].prompts[2]
        self.assertIn("## Historical Gate Evidence (inactive)", after)
        self.assertNotIn("## Active Gate Findings", after)
        self.assertIn("Take the four-entry ring.", after)

    def test_a_deferring_arbiter_leaves_the_manifest_pending(self):
        # Nothing was ruled, so nothing supersedes the findings: they are still what the
        # next author has to answer when the run is resumed.
        status, engine, _agents, artifacts = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("NEEDS_HUMAN_DECISION", "DV-2-B1")],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertIn("HUMAN_QUESTION.md", artifacts)
        self.assertEqual([f.finding_id for f in engine._sem.active_findings], ["DV-2-B1"])

    def test_the_threshold_route_also_leaves_a_pending_manifest_when_it_pauses(self):
        # Nothing about the manifest depends on an audit having run: the blockers of the
        # gate that triggered escalation are pending until a ruling supersedes them.
        status, engine, _agents, _art = run_arch(
            verifier=[nr(f"gap {i}") for i in range(1, 4)],
            wf=workflow(escalate_after_cycles=2),
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual([f.finding_id for f in engine._sem.active_findings], ["DV-2-B1"])

    def test_an_arbiter_that_dies_leaves_the_manifest_pending(self):
        agents = {"author": _Agent("author", ["# Draft"]),
                  "verifier": _Agent("verifier", [nr("g1"), nr("g2")]),
                  "lead": _Agent("lead", [READY]),
                  "arbiter": _Arbiter([audit("NEEDS_HUMAN_DECISION", "DV-2-B1")], ["boom"])}
        agents["arbiter"].status = "ERROR"
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-x", "architecture", "task", False, None, {})
            engine = Engine(_Ctx(tx, agents, workflow()), "architecture")
            try:
                with self.assertRaises(AgentError):
                    engine._run_architecture("task")
            finally:
                tx.finish("HUMAN_REQUIRED", "aborted in the test")
        self.assertEqual([f.finding_id for f in engine._sem.active_findings], ["DV-2-B1"])


class TestNonTerminatingFallbacks(unittest.TestCase):
    def test_insufficient_information_continues_through_the_existing_route(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("INSUFFICIENT_INFORMATION")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].arbitration_prompts, [])
        self.assertIn("### DV-2-B1", agents["author"].prompts[2])

    def test_two_failed_repairs_cannot_pass_and_fall_back_to_the_existing_route(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=["I could not decide, sorry."])
        self.assertEqual(status, "PASS", status)   # PASS came from FEASIBLE + Tech Lead
        # one primary invocation + two bounded repairs, and no ruling was created
        self.assertEqual(len(agents["arbiter"].semantic_prompts), 3)
        self.assertEqual(agents["arbiter"].arbitration_prompts, [])
        self.assertNotIn("Semantic consistency ruling", agents["author"].prompts[2])
        self.assertIn("### DV-2-B1", agents["author"].prompts[2])

    def test_an_invalid_assessment_triggers_a_semantic_specific_repair(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[same("DV-9-B1", "DV-1-B1"),          # an ID never supplied
                      same("DV-2-B1", "DV-1-B1")])
        self.assertEqual(status, "PASS", status)
        repair = agents["arbiter"].semantic_prompts[1]
        self.assertTrue(repair.startswith(MARKER))
        self.assertIn("STOP —", repair)
        self.assertIn("DV-2-B1", repair)               # the IDs it MAY name
        self.assertIn("Semantic Consistency Verdict", repair)
        self.assertIn("Semantic consistency ruling", agents["author"].prompts[2])


class TestThresholdPrecedenceAndAllowance(unittest.TestCase):
    def test_the_escalation_threshold_outranks_the_audit(self):
        # At escalate_after_cycles the run goes STRAIGHT to the full arbiter; no
        # semantic call is made for that streak, whatever it would have said.
        status, _e, agents, _ = run_arch(
            verifier=[nr(f"gap {i}") for i in range(1, 7)],
            wf=workflow(escalate_after_cycles=2),
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["arbiter"].audits, 0)
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)

    def test_a_human_required_gate_never_pays_for_an_audit(self):
        status, _e, agents, _ = run_arch(
            verifier=[nr("g1"),
                      "## Implementation Feasibility Verdict\nHUMAN_REQUIRED\n"],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["arbiter"].audits, 0)

    def test_allowance_exhaustion_preserves_the_existing_route(self):
        # Two audits allowed, five streaks of dispute: the third eligible streak makes no
        # call, the existing escalate_after_cycles route still fires at streak 5, and the
        # run neither pauses early nor passes.
        status, engine, agents, _ = run_arch(
            verifier=[nr(f"gap {i}") for i in range(1, 7)],
            wf=workflow(semantic_audit=dict(AUDIT, max_calls_per_mode=2)),
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", f"DV-{i}-B1")
                      for i in (2, 3)],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["arbiter"].audits, 2)
        self.assertEqual(engine._sem.calls["REPEAT"], 2)
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)

    def test_the_allowance_is_run_scoped_and_does_not_reset_with_the_streak(self):
        # FEASIBLE resets the streak (and the cases); it does not refund the calls.
        _status, engine, agents, _ = run_arch(
            verifier=[nr("g1"), nr("g2"), FEASIBLE],
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1")],
            lead=["## Tech Lead Verdict\nARCHITECT_REVIEW_NEEDS_REVISION\n\nTighten it.\n",
                  READY])
        self.assertEqual(engine._sem.calls["REPEAT"], 1)
        self.assertEqual(engine._sem.cases, [])
        self.assertEqual(engine._sem.prior_manifests, [])
        self.assertEqual(agents["arbiter"].audits, 1)


class TestReplayEligibility(unittest.TestCase):
    """A resume must not invent a paid call, and must not pay twice for one it made."""

    @staticmethod
    def _journal(*, through_iteration, author_only_last=False, semantic=None):
        j = {}
        for it in range(1, through_iteration + 1):
            j.update(journaled("arch.author", "architecture", it, f"# Draft {it}"))
            if author_only_last and it == through_iteration:
                continue
            j.update(journaled("arch.dev_review", "architecture", it,
                               nr(f"journaled gap {it}"), "NEEDS_REVISION"))
        if semantic:
            j.update(journaled(SEMANTIC_REPEAT_STEP, SEMANTIC_PHASE,
                               semantic[0], semantic[1], semantic[2]))
        return j

    def test_a_middle_of_journal_replay_with_no_cached_audit_makes_no_live_call(self):
        # A pre-ARCH-02 journal must not acquire audits it never made when replayed.
        cache = self._journal(through_iteration=3, author_only_last=True)
        status, _e, agents, _ = run_arch(verifier=[FEASIBLE], cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits, 0)
        self.assertEqual(agents["author"].calls, 0)   # every author was replayed

    def test_a_cached_audit_replays_for_zero_provider_calls_and_is_reparsed(self):
        # The journaled verdict is deliberately a LIE: the engine must reparse the TEXT
        # under today's contract, not trust the token it recorded last time.
        cache = self._journal(through_iteration=2)
        cache.update(journaled(SEMANTIC_REPEAT_STEP, SEMANTIC_PHASE, 2,
                               audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1"),
                               "SAME_OBJECTION_REVIEWER_RIGHT"))
        status, engine, agents, _ = run_arch(verifier=[FEASIBLE], cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].semantic_prompts, [])   # no provider call
        # Provider calls and ALLOWANCE usage are different things: the replay pays no
        # provider, but the audit it replays was paid for once and still counts against
        # this run directory's ceiling.
        self.assertEqual(engine._sem.calls["REPEAT"], 1)
        self.assertEqual(engine._sem.cases, [])                    # DISTINCT, not SAME

    def test_a_final_replayed_gate_with_no_completed_audit_retries_it_live(self):
        # The prior run stopped (or was interrupted) before its audit completed; the
        # audit retries at the journal boundary rather than being lost.
        cache = self._journal(through_iteration=2)
        status, _e, agents, _ = run_arch(
            verifier=[FEASIBLE],
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1")],
            cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits, 1)

    def test_a_resume_cannot_buy_the_whole_allowance_again(self):
        # THE defect this guards: `calls` used to start at zero on every resume, so a
        # journal holding an audit could replay it for free and then still buy
        # max_calls_per_mode fresh ones — the per-run ceiling held only until the first
        # resume. Allowance 2, one audit already in the journal: the replay is free, the
        # run may make exactly ONE more live audit, and the third eligible streak falls
        # back to the existing route (which escalates at streak 5, as always).
        cache = self._journal(through_iteration=3, author_only_last=True)
        cache.update(journaled(SEMANTIC_REPEAT_STEP, SEMANTIC_PHASE, 2,
                               audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-2-B1"),
                               "DISTINCT_OR_MATERIALLY_PROGRESSING"))
        allowance, journaled_audits = 2, 1
        status, engine, agents, _ = run_arch(
            verifier=[nr(f"live gap {i}") for i in (3, 4, 5)],
            wf=workflow(semantic_audit=dict(AUDIT, max_calls_per_mode=allowance)),
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", "DV-3-B1")],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"],
            cache=cache)
        self.assertEqual(status, "AWAITING_HUMAN", status)
        # One live audit only — iteration 3 (the first eligible LIVE gate); iteration 4
        # is eligible too and is refused because the allowance is spent.
        self.assertEqual(agents["arbiter"].audits, 1)
        self.assertEqual(engine._sem.calls["REPEAT"], 2)
        self.assertLessEqual(journaled_audits + agents["arbiter"].audits, allowance)
        # The existing route is untouched: escalate_after_cycles still fires, once.
        self.assertEqual(len(agents["arbiter"].arbitration_prompts), 1)

    def test_a_partly_replayed_iteration_may_audit_and_is_charged_exactly_once(self):
        # Iteration 2 replays its author and runs its developer review AND its audit
        # live. Both are live calls; the ITERATION still costs exactly one budget unit,
        # so a 2-iteration budget leaves room for exactly one fully live iteration.
        cache = self._journal(through_iteration=2, author_only_last=True)
        status, _e, agents, _ = run_arch(
            verifier=[nr("live gap 2"), nr("live gap 3")],
            wf=workflow(max_iterations=2),
            semantic=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", f"DV-{i}-B1")
                      for i in (2, 3)],
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"],
            cache=cache)
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(agents["author"].calls, 1, agents["author"].prompts)
        self.assertEqual(agents["verifier"].calls, 2)
        self.assertEqual(agents["arbiter"].audits, 2)


class TestRoleContracts(unittest.TestCase):
    """The transport contract is textual, so its regressions are textual too."""

    ROLES = Path(__file__).resolve().parent.parent / "roles"

    def role(self, name):
        return (self.ROLES / f"{name}.md").read_text(encoding="utf-8")

    def test_the_verifier_is_told_to_number_its_gaps(self):
        # Stable IDs are what make "the same objection, restated" a question that can be
        # asked about named evidence rather than about a blob of prose.
        text = self.role("Developer Verifier")
        self.assertIn("`### B<n>` heading", text)
        self.assertIn("never reuse a number", text)

    def test_the_verifier_knows_what_a_binding_semantic_ruling_is(self):
        text = self.role("Developer Verifier")
        self.assertIn("## Binding semantic consistency ruling", text)
        self.assertIn("one compliance revision", text)

    def test_the_verifier_closure_rule_still_abuts_the_output_format(self):
        # The ratified adjacency (DEV-07): the closure rule is dispositive, so it must be
        # the last thing read before the verdict is written. Re-pinned here because this
        # change set edits both the section above it and the block below it.
        text = self.role("Developer Verifier")
        self.assertIn("without advancing it. `FEASIBLE` means \"buildable with the normal "
                      "amount of implementer\njudgment\", not \"nothing left to improve\"."
                      "\n\n## Output Format", text)

    def test_the_arbiter_carries_the_exact_mode_marker_and_vocabulary(self):
        text = self.role("Arbitration Reviewer")
        self.assertIn(MARKER, text)
        for token in ("DISTINCT_OR_MATERIALLY_PROGRESSING", "SAME_OBJECTION_REVIEWER_RIGHT",
                      "SAME_OBJECTION_AUTHOR_RIGHT", "SAME_OBJECTION_BOTH_PARTIALLY_RIGHT",
                      "NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION",
                      "SELF_CONTRADICTION"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_the_arbiter_is_forbidden_to_re_review_broadly_in_semantic_mode(self):
        text = self.role("Arbitration Reviewer")
        self.assertIn("Do **not** re-review the proposal", text)
        self.assertIn("untrusted data", text)

    def test_the_arbiters_ordinary_vocabulary_is_untouched(self):
        # The conditional mode is additive: normal arbitration keeps its own header and
        # tokens, and the engine keeps routing dev/inv arbitration on them.
        text = self.role("Arbitration Reviewer")
        self.assertIn("### Arbitration Verdict", text)
        self.assertIn("DEVELOPER_IS_RIGHT", text)


if __name__ == "__main__":
    unittest.main()
