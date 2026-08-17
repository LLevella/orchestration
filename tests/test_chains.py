"""Integration test for the `chains` command (agent<->role chain inspector).

Runs the real CLI (no credentials, creates no run dir) and asserts the output names
every workflow, every configured agent, the writer marker, and the arbiter seat.
Copy-agnostic: works for the DeepSeek and Mistral copies and surfaces any active
TEMP_OVERRIDE substitution verbatim, so it never hard-codes a roster.
"""
import subprocess
import sys
import unittest
from pathlib import Path

from orchestrator.config import load_config

ORCH = Path(__file__).resolve().parent.parent


def _run(*extra):
    p = subprocess.run(
        [sys.executable, "orchestrate.py", "chains", *extra],
        cwd=str(ORCH), capture_output=True, text=True, timeout=60,
    )
    return p.returncode, p.stdout + p.stderr


class TestChains(unittest.TestCase):
    def test_lists_all_workflows_and_every_agent(self):
        rc, out = _run()
        self.assertEqual(rc, 0, out[-600:])
        for wf in ("ARCHITECTURE", "DEVELOPMENT", "INVESTIGATION"):
            self.assertIn(wf, out, f"workflow {wf} missing from chains output")
        agents_cfg, _ = load_config()
        for name in agents_cfg["agents"]:
            if name.startswith("_"):
                continue
            self.assertIn(name, out, f"agent {name} missing from chains output")
        self.assertIn("arbiter seat", out)   # the swappable independent family is shown
        self.assertIn("WRITER", out)         # the editing agent is marked
        self.assertIn("→", out)              # agent -> role arrow

    def test_workflow_filter_limits_output(self):
        rc, out = _run("inv")
        self.assertEqual(rc, 0, out[-600:])
        self.assertIn("INVESTIGATION", out)
        self.assertNotIn("ARCHITECTURE", out)  # other chains are filtered out

    def test_invalid_workflow_is_rejected(self):
        rc, _ = _run("nope")
        self.assertNotEqual(rc, 0)  # argparse choices rejects an unknown workflow


if __name__ == "__main__":
    unittest.main()
