"""Our git calls must ignore an inherited GIT_DIR / GIT_INDEX_FILE.

Why this exists: `GIT_DIR` overrides repository discovery, so `git -C /some/repo
status` with an inherited GIT_DIR operates on THAT gitdir while treating /some/repo
as the worktree — verified 2026-08-02, it reported every file of the *other*
repository as deleted. GIT_INDEX_FILE is worse: during a `git commit` it points at
the index git is still building.

We inherit exactly that whenever the suite runs from a pre-commit hook. The
hook runs the orchestration tests; test_workflows_mock spawns orchestrate.py as a
subprocess; every git call inside it then targets the commit-in-progress index
instead of the repo it was handed. The read-only guard sees the tree "change"
between a step's before/after snapshot and aborts as a guard violation. A
config-only commit was blocked four times that way, while the same suite passed
every single time it was run by hand — the difference being nothing but the
environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from orchestrator.config import _GIT_ENV_OVERRIDES, git_clean_env
from orchestrator.guard import GitGuard


class GitEnvIsolationTest(unittest.TestCase):
    def _git(self, repo, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
            capture_output=True, text=True, check=True, env=git_clean_env())

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="git-env-")
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        for r in (self.a, self.b):
            os.makedirs(r)
            self._git(r, "init", "-q", "-b", "main")
            with open(os.path.join(r, "f.txt"), "w") as fh:
                fh.write("base\n")
            self._git(r, "add", "-A")
            self._git(r, "commit", "-q", "-m", "base")
        # b is dirty; a is clean. A leaked GIT_DIR pointing at b must not make a look dirty.
        with open(os.path.join(self.b, "dirty.txt"), "w") as fh:
            fh.write("b is dirty\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_clean_env_drops_every_override(self):
        fake = {v: "/nonexistent" for v in _GIT_ENV_OVERRIDES}
        fake["PATH"] = os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, fake, clear=True):
            env = git_clean_env()
        for v in _GIT_ENV_OVERRIDES:
            self.assertNotIn(v, env, f"{v} survived the scrub")
        self.assertIn("PATH", env, "the scrub must not throw the rest of the environment away")

    def test_guard_snapshot_ignores_a_leaked_GIT_DIR(self):
        """The decisive one: clean repo 'a' must read clean even with b's gitdir leaked."""
        guard = GitGuard([("a", self.a)])
        clean = guard.snapshot()
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(self.b, ".git")}):
            leaked = guard.snapshot()
        self.assertEqual(clean, leaked,
                         "a leaked GIT_DIR changed what the guard saw — the retarget is back")

    def test_guard_branches_ignore_a_leaked_GIT_DIR(self):
        guard = GitGuard([("a", self.a)])
        self._git(self.a, "branch", "other")
        self._git(self.a, "checkout", "-q", "other")
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(self.b, ".git")}):
            self.assertEqual(guard.branches(), {"a": "other"})

    def test_guard_still_sees_a_real_change_in_its_own_repo(self):
        """The scrub must not blind the guard to the thing it exists to catch."""
        guard = GitGuard([("a", self.a)])
        before = guard.snapshot()
        with open(os.path.join(self.a, "sneaky.c"), "w") as fh:
            fh.write("int sneaky(void){return 1;}\n")
        self.assertNotEqual(before, guard.snapshot())

    def test_a_leaked_GIT_INDEX_FILE_does_not_affect_the_snapshot(self):
        guard = GitGuard([("a", self.a)])
        clean = guard.snapshot()
        with mock.patch.dict(os.environ,
                             {"GIT_INDEX_FILE": os.path.join(self.b, ".git", "index")}):
            self.assertEqual(clean, guard.snapshot())


if __name__ == "__main__":
    unittest.main()
