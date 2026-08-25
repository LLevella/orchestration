"""Merge-request input resolution for the `review` workflow.

A "merge request" reaches the orchestrator in one of three ways, in decreasing
order of convenience and increasing order of independence from GitLab:

  1. `--mr <iid>`   — ask `glab` for the MR's title and its source/target branch,
                      then diff those refs LOCALLY. Needs an authenticated glab.
  2. `--base/--head`— plain git refs (the default: `origin/main`...`HEAD`). Works
                      over SSH alone, which is the only GitLab access this
                      workspace is guaranteed to have.
  3. `--diff-file`  — a patch someone handed us. No git, no network, no repo.

Whatever the entry point, reviewers always receive the SAME thing: a merge-base
(three-dot) diff plus the commit list and diffstat. Three-dot is deliberate — it
shows what the branch adds to the target, not the unrelated commits the target
gained meanwhile, which is exactly what a human sees in the MR web view.

Nothing here writes to a repo. The only git commands used are read-only ones,
except the explicitly requested `--fetch` (which updates remote refs, never the
working tree).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import BASE, git_clean_env, resolve_repos

# A diff larger than this is truncated before it reaches any prompt. Announced
# in-band, never silently: a silently truncated diff reads to a reviewer as "that
# is the whole change" and produces a confident review of code it never saw.
DIFF_MAX_BYTES = 400 * 1024


class MRResolutionError(Exception):
    """Raised with a human-readable message when the MR cannot be resolved."""


@dataclass
class MRContext:
    """Everything the panel is shown about the change under review."""
    subject: str                 # human title of the change
    repo_label: str              # e.g. 'projects/weyro-ai', or '(diff file)'
    repo_path: str | None
    base: str | None
    head: str | None
    base_sha: str | None
    head_sha: str | None
    diff: str
    stat: str = ""
    commits: str = ""
    source: str = "refs"         # 'mr' | 'refs' | 'file'
    mr_iid: str | None = None
    # False when the reviewed repo is outside the workflows' watched set: the caller
    # adds it to the git guard for the run, so a read-only reviewer that writes to it
    # is still caught.
    guard_watched: bool = True
    notes: list[str] = field(default_factory=list)   # in-band caveats for reviewers + doc

    def spec_dict(self) -> dict:
        """Persisted into run.json so an interrupted review can be resumed."""
        return {"source": self.source, "repo_label": self.repo_label, "base": self.base,
                "head": self.head, "mr_iid": self.mr_iid, "subject": self.subject}

    def header_lines(self) -> list[str]:
        """Provenance block shared by the reviewer prompts and the remarks document."""
        out = [f"Change under review: {self.subject}"]
        if self.repo_path:
            out.append(f"Repository: {self.repo_label}")
        if self.base and self.head:
            out.append(f"Target (base): {self.base}"
                       + (f" @ {self.base_sha[:12]}" if self.base_sha else ""))
            out.append(f"Source (head): {self.head}"
                       + (f" @ {self.head_sha[:12]}" if self.head_sha else ""))
        if self.mr_iid:
            out.append(f"Merge request: !{self.mr_iid}")
        out += [f"NOTE: {n}" for n in self.notes]
        return out


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=git_clean_env())


def _rev(repo, ref):
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.strip() or None


def pick_repo(workflows_cfg, label: str | None):
    """Resolve which repository holds the MR -> (label, path, watched).

    A review is read-only, so it is NOT restricted to the workflows' watched repos:
    code worth reviewing routinely lives in a repository this workspace only reads
    (the monolith, a colleague's clone). `label` therefore accepts a watched repo's
    label first, and failing that any path — absolute, or relative to the workspace
    root — that is a git worktree. `watched` tells the caller whether the git guard
    already covers it; an unwatched review target is added to the guard for the run.
    """
    repos = resolve_repos(workflows_cfg)
    root = (BASE / workflows_cfg["paths"]["repo_root"]).resolve()
    if label:
        for lbl, path, _kind in repos:
            if lbl == label or lbl.endswith("/" + label):
                return lbl, path, True
        for cand in (Path(label), root / label):
            if (cand / ".git").exists() or _is_worktree(cand):
                rel = cand.resolve()
                try:
                    shown = str(rel.relative_to(root))
                except ValueError:
                    shown = str(rel)
                return shown, str(rel), False
        raise MRResolutionError(
            f"'{label}' is neither a watched repo nor a git repository path. Watched: "
            f"{[r[0] for r in repos] or '(none)'}; paths are tried as given and under "
            f"{root}.")
    dev = [r for r in repos if r[2] == "dev"]
    if len(dev) == 1:
        return dev[0][0], dev[0][1], True
    raise MRResolutionError(
        "no --repo given and the workspace does not have exactly one dev repo "
        f"(configured: {[r[0] for r in repos] or '(none)'}). Pass --repo <label-or-path>.")


def _is_worktree(path) -> bool:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, env=git_clean_env())
    return r.returncode == 0 and r.stdout.strip() == "true"


def _glab_mr(repo_path, iid):
    """Title + branches of MR !iid via glab. Returns a dict or raises MRResolutionError."""
    if shutil.which("glab") is None:
        raise MRResolutionError(
            "--mr needs the `glab` CLI, which is not in PATH. Use --base/--head instead.")
    r = subprocess.run(["glab", "mr", "view", str(iid), "-F", "json"],
                       cwd=str(repo_path), capture_output=True, text=True,
                       timeout=120, env=git_clean_env())
    if r.returncode != 0:
        raise MRResolutionError(
            f"`glab mr view {iid}` failed (glab may not be authenticated against this "
            f"GitLab, or the VPN is down): {(r.stderr or r.stdout).strip()[:400]}\n"
            "Fall back to: --base <target-branch> --head <source-branch>.")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise MRResolutionError(f"could not parse `glab mr view {iid}` output: {e}") from e
    if not d.get("source_branch") or not d.get("target_branch"):
        raise MRResolutionError(f"MR !{iid} carries no source/target branch in glab's answer.")
    return d


def _resolve_branch_ref(repo_path, branch):
    """Prefer the remote-tracking ref (what GitLab actually has) over a local branch
    of the same name, which may be stale or absent."""
    for cand in (f"origin/{branch}", branch):
        if _rev(repo_path, cand):
            return cand
    return None


def _capture(repo_path, base, head, notes):
    """Merge-base diff + diffstat + commit list for base...head."""
    base_sha, head_sha = _rev(repo_path, base), _rev(repo_path, head)
    if not base_sha:
        raise MRResolutionError(f"base ref '{base}' does not resolve in this repo. "
                                "Fetch it first (--fetch) or name another ref.")
    if not head_sha:
        raise MRResolutionError(f"head ref '{head}' does not resolve in this repo. "
                                "Fetch it first (--fetch) or name another ref.")
    mb = _git(repo_path, "merge-base", base_sha, head_sha)
    if mb.returncode == 0 and mb.stdout.strip():
        rng, two_dot = f"{base_sha}...{head_sha}", False
    else:
        # Unrelated histories: three-dot is undefined, so fall back and SAY SO —
        # a two-dot diff also shows what the base gained since they diverged.
        rng, two_dot = f"{base_sha}..{head_sha}", True
        notes.append("base and head share no merge base; the diff below is a plain "
                     "two-dot diff and may include changes that are not part of this MR.")
    diff = _git(repo_path, "diff", "--no-color", rng).stdout
    stat = _git(repo_path, "diff", "--no-color", "--stat", rng).stdout.strip()
    log_rng = f"{base_sha}..{head_sha}" if not two_dot else rng
    commits = _git(repo_path, "log", "--no-color", "--oneline", "--no-decorate", log_rng).stdout.strip()
    return base_sha, head_sha, diff, stat, commits


def _truncate(diff, notes):
    if len(diff) <= DIFF_MAX_BYTES:
        return diff
    notes.append(f"the diff is {len(diff)} bytes and was truncated to the first "
                 f"{DIFF_MAX_BYTES}; read the repository for the remainder before "
                 "judging anything below the cut.")
    return (diff[:DIFF_MAX_BYTES]
            + f"\n\n# ... TRUNCATED at {DIFF_MAX_BYTES} bytes of {len(diff)}. "
              "The rest of the change is NOT shown — read it in the repository.\n")


def resolve(workflows_cfg, *, mr=None, base=None, head=None, repo=None,
            diff_file=None, fetch=False, subject=None) -> MRContext:
    """Build the MRContext the review workflow reviews. See module docstring."""
    notes: list[str] = []

    if diff_file:
        try:
            text = Path(diff_file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise MRResolutionError(f"cannot read --diff-file {diff_file}: {e}") from e
        if not text.strip():
            raise MRResolutionError(f"--diff-file {diff_file} is empty; nothing to review.")
        notes.append("reviewers were given a patch file, not a repository: they cannot read "
                     "surrounding code, so judge only what the patch itself shows.")
        return MRContext(subject=subject or f"patch file {diff_file}",
                         repo_label="(diff file)", repo_path=None, base=None, head=None,
                         base_sha=None, head_sha=None, diff=_truncate(text, notes),
                         source="file", notes=notes)

    repo_label, repo_path, watched = pick_repo(workflows_cfg, repo)
    if _git(repo_path, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise MRResolutionError(f"{repo_label} ({repo_path}) is not a git worktree.")
    if fetch:
        f = _git(repo_path, "fetch", "--prune", "origin")
        if f.returncode != 0:
            notes.append(f"`git fetch origin` failed ({(f.stderr or '').strip()[:200]}); "
                         "refs below are whatever was already local.")

    iid, mr_subject = None, None
    if mr:
        d = _glab_mr(repo_path, mr)
        iid, mr_subject = str(mr), (d.get("title") or f"MR !{mr}")
        base = base or _resolve_branch_ref(repo_path, d["target_branch"]) or d["target_branch"]
        head = head or _resolve_branch_ref(repo_path, d["source_branch"]) or d["source_branch"]
        if d.get("state") and d["state"] != "opened":
            notes.append(f"MR !{mr} is {d['state']}, not open.")

    base = base or "origin/main"
    head = head or "HEAD"
    base_sha, head_sha, diff, stat, commits = _capture(repo_path, base, head, notes)
    if not diff.strip():
        raise MRResolutionError(
            f"{base}...{head} is an empty diff in {repo_label} — nothing to review. "
            "(Wrong refs? A stale local copy of the source branch? Try --fetch.)")

    # Reviewers read surrounding code from the CHECKED-OUT tree, which is not
    # necessarily the head being reviewed. Say so rather than let them assume.
    current = _rev(repo_path, "HEAD")
    if head_sha and current and current != head_sha:
        cur_name = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        notes.append(f"the working tree of {repo_label} is checked out at "
                     f"{cur_name} ({current[:12]}), NOT at the reviewed head — files you open "
                     "in the repository may differ from the diff. The diff is authoritative.")

    return MRContext(subject=subject or mr_subject or f"{base}...{head} in {repo_label}",
                     repo_label=repo_label, repo_path=str(repo_path), base=base, head=head,
                     base_sha=base_sha, head_sha=head_sha, diff=_truncate(diff, notes),
                     stat=stat, commits=commits, source=("mr" if mr else "refs"),
                     mr_iid=iid, notes=notes, guard_watched=watched)
