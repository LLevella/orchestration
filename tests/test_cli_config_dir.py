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

    def test_gemini_cell_carries_no_absolute_path_and_its_rules_exist(self):
        agents = json.loads((TOOL_ROOT / "config" / "agents.json").read_text())
        cell = agents["agents"]["gemini"]
        values = list(cell["command"]) + list(cell.get("env", {}).values()) \
            + list(cell.get("files", {}).keys()) + list(cell.get("files", {}).values())
        self.assertFalse(any(v.startswith("/") for v in values),
                         "the gemini cell carries an absolute path")
        for src in cell["files"].values():
            resolved = Path(src.replace("{config_dir}", CONFIG_DIR))
            self.assertTrue(resolved.is_file(), resolved)
            rules = json.loads(resolved.read_text())["permissions"]
            self.assertIn("write_file(*)", rules["deny"])
            self.assertIn("command(*)", rules["allow"])


class TestAgyTransport(unittest.TestCase):
    CFG = {"transport": "cli", "command": ["agy", "--input-format", "stream-json"],
           "stdin": True, "stdin_format": "ndjson_user_event",
           "usage_format": "agy_stream_json"}

    def test_prompt_travels_as_one_ndjson_user_event(self):
        cmd, stdin = CliAdapter("g", self.CFG)._build("SYS", "line1\nline2", False)
        self.assertEqual(stdin.count("\n"), 1, "more than one NDJSON line")
        ev = json.loads(stdin)
        self.assertEqual(ev["event"], "user")
        self.assertIn("line1\nline2", ev["message"]["content"])
        self.assertIn("SYS", ev["message"]["content"])

    def _parse(self, lines):
        return CliAdapter("g", self.CFG)._parse_output("\n".join(lines))

    def test_success_result_is_the_answer(self):
        out = self._parse(['{"event":"init"}', '{"event":"step_update"}',
                           json.dumps({"event": "result", "result": {
                               "status": "SUCCESS", "response": "VERDICT\n",
                               "usage": {"input_tokens": 10, "output_tokens": 3,
                                         "total_tokens": 13}}})])
        self.assertFalse(out.is_error)
        self.assertEqual(out.text, "VERDICT")
        self.assertEqual(out.usage["input"], 10)
        self.assertEqual(out.usage["total"], 13)

    def test_non_success_status_is_an_error_even_with_exit_zero(self):
        out = self._parse([json.dumps({"event": "result", "result": {
            "status": "ERROR", "response": "",
            "error": "RESOURCE_EXHAUSTED (code 429): Individual quota reached."}})])
        self.assertTrue(out.is_error)
        self.assertEqual(out.text, "")
        self.assertIn("quota", out.body["error"])

    def test_no_result_event_is_plain_output(self):
        out = self._parse(["plain words"])
        self.assertFalse(out.is_error)
        self.assertEqual(out.text, "plain words")

if __name__ == "__main__":
    unittest.main()
