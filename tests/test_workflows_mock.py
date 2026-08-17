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
SUCCESS = {"PASS", "READY_FOR_HUMAN_REVIEW", "ROOT_CAUSE_ESTABLISHED", "NO_DEFECT_FOUND"}
# fixtures dirs use full workflow names; the CLI subcommand is the short form.
CMD = {"architecture": "arch", "development": "dev", "investigation": "inv"}


def _run(workflow, scenario):
    """Run one mock workflow; return (returncode, final_status, output). Cleans its run dir."""
    p = subprocess.run(
        [sys.executable, "orchestrate.py", CMD[workflow], "--mock",
         "--scenario", scenario, "--task", "unit-test"],
        cwd=str(ORCH), capture_output=True, text=True, timeout=180,
    )
    out = p.stdout + p.stderr
    m = re.search(r"run (\S+) \(mock=True", out)
    if m:
        shutil.rmtree(RUNS / m.group(1), ignore_errors=True)
    ms = re.search(r"FINAL STATUS: (\S+)", out)
    return p.returncode, (ms.group(1) if ms else None), out


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

    def test_dev_pass(self):
        rc, status, out = _run("development", "pass")
        self.assertEqual(status, "READY_FOR_HUMAN_REVIEW", out[-600:])
        self.assertEqual(rc, 0)

    def test_every_shipped_scenario_terminates_cleanly(self):
        # Regression net: every fixture in every workflow must reach a terminal status
        # with a clean exit code (0 success / 2 human-required) — never hang or crash.
        ran = 0
        for wf in ("architecture", "development", "investigation"):
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
