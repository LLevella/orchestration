"""Unit tests for config loading + cross-config consistency.

Copy-agnostic: works for BOTH orchestrator copies (dataplane arbiter=DeepSeek,
phs-rs arbiter=Mistral) — it asserts CHAIN STRUCTURE and INTERNAL CONSISTENCY, never
a specific arbiter model. The consistency test is the guard that would have caught the
agents.json roles-allowlist miss (a role used in a workflow but not allowed for its agent).
"""
import unittest

from orchestrator.verdicts import ROLE_VERDICTS
from orchestrator.config import load_config, RunContext, WORKFLOWS as SHIPPED_WORKFLOWS

WORKFLOWS = ("architecture", "development", "investigation", "review")
# implreview uses a different arbiter convention (its arbiter is gemini; its independent
# 4th-family gate is the deepseek/mistral GATING cell) and mrreview has no arbiter at all
# (a panel of four peers plus a moderator, nothing to break a deadlock over), so both are
# exempt from the arbiter-is-4th-family test — but still covered by the consistency guard.
ALL_WORKFLOWS = SHIPPED_WORKFLOWS


class TestConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents_cfg, cls.wf = load_config()
        cls.agents = cls.agents_cfg["agents"]

    def test_loads_all_workflows(self):
        self.assertIn("agents", self.agents_cfg)
        for w in ALL_WORKFLOWS:
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

    def test_dev_has_no_code_review_architect_reviews_directly(self):
        """Code-review stage removed (Irina 2026-08-17): the developer is Claude, and after
        implementation the ARCHITECT (codex) reviews directly — there is no code_reviewer cell,
        and the roster chain/cells must not carry one."""
        d = self.wf["development"]
        self.assertEqual(d["developer"]["agent"], "claude")
        self.assertNotIn("code_reviewer", d)                          # peer code-review dropped
        self.assertEqual(d["architect"]["agent"], "codex")            # codex = the first review gate now
        self.assertEqual(d["tech_lead"]["agent"], "gemini")
        self.assertNotIn("code_reviewer", d["roster"]["cells"])
        self.assertNotIn("claude_reviewer", d["roster"]["chain"])
        # cells and chain stay aligned so cell[i] = chain[i] with all agents live
        self.assertEqual(d["roster"]["cells"],
                         ["developer", "architect", "tech_lead", "arbiter"])

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

    def test_implreview_shape(self):
        """implreview's intended shape (Irina 2026-07-14): author + two peer reviewers are
        DISTINCT Claude-family read-only agents; the Tech-Lead verifier and the arbiter are
        read-only; the Project-Manager gating cell is the independent 4th family (not
        claude/codex/gemini). Copy-agnostic: gating is deepseek (dataplane) or mistral (phs-rs)."""
        im = self.wf["implreview"]
        claude_cells = [im["author"]["agent"], im["dev_reviewer"]["agent"], im["devops_reviewer"]["agent"]]
        for a in claude_cells:
            self.assertTrue(a.startswith("claude"), f"implreview author/reviewers must be Claude-family, got {a}")
            self.assertFalse(self.agents[a].get("can_edit", False), f"implreview {a} must be read-only")
        self.assertEqual(len(set(claude_cells)), 3, "author + 2 reviewers must be 3 DISTINCT agents (one agent cannot fill two cells)")
        for key in ("verifier", "gating", "arbiter"):
            a = im[key]["agent"]
            self.assertIn(a, self.agents, f"implreview.{key}: unknown agent '{a}'")
            self.assertFalse(self.agents[a].get("can_edit", False), f"implreview.{key} must be read-only")
        # The independent-family gate in implreview is GATING, not the arbiter.
        gate = im["gating"]["agent"]
        self.assertNotIn(gate, ("claude", "codex", "gemini"),
                         f"implreview gating {gate} should be an independent 4th family (deepseek/mistral)")

    def test_workflow_config_consistency(self):
        """THE guard. Every (agent, role) used by any workflow must:
          (a) reference a configured agent,
          (b) have that role in the agent's roles allowlist  (catches the agents.json miss),
          (c) have a role file on disk,
          (d) have a verdict spec in verdicts.py.
        """
        present = RunContext(self.agents_cfg, self.wf, {}, None, None).roles_present()
        for w in ALL_WORKFLOWS:
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


if __name__ == "__main__":
    unittest.main()
