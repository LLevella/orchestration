"""The architecture cycle's four prompts say true things about the loop (DEV-07, 2026-08-31).

Two adversarial review panels returned CRITICAL_FLAWS on the prompts that drive the
`architecture` workflow. The defects were textual, so the regressions are textual too - each
test below is pinned against one of them:

  * the author cell was given `System Architect`, a role written as a REVIEWER of an
    implementation ("Developer summary / Git diff / Tests-build output"), patched by a wrapper
    that told it "actually you are an author". It is now its own role, and `System Architect`
    stays a diff reviewer for `development`;
  * the Developer Verifier had no closure rule, so an empty gap list could still come back
    NEEDS_REVISION (329 NEEDS_REVISION vs 19 FEASIBLE in the run corpus);
  * both gates could block on an uncited impression;
  * the prompts misstated who ran before and after them, and named a person.

These assert on the shipped prompt files and the shipped config, which is what a run loads.
"""
import unittest
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.verdicts import ROLE_VERDICTS

ROLES = Path(__file__).resolve().parent.parent / "roles"

AUTHOR = "Architecture Author"
VERIFIER = "Developer Verifier"
TECH_LEAD = "Technical Lead"
ARBITER = "Arbitration Reviewer"

# Verbatim, from the ratified decision (DECISIONS-convergence-2026-08-31, decision 3).
# The wording is the contract: it is what makes an empty gap list dispositive.
CLOSURE_RULE = """## Verdict Closure Rule
The verdict is determined by the Blocking Implementation Gaps list, not by your overall
impression. If that list is empty, you MUST return `FEASIBLE`. Every gap you list must name a
decision the implementer would be forced to invent, or a cost the proposal does not budget —
and must be phrased as a question the Architect can close in one revision. Anything you cannot
phrase that way is a `Note for Architect`, not a gap. Returning `NEEDS_REVISION` with an empty
or unactionable gap list is as serious a failure as rubber-stamping: it burns the run's budget
without advancing it. `FEASIBLE` means "buildable with the normal amount of implementer
judgment", not "nothing left to improve"."""


def role_text(role):
    return (ROLES / f"{role}.md").read_text(encoding="utf-8")


class TestAuthorReviewerSplit(unittest.TestCase):
    """One document, one voice: the author is an author, the reviewer is a reviewer."""

    def setUp(self):
        _agents, self.wf = load_config()

    def test_architecture_authors_with_the_author_role_and_no_wrapper(self):
        author = self.wf["architecture"]["author"]
        self.assertEqual(author["role"], AUTHOR)
        # The wrapper existed only to contradict the reviewer role underneath it.
        self.assertNotIn("wrapper", author,
                         "the author role speaks for itself; a wrapper re-introduces two voices")

    def test_the_author_role_never_mentions_reviewing_a_diff(self):
        self.assertNotIn("diff", role_text(AUTHOR).lower())

    def test_the_author_role_does_not_call_itself_a_reviewer(self):
        text = role_text(AUTHOR).lower()
        for phrase in ("normally a review", "you review the", "base role"):
            self.assertNotIn(phrase, text)

    def test_development_still_reviews_a_diff_with_system_architect(self):
        self.assertEqual(self.wf["development"]["architect"]["role"], "System Architect")
        # Unchanged in substance: it is still entered on a developer's diff.
        self.assertIn("Git diff", role_text("System Architect"))

    def test_the_author_role_is_wired_end_to_end(self):
        """Role file on disk, verdict spec (nominal — the author is called parse=False),
        and an allowlist that still admits every agent that could author before."""
        agents, _wf = load_config()
        self.assertTrue((ROLES / f"{AUTHOR}.md").exists())
        self.assertIn(AUTHOR, ROLE_VERDICTS)
        eligible = {n for n, a in agents["agents"].items() if AUTHOR in (a.get("roles") or [])}
        self.assertEqual(eligible, {"codex", "claude_author", "claude_haiku"},
                         "the split must not change who can fill the author cell")


class TestVerifierClosureRule(unittest.TestCase):
    def setUp(self):
        self.text = role_text(VERIFIER)

    def test_closure_rule_is_present_verbatim(self):
        self.assertIn(CLOSURE_RULE, self.text)

    def test_closure_rule_sits_immediately_above_the_output_format(self):
        # It is dispositive, so it must be the last thing read before the verdict is written.
        self.assertIn(CLOSURE_RULE + "\n\n## Output Format", self.text)

    def test_iterations_2plus_rule_exists(self):
        self.assertIn("## Iterations 2+", self.text)
        self.assertIn("Do not re-open points you already settled", self.text)


class TestBlockersNeedEvidence(unittest.TestCase):
    """A blocking objection quotes the proposal; anything uncitable is advisory."""

    def test_verifier_requires_a_quote_and_routes_the_rest_to_notes(self):
        text = role_text(VERIFIER)
        self.assertIn("must quote the line of the proposal it is about", text)
        self.assertIn("advisory by definition", text)
        self.assertIn("Notes for Architect", text)

    def test_tech_lead_requires_a_quote_and_routes_the_rest_to_notes(self):
        text = role_text(TECH_LEAD)
        self.assertIn("quote the line of the artifact it is about", text)
        self.assertIn("advisory by definition", text)

    def test_tech_lead_may_not_relabel_advisory_notes_as_blockers(self):
        self.assertIn("may not re-label the Verifier's advisory notes as blockers",
                      role_text(TECH_LEAD))


class TestReadScopeBoundary(unittest.TestCase):
    """Both read-only architecture-cycle roles describe the same trust boundary.

    The verifier used to be allowed "adjacent/source repos", which crosses the workspace
    isolation the task scope draws and contradicts the global policy appended to every role.
    """

    def test_read_only_roles_are_limited_to_the_repositories_in_the_task_scope(self):
        for role in (AUTHOR, VERIFIER):
            with self.subTest(role=role):
                # The permission spans wrapped lines, so compare on collapsed whitespace.
                text = " ".join(role_text(role).split())
                self.assertIn("repositories explicitly listed in the task's scope", text)
                self.assertIn("never read or reference anything outside them", text)
                self.assertNotIn("adjacent", text)


class TestPromptsDescribeTheRealWorkflow(unittest.TestCase):
    def test_verifier_knows_it_is_the_first_gate(self):
        text = role_text(VERIFIER)
        self.assertIn("**FIRST** gate", text)
        self.assertIn("The Tech Lead runs **after** you", text)

    def test_verifier_does_not_invent_a_code_reviewer(self):
        text = role_text(VERIFIER)
        self.assertIn("no Code Reviewer in this workflow", text)
        # The old text claimed a Code Reviewer had already worked on the proposal.
        self.assertNotIn("the Architect, the Code Reviewer, and the Tech Lead have already", text)

    def test_tech_lead_reads_the_feasibility_report_not_a_diff(self):
        text = role_text(TECH_LEAD)
        self.assertIn("Developer Verifier's feasibility report", text)
        self.assertIn("there is no developer summary, no git diff and no build output", text)

    def test_both_gates_judge_against_the_task_acceptance_criteria(self):
        for role in (VERIFIER, TECH_LEAD):
            with self.subTest(role=role):
                text = role_text(role)
                self.assertIn("acceptance criteria stated in the task", text)
                self.assertIn("that absence is itself the first blocking gap", text)

    def test_no_architecture_cycle_prompt_names_a_person(self):
        for role in (AUTHOR, VERIFIER, TECH_LEAD, ARBITER):
            with self.subTest(role=role):
                self.assertNotIn("Irina", role_text(role))


if __name__ == "__main__":
    unittest.main()
