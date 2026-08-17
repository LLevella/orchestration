"""Unit tests for the verdict parser (orchestrator/verdicts.py).

The parser is load-bearing: the engine routes entirely on the token it extracts.
These cover the cases that actually bit us (the investigator's ROOT_CAUSE_ESTABLISHED
token, header-anchoring vs prose mentions, longest-match, empty/unknown inputs).
Copy-agnostic: depends only on verdicts.py, identical in both orchestrator copies.
"""
import unittest

from orchestrator.verdicts import extract_verdict, ROLE_VERDICTS


class TestExtractVerdict(unittest.TestCase):
    def test_investigator_root_cause_established_is_recognized(self):
        # An over-eager Investigator may self-declare this terminal token; the parser
        # MUST recognize it so the engine can route it into the gate (not fail-safe).
        t = "## Investigator Verdict\nROOT_CAUSE_ESTABLISHED\n\nbody..."
        self.assertEqual(extract_verdict("Root Cause Investigator", t), "ROOT_CAUSE_ESTABLISHED")

    def test_investigator_handoff_token(self):
        t = "## Investigator Verdict\nHYPOTHESIS_READY_FOR_REFUTATION\nrest"
        self.assertEqual(extract_verdict("Root Cause Investigator", t),
                         "HYPOTHESIS_READY_FOR_REFUTATION")

    def test_developer_verifier_tokens(self):
        for tok in ("FEASIBLE", "NEEDS_REVISION", "HUMAN_REQUIRED"):
            t = f"## Implementation Feasibility Verdict\n{tok}\n..."
            self.assertEqual(extract_verdict("Developer Verifier", t), tok)

    def test_header_anchored_beats_earlier_prose_mention(self):
        # NEEDS_FIX appears in prose BEFORE the header; the real verdict (after the
        # header) is PASS. Header-anchoring must win.
        t = "I first thought NEEDS_FIX.\n\n## Code Review Verdict\nPASS\n"
        self.assertEqual(extract_verdict("Software Engineering Code Reviewer", t), "PASS")

    def test_first_known_token_when_no_header(self):
        t = "no header, but the conclusion is REFUTED here."
        self.assertEqual(extract_verdict("Hypothesis Refuter", t), "REFUTED")

    def test_unknown_role_returns_none(self):
        self.assertIsNone(extract_verdict("No Such Role", "PASS NEEDS_FIX FEASIBLE"))

    def test_empty_or_none_text_returns_none(self):
        self.assertIsNone(extract_verdict("Software Engineering Code Reviewer", ""))
        self.assertIsNone(extract_verdict("Software Engineering Code Reviewer", None))

    def test_no_known_token_returns_none(self):
        t = "## Implementation Feasibility Verdict\n(the model forgot to emit a verdict)"
        self.assertIsNone(extract_verdict("Developer Verifier", t))

    def test_multiword_token_matched_whole(self):
        # tokens with underscores/length must match whole (longest-first), not a prefix.
        t = "## Tech Lead Verdict\nESCALATE_TO_HUMAN\n"
        self.assertEqual(extract_verdict("Technical Lead", t), "ESCALATE_TO_HUMAN")

    def test_every_role_has_header_and_nonempty_tokens(self):
        # structural invariant: each role maps to (header, [>=1 tokens]).
        for role, (header, tokens) in ROLE_VERDICTS.items():
            self.assertTrue(header, role)
            self.assertTrue(tokens, role)
            self.assertTrue(all(isinstance(x, str) and x for x in tokens), role)


if __name__ == "__main__":
    unittest.main()
