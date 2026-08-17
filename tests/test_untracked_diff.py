"""Untracked (new) files must reach reviewers with their CONTENTS, not just names.

Why this exists: `git diff <baseline>` structurally omits anything git does not
track, so a run that ADDS files — most runs — used to show reviewers only a list of
names. On 2026-08-02 that deadlocked a run for two hours: the architect returned
NEEDS_FIX five times, each time endorsing the design but refusing to sign off on
safety-critical shell it had never been shown, while the developer never pasted the
contents. No amount of iterating could have converged.

Copy-agnostic: throwaway git repos, never the configured ones.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from orchestrator.config import (_UNTRACKED_FILE_MAX_BYTES,
                                 _UNTRACKED_TOTAL_MAX_BYTES, RunContext,
                                 git_clean_env)


class _Ctx(RunContext):
    """RunContext with config loading bypassed — only the git/diff helpers are used."""
    def __init__(self, repos, exclude="orchestration"):
        self.repos = repos
        self.exclude = exclude
        self._baselines = {label: self._head(path) for label, path, _ in repos}


class UntrackedDiffTest(unittest.TestCase):
    def _git(self, repo, *args):
        # env=git_clean_env(): the pre-commit hook runs this suite with GIT_DIR and
        # GIT_INDEX_FILE exported, and GIT_DIR beats `-C`. Without the scrub these
        # helpers init/commit against the OUTER repo, the temp repo never gets a
        # commit, and every file reads as deleted-and-untracked at once.
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
            capture_output=True, text=True, check=True, env=git_clean_env())

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="untracked-diff-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-q", "-b", "main")
        self._write("tracked.c", "int main(void){return 0;}\n")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "-q", "-m", "base")
        self.ctx = _Ctx([("repo", self.repo, "dev")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(rel) else None
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def _diff(self):
        return self.ctx._repo_diff(self.repo, self.ctx._baselines["repo"])

    def test_untracked_file_contents_are_in_the_diff(self):
        """The whole point: a reviewer must be able to read a file the run added."""
        self._write("new_helper.sh", "#!/bin/sh\nagent_marker_do_not_reformat() { echo 42; }\n")
        out = self._diff()
        self.assertIn("new_helper.sh", out)
        self.assertIn("agent_marker_do_not_reformat", out,
                      "untracked file CONTENTS missing — reviewers would see only the name")

    def test_tracked_edits_still_appear(self):
        """The pre-existing behaviour must not be lost while adding the new one."""
        self._write("tracked.c", "int main(void){return 1;}\n")
        self.assertIn("return 1", self._diff())

    def test_both_together(self):
        self._write("tracked.c", "int main(void){return 7;}\n")
        self._write("added.py", "SENTINEL_ADDED = True\n")
        out = self._diff()
        self.assertIn("return 7", out)
        self.assertIn("SENTINEL_ADDED", out)

    def test_oversized_file_is_truncated_and_says_so(self):
        """A silent cap reads as 'that is the whole file'; it must announce itself."""
        self._write("big.txt", "x" * (_UNTRACKED_FILE_MAX_BYTES + 5000))
        out = self._diff()
        self.assertIn("TRUNCATED", out)
        self.assertIn("big.txt", out)
        self.assertLess(len(out), _UNTRACKED_FILE_MAX_BYTES * 3)

    def test_total_budget_exhaustion_names_what_was_dropped(self):
        n = (_UNTRACKED_TOTAL_MAX_BYTES // _UNTRACKED_FILE_MAX_BYTES) + 3
        for i in range(n):
            self._write(f"f{i:02d}.txt", "y" * (_UNTRACKED_FILE_MAX_BYTES + 100))
        out = self._diff()
        self.assertIn("NOT shown above", out)
        self.assertLess(len(out), _UNTRACKED_TOTAL_MAX_BYTES * 2)

    def test_binary_file_is_reported_not_dumped(self):
        with open(os.path.join(self.repo, "blob.bin"), "wb") as fh:
            fh.write(bytes(range(256)) * 40)
        out = self._diff()
        self.assertIn("blob.bin", out)
        self.assertNotIn("\x00", out, "raw binary leaked into the reviewer prompt")

    def test_the_tool_directory_is_still_excluded(self):
        os.makedirs(os.path.join(self.repo, "orchestration"), exist_ok=True)
        self._write("orchestration/secret_tooling.py", "TOOLING_SENTINEL = 1\n")
        self.assertNotIn("TOOLING_SENTINEL", self._diff())

    def test_gitignored_files_are_not_shown(self):
        self._write(".gitignore", "*.o\n")
        self._git(self.repo, "add", ".gitignore")
        self._git(self.repo, "commit", "-q", "-m", "ignore")
        self._write("build.o", "IGNORED_SENTINEL\n")
        self.assertNotIn("IGNORED_SENTINEL", self._diff())

    def test_clean_repo_yields_no_untracked_section(self):
        self.assertNotIn("Untracked", self._diff())


if __name__ == "__main__":
    unittest.main()
