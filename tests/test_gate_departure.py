"""ARCH-02 development run 2: the gate's departure from the reviewer, and the quality signal.

Two gaps this closes, and the regressions these tests exist to catch:

  * **A gate's departure was unobserved.** The Tech Lead runs only on drafts the Developer
    Verifier already passed, yet one corpus run overturned FEASIBLE in 33 seconds by
    re-labelling two of the reviewer's own advisory notes as blockers. The prompt rules
    forbid that; until now nothing CHECKED that the rule was kept. So: an approving gate
    must still cost nothing, `tl_rejects` must count exactly what the design says it
    counts, an unjustified departure must cost the author no iteration, and no fallback
    may ever produce a PASS.
  * **Quality was not measured at all.** `author_blocker_acceptance_rate` must be derived
    from what the run already journals — which means the raw author step keeps its
    dispositions while every document and every later prompt does not, a defective
    disposition is counted rather than guessed or repaired, and a run that cannot honestly
    report a value reports `null` or nothing at all.

They drive the real `_run_architecture` loop over scripted agents, so a route asserted
here is the route the engine really takes.
"""
from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import orchestrate

from orchestrator.engine import (SEMANTIC_GATE_STEP, SEMANTIC_PHASE, SEMANTIC_REPEAT_STEP,
                                 TECHLEAD_RECONSIDER_STEP, Engine, _ArchQuality,
                                 _ArchSemanticState)
from orchestrator.transcript import QUALITY_PENDING, Transcript
from orchestrator.verdicts import (SemanticVerdictContract, extract_blocker_dispositions,
                                   extract_blocking_findings, extract_semantic_assessment,
                                   semantic_marker)
from tests.test_semantic_repeat import (AUDIT, FEASIBLE, READY, _Agent, _Ctx, _Guard,
                                        audit, journaled, nr, workflow)

GATE_MARKER = semantic_marker("GATE_DEPARTURE")
REPEAT_MARKER = semantic_marker("REPEAT")


# ---- scripted Tech-Lead and arbiter output ---------------------------------------
def tl_block(n, kind="MISSED_RISK", detail="the ring is unbounded"):
    return (f"### B{n}\nKind: {kind}\nChecklist item: NONE\n"
            f"Proposal evidence: \"a ring whose depth nobody states\"\n"
            f"Failure mechanism: {detail}\nAcceptance criterion: bounded footprint\n")


def tl_reject(*numbers, verdict="ARCHITECT_REVIEW_NEEDS_REVISION", manifest=True):
    """A Tech-Lead non-approval carrying the canonical `## Architecture Blocking
    Findings` projection (or, with manifest=False, a legacy report without one)."""
    text = (f"## Tech Lead Verdict\n{verdict}\n\n"
            "## Architect Blocking Issues Assessment\n- Issue: the depth is unstated.\n"
            "- Classification: VALID_BLOCKER\n")
    if manifest:
        text += ("\n## Architecture Blocking Findings\n\n"
                 + "\n".join(tl_block(n) for n in numbers))
    return text


def gate(token, retained="NONE", rejected="NONE", evidence="cited", ruling="Bound it.",
         current="NONE", prior="NONE"):
    return (f"## Semantic Consistency Verdict\n{token}\n\n"
            f"## Current Finding IDs\n{current}\n\n"
            f"## Matched Prior Finding IDs\n{prior}\n\n"
            f"## Retained Tech-Lead Finding IDs\n{retained}\n\n"
            f"## Rejected Tech-Lead Finding IDs\n{rejected}\n\n"
            f"## Cited Evidence\n{evidence}\n\n"
            f"## Binding Ruling\n{ruling}\n")


def justified(*ids):
    return gate("JUSTIFIED_GATE_DEPARTURE", retained=", ".join(ids), current=", ".join(ids),
                evidence=f"{', '.join(ids)} each quote the proposal and name the failure.")


def partial(retained, rejected):
    return gate("PARTIALLY_JUSTIFIED_GATE_DEPARTURE", retained=retained, rejected=rejected,
                evidence=f"{retained} cites evidence; {rejected} re-labels an advisory note.",
                ruling=f"Only {retained} stands; {rejected} is set aside.")


def unjustified(*ids):
    return gate("UNJUSTIFIED_GATE_DEPARTURE", rejected=", ".join(ids),
                evidence=f"{', '.join(ids)} quote nothing and name no failure mechanism.",
                ruling="Approve unless you can quote the proposal text and the failure.")


class _MultiArbiter:
    """ONE seat, THREE contracts — exactly like the real `architecture.arbiter` cell:
    repeat audits, gate-departure audits, and ordinary arbitration, told apart by the
    exact first-line marker."""

    def __init__(self, gate_replies=(), repeat=(), arbitration=()):
        self.name = "arbiter"
        self.queues = {"gate": list(gate_replies), "repeat": list(repeat),
                       "arb": list(arbitration)}
        self.prompts = {"gate": [], "repeat": [], "arb": []}

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        kind = ("gate" if prompt.startswith(GATE_MARKER)
                else "repeat" if prompt.startswith(REPEAT_MARKER) else "arb")
        self.prompts[kind].append(prompt)
        q, seen = self.queues[kind], self.prompts[kind]
        from orchestrator.adapters import OK, AgentResponse
        text = q[min(len(seen) - 1, len(q) - 1)] if q else ""
        return AgentResponse(self.name, OK, text=text)

    def audits(self, kind="gate"):
        """Logical audits, not invocations: a repair re-invokes the same seat."""
        return len([p for p in self.prompts[kind] if "STOP —" not in p])


class _Lead:
    """The Tech-Lead seat: the control review and its one-shot reconsideration are
    different steps, so they get different scripted queues."""

    def __init__(self, control=(READY,), reconsider=()):
        self.name = "lead"
        self.control, self.reconsider = list(control), list(reconsider)
        self.control_prompts: list[str] = []
        self.reconsider_prompts: list[str] = []

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        from orchestrator.adapters import OK, AgentResponse
        # A verdict-repair prompt echoes its ORIGINAL TASK, so this phrase is present
        # exactly when the step being repaired is the reconsideration.
        reconsidering = "UNCHANGED, no revision was made" in prompt
        if reconsidering:
            self.reconsider_prompts.append(prompt)
            q, seen = self.reconsider, self.reconsider_prompts
        else:
            self.control_prompts.append(prompt)
            q, seen = self.control, self.control_prompts
        text = q[min(len(seen) - 1, len(q) - 1)] if q else ""
        return AgentResponse(self.name, OK, text=text)


def run_arch(*, verifier, lead=None, gate_replies=(), repeat=(), arbitration=(),
             author=("# Draft\n\n## Proposed Design\nd\n",), wf=None, cache=None,
             human_answer=None, run_id="run-gate"):
    """Drive the real architecture loop; return (status, engine, agents, run_dir_files)."""
    agents = {"author": _Agent("author", author),
              "verifier": _Agent("verifier", verifier),
              "lead": lead if lead is not None else _Lead(),
              "arbiter": _MultiArbiter(gate_replies, repeat, arbitration)}
    with tempfile.TemporaryDirectory() as tmp:
        tx = Transcript(Path(tmp), run_id, "architecture", "task", False, None, {})
        ctx = _Ctx(tx, agents, wf or workflow(), cache, human_answer)
        engine = Engine(ctx, "architecture")
        status = engine._run_architecture("the task and its acceptance criteria")
        quality = json.loads((tx.dir / "quality.json").read_text(encoding="utf-8")) \
            if (tx.dir / "quality.json").exists() else None
        artifacts = {p.name: p.read_text(encoding="utf-8")
                     for p in (tx.dir / "artifacts").glob("*")}
    return status, engine, agents, quality, artifacts


# =================================================================================
class TestTheApprovingGatePaysNothing(unittest.TestCase):
    def test_tech_lead_approval_makes_no_semantic_call(self):
        # The central cost promise, restated for run 2: the ordinary successful route is
        # Author -> FEASIBLE -> READY_FOR_HUMAN_REVIEW, and it must make ZERO audits.
        status, _e, agents, quality, _a = run_arch(verifier=[FEASIBLE])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["gate"], [])
        self.assertEqual(agents["arbiter"].prompts["repeat"], [])
        self.assertEqual(agents["arbiter"].prompts["arb"], [])
        self.assertEqual(agents["lead"].reconsider_prompts, [])
        # A first-pass successful run has no eligible finding at all: `null`, not zero.
        self.assertIsNone(quality["overall"]["rate"])
        self.assertEqual(quality["overall"]["eligible"], 0)

    def test_a_later_approval_after_a_departure_still_makes_only_one_audit(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[justified("TL-1-B1")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)


class TestTechLeadManifestExtraction(unittest.TestCase):
    def test_structured_findings_get_stable_tl_ids(self):
        got = extract_blocking_findings(tl_reject(1, 2), source="TL", iteration=3,
                                        verdict="DEVELOPER_NEEDS_FIX")
        self.assertEqual([f.finding_id for f in got], ["TL-3-B1", "TL-3-B2"])
        self.assertFalse(any(f.synthetic for f in got))

    def test_a_report_without_a_manifest_collapses_to_the_whole_report(self):
        # The Tech Lead's reasoning is spread over several sections, so the indivisible
        # unit is the COMPLETE non-approval — not a fragment Python chose.
        got = extract_blocking_findings(tl_reject(manifest=False), source="TL", iteration=2,
                                        verdict="ARCHITECT_REVIEW_NEEDS_REVISION")
        self.assertEqual([f.finding_id for f in got], ["TL-2-B0"])
        self.assertTrue(got[0].synthetic)
        self.assertIn("Tech Lead Verdict", got[0].text)
        self.assertIn("Architect Blocking Issues Assessment", got[0].text)

    def test_an_approval_carries_no_findings(self):
        self.assertEqual(extract_blocking_findings(READY, source="TL", iteration=1,
                                                   verdict="READY_FOR_HUMAN_REVIEW"), [])

    def test_a_duplicated_number_collapses_to_one_synthetic_b0(self):
        got = extract_blocking_findings(tl_reject(1, 1), source="TL", iteration=1,
                                        verdict="DEVELOPER_NEEDS_FIX")
        self.assertEqual([f.finding_id for f in got], ["TL-1-B0"])


class TestGateParser(unittest.TestCase):
    two = SemanticVerdictContract(mode="GATE_DEPARTURE",
                                  supplied_current_ids=frozenset({"TL-1-B1", "TL-1-B2"}))
    one = SemanticVerdictContract(mode="GATE_DEPARTURE",
                                  supplied_current_ids=frozenset({"TL-1-B0"}))

    def parse(self, text, contract=None):
        return extract_semantic_assessment(text, contract=contract or self.two)

    def test_justified_must_retain_every_supplied_finding(self):
        self.assertTrue(self.parse(justified("TL-1-B1", "TL-1-B2")))
        self.assertIsNone(self.parse(justified("TL-1-B1")))          # one uncovered

    def test_justified_may_reject_nothing(self):
        self.assertIsNone(self.parse(gate("JUSTIFIED_GATE_DEPARTURE", "TL-1-B1", "TL-1-B2")))

    def test_partial_needs_a_disjoint_covering_partition(self):
        self.assertTrue(self.parse(partial("TL-1-B1", "TL-1-B2")))
        # overlapping
        self.assertIsNone(self.parse(gate("PARTIALLY_JUSTIFIED_GATE_DEPARTURE",
                                          "TL-1-B1, TL-1-B2", "TL-1-B2")))
        # not covering
        self.assertIsNone(self.parse(gate("PARTIALLY_JUSTIFIED_GATE_DEPARTURE",
                                          "TL-1-B1", "NONE")))

    def test_a_synthetic_b0_permits_only_all_retained_or_all_rejected(self):
        # One indivisible finding cannot be split into two non-empty halves, so the
        # PARTIAL token is structurally unavailable against it — by construction, not by
        # a special case in the engine.
        self.assertTrue(self.parse(justified("TL-1-B0"), self.one))
        self.assertTrue(self.parse(unjustified("TL-1-B0"), self.one))
        self.assertIsNone(self.parse(partial("TL-1-B0", "TL-1-B0"), self.one))

    def test_unjustified_must_reject_all_and_retain_none(self):
        self.assertTrue(self.parse(unjustified("TL-1-B1", "TL-1-B2")))
        self.assertIsNone(self.parse(gate("UNJUSTIFIED_GATE_DEPARTURE",
                                          "TL-1-B1", "TL-1-B2")))

    def test_a_ruling_needs_evidence_and_a_ruling(self):
        for ev, ruling in (("NONE", "Bound it."), ("cited", "NONE")):
            with self.subTest(evidence=ev, ruling=ruling):
                self.assertIsNone(self.parse(
                    gate("JUSTIFIED_GATE_DEPARTURE", "TL-1-B1, TL-1-B2",
                         evidence=ev, ruling=ruling)))

    def test_an_id_that_was_never_supplied_is_unparseable(self):
        self.assertIsNone(self.parse(justified("TL-9-B1")))

    def test_a_repeat_token_is_not_valid_in_gate_mode(self):
        self.assertIsNone(self.parse(gate("SAME_OBJECTION_AUTHOR_RIGHT", "TL-1-B1")))

    def test_the_inconclusive_tokens_need_no_partition(self):
        for token in ("NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION",
                      "SELF_CONTRADICTION"):
            with self.subTest(token=token):
                a = self.parse(gate(token, evidence="NONE", ruling="NONE"))
                self.assertEqual(a.verdict, token)


class TestGatePromptBounds(unittest.TestCase):
    def _engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-gb", "architecture", "task", False, None, {})
            engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
            tx.finish("PASS", "prompt-shape test only")
        engine._sem = _ArchSemanticState(engine._semantic_cfg())
        return engine

    def _findings(self, size):
        return extract_blocking_findings(tl_reject(1).replace("the ring is unbounded",
                                                              "X" * size),
                                         source="TL", iteration=1,
                                         verdict="DEVELOPER_NEEDS_FIX")

    def test_the_dynamic_evidence_never_exceeds_the_configured_bound(self):
        engine = self._engine()
        cap = AUDIT["gate_max_prompt_chars"]
        records = [{"assessment": extract_semantic_assessment(
                        gate("INSUFFICIENT_INFORMATION", evidence="NONE", ruling="NONE"),
                        contract=SemanticVerdictContract(mode="GATE_DEPARTURE")),
                    "text": "Y" * 30000, "iteration": n} for n in range(1, 5)]
        text, used = engine._gate_evidence("T" * 40000, "V" * 40000, self._findings(40000),
                                           "L" * 40000, records, cap)
        self.assertLessEqual(used, cap)
        self.assertIn("[TRUNCATED]", text)

    def test_the_prompt_opens_with_the_exact_mode_marker_and_labels_evidence(self):
        engine = self._engine()
        prompt = engine._semantic_gate_prompt("task", FEASIBLE, self._findings(20),
                                              tl_reject(1), [])
        self.assertEqual(prompt.splitlines()[0], "[[ARCH_SEMANTIC_AUDIT_V1:GATE_DEPARTURE]]")
        self.assertIn("UNTRUSTED EVIDENCE", prompt)

    def test_no_gate_prompt_tells_the_gate_which_iteration_it_is_on(self):
        # "No budget hints in gate prompts" is a rule about EVERY prompt a gate reads.
        _s, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[justified("TL-1-B1")])
        for prompt in agents["lead"].control_prompts:
            for banned in ("max_iterations", "iteration budget", "iterations left",
                           "tl_rejects", "techlead_reject_before_arbiter"):
                self.assertNotIn(banned, prompt)


class TestTlRejectsAccounting(unittest.TestCase):
    """The exact accounting the design specifies, asserted on the counter itself."""

    def test_justified_increments_once_and_routes_all_findings(self):
        status, engine, agents, quality, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, 2), READY]),
            gate_replies=[justified("TL-1-B1", "TL-1-B2")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)
        self.assertEqual(agents["arbiter"].prompts["arb"], [])
        author = agents["author"].prompts[1]
        self.assertIn("## Active Gate Findings", author)
        self.assertIn("### TL-1-B1", author)
        self.assertIn("### TL-1-B2", author)
        # Both were presented to an author, so both are eligible.
        self.assertEqual(quality["overall"]["eligible"], 2)

    def test_partial_increments_once_and_routes_only_retained_findings(self):
        status, _e, agents, quality, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, 2), READY]),
            gate_replies=[partial("TL-1-B1", "TL-1-B2")])
        self.assertEqual(status, "PASS", status)
        author = agents["author"].prompts[1]
        self.assertIn("### TL-1-B1", author)
        self.assertNotIn("### TL-1-B2", author)
        # The rejected finding was never put to an author: not eligible, and recorded
        # with the reason rather than silently dropped.
        self.assertEqual(quality["overall"]["eligible"], 1)
        rejected = [e for e in quality["excluded"] if e["finding_id"] == "TL-1-B2"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["state"], "rejected_by_assessment")

    def test_the_threshold_still_invokes_normal_arbitration_and_clears_the_manifest(self):
        # techlead_reject_before_arbiter = 2: the second justified departure escalates.
        wf = workflow()
        wf["architecture"]["techlead_reject_before_arbiter"] = 2
        status, engine, agents, quality, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), tl_reject(1), READY]),
            gate_replies=[justified("TL-1-B1"), justified("TL-2-B1")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"],
            wf=wf)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 2)
        self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)
        after = agents["author"].prompts[2]
        self.assertNotIn("## Active Gate Findings", after)
        self.assertIn("Bound it.", after)
        # TL-2-B1 was pending during arbitration and is superseded by the ruling.
        self.assertEqual([e["state"] for e in quality["excluded"]
                          if e["finding_id"] == "TL-2-B1"], ["superseded_by_ruling"])

    def test_an_original_escalate_to_human_verdict_still_invokes_arbitration(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, verdict="ESCALATE_TO_HUMAN"), READY]),
            gate_replies=[justified("TL-1-B1")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)
        self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)

    def test_needs_human_decision_leaves_tl_rejects_unchanged_and_arbitrates(self):
        # tl_esc = 1: if the assessment had incremented the counter, the threshold branch
        # would be indistinguishable from this one. It must NOT increment.
        wf = workflow()
        wf["architecture"]["techlead_reject_before_arbiter"] = 1
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[gate("NEEDS_HUMAN_DECISION", current="TL-1-B1",
                               evidence="both readings are defensible", ruling="NONE")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"],
            wf=wf)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)
        self.assertIn("NEEDS_HUMAN_DECISION", agents["arbiter"].prompts["arb"][0])

    def test_self_contradiction_arbitrates_with_the_evidence(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[gate("SELF_CONTRADICTION", current="TL-1-B1",
                               evidence="it says both", ruling="NONE")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertIn("SELF_CONTRADICTION", agents["arbiter"].prompts["arb"][0])


class TestUnjustifiedDepartureAndReconsideration(unittest.TestCase):
    def test_it_bypasses_the_author_and_invokes_exactly_one_reconsideration(self):
        status, _e, agents, quality, artifacts = run_arch(
            verifier=[FEASIBLE],
            lead=_Lead(control=[tl_reject(1)], reconsider=[READY]),
            gate_replies=[unjustified("TL-1-B1")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(len(agents["lead"].reconsider_prompts), 1)
        self.assertEqual(len(agents["lead"].control_prompts), 1)
        # No author iteration was spent: exactly ONE author call in the whole run.
        self.assertEqual(agents["author"].calls, 1)
        self.assertEqual(agents["verifier"].calls, 1)
        self.assertEqual(agents["arbiter"].prompts["arb"], [])
        self.assertIn("architecture-final.md", artifacts)
        # Rejected findings are never eligible.
        self.assertEqual(quality["overall"]["eligible"], 0)
        self.assertEqual([e["state"] for e in quality["excluded"]],
                         ["rejected_by_assessment"])

    def test_tl_rejects_is_neither_incremented_nor_reset(self):
        # tl_esc = 2. An unjustified departure must not move the counter at all, so the
        # justified departure that follows it is still only the FIRST counted rejection.
        wf = workflow()
        wf["architecture"]["techlead_reject_before_arbiter"] = 2
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), tl_reject(1), READY],
                       reconsider=["## Tech Lead Verdict\nDEVELOPER_NEEDS_FIX\n\nStill no.\n"]),
            gate_replies=[unjustified("TL-1-B1"), justified("TL-2-B1")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        # The reconsideration failed -> full arbitration (one call). The iteration-2
        # justified departure is then the FIRST counted rejection, which is below
        # tl_esc=2, so it goes back to the author and no SECOND arbitration happens.
        self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)
        self.assertEqual(agents["arbiter"].audits("gate"), 2)

    def test_the_reconsideration_reads_the_unchanged_draft_and_the_correction(self):
        _s, _e, agents, _q, artifacts = run_arch(
            verifier=[FEASIBLE],
            author=("# Draft one\n\n## Proposed Design\nunchanged\n",),
            lead=_Lead(control=[tl_reject(1)], reconsider=[READY]),
            gate_replies=[unjustified("TL-1-B1")])
        prompt = agents["lead"].reconsider_prompts[0]
        self.assertIn("UNCHANGED, no revision was made", prompt)
        self.assertIn("# Draft one", prompt)
        self.assertIn("Approve unless you can quote", prompt)      # the binding ruling
        self.assertIn("Tech Lead Verdict", prompt)                 # its own prior report
        self.assertIn("Implementation Feasibility Verdict", prompt)  # the DV report
        # And the draft it approved is the very same text.
        self.assertIn("# Draft one", artifacts["architecture-final.md"])

    def test_only_ready_for_human_review_closes_the_run(self):
        for verdict in ("DEVELOPER_NEEDS_FIX", "ARCHITECT_REVIEW_NEEDS_REVISION",
                        "ESCALATE_TO_HUMAN"):
            with self.subTest(verdict=verdict):
                status, _e, agents, _q, _a = run_arch(
                    verifier=[FEASIBLE, FEASIBLE],
                    lead=_Lead(control=[tl_reject(1), READY],
                               reconsider=[f"## Tech Lead Verdict\n{verdict}\n\nno.\n"]),
                    gate_replies=[unjustified("TL-1-B1")],
                    arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
                self.assertEqual(status, "PASS", status)      # via the ARBITER's ruling
                self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)

    def test_an_unparseable_reconsideration_invokes_full_arbitration(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY], reconsider=["I have no opinion."]),
            gate_replies=[unjustified("TL-1-B1")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(len(agents["arbiter"].prompts["arb"]), 1)
        # one primary invocation + the two bounded verdict repairs
        self.assertEqual(len(agents["lead"].reconsider_prompts), 3)


class TestFallbacksUseTheOriginalTechLeadRoute(unittest.TestCase):
    def _run(self, replies, wf=None):
        return run_arch(verifier=[FEASIBLE, FEASIBLE],
                        lead=_Lead(control=[tl_reject(1), READY]),
                        gate_replies=replies, wf=wf)

    def test_insufficient_information_applies_the_original_verdict(self):
        status, _e, agents, quality, _a = self._run(
            [gate("INSUFFICIENT_INFORMATION", evidence="NONE", ruling="NONE")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["arb"], [])
        self.assertEqual(agents["lead"].reconsider_prompts, [])
        # A sub-threshold result activates ALL original Tech-Lead blockers.
        self.assertIn("### TL-1-B1", agents["author"].prompts[1])
        self.assertEqual(quality["overall"]["eligible"], 1)

    def test_an_unparseable_assessment_applies_the_original_verdict(self):
        status, _e, agents, _q, _a = self._run(["I could not decide, sorry."])
        self.assertEqual(status, "PASS", status)
        # one primary + two bounded semantic repairs, and no ruling was created
        self.assertEqual(len(agents["arbiter"].prompts["gate"]), 3)
        self.assertEqual(agents["arbiter"].prompts["arb"], [])
        self.assertEqual(agents["lead"].reconsider_prompts, [])
        self.assertIn("### TL-1-B1", agents["author"].prompts[1])

    def test_an_invalid_partition_triggers_a_gate_specific_repair(self):
        status, _e, agents, _q, _a = self._run(
            [justified("TL-9-B1"), justified("TL-1-B1")])   # an ID never supplied
        self.assertEqual(status, "PASS", status)
        repair = agents["arbiter"].prompts["gate"][1]
        self.assertTrue(repair.startswith(GATE_MARKER))
        self.assertIn("STOP —", repair)
        self.assertIn("Retained Tech-Lead Finding IDs", repair)
        self.assertIn("PARTIALLY_JUSTIFIED_GATE_DEPARTURE", repair)
        self.assertIn("TL-1-B1", repair)                    # the IDs it MAY name

    def test_allowance_exhaustion_uses_the_original_route(self):
        wf = workflow(semantic_audit=dict(AUDIT, max_calls_per_mode=1))
        wf["architecture"]["techlead_reject_before_arbiter"] = 9
        status, engine, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), tl_reject(1), READY]),
            gate_replies=[justified("TL-1-B1")], wf=wf)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)
        self.assertEqual(engine._sem.calls["GATE_DEPARTURE"], 1)
        # Iteration 2's departure was NOT audited, and its findings still routed.
        self.assertIn("### TL-2-B1", agents["author"].prompts[2])

    def test_an_unparseable_tech_lead_verdict_buys_no_assessment(self):
        # Not one of the three declared departure verdicts. It stays on the existing
        # fail-safe route (counted as a non-approval), and pays for no audit of evidence
        # a reply that structured none could not have cited.
        # The primary reply plus both bounded verdict repairs stay unparseable, so the
        # control step really does end with no verdict.
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=["I have concerns but no verdict."] * 3 + [READY]),
            gate_replies=[justified("TL-1-B0")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["gate"], [])
        self.assertEqual(agents["lead"].reconsider_prompts, [])
        self.assertIn("### TL-1-B0", agents["author"].prompts[1])

    def test_developer_needs_fix_is_also_a_departure(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, verdict="DEVELOPER_NEEDS_FIX"), READY]),
            gate_replies=[justified("TL-1-B1")])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)

    def test_a_workflow_without_semantic_audit_makes_no_gate_call(self):
        status, _e, agents, _q, _a = self._run([], wf=workflow(semantic_audit=None))
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["gate"], [])

    def test_the_two_allowances_are_independent(self):
        # Three repeat audits exhaust the REPEAT allowance; the gate allowance is
        # untouched and its own audit still happens.
        wf = workflow(semantic_audit=dict(AUDIT, max_calls_per_mode=3))
        status, engine, agents, _q, _a = run_arch(
            verifier=[nr("g1"), nr("g2"), nr("g3"), nr("g4"), FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            repeat=[audit("DISTINCT_OR_MATERIALLY_PROGRESSING", f"DV-{n}-B1")
                    for n in (2, 3, 4)],
            gate_replies=[justified("TL-5-B1")], wf=wf)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(engine._sem.calls, {"REPEAT": 3, "GATE_DEPARTURE": 1})


class TestGateReplayEligibility(unittest.TestCase):
    """The gate mode shares run 1's replay contract: a resume must not invent a paid
    call, and must not pay twice for one it already made."""

    def test_a_middle_of_journal_replay_makes_no_live_gate_audit(self):
        # A pre-ARCH-02 journal must not acquire audits it never made when replayed.
        cache = {}
        cache.update(journaled("arch.author", "architecture", 1, "# Draft"))
        cache.update(journaled("arch.dev_review", "architecture", 1, FEASIBLE, "FEASIBLE"))
        cache.update(journaled("arch.tech_lead", "techlead", 1, tl_reject(1),
                               "ARCHITECT_REVIEW_NEEDS_REVISION"))
        cache.update(journaled("arch.author", "architecture", 2, "# Draft 2"))
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE], lead=_Lead(control=[READY]),
            gate_replies=[justified("TL-1-B1")], cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["gate"], [])
        # ...and the historical route still ran: the replayed non-approval sent the
        # author back, exactly as the journal recorded.
        self.assertEqual(agents["author"].calls, 0)

    def test_a_cached_gate_audit_replays_free_and_still_counts_against_the_allowance(self):
        # The journaled verdict is deliberately a LIE: the engine reparses the TEXT under
        # today's contract. And the replay pays no provider, yet the audit it replays was
        # paid for once and cannot be un-spent.
        cache = {}
        cache.update(journaled("arch.author", "architecture", 1, "# Draft"))
        cache.update(journaled("arch.dev_review", "architecture", 1, FEASIBLE, "FEASIBLE"))
        cache.update(journaled("arch.tech_lead", "techlead", 1, tl_reject(1),
                               "ARCHITECT_REVIEW_NEEDS_REVISION"))
        cache.update(journaled(SEMANTIC_GATE_STEP, SEMANTIC_PHASE, 1,
                               unjustified("TL-1-B1"), "JUSTIFIED_GATE_DEPARTURE"))
        status, engine, agents, _q, _a = run_arch(
            verifier=[FEASIBLE], lead=_Lead(control=[READY], reconsider=[READY]),
            cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].prompts["gate"], [])       # no provider call
        self.assertEqual(engine._sem.calls["GATE_DEPARTURE"], 1)      # still charged
        # Reparsed as UNJUSTIFIED (the text), not JUSTIFIED (the recorded token): the
        # reconsideration ran, which only the unjustified route does.
        self.assertEqual(len(agents["lead"].reconsider_prompts), 1)

    def test_a_final_replayed_gate_retries_the_audit_live(self):
        # The prior run stopped before its audit completed; it retries at the journal
        # boundary rather than being lost.
        cache = {}
        cache.update(journaled("arch.author", "architecture", 1, "# Draft"))
        cache.update(journaled("arch.dev_review", "architecture", 1, FEASIBLE, "FEASIBLE"))
        cache.update(journaled("arch.tech_lead", "techlead", 1, tl_reject(1),
                               "ARCHITECT_REVIEW_NEEDS_REVISION"))
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE], lead=_Lead(control=[READY]),
            gate_replies=[justified("TL-1-B1")], cache=cache)
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["arbiter"].audits("gate"), 1)


class TestDisagreementComposition(unittest.TestCase):
    def test_rejected_findings_stay_in_the_history_but_not_in_author_prompts(self):
        status, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, 2), tl_reject(1), READY]),
            gate_replies=[partial("TL-1-B1", "TL-1-B2"),
                          gate("NEEDS_HUMAN_DECISION", current="TL-2-B1",
                               evidence="undecidable", ruling="NONE")],
            arbitration=["## Arbitration Verdict\nTECH_LEAD_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        history = agents["arbiter"].prompts["arb"][0]
        # The full report and the assessment are both there, and the rejected finding is
        # labelled inactive rather than deleted...
        self.assertIn("TL-1-B2", history)
        self.assertIn("REJECTED by this assessment", history)
        # ...while the separate ACTIVE list names only what is still binding.
        active = history.split("## Active gate findings", 1)[1]
        self.assertIn("TL-2-B1", active)
        self.assertNotIn("TL-1-B2", active)
        # The author never saw the rejected finding as feedback.
        for prompt in agents["author"].prompts:
            self.assertNotIn("### TL-1-B2", prompt)

    def test_a_partial_ruling_sends_the_correction_not_the_full_report(self):
        # The full Tech-Lead report contains the rejected finding's reasoning; handing it
        # to the author as "binding" would restore a blocker the arbiter set aside.
        _s, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1, 2), READY]),
            gate_replies=[partial("TL-1-B1", "TL-1-B2")])
        author = agents["author"].prompts[1]
        self.assertIn("Gate-departure assessment ruling", author)
        self.assertIn("Only TL-1-B1 stands", author)

    def test_a_justified_ruling_still_sends_the_tech_lead_report(self):
        _s, _e, agents, _q, _a = run_arch(
            verifier=[FEASIBLE, FEASIBLE],
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[justified("TL-1-B1")])
        author = agents["author"].prompts[1]
        self.assertIn("Tech Lead ruling", author)
        self.assertIn("Architect Blocking Issues Assessment", author)


class TestDispositionParsing(unittest.TestCase):
    PROPOSAL = ("# Architecture Proposal: X\n\n## Context & Task\nt\n\n"
                "## Components & Boundaries\nb\n\n## Disagreements\n- no.\n\n"
                "## Changelog\n- c\n")

    def parse(self, block, expected=("DV-1-B1",), proposal=None):
        return extract_blocker_dispositions((proposal or self.PROPOSAL) + block,
                                            expected_ids=set(expected))

    def test_the_whole_block_is_stripped_and_the_proposal_survives(self):
        clean, _d = self.parse("\n## Blocker Dispositions\n"
                               "- DV-1-B1 | ACCEPTED | Components & Boundaries\n")
        self.assertNotIn("Blocker Dispositions", clean)
        self.assertNotIn("DV-1-B1 | ACCEPTED", clean)
        self.assertIn("## Changelog", clean)
        self.assertTrue(clean.startswith("# Architecture Proposal: X"))

    def test_accepted_and_disagreed_are_classified(self):
        _c, d = self.parse("\n## Blocker Dispositions\n"
                           "- DV-1-B1 | ACCEPTED | Components & Boundaries\n"
                           "- DV-1-B2 | DISAGREED | Disagreements\n",
                           expected=("DV-1-B1", "DV-1-B2"))
        self.assertEqual(d["DV-1-B1"].disposition, "ACCEPTED")
        self.assertEqual(d["DV-1-B1"].target_section, "Components & Boundaries")
        self.assertEqual(d["DV-1-B2"].disposition, "DISAGREED")

    def test_a_missing_entry_is_unclassified(self):
        _c, d = self.parse("\n## Blocker Dispositions\n"
                           "- DV-1-B1 | ACCEPTED | Components & Boundaries\n",
                           expected=("DV-1-B1", "DV-1-B2"))
        self.assertEqual(d["DV-1-B2"].disposition, "UNCLASSIFIED")
        self.assertIn("no disposition", d["DV-1-B2"].reason)

    def test_no_block_at_all_is_unclassified_and_leaves_the_text_alone(self):
        clean, d = self.parse("")
        self.assertEqual(clean, self.PROPOSAL.strip())
        self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")

    def test_a_duplicate_entry_is_unclassified(self):
        _c, d = self.parse("\n## Blocker Dispositions\n"
                           "- DV-1-B1 | ACCEPTED | Components & Boundaries\n"
                           "- DV-1-B1 | DISAGREED | Disagreements\n")
        self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")
        self.assertIn("more than once", d["DV-1-B1"].reason)

    def test_an_unknown_token_is_unclassified(self):
        _c, d = self.parse("\n## Blocker Dispositions\n"
                           "- DV-1-B1 | PARTIALLY_ACCEPTED | Components & Boundaries\n")
        self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")

    def test_a_structurally_invalid_row_is_unclassified(self):
        _c, d = self.parse("\n## Blocker Dispositions\n- DV-1-B1 accepted, honestly\n")
        self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")

    def test_accepted_must_target_a_real_non_disagreements_section(self):
        for target, why in (("Nonexistent Section", "not an H2 section"),
                            ("Disagreements", "targeted the Disagreements"),
                            ("", "named no target")):
            with self.subTest(target=target):
                _c, d = self.parse("\n## Blocker Dispositions\n"
                                   f"- DV-1-B1 | ACCEPTED | {target}\n")
                self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")
                self.assertIn(why, d["DV-1-B1"].reason)

    def test_disagreed_requires_a_disagreements_section(self):
        without = ("# P\n\n## Context & Task\nt\n\n## Components & Boundaries\nb\n")
        _c, d = self.parse("\n## Blocker Dispositions\n- DV-1-B1 | DISAGREED | Disagreements\n",
                           proposal=without)
        self.assertEqual(d["DV-1-B1"].disposition, "UNCLASSIFIED")
        self.assertIn("no '## Disagreements' section", d["DV-1-B1"].reason)

    def test_an_unknown_id_does_not_expand_the_denominator(self):
        _c, d = self.parse("\n## Blocker Dispositions\n"
                           "- DV-1-B1 | ACCEPTED | Components & Boundaries\n"
                           "- DV-7-B7 | ACCEPTED | Components & Boundaries\n")
        self.assertEqual(set(d), {"DV-1-B1"})

    def test_a_decorated_heading_is_still_stripped(self):
        # Leaking the table into a human-facing proposal is the failure this guards, so
        # the heading match is deliberately lenient about the rest of the line.
        clean, d = self.parse("\n## Blocker Dispositions (transport only)\n"
                              "- DV-1-B1 | ACCEPTED | Components & Boundaries\n")
        self.assertNotIn("Blocker Dispositions", clean)
        self.assertEqual(d["DV-1-B1"].disposition, "ACCEPTED")

    def test_a_block_that_is_not_last_loses_only_itself(self):
        clean, d = extract_blocker_dispositions(
            "# P\n\n## Components & Boundaries\nb\n\n## Blocker Dispositions\n"
            "- DV-1-B1 | ACCEPTED | Components & Boundaries\n\n## Changelog\n- c\n",
            expected_ids={"DV-1-B1"})
        self.assertNotIn("Blocker Dispositions", clean)
        self.assertIn("## Changelog", clean)
        self.assertIn("## Components & Boundaries", clean)
        self.assertEqual(d["DV-1-B1"].disposition, "ACCEPTED")

    def test_the_block_is_stripped_even_when_nothing_was_expected(self):
        # A spurious block must never leak into the proposal document.
        clean, d = extract_blocker_dispositions(
            self.PROPOSAL + "\n## Blocker Dispositions\n- DV-1-B1 | ACCEPTED | X\n",
            expected_ids=set())
        self.assertNotIn("Blocker Dispositions", clean)
        self.assertEqual(d, {})


class TestTheQualitySignal(unittest.TestCase):
    ACCEPT = ("# Architecture Proposal: X\n\n## Proposed Design\nbounded ring\n\n"
              "## Blocker Dispositions\n- DV-1-B1 | ACCEPTED | Proposed Design\n")
    DISAGREE = ("# Architecture Proposal: X\n\n## Proposed Design\nd\n\n"
                "## Disagreements\n- the ring is already bounded.\n\n"
                "## Blocker Dispositions\n- DV-1-B1 | DISAGREED | Disagreements\n")

    def test_the_raw_author_step_keeps_its_dispositions_and_the_documents_do_not(self):
        status, _e, agents, _q, artifacts = run_arch(
            verifier=[nr("the ring is unbounded"), FEASIBLE],
            author=("# Draft\n\n## Proposed Design\nd\n", self.ACCEPT))
        self.assertEqual(status, "PASS", status)
        for name, text in artifacts.items():
            with self.subTest(artifact=name):
                self.assertNotIn("Blocker Dispositions", text)
                self.assertNotIn("| ACCEPTED |", text)
        # ...and no later prompt carries them either.
        for prompt in (agents["verifier"].prompts[1:] + agents["lead"].control_prompts):
            self.assertNotIn("Blocker Dispositions", prompt)

    def test_accepted_and_disagreed_produce_exact_counts(self):
        _s, _e, _a, quality, _art = run_arch(
            verifier=[nr("the ring is unbounded"), FEASIBLE],
            author=("# Draft\n\n## Proposed Design\nd\n", self.ACCEPT))
        self.assertEqual(quality["overall"], {"accepted": 1, "eligible": 1, "rate": 1.0,
                                              "disagreed": 0, "unclassified": 0})
        _s, _e, _a, quality, _art = run_arch(
            verifier=[nr("the ring is unbounded"), FEASIBLE],
            author=("# Draft\n\n## Proposed Design\nd\n", self.DISAGREE))
        self.assertEqual(quality["overall"], {"accepted": 0, "eligible": 1, "rate": 0.0,
                                              "disagreed": 1, "unclassified": 0})

    def test_a_disposition_defect_becomes_unclassified_without_changing_the_route(self):
        # No disposition block at all: still one eligible finding, still UNCLASSIFIED,
        # still exactly one author call per iteration — a measurement must never cause
        # another author call or a different route.
        status, _e, agents, quality, _art = run_arch(
            verifier=[nr("the ring is unbounded"), FEASIBLE],
            author=("# Draft\n\n## Proposed Design\nd\n",
                    "# Draft 2\n\n## Proposed Design\nbounded\n"))
        self.assertEqual(status, "PASS", status)
        self.assertEqual(agents["author"].calls, 2)
        self.assertEqual(quality["overall"]["eligible"], 1)
        self.assertEqual(quality["overall"]["unclassified"], 1)
        self.assertIsNotNone(quality["findings"][0]["unclassified_reason"])

    def test_a_final_finding_with_no_later_author_is_pending_and_excluded(self):
        wf = workflow(max_iterations=1)
        status, _e, _a, quality, _art = run_arch(
            verifier=[nr("the ring is unbounded")], wf=wf,
            arbitration=["## Arbitration Verdict\nNEEDS_HUMAN_DECISION\n"])
        self.assertEqual(status, "AWAITING_HUMAN", status)
        self.assertEqual(quality["overall"]["eligible"], 0)
        self.assertIsNone(quality["overall"]["rate"])
        self.assertEqual([e["state"] for e in quality["excluded"]], ["pending"])
        self.assertIn("no acceptance decision was ever asked for",
                      quality["excluded"][0]["reason"])

    def test_findings_superseded_by_arbitration_are_not_eligible(self):
        wf = workflow(escalate_after_cycles=1)
        status, _e, agents, quality, _art = run_arch(
            verifier=[nr("g1"), FEASIBLE], wf=wf,
            arbitration=["## Arbitration Verdict\nREVIEWER_IS_RIGHT\n\nBound it.\n"])
        self.assertEqual(status, "PASS", status)
        self.assertEqual(quality["overall"]["eligible"], 0)
        self.assertEqual([e["state"] for e in quality["excluded"]],
                         ["superseded_by_ruling"])
        self.assertNotIn("## Active Gate Findings", agents["author"].prompts[1])

    def test_source_gate_counts_sum_to_the_overall_counts(self):
        # One DV finding accepted, one TL finding accepted: two gates, two rows.
        author2 = ("# Draft 2\n\n## Proposed Design\nbounded ring\n\n"
                   "## Blocker Dispositions\n- DV-1-B1 | ACCEPTED | Proposed Design\n")
        author3 = ("# Draft 3\n\n## Proposed Design\nbounded ring\n\n"
                   "## Disagreements\n- no.\n\n"
                   "## Blocker Dispositions\n- TL-2-B1 | DISAGREED | Disagreements\n")
        _s, _e, _a, quality, _art = run_arch(
            verifier=[nr("the ring is unbounded"), FEASIBLE, FEASIBLE],
            author=("# Draft\n\n## Proposed Design\nd\n", author2, author3),
            lead=_Lead(control=[tl_reject(1), READY]),
            gate_replies=[justified("TL-2-B1")])
        gates = quality["by_source_gate"]
        self.assertEqual(set(gates), {"DV", "TL"})
        for key in ("accepted", "eligible", "disagreed", "unclassified"):
            with self.subTest(key=key):
                self.assertEqual(sum(g[key] for g in gates.values()),
                                 quality["overall"][key])
        self.assertEqual(quality["overall"], {"accepted": 1, "eligible": 2, "rate": 0.5,
                                              "disagreed": 1, "unclassified": 0})

    def test_plain_and_sub_threshold_routes_contribute_without_any_audit(self):
        # Neither gate needed an assessment for its findings to be measured: the
        # active-manifest contract does not depend on an audit having run.
        wf = workflow(semantic_audit=None)
        _s, _e, agents, quality, _art = run_arch(
            verifier=[nr("gap"), FEASIBLE, FEASIBLE], wf=wf,
            lead=_Lead(control=[tl_reject(1), READY]))
        self.assertEqual(agents["arbiter"].prompts["gate"], [])
        self.assertEqual(agents["arbiter"].prompts["repeat"], [])
        self.assertEqual(sorted(f["source_gate"] for f in quality["findings"]),
                         ["DV", "TL"])
        self.assertEqual(quality["overall"]["eligible"], 2)

    def test_the_schema_is_version_1_and_names_the_metric_and_its_limits(self):
        _s, _e, _a, quality, _art = run_arch(verifier=[FEASIBLE])
        self.assertEqual(quality["schema_version"], 1)
        self.assertEqual(quality["metric"], "author_blocker_acceptance_rate")
        self.assertIn("eligible", quality["formula"])
        self.assertIn("not whether the finding or the resulting proposal was "
                      "technically correct", quality["note"])

    def test_replay_reconstructs_observations_without_duplication(self):
        cache = {}
        cache.update(journaled("arch.author", "architecture", 1, "# Draft"))
        cache.update(journaled("arch.dev_review", "architecture", 1,
                               nr("journaled gap"), "NEEDS_REVISION"))
        _s, _e, _a, quality, _art = run_arch(
            verifier=[FEASIBLE],
            author=("# Draft 2\n\n## Proposed Design\nbounded\n\n"
                    "## Blocker Dispositions\n- DV-1-B1 | ACCEPTED | Proposed Design\n",),
            cache=cache)
        self.assertEqual([f["finding_id"] for f in quality["findings"]], ["DV-1-B1"])
        self.assertEqual(quality["overall"], {"accepted": 1, "eligible": 1, "rate": 1.0,
                                              "disagreed": 0, "unclassified": 0})

    def test_finishing_the_transcript_alone_never_writes_a_quality_file(self):
        # The signal handler in orchestrate.py calls tx.finish DIRECTLY. Only the
        # engine's _finish derives the metric, which is what keeps a killed run out of
        # the cohorts instead of contributing a partial value.
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-killed", "architecture", "task", False,
                            None, {})
            tx.finish("HUMAN_REQUIRED", "Interrupted by signal 15")
            self.assertFalse((tx.dir / "quality.json").exists())

    def test_a_non_architecture_run_writes_no_quality_file(self):
        # The metric is architecture-only; nothing else grows an artifact.
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-dev", "development", "task", False, None, {})
            engine = Engine(_Ctx(tx, {}, workflow()), "development")
            engine._finish("HUMAN_REQUIRED", "nothing ran")
            self.assertFalse((tx.dir / "quality.json").exists())

    def test_a_run_interrupted_before_the_loop_writes_no_quality_file(self):
        # `_quality` exists only once architecture processing began. A run killed by a
        # signal finishes the transcript DIRECTLY (orchestrate.py) and never reaches
        # _finish, so it contributes no fabricated value either.
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-early", "architecture", "task", False, None, {})
            engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
            engine._finish("HUMAN_REQUIRED", "died in setup")
            self.assertFalse((tx.dir / "quality.json").exists())

    def test_an_interrupted_finalization_publishes_no_staged_quality_file(self):
        # The engine derives the metric BEFORE tx.finish, so a signal CAN land with the
        # summary already computed. Deriving must therefore not be publishing: until the
        # normal finalization renames it, there is no `quality.json` to be swept into a
        # cohort, and a forced finalization drops it instead of adopting it.
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-mid", "architecture", "task", False, None, {})
            engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
            engine._quality = _ArchQuality()
            engine._write_quality()
            self.assertFalse((tx.dir / "quality.json").exists())
            tx.finish("HUMAN_REQUIRED", "Interrupted by signal 15", interrupted=True)
            self.assertFalse((tx.dir / "quality.json").exists())
            self.assertFalse((tx.dir / QUALITY_PENDING).exists())
            # The run is still finalized: excluding it from the cohorts must not cost it
            # its STATUS line, which is the only thing that says it is not still running.
            self.assertTrue((tx.dir / "STATUS").read_text(encoding="utf-8")
                            .startswith("HUMAN_REQUIRED"))

    def test_the_real_signal_handler_discards_a_staged_quality_file(self):
        # The wiring itself, not a hand-written imitation of it: install the handler
        # orchestrate.py installs, interrupt a run whose metric is already derived, and
        # check what the run directory is left holding. os._exit would take the test
        # process with it, so it is the one thing stubbed.
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-sig", "architecture", "task", False, None, {})
            engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
            engine._quality = _ArchQuality()
            engine._write_quality()
            previous = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]
            exits = []
            try:
                orchestrate._install_interrupt_handler(tx)
                handler = signal.getsignal(signal.SIGTERM)
                with mock.patch.object(orchestrate.os, "_exit", exits.append):
                    handler(signal.SIGTERM, None)
            finally:
                for sig, old_handler in zip((signal.SIGTERM, signal.SIGINT), previous):
                    signal.signal(sig, old_handler)
            self.assertEqual(exits, [2])
            self.assertFalse((tx.dir / "quality.json").exists())
            self.assertFalse((tx.dir / QUALITY_PENDING).exists())

    def test_a_signal_during_a_normal_finish_neither_publishes_nor_unpublishes(self):
        # A signal that arrives once finalization is already under way finds the
        # transcript finished and returns without touching anything. Whichever side of
        # the rename it lands on, the run keeps exactly one answer: published-and-
        # complete, or absent. Never a file the engine did not resolve.
        for engine_finished_first in (False, True):
            with self.subTest(published_before_signal=engine_finished_first):
                with tempfile.TemporaryDirectory() as tmp:
                    tx = Transcript(Path(tmp), "run-race", "architecture", "task",
                                    False, None, {})
                    engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
                    engine._quality = _ArchQuality()
                    engine._write_quality()
                    if engine_finished_first:
                        engine._finish("PASS", "engine won the race")
                    tx.finish("HUMAN_REQUIRED", "Interrupted by signal 15",
                              interrupted=True)
                    self.assertEqual((tx.dir / "quality.json").exists(),
                                     engine_finished_first)

    def test_the_resume_parent_is_recorded_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(Path(tmp), "run-res", "architecture", "task", False, None, {})
            tx.update_meta(resumed_from="run-parent")
            engine = Engine(_Ctx(tx, {}, workflow()), "architecture")
            engine._quality = _ArchQuality()
            engine._finish("PASS", "shape test only")
            q = json.loads((tx.dir / "quality.json").read_text(encoding="utf-8"))
        self.assertEqual(q["resumed_from"], "run-parent")


class TestUnchangedContracts(unittest.TestCase):
    ROLES = Path(__file__).resolve().parent.parent / "roles"

    def role(self, name):
        return (self.ROLES / f"{name}.md").read_text(encoding="utf-8")

    def test_the_developer_verifier_closure_rule_still_abuts_the_output_format(self):
        # The ratified adjacency (DEV-07). Re-pinned in run 2 because this change set
        # edits the roles either side of it.
        self.assertIn("without advancing it. `FEASIBLE` means \"buildable with the normal "
                      "amount of implementer\njudgment\", not \"nothing left to improve\"."
                      "\n\n## Output Format", self.role("Developer Verifier"))

    def test_the_tech_lead_requires_the_canonical_manifest_on_non_approval(self):
        text = self.role("Technical Lead")
        self.assertIn("## Architecture Blocking Findings", text)
        self.assertIn("projection, not a third opinion", text)
        self.assertIn("Advisory entries are excluded", text)
        self.assertIn("architecture-workflow only", text)

    def test_the_tech_lead_verdict_vocabulary_is_untouched(self):
        from orchestrator.verdicts import ROLE_VERDICTS
        self.assertEqual(ROLE_VERDICTS["Technical Lead"][1],
                         ["READY_FOR_HUMAN_REVIEW", "DEVELOPER_NEEDS_FIX",
                          "ARCHITECT_REVIEW_NEEDS_REVISION", "ESCALATE_TO_HUMAN"])

    def test_the_arbiter_carries_the_gate_marker_and_vocabulary(self):
        text = self.role("Arbitration Reviewer")
        self.assertIn(GATE_MARKER, text)
        self.assertIn(REPEAT_MARKER, text)
        for token in ("JUSTIFIED_GATE_DEPARTURE", "PARTIALLY_JUSTIFIED_GATE_DEPARTURE",
                      "UNJUSTIFIED_GATE_DEPARTURE"):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertIn("### Arbitration Verdict", text)      # ordinary mode intact
        self.assertIn("DEVELOPER_IS_RIGHT", text)

    def test_the_author_knows_the_disposition_block_is_stripped(self):
        text = self.role("Architecture Author")
        self.assertIn("## Blocker Dispositions", text)
        self.assertIn("Partial acceptance is `DISAGREED`", text)
        self.assertIn("parses it and\nthen removes it", text)

    def test_the_development_workflow_declares_no_semantic_audit(self):
        from orchestrator.config import load_config
        _agents, wf = load_config()
        for name in ("development", "investigation", "review", "implreview", "mrreview"):
            with self.subTest(workflow=name):
                self.assertNotIn("semantic_audit", wf[name])

    def test_the_gate_step_ids_are_the_ones_the_journal_records(self):
        self.assertEqual(SEMANTIC_GATE_STEP, "arch.semantic_gate_departure")
        self.assertEqual(SEMANTIC_REPEAT_STEP, "arch.semantic_repeat")
        self.assertEqual(TECHLEAD_RECONSIDER_STEP, "arch.tech_lead_reconsider")


if __name__ == "__main__":
    unittest.main()
