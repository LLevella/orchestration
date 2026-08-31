"""Integration tests: run each workflow end-to-end through the mock path.

Exercises the real engine state machines (the developer-centric arch chain, the dev
chain, the investigation loop) via scripted fixtures — no AI creds, no network. Asserts
the final terminal status + exit code (0 success / 2 HUMAN_REQUIRED). Copy-agnostic:
mock adapters ignore the arbiter model, so this passes for both DeepSeek and Mistral copies.

Each run is cleaned up (its runs/<id>/ dir is removed).
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from orchestrator.config import load_config, project_base
from orchestrator.transcript import QUALITY_PENDING

ORCH = Path(__file__).resolve().parent.parent          # the tool directory
# Run history is not necessarily inside the tool: a project layer can point runs_dir
# somewhere else, and then that is where these runs must be found and cleaned up.
_AGENTS, _WF = load_config()
RUNS = ((project_base(_WF) / _WF["paths"]["runs_dir"]).resolve()
        if _WF["paths"].get("runs_dir") else ORCH / "runs")
SUCCESS = {"PASS", "READY_FOR_HUMAN_REVIEW", "ROOT_CAUSE_ESTABLISHED", "NO_DEFECT_FOUND",
           "REVIEW_COMPLETE", "IMPLREVIEW_COMPLETE"}
# fixtures dirs use full workflow names; the CLI subcommand is the short form.
CMD = {"architecture": "arch", "development": "dev", "investigation": "inv", "review": "review",
       "implreview": "implreview", "mrreview": "mrreview"}


def _step_counts(run_dir):
    """`step=N` per logical step id, from the run's own journal.

    Terminal status alone is too weak an assertion for a scripted fixture: a queue that
    runs dry silently repeats its last response, so a fixture can keep "passing" while
    taking a completely different route through the engine. Counting the steps pins the
    route — and, for ARCH-02, pins that the ordinary path pays for no audit."""
    ev = run_dir / "events.jsonl"
    counts = {}
    if not ev.exists():
        return ""
    for line in ev.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "step":
            counts[e["step"]] = counts.get(e["step"], 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _derived(run_dir):
    """The run's DERIVED evidence, appended to the captured output so a test can assert
    on it after the run directory is removed: `quality.json`, the proposal artifacts, and
    the raw author step (the one place the author's disposition block is allowed to
    survive)."""
    out = ""
    q = run_dir / "quality.json"
    if q.exists():
        out += "\n[QUALITY FILE]\n" + q.read_text(encoding="utf-8").strip() + "\n"
    # The staging path quality.json is published FROM. A normally finished run must
    # leave none behind: one surviving here would mean the rename never happened and
    # the run's own measurement stayed invisible to the cohorts.
    out += "\n[QUALITY PENDING] " + str((run_dir / QUALITY_PENDING).exists()) + "\n"
    out += "\n[ARTIFACTS]\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((run_dir / "artifacts").glob("*.md"))
    ) + "\n"
    out += "\n[RAW AUTHOR]\n" + "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((run_dir / "iterations").glob("*arch-author*.md"))) + "\n"
    return out


def _run(workflow, scenario, extra=None, keep=False):
    """Run one mock workflow; return (returncode, final_status, output). The run's
    STATUS file (status + reason) and its per-step counts are appended to `output`
    before the run dir is removed, so tests can assert on the reason (e.g. which
    iteration converged) and on the route actually taken. `keep=True` additionally
    appends the run's derived evidence (see `_derived`)."""
    cmd = [sys.executable, "orchestrate.py", CMD[workflow], "--mock",
           "--scenario", scenario, "--task", "unit-test"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, cwd=str(ORCH), capture_output=True, text=True, timeout=180)
    out = p.stdout + p.stderr
    m = re.search(r"run (\S+) \(mock=True", out)
    if m:
        sf = RUNS / m.group(1) / "STATUS"
        if sf.exists():
            out += "\n[STATUS FILE]\n" + sf.read_text(encoding="utf-8")
        out += "\n[STEP COUNTS] " + _step_counts(RUNS / m.group(1)) + "\n"
        if keep:
            out += _derived(RUNS / m.group(1))
        shutil.rmtree(RUNS / m.group(1), ignore_errors=True)
    ms = re.search(r"FINAL STATUS: (\S+)", out)
    return p.returncode, (ms.group(1) if ms else None), out


def _counts(out):
    line = out.split("[STEP COUNTS] ", 1)[1].splitlines()[0] if "[STEP COUNTS] " in out else ""
    return dict((k, int(v)) for k, v in (kv.split("=") for kv in line.split() if "=" in kv))


def _scenarios(workflow):
    return sorted(p.stem for p in (ORCH / "fixtures" / workflow).glob("*.json"))


class TestWorkflowsMock(unittest.TestCase):
    def test_arch_pass(self):
        rc, status, out = _run("architecture", "pass")
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(rc, 0)

    def test_arch_happy_path_makes_no_semantic_call(self):
        # ARCH-02's central cost promise: a run where no repeat occurs pays for zero
        # additional agent calls. This fixture IS that promise.
        _rc, status, out = _run("architecture", "pass")
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(_counts(out),
                         {"arch.author": 1, "arch.dev_review": 1, "arch.tech_lead": 1},
                         out[-600:])

    def test_arch_techlead_rework_assesses_the_gate_departure_once(self):
        # The Developer returns FEASIBLE on every draft, so no REPEAT audit is ever
        # eligible — but the Tech-Lead rejection overturns a FEASIBLE result, so it IS
        # assessed for gate departure, exactly once, and the assessment finds it
        # justified. Counted, not inferred: an exhausted mock queue repeats its last
        # response, so only the step counts prove which route ran.
        _rc, status, out = _run("architecture", "techlead_rework")
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(_counts(out),
                         {"arch.author": 2, "arch.dev_review": 2, "arch.tech_lead": 2,
                          "arch.semantic_gate_departure": 1}, out[-600:])
        counts = out.split("[STEP COUNTS]", 1)[1]
        self.assertNotIn("arch.semantic_repeat=", counts)
        self.assertNotIn("arch.tech_lead_reconsider", counts)

    def test_arch_techlead_rework_measures_the_accepted_blocker(self):
        # The whole quality signal of this run: the Tech Lead's TL-1-B1 reached the
        # author as an active finding, and draft 2 said it applied it. The dispositions
        # live in the raw author step only — never in the proposal artifacts.
        rc, _status, out = _run("architecture", "techlead_rework", keep=True)
        self.assertEqual(rc, 0)
        quality = json.loads(out.split("[QUALITY FILE]\n", 1)[1].split("\n[", 1)[0])
        self.assertEqual(quality["schema_version"], 1)
        self.assertEqual(quality["overall"], {"accepted": 1, "eligible": 1, "rate": 1.0,
                                              "disagreed": 0, "unclassified": 0})
        self.assertEqual(quality["by_source_gate"]["TL"]["accepted"], 1)
        self.assertEqual(quality["findings"][0]["finding_id"], "TL-1-B1")
        # Published by the normal finish, not merely staged by the derivation.
        self.assertIn("[QUALITY PENDING] False", out)
        artifacts = out.split("[ARTIFACTS]\n", 1)[1].split("\n[RAW AUTHOR]", 1)[0]
        self.assertNotIn("Blocker Dispositions", artifacts)
        self.assertIn("Blocker Dispositions", out.split("[RAW AUTHOR]\n", 1)[1])

    def test_arch_pass_writes_a_null_rate_rather_than_zero(self):
        # A first-pass successful run has no eligible finding at all. `rate: null` is the
        # honest answer; 0.0 would read as "the author accepted nothing".
        _rc, _status, out = _run("architecture", "pass", keep=True)
        quality = json.loads(out.split("[QUALITY FILE]\n", 1)[1].split("\n[", 1)[0])
        self.assertEqual(quality["overall"]["eligible"], 0)
        self.assertIsNone(quality["overall"]["rate"])
        self.assertEqual(quality["findings"], [])

    def test_arch_escalate_pauses_for_human(self):
        # Developer NEEDS_REVISION 5x -> Arbiter -> NEEDS_HUMAN_DECISION -> run PAUSES
        # (AWAITING_HUMAN) for a resumable human decision.
        rc, status, out = _run("architecture", "escalate")
        self.assertEqual(status, "AWAITING_HUMAN", out[-600:])
        self.assertEqual(rc, 2)
        self.assertIn("Arbiter", out, out[-600:])

    def test_arch_escalate_reaches_the_arbiter_through_the_semantic_route(self):
        # ARCH-02 (2026-08-31): the byte-identical objection is ruled a repeat at streak
        # 2 (one compliance revision) and again at streak 3, so full arbitration happens
        # at streak 3 instead of streak 5. The terminal status is unchanged; the route
        # and its cost are not. Counted, not inferred: an exhausted mock queue repeats
        # its last response, so only the step counts prove which route ran.
        _rc, _status, out = _run("architecture", "escalate")
        self.assertEqual(_counts(out),
                         {"arch.author": 3, "arch.dev_review": 3,
                          "arch.semantic_repeat": 2, "arch.arbiter": 1}, out[-600:])

    def test_arch_human_decide_pauses_without_answer(self):
        # No --human-answer: the arbiter's NEEDS_HUMAN_DECISION pauses the run.
        rc, status, out = _run("architecture", "human_decide")
        self.assertEqual(status, "AWAITING_HUMAN", out[-600:])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(out),
                         {"arch.author": 3, "arch.dev_review": 3,
                          "arch.semantic_repeat": 2, "arch.arbiter": 1}, out[-600:])

    def test_arch_human_answer_resumes_to_pass(self):
        # With --human-answer the arbiter's deferral is filled by the human ruling, the
        # Developer then reaches FEASIBLE and the Tech Lead approves -> PASS.
        rc, status, out = _run("architecture", "human_decide",
                               extra=["--human-answer", "Bound the buffer to a fixed 4-entry budget."])
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(rc, 0)
        self.assertIn("iteration 4", out, out[-600:])
        self.assertEqual(_counts(out),
                         {"arch.author": 4, "arch.dev_review": 4, "arch.tech_lead": 1,
                          "arch.semantic_repeat": 2, "arch.arbiter": 1}, out[-600:])

    def test_arch_human_answer_survives_the_arbiters_own_rulings(self):
        # Regression (2026-08-26): the dispute runs to the iteration budget and the arbiter
        # decides on its own TWICE along the way. Those rulings must not swallow the pending
        # human answer, and at the budget boundary the answer must outrank the arbiter and buy
        # a bounded allowance — otherwise the answer has no iteration to be applied in and the
        # run pauses again (which is exactly how resumes kept dying).
        rc, status, out = _run("architecture", "human_at_budget",
                               extra=["--human-answer", "Take the full decomposition as drafted."])
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(rc, 0)
        self.assertIn("iteration 11", out, out[-600:])   # spent past max_iterations=10
        # ARCH-02: the first streak materially progresses, so its three eligible audits
        # (streaks 2-4) change no route and exhaust max_calls_per_mode=3. The second
        # streak is therefore not audited at all and the TWO mid-loop arbiter rulings
        # this fixture exists to pin still happen, at streaks 5 and 10.
        self.assertEqual(_counts(out),
                         {"arch.author": 11, "arch.dev_review": 11, "arch.tech_lead": 1,
                          "arch.semantic_repeat": 3, "arch.arbiter": 2}, out[-600:])

    def test_arch_human_at_budget_pauses_without_answer(self):
        # The same dispute without an answer still stops at the cap: the extra allowance is
        # bought by a human answer, it does not loosen the safety cap on its own.
        rc, status, out = _run("architecture", "human_at_budget")
        self.assertEqual(status, "AWAITING_HUMAN", out[-600:])
        self.assertEqual(rc, 2)
        # The third, tail arbitration happens ONLY in the no-answer case.
        self.assertEqual(_counts(out),
                         {"arch.author": 10, "arch.dev_review": 10,
                          "arch.semantic_repeat": 3, "arch.arbiter": 3}, out[-600:])

    def test_arch_techlead_reject_loops_then_passes(self):
        # Developer FEASIBLE but Tech-Lead rejects once -> back to Architect<->Developer
        # (not straight to the arbiter) -> FEASIBLE again -> Tech-Lead READY -> PASS at iter 2.
        rc, status, out = _run("architecture", "techlead_rework")
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(rc, 0)
        self.assertIn("iteration 2", out, out[-600:])

    def test_dev_pass(self):
        rc, status, out = _run("development", "pass")
        self.assertEqual(status, "READY_FOR_HUMAN_REVIEW", out[-600:])
        self.assertEqual(rc, 0)

    def test_development_fixtures_keep_their_vocabularies_and_routes(self):
        # ARCH-02 is architecture-only. The development Tech-Lead and Arbiter steps keep
        # their own verdicts and their own routes: no semantic audit, no reconsideration,
        # and no quality artifact anywhere in the workflow.
        for scenario in _scenarios("development"):
            with self.subTest(scenario=scenario):
                _rc, status, out = _run("development", scenario, keep=True)
                self.assertIsNotNone(status)
                counts = out.split("[STEP COUNTS]", 1)[1]
                for step in ("semantic_repeat", "semantic_gate_departure",
                             "tech_lead_reconsider"):
                    self.assertNotIn(step, counts)
                self.assertNotIn("[QUALITY FILE]", out)

    def test_review_pass(self):
        # Panel (3 lenses) + Review Arbiter NEEDS_REVISION -> REVIEW_COMPLETE (success).
        rc, status, out = _run("review", "pass")
        self.assertEqual(status, "REVIEW_COMPLETE", out[-600:])
        self.assertEqual(rc, 0)

    def test_review_insufficient_reaches_human_required(self):
        # Arbiter INSUFFICIENT_INPUT -> HUMAN_REQUIRED (a review fails only on infra/no-input).
        rc, status, out = _run("review", "insufficient")
        self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
        self.assertEqual(rc, 2)

    def test_review_iterate_converges(self):
        # Refine loop: round 1 NEEDS_REVISION -> reviser -> round 2 STRONG -> REVIEW_COMPLETE.
        rc, status, out = _run("review", "converge", extra=["--iterate"])
        self.assertEqual(status, "REVIEW_COMPLETE", out[-600:])
        self.assertEqual(rc, 0)
        # Proves it actually LOOPED (converged on round 2), not one-shot: reason names iteration 2.
        self.assertIn("iteration 2", out, out[-600:])

    def test_review_iterate_stuck_reaches_human_required(self):
        # Never reaches STRONG within the budget -> HUMAN_REQUIRED (like arch not reaching FEASIBLE).
        rc, status, out = _run("review", "stuck", extra=["--iterate", "--max-iterations", "2"])
        self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
        self.assertEqual(rc, 2)

    def test_implreview_pass(self):
        # Round 1: both peers FEASIBLE -> Tech Lead READY -> PM APPROVED -> IMPLREVIEW_COMPLETE.
        rc, status, out = _run("implreview", "pass")
        self.assertEqual(status, "IMPLREVIEW_COMPLETE", out[-600:])
        self.assertEqual(rc, 0)

    def test_implreview_rework_converges_on_round_two(self):
        # Round 1 developer NEEDS_REVISION -> back to the author; round 2 both FEASIBLE ->
        # Tech Lead READY -> PM APPROVED. Proves the back-edge (converged at iteration 2).
        rc, status, out = _run("implreview", "rework")
        self.assertEqual(status, "IMPLREVIEW_COMPLETE", out[-600:])
        self.assertEqual(rc, 0)
        self.assertIn("iteration 2", out, out[-600:])

    def test_implreview_stuck_reaches_human_required(self):
        # Developer repeats the SAME block; after trigger_after_cycles it routes to the
        # Arbiter, which returns NEEDS_HUMAN_DECISION -> HUMAN_REQUIRED.
        rc, status, out = _run("implreview", "stuck")
        self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
        self.assertEqual(rc, 2)
        self.assertIn("Arbiter", out, out[-600:])

    def test_mrreview_panel_writes_the_remarks_document(self):
        # Four experts + moderator over a canned diff: REVIEW_COMPLETE, and the
        # deliverable exists. The document is the point of the workflow, so a run that
        # ends "successfully" without one is a failure.
        rc, status, out = _run("mrreview", "pass")
        self.assertEqual(status, "REVIEW_COMPLETE", out[-600:])
        self.assertEqual(rc, 0)
        self.assertIn("mr-review.md", out, out[-600:])

    def test_mrreview_unmoderated_still_delivers_and_flags_the_human(self):
        # Consolidation returns INSUFFICIENT_INPUT: the four opinions are never thrown
        # away (the document holds them unmerged) but the run ends HUMAN_REQUIRED.
        rc, status, out = _run("mrreview", "unmoderated")
        self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
        self.assertEqual(rc, 2)
        self.assertIn("mr-review.md", out, out[-600:])

    def test_a_run_records_the_code_it_ran_on(self):
        # A run that cannot say which code produced it cannot be compared with any
        # other run (2026-08-30). End-to-end: the version reaches the STATUS footer a
        # human actually reads, through the real CLI. The CONTENT is not asserted here
        # — a checkout with no git records a stated null, which is equally correct.
        _rc, _status, out = _run("development", "pass")
        self.assertIn("[STATUS FILE]", out)
        self.assertIn("code: ", out.split("[STATUS FILE]", 1)[1])

    def test_every_shipped_scenario_terminates_cleanly(self):
        # Regression net: every fixture in every workflow must reach a terminal status
        # with a clean exit code (0 success / 2 human-required) — never hang or crash.
        ran = 0
        for wf in ("architecture", "development", "investigation", "review", "implreview",
                   "mrreview"):
            for sc in _scenarios(wf):
                rc, status, out = _run(wf, sc)
                self.assertIsNotNone(status, f"{wf}/{sc}: no FINAL STATUS\n{out[-600:]}")
                self.assertIn(rc, (0, 2), f"{wf}/{sc}: unexpected exit {rc}\n{out[-600:]}")
                # exit code must agree with the terminal vocabulary
                self.assertEqual(rc == 0, status in SUCCESS, f"{wf}/{sc}: rc/status mismatch ({rc},{status})")
                ran += 1
        self.assertGreaterEqual(ran, 6, "expected several shipped scenarios")


if __name__ == "__main__":
    unittest.main()
