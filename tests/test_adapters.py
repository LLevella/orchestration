"""Unit tests for adapter construction/dispatch (orchestrator/adapters.py).

Copy-agnostic. Verifies build_adapters routes each agent to the right adapter class:
native CLI tools (claude/codex/gemini) -> CliAdapter; the API/local arbiter
(DeepSeek or Mistral, whichever this copy configures) -> ApiAdapter; mock -> MockAdapter;
and that '_'-prefixed keys are skipped. No network is touched (adapters are only constructed).
"""
import unittest

from orchestrator.config import load_config
from orchestrator.adapters import build_adapters, CliAdapter, ApiAdapter, MockAdapter

CLI_AGENTS = ("claude", "codex", "gemini")


class TestBuildAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents_cfg, _ = load_config()
        cls.names = [k for k in cls.agents_cfg["agents"] if not k.startswith("_")]

    def test_mock_mode_builds_only_mock_adapters(self):
        ad = build_adapters(self.agents_cfg, mock=True, scenario={}, cwd=None)
        self.assertTrue(ad)
        for name, a in ad.items():
            self.assertIsInstance(a, MockAdapter, name)

    def test_real_dispatch_cli_vs_api(self):
        ad = build_adapters(self.agents_cfg, mock=False, scenario=None, cwd=None)
        for n in CLI_AGENTS:
            self.assertIsInstance(ad[n], CliAdapter, n)
        # the 4th agent (arbiter; api_base present) -> ApiAdapter, regardless of model.
        api_agents = [n for n in self.names if "api_base" in self.agents_cfg["agents"][n]]
        self.assertTrue(api_agents, "expected at least one api_base agent (the arbiter)")
        for n in api_agents:
            self.assertIsInstance(ad[n], ApiAdapter, n)

    def test_underscore_keys_are_skipped(self):
        cfg = {"agents": {
            "_doc": {"note": "not an agent"},
            "claude": self.agents_cfg["agents"]["claude"],
        }}
        ad = build_adapters(cfg, mock=False, scenario=None, cwd=None)
        self.assertNotIn("_doc", ad)
        self.assertIn("claude", ad)

    def test_can_edit_flag_preserved(self):
        ad = build_adapters(self.agents_cfg, mock=False, scenario=None, cwd=None)
        # only the developer (claude) may edit; reviewers/arbiter are read-only.
        self.assertTrue(ad["claude"].can_edit)
        for n in ("codex", "gemini"):
            self.assertFalse(ad[n].can_edit, n)


if __name__ == "__main__":
    unittest.main()
