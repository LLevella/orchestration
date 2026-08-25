"""Integration tests: run each workflow end-to-end through the mock path.

Exercises the real engine state machines (the developer-centric arch chain, the dev
chain, the investigation loop) via scripted fixtures — no AI creds, no network. Asserts
the final terminal status + exit code (0 success / 2 HUMAN_REQUIRED). Copy-agnostic:
mock adapters ignore the arbiter model, so this passes for both DeepSeek and Mistral copies.

Each run is cleaned up (its runs/<id>/ dir is removed).
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ORCH = Path(__file__).resolve().parent.parent          # orchestration/
RUNS = ORCH / "runs"
SUCCESS = {"PASS", "READY_FOR_HUMAN_REVIEW", "ROOT_CAUSE_ESTABLISHED", "NO_DEFECT_FOUND",
           "REVIEW_COMPLETE"}
# fixtures dirs use full workflow names; the CLI subcommand is the short form.
CMD = {"architecture": "arch", "development": "dev", "investigation": "inv",
       "review": "review"}


def _run(workflow, scenario, keep=False):
    """Run one mock workflow; return (returncode, final_status, output), or
    (..., run_dir) when `keep` — the caller then owns the cleanup. Without `keep`
    the run dir is removed here."""
    p = subprocess.run(
        [sys.executable, "orchestrate.py", CMD[workflow], "--mock",
         "--scenario", scenario, "--task", "unit-test"],
        cwd=str(ORCH), capture_output=True, text=True, timeout=180,
    )
    out = p.stdout + p.stderr
    m = re.search(r"run (\S+) \(mock=True", out)
    run_dir = RUNS / m.group(1) if m else None
    if m and not keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    ms = re.search(r"FINAL STATUS: (\S+)", out)
    status = ms.group(1) if ms else None
    return (p.returncode, status, out, run_dir) if keep else (p.returncode, status, out)


def _scenarios(workflow):
    return sorted(p.stem for p in (ORCH / "fixtures" / workflow).glob("*.json"))


class TestWorkflowsMock(unittest.TestCase):
    def test_arch_pass(self):
        rc, status, out = _run("architecture", "pass")
        self.assertEqual(status, "PASS", out[-600:])
        self.assertEqual(rc, 0)

    def test_arch_escalate_reaches_human_required(self):
        # dev-review NEEDS_REVISION deadlock -> Tech Lead -> Arbiter -> HUMAN_REQUIRED
        rc, status, out = _run("architecture", "escalate")
        self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
        self.assertEqual(rc, 2)

    def test_review_pass_writes_the_remarks_document(self):
        # The panel run's deliverable is a file, so "it finished" is not enough:
        # assert the document exists and carries the consolidated remarks.
        rc, status, out, run_dir = _run("review", "pass", keep=True)
        try:
            self.assertEqual(status, "REVIEW_COMPLETE", out[-600:])
            self.assertEqual(rc, 0)
            doc = (run_dir / "artifacts" / "mr-review.md").read_text(encoding="utf-8")
            self.assertIn("## Remarks", doc)
            self.assertIn("by 2 of 4 reviewers", doc)           # deterministic tally
            self.assertIn("Reviewer 4", doc)                    # every seat is accounted for
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_review_without_consolidation_still_delivers_and_flags_it(self):
        # Consolidation failed; the four opinions were still paid for, so the document
        # must exist, say it is unmerged, and the run must NOT read as a success.
        rc, status, out, run_dir = _run("review", "unmoderated", keep=True)
        try:
            self.assertEqual(status, "HUMAN_REQUIRED", out[-600:])
            self.assertEqual(rc, 2)
            doc = (run_dir / "artifacts" / "mr-review.md").read_text(encoding="utf-8")
            self.assertIn("NOT consolidated", doc)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_dev_pass(self):
        rc, status, out = _run("development", "pass")
        self.assertEqual(status, "READY_FOR_HUMAN_REVIEW", out[-600:])
        self.assertEqual(rc, 0)

    def test_every_shipped_scenario_terminates_cleanly(self):
        # Regression net: every fixture in every workflow must reach a terminal status
        # with a clean exit code (0 success / 2 human-required) — never hang or crash.
        ran = 0
        for wf in CMD:
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
