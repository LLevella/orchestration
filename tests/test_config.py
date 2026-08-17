"""Unit tests for config loading + cross-config consistency.

The tests assert chain structure and internal consistency, not a product-domain
context. The consistency test catches missing role files or agent role allow-list
drift.
"""
import os
import subprocess
import tempfile
import unittest

from orchestrator.verdicts import ROLE_VERDICTS
from orchestrator.config import load_config, RunContext

WORKFLOWS = ("architecture", "development", "investigation")


class TestConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents_cfg, cls.wf = load_config()
        cls.agents = cls.agents_cfg["agents"]

    def test_loads_all_workflows(self):
        self.assertIn("agents", self.agents_cfg)
        for w in WORKFLOWS:
            self.assertIn(w, self.wf)

    def test_arch_is_developer_centric(self):
        a = self.wf["architecture"]
        self.assertEqual(a["author"]["agent"], "codex")
        # primary review = a Claude-FAMILY agent (the developer who builds it reviews). A copy
        # may use a dedicated reviewer instance (e.g. claude_reviewer / a cost-tier Claude), so
        # assert the family, not the exact name.
        self.assertTrue(a["developer_verifier"]["agent"].startswith("claude"),
                        f"developer_verifier must be Claude-family, got {a['developer_verifier']['agent']}")
        self.assertEqual(a["tech_lead"]["agent"], "gemini")           # tier-1 arbitration
        self.assertNotIn("code_reviewer", a)                          # standalone gate dropped
        self.assertIn("arbiter", a)                                   # tier-2 arbitration

    def test_dev_code_review_is_peer_review(self):
        d = self.wf["development"]
        self.assertEqual(d["developer"]["agent"], "claude")
        # peer code review = a Claude-FAMILY agent (developers review each other's code); a
        # dedicated reviewer instance (claude_reviewer / cost-tier Claude) is allowed.
        self.assertTrue(d["code_reviewer"]["agent"].startswith("claude"),
                        f"code_reviewer must be Claude-family, got {d['code_reviewer']['agent']}")
        self.assertEqual(d["architect"]["agent"], "codex")       # codex = architecture review
        self.assertEqual(d["tech_lead"]["agent"], "gemini")

    def test_inv_chain_shape(self):
        i = self.wf["investigation"]
        inv = i["investigator"]["agent"]
        self.assertEqual(inv, "claude")  # only role permitted to edit/build
        # refuter/verifier/arbiter agents legitimately vary (e.g. a provider-outage
        # override), so don't hardcode them: assert they are read-only skeptics, not the
        # investigator, and that the two first-line skeptics (refuter, verifier) differ.
        for key in ("refuter", "verifier", "arbiter"):
            a = i[key]["agent"]
            self.assertIn(a, self.agents, f"inv.{key}: unknown agent '{a}'")
            self.assertFalse(self.agents[a].get("can_edit", False), f"inv.{key}: must be read-only")
            self.assertNotEqual(a, inv, f"inv.{key}: must not be the investigator")
        self.assertNotEqual(i["refuter"]["agent"], i["verifier"]["agent"],
                            "refuter and verifier must be two distinct skeptics")

    def test_arbiter_is_a_readonly_independent_agent(self):
        # copy-agnostic: arbiter agent differs per copy (DeepSeek / Mistral). Assert it is
        # a configured, read-only agent that is NOT one of the three disputants.
        for w in WORKFLOWS:
            arb = self.wf[w]["arbiter"]["agent"]
            self.assertIn(arb, self.agents, f"{w}: arbiter agent {arb} not configured")
            self.assertFalse(self.agents[arb].get("can_edit", False),
                             f"{w}: arbiter {arb} must be read-only")
            self.assertNotIn(arb, ("claude", "codex", "gemini"),
                             f"{w}: arbiter {arb} should be an independent 4th family")

    def test_workflow_config_consistency(self):
        """THE guard. Every (agent, role) used by any workflow must:
          (a) reference a configured agent,
          (b) have that role in the agent's roles allowlist  (catches the agents.json miss),
          (c) have a role file on disk,
          (d) have a verdict spec in verdicts.py.
        """
        present = RunContext(self.agents_cfg, self.wf, {}, None, None).roles_present()
        for w in WORKFLOWS:
            for key, spec in self.wf[w].items():
                if not (isinstance(spec, dict) and "agent" in spec):
                    continue
                agent, role = spec["agent"], spec["role"]
                self.assertIn(agent, self.agents, f"{w}.{key}: unknown agent '{agent}'")
                self.assertIn(role, self.agents[agent]["roles"],
                              f"{w}.{key}: role '{role}' not in '{agent}' roles allowlist (agents.json)")
                self.assertIn(role, ROLE_VERDICTS,
                              f"{w}.{key}: role '{role}' has no verdict spec (verdicts.py)")
                self.assertTrue(present.get(role),
                                f"{w}.{key}: role file for '{role}' missing on disk")


class TestCaptureDiffBaseline(unittest.TestCase):
    """Regression (Irina 2026-07-09): an upstream `git pull` that fast-forwards a
    READ-ONLY analysis repo mid-run — HEAD moves past the frozen run-start baseline
    while the working tree stays clean — must NOT appear in capture_diff. That is
    repo drift (another team's merged work), not a developer scope violation; the
    old code diffed read-only repos against the frozen baseline and so swept the
    upstream delta into every review, wrongly blocking the run until it deadlocked.
    A developer's illicit working-tree edit in a read-only repo MUST still surface,
    and writable repos keep the run-start baseline so committed dev work is shown.
    Copy-agnostic: uses throwaway git repos, never the configured repos.
    """

    @classmethod
    def setUpClass(cls):
        cls.agents_cfg, cls.wf = load_config()

    def _git(self, repo, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
            capture_output=True, text=True, check=True)

    def _init_repo(self, path):
        os.makedirs(path, exist_ok=True)
        self._git(path, "init", "-q")
        with open(os.path.join(path, "f.txt"), "w") as fh:
            fh.write("base\n")
        self._git(path, "add", "-A")
        self._git(path, "commit", "-q", "-m", "base")
        return self._git(path, "rev-parse", "HEAD").stdout.strip()

    def _ctx(self, dev, ana, dev_base, ana_base):
        ctx = RunContext(self.agents_cfg, self.wf, {}, None, None)
        ctx.repos = [("devrepo", dev, "dev"), ("anarepo", ana, "analysis")]
        ctx._baselines = {"devrepo": dev_base, "anarepo": ana_base}
        ctx.exclude = "orchestration"
        return ctx

    def test_upstream_pull_in_readonly_repo_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev, ana = os.path.join(tmp, "dev"), os.path.join(tmp, "ana")
            dev_base, ana_base = self._init_repo(dev), self._init_repo(ana)
            # Simulate `git pull` fast-forward: a NEW commit lands in the read-only
            # repo, moving HEAD past the baseline; the working tree stays clean.
            with open(os.path.join(ana, "upstream.txt"), "w") as fh:
                fh.write("another team's merged work\n")
            self._git(ana, "add", "-A")
            self._git(ana, "commit", "-q", "-m", "upstream merge")
            diff = self._ctx(dev, ana, dev_base, ana_base).capture_diff()
            self.assertNotIn("anarepo", diff)
            self.assertNotIn("upstream.txt", diff)

    def test_working_tree_edit_in_readonly_repo_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev, ana = os.path.join(tmp, "dev"), os.path.join(tmp, "ana")
            dev_base, ana_base = self._init_repo(dev), self._init_repo(ana)
            # A developer illicitly edits the read-only repo's working tree (uncommitted).
            with open(os.path.join(ana, "f.txt"), "w") as fh:
                fh.write("base\nillicit edit\n")
            diff = self._ctx(dev, ana, dev_base, ana_base).capture_diff()
            self.assertIn("anarepo", diff)
            self.assertIn("READ-ONLY", diff)

    def test_writable_repo_uses_frozen_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev, ana = os.path.join(tmp, "dev"), os.path.join(tmp, "ana")
            dev_base, ana_base = self._init_repo(dev), self._init_repo(ana)
            # Committed developer work past the baseline must still be shown for a
            # writable repo (baseline diff captures committed + uncommitted work).
            with open(os.path.join(dev, "feature.txt"), "w") as fh:
                fh.write("developer feature\n")
            self._git(dev, "add", "-A")
            self._git(dev, "commit", "-q", "-m", "feature")
            diff = self._ctx(dev, ana, dev_base, ana_base).capture_diff()
            self.assertIn("devrepo", diff)
            self.assertIn("WRITABLE", diff)


if __name__ == "__main__":
    unittest.main()
