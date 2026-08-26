"""Properties of the MR review panel (Irina 2026-08-21).

The panel exists to produce four INDEPENDENT opinions and one document a human can
hand to an MR author. Each test below pins one property of that, against the way it
would plausibly rot:

  1. Four DIFFERENT agents sit on the panel — a roster that quietly gave two seats to
     the same model would still "work", and would silently be a panel of three.
  2. No reviewer is shown another reviewer's opinion, and each is told it is one of
     four — independence is the whole point of asking four times.
  3. The document names no model and no run: reviewers are Reviewer 1..N. Provenance
     belongs in the run dir; the deliverable is ours to hand to a colleague.
  4. The tally in the document is computed from the verdicts, never asked of a model.
  5. A dead vendor costs one opinion, not the run.
  6. The MR is resolved as a three-dot (merge-base) diff, so commits the target
     branch gained meanwhile are not blamed on this MR.

These assert on the real config, the real prompt builders, the real document
assembler, and a real throwaway git repo — never on a model's answer.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator import mr as mrlib
from orchestrator.adapters import build_adapters
from orchestrator.config import load_config
from orchestrator.engine import Engine
from orchestrator.roster import Roster

ORCH = Path(__file__).resolve().parent.parent


def _mr(diff="--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n"):
    return mrlib.MRContext(subject="add retry to backend call", repo_label="example/service",
                           repo_path="/w/example/service", base="origin/main", head="feat/x",
                           base_sha="a" * 40, head_sha="b" * 40, diff=diff,
                           stat=" x.py | 2 +-", commits="deadbee add retry")


def _engine():
    _, workflows = load_config()
    ctx = MagicMock()
    ctx.workflows = workflows
    ctx.repo_scope_line.return_value = "Scope — WRITABLE: example/service; READ-ONLY: (none)."
    return Engine(ctx, "mrreview")


def _opinions(verdicts=("REQUEST_CHANGES", "APPROVE", "APPROVE", "APPROVE_WITH_COMMENTS")):
    return [{"n": i, "cell": f"expert_{i}", "agent": f"agent_{i}", "verdict": v,
             "text": f"## MR Review Verdict\n{v}\n\n## Summary\n- opinion {i}\n"}
            for i, v in enumerate(verdicts, 1)]


class TestPanelComposition(unittest.TestCase):
    def test_four_distinct_agents_review_and_a_fifth_moderates(self):
        agents_cfg, workflows = load_config()
        adapters = build_adapters(agents_cfg, mock=True, scenario=None)  # all live
        assignment = Roster(workflows["mrreview"], adapters).assign()
        panel = [assignment[c] for c in ("expert_1", "expert_2", "expert_3", "expert_4")]
        self.assertEqual(len(set(panel)), 4, f"panel is not four distinct agents: {panel}")
        self.assertNotIn(assignment["moderator"], panel,
                         "the moderator must not also hold a panel seat")

    def test_every_panel_agent_is_allowed_to_hold_the_role(self):
        # The roster honours agents.json's `roles` allowlist; a seat whose agent is not
        # allowed the role resolves to None and the run dies with "no live agent".
        agents_cfg, workflows = load_config()
        for cell in ("expert_1", "expert_2", "expert_3", "expert_4", "moderator"):
            spec = workflows["mrreview"][cell]
            allowed = agents_cfg["agents"][spec["agent"]].get("roles") or []
            self.assertIn(spec["role"], allowed,
                          f"{spec['agent']} may not hold {spec['role']} ({cell})")


class TestPanelPrompts(unittest.TestCase):
    def test_expert_is_told_it_is_one_of_four_and_sees_no_other_opinion(self):
        e = _engine()
        p = e._mr_expert_prompt("brief", _mr(), "correctness lens", 2, 4)
        self.assertIn("reviewer 2 of 4", p)
        self.assertIn("you will not see their", p.lower())
        self.assertIn("correctness lens", p)          # its emphasis is carried
        self.assertIn("+new", p)                      # and the actual diff
        self.assertNotIn("Reviewer 1 —", p)           # nobody else's opinion leaks in

    def test_expert_lens_is_an_emphasis_not_a_boundary(self):
        # A lens that reads as a scope limit turns four reviewers into four partial
        # reviews with gaps between them.
        p = _engine()._mr_expert_prompt("", _mr(), "security lens", 3, 4)
        self.assertIn("EMPHASIS, not a boundary", p)

    def test_reviewer_is_told_not_to_police_the_repo_s_language(self):
        # The global policy ("produce every artifact in English") is about the agent's
        # own output; applied to the reviewed code it produces a false remark about a
        # team that simply writes in another language — seen on the first real run.
        p = _engine()._mr_expert_prompt("", _mr(), "lens", 1, 4)
        self.assertIn("YOUR output only", p)
        self.assertIn("never raise a remark about the natural language", p)

    def test_moderator_receives_every_opinion_by_number(self):
        p = _engine()._mr_moderator_prompt("brief", _mr(), _opinions())
        for i in (1, 2, 3, 4):
            self.assertIn(f"## Reviewer {i} —", p)
        self.assertIn("never invent a remark", p)


class TestReviewDocument(unittest.TestCase):
    def test_document_is_anonymous_and_carries_no_run_provenance(self):
        e = _engine()
        ops = _opinions()
        doc = e._review_document(_mr(), ops, "## Remarks\n| # | Severity |\n")
        for o in ops:
            self.assertNotIn(o["agent"], doc, "the document must not name the models")
        for banned in ("orchestrat", "run_id", "mock"):
            self.assertNotIn(banned, doc.lower(), f"'{banned}' leaked into the deliverable")
        self.assertIn("Reviewer 1", doc)
        self.assertIn("add retry to backend call", doc)   # the change is identified

    def test_tally_is_computed_from_the_verdicts(self):
        head, lines = _engine()._panel_tally(_opinions())
        self.assertIn("Changes requested", head)
        self.assertIn("1 of 4", head)
        self.assertEqual(len(lines), 4)

    def test_a_lost_seat_is_reported_not_hidden(self):
        e = _engine()
        ops = _opinions()
        ops[2] = {"n": 3, "cell": "expert_3", "agent": "gemini", "verdict": None,
                  "text": "", "error": "usage limit"}
        head, lines = e._panel_tally(ops)
        self.assertIn("no opinion", lines[2])
        self.assertIn("3 reviewers who answered", head)   # the tally counts answers, not seats
        doc = e._review_document(_mr(), ops, "## Remarks\n")
        self.assertIn("Reviewer 3", doc)
        self.assertIn("unavailable", doc)

    def test_verdict_plumbing_is_stripped_from_the_deliverable(self):
        doc = _engine()._review_document(_mr(), _opinions(), "## Remarks\n- x\n")
        self.assertNotIn("MR Review Verdict", doc)   # the header block is orchestration noise
        self.assertIn("opinion 1", doc)              # the review itself survives

    def test_unconsolidated_fallback_says_it_is_unmerged(self):
        body = _engine()._fallback_body(_opinions())
        self.assertIn("NOT consolidated", body)
        self.assertIn("From Reviewer 1", body)


class TestReviewScope(unittest.TestCase):
    def test_scope_line_names_the_reviewed_repo_not_the_dev_repo(self):
        # A review targets one repository, which may be one the dev workflow never
        # touches; reusing the dev scope line would tell reviewers the wrong repo.
        line = _engine()._review_scope_line(_mr())
        self.assertIn("example/service", line)      # the MRContext under test
        self.assertIn("READ-ONLY", line)
        self.assertIn("not read", line.lower())

    def test_patch_only_review_says_there_is_no_repository(self):
        mr = _mr()
        mr.repo_path = None
        self.assertIn("no repository in scope", _engine()._review_scope_line(mr))


class TestGuardScope(unittest.TestCase):
    """A read-only review must not die because someone edited an unrelated repo."""

    def _violation(self, labels):
        from orchestrator.guard import GuardViolation
        return GuardViolation("read-only agent altered the working tree", labels=labels)

    def test_change_in_another_repo_is_tolerated(self):
        e = _engine()
        e._review_repo_label = "projects/monolith"
        self.assertTrue(e._outside_review_scope(self._violation({"example/service"})))

    def test_change_in_the_reviewed_repo_still_aborts(self):
        e = _engine()
        e._review_repo_label = "projects/monolith"
        self.assertFalse(e._outside_review_scope(
            self._violation({"projects/monolith", "example/service"})))

    def test_unattributable_change_still_aborts(self):
        # No labels = the guard could not say where it happened; stay conservative.
        e = _engine()
        e._review_repo_label = "projects/monolith"
        self.assertFalse(e._outside_review_scope(self._violation(set())))

    def test_other_workflows_are_unaffected(self):
        e = _engine()
        e.workflow = "development"
        e._review_repo_label = "projects/monolith"
        self.assertFalse(e._outside_review_scope(self._violation({"example/service"})))

    def test_guard_attributes_a_content_change_to_its_repo(self):
        # A further edit to an ALREADY dirty file adds no porcelain line; without the
        # per-repo hashes the violation would carry no repo and abort every review.
        from orchestrator.guard import GitGuard, GuardViolation
        g = GitGuard([("repo-a", "/x")], enabled=False)
        before = ("h1", ["repo-a: f.py"], {"repo-a": "aaa"})
        after = ("h2", ["repo-a: f.py"], {"repo-a": "bbb"})
        with self.assertRaises(GuardViolation) as cm:
            g.assert_unchanged(before, after, "codex", "step")
        self.assertEqual(cm.exception.labels, frozenset({"repo-a"}))


class TestMRResolution(unittest.TestCase):
    """Real git, throwaway repo: the diff a reviewer sees must be the MR's own work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        self._write("app.py", "base\n")
        self._git("add", "-A"); self._git("commit", "-qm", "base")
        self._git("checkout", "-qb", "feature")
        self._write("app.py", "base\nfeature work\n")
        self._git("add", "-A"); self._git("commit", "-qm", "feature work")
        self._git("checkout", "-q", "main")
        self._write("other.py", "moved on without us\n")
        self._git("add", "-A"); self._git("commit", "-qm", "unrelated main commit")
        self.cfg = {"paths": {"repo_root": str(self.repo.parent), "dev_repos": ["repo"]}}

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True)

    def _write(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")

    def test_diff_is_merge_based_so_target_branch_churn_is_not_blamed_on_the_mr(self):
        mr = mrlib.resolve(self.cfg, base="main", head="feature")
        self.assertIn("feature work", mr.diff)
        self.assertNotIn("moved on without us", mr.diff,
                         "a two-dot diff would blame the target branch's own commits on this MR")
        self.assertIn("feature work", mr.commits)

    def test_working_tree_on_another_commit_is_announced(self):
        # Reviewers read surrounding code from the CHECKED-OUT tree; if that is not the
        # head under review they must be told, or they will trust the wrong file.
        mr = mrlib.resolve(self.cfg, base="main", head="feature")
        self.assertTrue(any("NOT at the reviewed head" in n for n in mr.notes), mr.notes)

    def test_empty_diff_is_refused_with_a_usable_message(self):
        with self.assertRaises(mrlib.MRResolutionError) as cm:
            mrlib.resolve(self.cfg, base="main", head="main")
        self.assertIn("nothing to review", str(cm.exception))

    def test_oversized_diff_is_truncated_in_band(self):
        self._git("checkout", "-q", "feature")
        self._write("big.py", "x = 1\n" * (mrlib.DIFF_MAX_BYTES // 3))
        self._git("add", "-A"); self._git("commit", "-qm", "big")
        self._git("checkout", "-q", "main")
        mr = mrlib.resolve(self.cfg, base="main", head="feature")
        self.assertIn("TRUNCATED", mr.diff)
        self.assertTrue(any("truncated" in n for n in mr.notes), mr.notes)

    def test_unknown_repo_label_names_the_configured_ones(self):
        with self.assertRaises(mrlib.MRResolutionError) as cm:
            mrlib.resolve(self.cfg, repo="not-a-repo")
        self.assertIn("repo", str(cm.exception))

    def test_any_git_path_may_be_reviewed_and_is_flagged_for_the_guard(self):
        # Code worth reviewing routinely lives outside the workflows' watched repos
        # (the monolith, a colleague's clone). Such a target is reviewable, and it is
        # reported as unwatched so the caller can add it to the git guard for the run.
        label, path, watched = mrlib.pick_repo(self.cfg, str(self.repo))
        self.assertEqual(Path(path), self.repo)
        self.assertFalse(watched)
        # a watched repo, by label, is still watched
        _, _, watched_label = mrlib.pick_repo(self.cfg, "repo")
        self.assertTrue(watched_label)


if __name__ == "__main__":
    unittest.main()
