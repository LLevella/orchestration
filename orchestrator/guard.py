"""Git working-tree safety guard (multi-repo).

Only the Developer (Claude) step may edit production code. Around every
read-only step we snapshot the working tree of every watched repo (the writable
`dev_repos` and the read-only `analysis_repos`) and assert nothing changed. A
change under a read-only agent aborts the run.

The workspace root the tool is launched against is typically not a git repo itself;
each project under it is a separate repo, so the guard watches a *list* of repos,
named by the per-project layer.

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

from orchestrator.config import git_clean_env


class GuardViolation(Exception):
    """A read-only step changed something it must not.

    `labels` names the watched repos the change landed in (empty when the guard
    could not attribute it). Callers use it to tell a violation IN the work under
    way from unrelated activity in another repo - a human editing their own repo
    during a long read-only run is not the agent misbehaving.
    """

    def __init__(self, message, labels=()):
        super().__init__(message)
        self.labels = frozenset(labels)


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
        # Scrubbed env: an inherited GIT_DIR/GIT_INDEX_FILE would silently retarget
        # this call at another repository's gitdir and at a commit-in-progress index
        # (see orchestrator/config.py::git_clean_env for the incident).
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, env=git_clean_env())

    def _is_repo(self, repo) -> bool:
        r = self._git(repo, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"

    @property
    def active(self) -> bool:
        return self.enabled and self.available

    def snapshot(self):
        """Return (hash, [labelled porcelain lines], {label: per-repo hash}) or None.

        The per-repo hashes exist so a violation can be attributed to a repository
        even when nothing new appeared in `git status` - a further edit to a file
        that was already dirty changes the blob but adds no porcelain entry."""
        if not self.active:
            return None
        excl = f":(exclude){self.exclude}"
        entries: list[str] = []
        blobs: list[str] = []
        by_label: dict[str, str] = {}
        for label, repo in self.valid:
            porc = [f"{label}: {_path_of(l)}"
                    for l in self._git(repo, "status", "--porcelain").stdout.splitlines()
                    if not _path_of(l).startswith(self.exclude + "/")
                    and _path_of(l) != self.exclude]
            diff = self._git(repo, "diff", "--", ".", excl).stdout
            cached = self._git(repo, "diff", "--cached", "--", ".", excl).stdout
            entries.extend(porc)
            per_repo = f"== {label} ==\n" + "\n".join(sorted(porc)) + "\0" + diff + "\0" + cached
            blobs.append(per_repo)
            by_label[label] = hashlib.sha256(per_repo.encode("utf-8", "replace")).hexdigest()
        blob = "\n".join(blobs)
        return (hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest(), sorted(entries), by_label)

    def assert_unchanged(self, before, after, who: str, step: str):
        if before is None or after is None:
            return
        if before[0] != after[0]:
            added = sorted(set(after[1]) - set(before[1]))
            detail = added or "(content change to already-modified files)"
            b_lbl = before[2] if len(before) > 2 else {}
            a_lbl = after[2] if len(after) > 2 else {}
            labels = {lbl for lbl in set(b_lbl) | set(a_lbl) if b_lbl.get(lbl) != a_lbl.get(lbl)}
            labels |= {line.split(":", 1)[0] for line in added if ":" in line}
            raise GuardViolation(
                f"Read-only agent '{who}' altered the working tree during step "
                f"'{step}'. Only the Developer may edit production code. Delta: {detail}",
                labels=labels,
            )

    def branches(self):
        """Return {label: current branch} across all repos, or None if inactive.

        Checked around EVERY step, including the Developer's editable one, because
        switching branches mutates no file and is therefore invisible to
        snapshot()/assert_unchanged(). Observed 2026-08-01: a Developer agent ran
        `git checkout` 70 s into a run despite an explicit prohibition in its task,
        and the whole change silently landed on an unrelated branch - the diff looked
        perfect and the wrong base was only found in review.
        """
        if not self.active:
            return None
        out = {}
        for label, repo in self.valid:
            r = self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            out[label] = r.stdout.strip() if r.returncode == 0 else None
        return out

    def assert_branch_unchanged(self, before, after, who: str, step: str):
        """Abort if any watched repo changed its checked-out branch during a step.

        Applies to every role. A detached HEAD or an unreadable branch reads as None
        and is ignored rather than treated as a move, so this cannot fire spuriously
        on a repo the guard could not interrogate.
        """
        if not before or not after:
            return
        moved = [(label, before[label], after.get(label))
                 for label in before
                 if before.get(label) and after.get(label)
                 and before[label] != after[label]]
        if moved:
            detail = "; ".join(f"{label}: {b} -> {a}" for label, b, a in moved)
            raise GuardViolation(
                f"Agent '{who}' changed the checked-out branch during step '{step}' "
                f"({detail}). Branch switching is prohibited for every role: the "
                f"run's work lands on the wrong base and the diff does not show it.",
                labels={label for label, _b, _a in moved},
            )
