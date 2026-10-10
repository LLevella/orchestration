"""`{config_dir}` in a CLI command template resolves to the tool's config/ dir.

agents.json must not carry an absolute path to a file shipped beside it - that
ties the tool to one checkout. The CLI adapter substitutes `{config_dir}` at
build time instead; these tests hold that substitution and the gemini cell's
use of it for its read-only policy.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from orchestrator.adapters import CONFIG_DIR, CliAdapter, build_adapters

TOOL_ROOT = Path(__file__).resolve().parent.parent


class TestConfigDirPlaceholder(unittest.TestCase):
    def test_config_dir_is_the_tools_own_config_directory(self):
        self.assertEqual(Path(CONFIG_DIR), TOOL_ROOT / "config")

    def test_placeholder_is_resolved_in_command_and_mode_args(self):
        cfg = {
            "transport": "cli",
            "command": ["tool", "--a", "{config_dir}/x.toml", "-p", "{prompt}"],
            "read_only_args": ["--b", "{config_dir}/ro.toml"],
            "edit_args": ["--c", "{config_dir}/rw.toml"],
            "can_edit": True,
        }
        adapter = CliAdapter("t", cfg)
        cmd, _ = adapter._build("sys", "hello", allow_edit=False)
        self.assertIn(f"{CONFIG_DIR}/x.toml", cmd)
        self.assertIn(f"{CONFIG_DIR}/ro.toml", cmd)
        self.assertFalse(any("{config_dir}" in c for c in cmd))
        cmd_edit, _ = adapter._build("sys", "hello", allow_edit=True)
        self.assertIn(f"{CONFIG_DIR}/rw.toml", cmd_edit)

    def test_gemini_cell_names_no_absolute_path_and_its_policy_exists(self):
        agents = json.loads((TOOL_ROOT / "config" / "agents.json").read_text())
        template = agents["agents"]["gemini"]["command"]
        self.assertFalse(any(c.startswith("/") for c in template),
                         "the gemini command template carries an absolute path")
        gemini = build_adapters(agents, mock=False, scenario=None)["gemini"]
        cmd, stdin = gemini._build("sys", "prompt", allow_edit=False)
        policy = cmd[cmd.index("--policy") + 1]
        self.assertTrue(os.path.isabs(policy))
        self.assertTrue(Path(policy).is_file(), policy)
        self.assertEqual(Path(policy).parent, Path(CONFIG_DIR))


if __name__ == "__main__":
    unittest.main()
