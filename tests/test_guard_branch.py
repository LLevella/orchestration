"""The guard must catch a branch switch by ANY role, including the Developer.

Why this exists: on 2026-08-01 a Developer agent ran `git checkout` 70 seconds into
a run, despite an explicit prohibition in its task, and its entire change landed on
an unrelated branch. Nothing caught it — `assert_unchanged()` hashes the working
tree, and switching branches mutates no file, so the diff looked perfect and the
wrong base was only discovered in review. These tests pin the fix.

Copy-agnostic: uses throwaway git repos, never the configured ones.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from orchestrator.config import git_clean_env
from orchestrator.guard import GitGuard, GuardViolation


class GuardBranchTest(unittest.TestCase):
    def _git(self, repo, *args):
        # env=git_clean_env(): the pre-commit hook runs this suite with GIT_DIR and
        # GIT_INDEX_FILE exported, and GIT_DIR beats `-C`. Without the scrub these
        # helpers operate on the OUTER repo instead of the temp one, and every test
        # in this class errors out — which is exactly what the guard itself was
        # fixed for; the tests must be hermetic against the same leak.
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
            capture_output=True, text=True, check=True, env=git_clean_env())

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard-branch-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("base\n")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "-q", "-m", "base")
        self._git(self.repo, "branch", "other")
        self.guard = GitGuard([("repo", self.repo)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_branches_reports_current_branch(self):
        self.assertEqual(self.guard.branches(), {"repo": "main"})

    def test_no_move_passes(self):
        before = self.guard.branches()
        self.guard.assert_branch_unchanged(before, self.guard.branches(), "dev", "step")

    def test_switch_is_caught_even_for_an_editable_role(self):
        """The Developer edits files legitimately, so only the branch check can see this."""
        before = self.guard.branches()
        self._git(self.repo, "checkout", "-q", "other")
        with self.assertRaises(GuardViolation) as cm:
            self.guard.assert_branch_unchanged(before, self.guard.branches(),
                                               "claude", "dev.developer")
        msg = str(cm.exception)
        self.assertIn("main -> other", msg)
        self.assertIn("dev.developer", msg)
        self.assertIn("claude", msg)

    def test_file_edits_alone_do_not_trip_the_branch_check(self):
        """A Developer doing its job must not be aborted by this check."""
        before = self.guard.branches()
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("developer work\n")
        with open(os.path.join(self.repo, "new.txt"), "w") as fh:
            fh.write("new file\n")
        self.guard.assert_branch_unchanged(before, self.guard.branches(), "claude", "step")

    def test_commit_on_the_same_branch_is_not_a_move(self):
        before = self.guard.branches()
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("x\n")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "-q", "-m", "wip")
        self.guard.assert_branch_unchanged(before, self.guard.branches(), "claude", "step")

    def test_detached_head_is_ignored_not_reported_as_a_move(self):
        """A repo the guard cannot name must not produce a spurious abort."""
        before = self.guard.branches()
        after = dict(before)
        after["repo"] = "HEAD"          # detached
        self.guard.assert_branch_unchanged(before, {"repo": None}, "claude", "step")
        # ...and a real rename away from None is likewise not treated as a move
        self.guard.assert_branch_unchanged({"repo": None}, after, "claude", "step")

    def test_inactive_guard_returns_none_and_never_fires(self):
        off = GitGuard([("repo", self.repo)], enabled=False)
        self.assertIsNone(off.branches())
        off.assert_branch_unchanged(None, None, "claude", "step")

    def test_non_repo_path_fails_closed_without_crashing(self):
        bogus = GitGuard([("nope", os.path.join(self.tmp, "not-a-repo"))])
        self.assertFalse(bogus.available)
        self.assertIsNone(bogus.branches())


if __name__ == "__main__":
    unittest.main()
