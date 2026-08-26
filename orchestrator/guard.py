"""Git working-tree safety guard (multi-repo).

Only the Developer (Claude) step may edit production code. Around every
read-only step we snapshot the working tree of every watched repo (the writable
`dev_repos` and the read-only `analysis_repos`) and assert nothing changed. A
change under a read-only agent aborts the run.

The workspace root that holds the orchestration/ tool is itself not a git repo;
each project under it (dataplane-phs-ng, dataplane-sdk, MCD4_Lite, ncc-core,
idu/sdk-s8000, ...) is a separate repo, so the guard watches a *list* of repos.

The snapshot is a hash over, for each repo: filtered `git status --porcelain` +
`git diff` + `git diff --cached` (each excluding the tool dir). This catches not
only new files but also further edits to files that were already dirty before
the run. Porcelain entries are labelled with their repo so a violation delta
points at the offending repo.
"""
from __future__ import annotations

import hashlib
import os
import subprocess


class GuardViolation(Exception):
    pass


def _path_of(porcelain_line: str) -> str:
    p = porcelain_line[3:]
    if " -> " in p:  # rename: "old -> new"
        p = p.split(" -> ", 1)[1]
    return p.strip().strip('"')


class GitGuard:
    def __init__(self, repos, exclude_prefix: str = "orchestration", enabled: bool = True):
        """repos: an iterable of (label, abspath), or a single path for back-compat."""
        if isinstance(repos, (str, bytes)) or hasattr(repos, "__fspath__"):
            repos = [(os.fspath(repos), os.fspath(repos))]
        self.repos = [(str(label), str(path)) for label, path in repos]
        self.exclude = exclude_prefix
        self.enabled = enabled
        self.valid = [(label, path) for label, path in self.repos if self._is_repo(path)]
        self.invalid = [label for label, path in self.repos if (label, path) not in self.valid]
        # Active only if EVERY configured repo is a real git worktree, so a
        # mistyped or non-git path fails closed rather than silently unguarded.
        self.available = bool(self.repos) and not self.invalid

    def _git(self, repo, *args):
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True)

    def _is_repo(self, repo) -> bool:
        r = self._git(repo, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"

    @property
    def active(self) -> bool:
        return self.enabled and self.available

    def snapshot(self):
        """Return (hash, [labelled porcelain lines]) across all repos, or None if inactive."""
        if not self.active:
            return None
        excl = f":(exclude){self.exclude}"
        entries: list[str] = []
        blobs: list[str] = []
        for label, repo in self.valid:
            porc = [f"{label}: {_path_of(l)}"
                    for l in self._git(repo, "status", "--porcelain").stdout.splitlines()
                    if not _path_of(l).startswith(self.exclude + "/")
                    and _path_of(l) != self.exclude]
            diff = self._git(repo, "diff", "--", ".", excl).stdout
            cached = self._git(repo, "diff", "--cached", "--", ".", excl).stdout
            entries.extend(porc)
            blobs.append(f"== {label} ==\n" + "\n".join(sorted(porc)) + "\0" + diff + "\0" + cached)
        blob = "\n".join(blobs)
        return (hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest(), sorted(entries))

    def assert_unchanged(self, before, after, who: str, step: str):
        if before is None or after is None:
            return
        if before[0] != after[0]:
            added = sorted(set(after[1]) - set(before[1]))
            detail = added or "(content change to already-modified files)"
            raise GuardViolation(
                f"Read-only agent '{who}' altered the working tree during step "
                f"'{step}'. Only the Developer may edit production code. Delta: {detail}"
            )
