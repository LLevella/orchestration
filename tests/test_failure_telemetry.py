"""P2: complete failure telemetry and honest token accounting.

The defect these tests pin (investigation 20260829T145046Z-investigation-69f4):

  1. `_parse_claude_json` recorded `input_tokens` only, so a real step whose true
     input was 2,891,127 tokens (52 fresh + 129,660 cache-creation + 2,761,415
     cache-read) was stored as `input: 52` — under-reported 55,599x.
  2. `CliAdapter.run` returned on `returncode != 0` BEFORE parsing, discarding a
     complete JSON body carrying `usage`, `total_cost_usd` and the real reason.
     The CLI wrote nothing to stderr, so the operator got
     `agent 'claude_reviewer' (Developer Verifier) returned ERROR: no detail`
     instead of "You've hit your session limit".

The captured body is a scrubbed copy in `tests/data/` — never read from the run
corpus, which is read-only historical evidence and not a test dependency.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from orchestrator import failures
from orchestrator.adapters import (ERROR, OK, UNAVAILABLE, AgentResponse, ApiAdapter,
                                   CliAdapter, _normalize_claude_usage)
from orchestrator.roster import is_transient, is_unavailable
from orchestrator.verdicts import ROLE_VERDICTS, extract_verdict

DATA = Path(__file__).resolve().parent / "data"
CLAUDE_CFG = {"command": ["claude", "-p", "{prompt}"], "usage_format": "claude_json"}


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class _FakeRun:
    """Stands in for subprocess.run so no CLI is ever launched."""
    def __init__(self, result):
        self.result = result

    def __call__(self, *a, **kw):
        return self.result


def _run_cli(monkey_result, cfg=None):
    ad = CliAdapter("claude_reviewer", dict(cfg or CLAUDE_CFG))
    ad.availability = lambda: (True, "ok")           # no PATH dependency
    import orchestrator.adapters as mod
    real, mod.subprocess.run = mod.subprocess.run, _FakeRun(monkey_result)
    try:
        return ad.run("sys", "prompt")
    finally:
        mod.subprocess.run = real


class TestCapturedSessionLimit(unittest.TestCase):
    """The real b343 payload, end to end through the adapter."""

    @classmethod
    def setUpClass(cls):
        cls.raw = (DATA / "claude_session_limit.json").read_text(encoding="utf-8")
        cls.body = json.loads(cls.raw)
        # A non-zero exit AND an is_error body — exactly how it arrived.
        cls.resp = _run_cli(_completed(stdout=cls.raw, stderr="", returncode=1))

    def test_every_input_class_is_captured(self):
        u = self.resp.usage
        self.assertIsNotNone(u, "usage was discarded with the error body")
        # modelUsage scope: opus + the haiku side-calls, matching total_cost_usd.
        self.assertEqual(u["input_fresh"], 16505)          # 16,453 haiku + 52 opus
        self.assertEqual(u["input_cache_creation"], 129660)
        self.assertEqual(u["input_cache_read"], 2761415)
        self.assertEqual(u["input"], 2907580)
        self.assertEqual(u["output"], 27999)               # 21 haiku + 27,978 opus
        self.assertEqual(u["total"], 2935579)

    def test_top_level_input_evidence_totals_2891127(self):
        """The single-model view the old parser saw: it stored 52 of these."""
        u = _normalize_claude_usage(self.body["usage"])
        self.assertEqual(u["input"], 2891127)
        self.assertEqual(u["input_fresh"], 52)
        self.assertNotEqual(u["input"], 52)

    def test_provider_reported_cost_is_kept(self):
        self.assertAlmostEqual(self.resp.cost_usd, 3.393575499999999, places=9)

    def test_reason_is_the_providers_own_words(self):
        self.assertIn("You've hit your session limit", self.resp.error)
        self.assertIn("You've hit your session limit", self.resp.failure.reason)
        self.assertIn("You've hit your session limit", failures.operator_reason(self.resp))
        self.assertNotEqual(failures.operator_reason(self.resp), "no detail")

    def test_classified_quota_account_not_generic_rate_limit(self):
        f = self.resp.failure
        self.assertEqual((f.kind, f.scope), (failures.QUOTA, failures.SCOPE_ACCOUNT))
        self.assertEqual(f.classification_source, failures.SRC_STRUCTURED)
        self.assertEqual(f.http_status, 429)

    def test_text_carries_no_verdict_parseable_content(self):
        self.assertEqual(self.resp.text, "")
        for role in ROLE_VERDICTS:
            self.assertIsNone(extract_verdict(role, self.resp.text), role)

    def test_raw_body_is_preserved_and_bounded(self):
        """A2: the body may never be `text`, but it may never be lost either."""
        raw = self.resp.failure.raw
        self.assertIn("You've hit your session limit", raw)
        self.assertIn("total_cost_usd", raw)
        self.assertLessEqual(len(raw), 2 * failures.RAW_BOUND + 200)

    def test_still_unavailable_to_the_legacy_helpers(self):
        self.assertTrue(is_unavailable(self.resp.failure.raw))
        self.assertTrue(self.resp.failure.roster_unavailable)
        self.assertFalse(self.resp.failure.retry_same_agent)  # a session limit persists
        # ...and the decision the engine ACTS on is the same one it was before P2:
        # the body carries `api_error_status: 429`, which the legacy markers already
        # matched, so this step shifted the roster then and still shifts it now.
        self.assertTrue(self.resp.failure.compat_roster_unavailable)
        self.assertFalse(self.resp.failure.compat_retry_same_agent)

    def test_date_less_reset_prose_yields_no_retry_at(self):
        """"resets 7:50pm" has no date: guessing one installs a wrong deadline."""
        self.assertIsNone(self.resp.failure.retry_at)


class TestErrorBodySemantics(unittest.TestCase):
    def test_is_error_is_unsuccessful_on_exit_code_zero(self):
        body = json.dumps({"result": "Spending cap reached resets 9pm", "is_error": True,
                           "total_cost_usd": 0.25,
                           "usage": {"input_tokens": 10, "output_tokens": 2}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertIn("Spending cap", resp.error)
        self.assertAlmostEqual(resp.cost_usd, 0.25)
        self.assertTrue(resp.failure.roster_unavailable)

    def test_error_body_cannot_become_an_answer_or_a_verdict(self):
        """A process failure is authoritative even if the body holds a real token."""
        body = json.dumps({"result": "## Investigator Verdict\nNO_DEFECT_FOUND",
                           "is_error": True})
        resp = _run_cli(_completed(stdout=body, returncode=1))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertIsNone(extract_verdict("Root Cause Investigator", resp.text))
        # ...but the operator still sees what the provider said.
        self.assertIn("NO_DEFECT_FOUND", resp.failure.raw)

    def test_is_error_without_result_is_still_an_error_body(self):
        """Recognition must not hang on `result` being present.

        `claude -p --output-format json` can report a failure without echoing a
        `result` string. Keyed on `result`, such a body fell through to the
        plain-text branch: the raw error JSON became `AgentResponse.text`, so a
        verdict token inside it was offered to the gate — the exact A2 boundary
        violation. Exit code is 0 here, so ONLY the body can catch it."""
        body = json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution",
                           "api_error_status": 429, "total_cost_usd": 0.5,
                           "usage": {"input_tokens": 3, "output_tokens": 0},
                           "detail": "## Investigator Verdict\nNO_DEFECT_FOUND"})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        for role in ROLE_VERDICTS:
            self.assertIsNone(extract_verdict(role, resp.text), role)
        # the body itself is not lost: it is retained, bounded, for the operator
        self.assertIn("NO_DEFECT_FOUND", resp.failure.raw)
        self.assertIn("error_during_execution", resp.failure.raw)
        # ...and its telemetry survives too
        self.assertAlmostEqual(resp.cost_usd, 0.5)
        self.assertEqual(resp.usage["input_fresh"], 3)
        self.assertEqual(resp.failure.http_status, 429)

    def test_successful_body_is_unchanged(self):
        body = json.dumps({"result": "## Investigator Verdict\nNO_DEFECT_FOUND",
                           "total_cost_usd": 0.1,
                           "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                                     "cache_read_input_tokens": 3, "output_tokens": 4}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, OK)
        self.assertEqual(extract_verdict("Root Cause Investigator", resp.text),
                         "NO_DEFECT_FOUND")
        self.assertIsNone(resp.failure)
        self.assertEqual(resp.usage["input"], 6)
        self.assertEqual(resp.usage["total"], 10)

    def test_non_json_failure_still_yields_a_reason(self):
        resp = _run_cli(_completed(stdout="", stderr="boom: it broke", returncode=2))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.error, "boom: it broke")
        self.assertIn("boom: it broke", resp.failure.raw)

    def test_timeout_is_a_local_fact(self):
        ad = CliAdapter("claude", dict(CLAUDE_CFG))
        ad.availability = lambda: (True, "ok")
        import orchestrator.adapters as mod

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=900)
        real, mod.subprocess.run = mod.subprocess.run, boom
        try:
            resp = ad.run("sys", "prompt")
        finally:
            mod.subprocess.run = real
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.failure.kind, failures.TIMEOUT)
        self.assertEqual(resp.failure.classification_source, failures.SRC_LOCAL)
        self.assertFalse(resp.failure.retry_same_agent)  # 900s again buys nothing

    def test_missing_command_is_configuration(self):
        ad = CliAdapter("gemini", {"command": ["definitely-not-on-this-path"]})
        resp = ad.run("sys", "prompt")
        self.assertEqual(resp.status, UNAVAILABLE)
        self.assertEqual(resp.failure.kind, failures.CONFIGURATION)


class TestNoUsableAnswer(unittest.TestCase):
    """A CLI that exits 0 and says nothing is unsuccessful, per the approved
    response contract — but it must not become a new recovery path either."""

    def test_empty_stdout_on_a_zero_exit_is_unsuccessful(self):
        resp = _run_cli(_completed(stdout="   \n", stderr="", returncode=0))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertIn("no answer", resp.error)
        self.assertEqual(resp.failure.classification_source, failures.SRC_LOCAL)
        self.assertNotEqual(failures.operator_reason(resp), "no detail")

    def test_a_claude_body_with_an_empty_result_is_unsuccessful(self):
        """The transport and the JSON both say "fine"; there is still no answer."""
        body = json.dumps({"result": "", "total_cost_usd": 0.02,
                           "usage": {"input_tokens": 5, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0, "output_tokens": 0}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        # the telemetry of the wasted call is still kept
        self.assertAlmostEqual(resp.cost_usd, 0.02)
        self.assertEqual(resp.usage["input"], 5)

    def test_no_answer_cannot_satisfy_a_gate(self):
        for role in ROLE_VERDICTS:
            self.assertIsNone(extract_verdict(role, ""), role)
        resp = _run_cli(_completed(stdout="", returncode=0))
        self.assertIsNone(extract_verdict("Root Cause Investigator", resp.text))

    def test_no_answer_does_not_broaden_roster_recovery(self):
        """Nothing was said, so no legacy marker matches: the run stops with a real
        reason instead of walking the chain looking for a different vendor."""
        resp = _run_cli(_completed(stdout="", returncode=0))
        self.assertFalse(resp.failure.compat_roster_unavailable)
        self.assertFalse(resp.failure.compat_retry_same_agent)

    def test_a_non_empty_answer_is_untouched(self):
        resp = _run_cli(_completed(stdout="the answer", returncode=0),
                        cfg={"command": ["gemini"]})
        self.assertEqual(resp.status, OK)
        self.assertEqual(resp.text, "the answer")


class TestRecoveryBoundaryIsUnchanged(unittest.TestCase):
    """P2 is telemetry. It must not change WHICH failures retry or shift left —
    that is P1, which is not in this change set. `retry_same_agent` /
    `roster_unavailable` describe the kind; `compat_*` is what the engine applies,
    and it is exactly the pre-P2 legacy-marker answer."""

    def test_timeout_is_described_but_does_not_shift(self):
        f = failures.classify(stderr="TimeoutExpired: 'claude' timed out after 900 seconds",
                              local_kind=failures.TIMEOUT)
        self.assertEqual(f.kind, failures.TIMEOUT)
        self.assertTrue(f.roster_unavailable)          # P1 policy: describes the kind
        self.assertFalse(f.compat_roster_unavailable)  # P2 behaviour: unchanged
        self.assertFalse(f.compat_retry_same_agent)
        self.assertFalse(is_unavailable(f.raw), "pre-P2 did not shift on a timeout")

    def test_missing_command_is_described_but_does_not_shift(self):
        f = failures.classify(stderr="'codex' not found in PATH",
                              local_kind=failures.CONFIGURATION)
        self.assertEqual(f.kind, failures.CONFIGURATION)
        self.assertTrue(f.roster_unavailable)
        self.assertFalse(f.compat_roster_unavailable)
        self.assertFalse(is_unavailable(f.raw), "pre-P2 did not shift on a missing command")

    def test_compat_is_exactly_the_legacy_verdict_even_when_that_is_accidental(self):
        """`compat_*` is not "what we think should recover" — it is literally what
        `is_unavailable()` said before P2, quirks included. A raised
        FileNotFoundError shifts the roster because the marker `enotfound` is a
        substring of the exception NAME. That is pre-existing behaviour; P2 keeps
        it rather than quietly correcting it, which would be a P1 policy change."""
        raised = "FileNotFoundError: [Errno 2] No such file or directory: 'codex'"
        f = failures.classify(stderr=raised, local_kind=failures.CONFIGURATION)
        self.assertTrue(is_unavailable(raised))          # the accident, pinned
        self.assertEqual(f.compat_roster_unavailable, is_unavailable(f.raw))
        self.assertEqual(f.compat_retry_same_agent, is_transient(f.raw))

    def test_a_numeric_500_is_described_but_does_not_newly_retry(self):
        """500 is capacity/provider now, but "500" was never a legacy marker, so it
        must not start burning a second call on the same agent."""
        f = failures.classify(http_status=500, stderr="(no prose at all)")
        self.assertEqual(f.kind, failures.CAPACITY)
        self.assertTrue(f.retry_same_agent)
        self.assertFalse(f.compat_retry_same_agent)

    def test_every_legacy_marker_still_drives_the_applied_decision(self):
        from orchestrator.roster import TRANSIENT_MARKERS, UNAVAILABLE_MARKERS
        for marker in UNAVAILABLE_MARKERS:
            f = failures.classify(stderr=f"agent said: {marker} happened")
            self.assertTrue(f.compat_roster_unavailable, marker)
        for marker in TRANSIENT_MARKERS:
            f = failures.classify(stderr=f"agent said: {marker} happened")
            self.assertTrue(f.compat_retry_same_agent, marker)

    def test_applied_decision_reads_stdout_too(self):
        """The only widening P2 makes is EVIDENCE, which criterion 4 requires: the
        old seam read `resp.error or resp.text`, i.e. one stream, head-bounded."""
        f = failures.classify(stdout="You've reached your usage limit", stderr="")
        self.assertTrue(f.compat_roster_unavailable)
        tail = failures.classify(stderr="x" * (failures.RAW_BOUND * 4) + "\nsocket hang up")
        self.assertTrue(tail.compat_retry_same_agent)


class TestUsageShapes(unittest.TestCase):
    def test_codex_reports_a_bare_total(self):
        ad = CliAdapter("codex", {"command": ["codex"], "usage_format": "codex_text"})
        parsed = ad._parse_output("some answer\ntokens used: 12,345\n")
        self.assertEqual(parsed.usage["total"], 12345)
        for k in ("input", "input_fresh", "input_cache_creation",
                  "input_cache_read", "output"):
            self.assertIsNone(parsed.usage[k], k)

    def test_openai_style_shape(self):
        from orchestrator.adapters import _normalize_openai_usage
        u = _normalize_openai_usage({"usage": {"prompt_tokens": 100,
                                               "completion_tokens": 20,
                                               "total_tokens": 120}})
        self.assertEqual((u["input"], u["output"], u["total"]), (100, 20, 120))
        self.assertIsNone(u["input_cache_read"])

    def test_openai_total_derived_when_absent(self):
        from orchestrator.adapters import _normalize_openai_usage
        u = _normalize_openai_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        self.assertEqual(u["total"], 120)

    def test_reported_zero_stays_zero_missing_stays_null(self):
        u = _normalize_claude_usage({"input_tokens": 0, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0, "output_tokens": 0})
        self.assertEqual(u["input"], 0)
        self.assertEqual(u["total"], 0)
        partial = _normalize_claude_usage({"input_tokens": 5, "output_tokens": 7})
        self.assertEqual(partial["input_fresh"], 5)
        self.assertIsNone(partial["input_cache_read"])
        self.assertIsNone(partial["input"])     # never inferred from a missing class
        self.assertIsNone(partial["total"])

    def test_incomplete_multi_model_usage_does_not_fake_a_total(self):
        from orchestrator.adapters import _normalize_claude_model_usage
        u = _normalize_claude_model_usage({
            "opus": {"inputTokens": 10, "outputTokens": 5,
                     "cacheReadInputTokens": 1, "cacheCreationInputTokens": 2},
            "haiku": {"inputTokens": 3, "outputTokens": 1},   # no cache classes
        })
        self.assertEqual(u["input_fresh"], 13)
        self.assertEqual(u["output"], 6)
        self.assertIsNone(u["input_cache_read"])
        self.assertIsNone(u["input"])
        self.assertIsNone(u["total"])

    def test_an_unreadable_model_entry_cannot_publish_a_partial_all_model_total(self):
        """A modelUsage block is an ALL-MODEL scope claim. If one entry cannot be
        read, its siblings' sum is a per-model slice wearing the whole step's
        label — so the block is refused as a unit and the top-level `usage` (a
        scope we CAN establish) answers instead."""
        from orchestrator.adapters import _normalize_claude_model_usage
        mixed = {"claude-haiku": {"inputTokens": 16453, "cacheCreationInputTokens": 0,
                                  "cacheReadInputTokens": 0, "outputTokens": 21},
                 "claude-opus": "unreadable"}
        self.assertIsNone(_normalize_claude_model_usage(mixed))
        body = json.dumps({"result": "the answer", "modelUsage": mixed,
                           "usage": {"input_tokens": 52,
                                     "cache_creation_input_tokens": 129660,
                                     "cache_read_input_tokens": 2761415,
                                     "output_tokens": 27978}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.usage["input"], 2891127)   # NOT the haiku-only 16,453
        self.assertEqual(resp.usage["input_fresh"], 52)
        self.assertEqual(resp.usage["output"], 27978)

    def test_an_unreadable_model_entry_with_no_fallback_leaves_usage_null(self):
        """No establishable scope at all: null, never a partial sum."""
        body = json.dumps({"result": "the answer",
                           "modelUsage": {"claude-haiku": {"inputTokens": 16453,
                                                           "outputTokens": 21},
                                          "claude-opus": None}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, OK)
        self.assertIsNone(resp.usage)

    def test_no_token_information_leaves_usage_null(self):
        resp = _run_cli(_completed(stdout="plain text answer", returncode=0),
                        cfg={"command": ["gemini"]})
        self.assertEqual(resp.status, OK)
        self.assertIsNone(resp.usage)
        self.assertIsNone(resp.cost_usd)   # unpriced, NOT $0.00


class TestMalformedPayloads(unittest.TestCase):
    """Telemetry parsing degrades; it never aborts the orchestration path.

    Providers vary and bodies get truncated. A value of an unexpected type must
    make THAT figure null (or the whole response a structured failure) — an
    exception escaping the adapter would lose a step that actually ran."""

    def test_non_numeric_usage_and_cost_degrade_to_null(self):
        body = json.dumps({"result": "the answer",
                           "total_cost_usd": "3.39",
                           "usage": {"input_tokens": "52", "output_tokens": None,
                                     "cache_read_input_tokens": {"n": 1}}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, OK)          # the answer is still an answer
        self.assertEqual(resp.text, "the answer")
        self.assertIsNone(resp.usage)              # no class was readable
        self.assertIsNone(resp.cost_usd)           # unreadable price is unpriced

    def test_an_overflowing_cost_still_yields_the_whole_failure_record(self):
        """A JSON integer is unbounded, so `float()` on it can overflow.

        The value is malformed, not expensive — but the conversion happens while
        the failure record is being built, so raising there would discard the
        usage, the reason and the raw body of a step that really ran, putting the
        operator back on `returned ERROR: no detail`.

        4000 digits is inside the window that can actually reach the adapter:
        above ~1e308 `float()` overflows, and past 4300 digits Python's own
        integer/string limit makes `json_or_none` refuse the body outright."""
        body = json.dumps({"result": "You've hit your session limit · resets 7:50pm",
                           "is_error": True, "api_error_status": 429,
                           "total_cost_usd": 10 ** 4000,
                           "usage": {"input_tokens": 52, "output_tokens": 27978}})
        resp = _run_cli(_completed(stdout=body, returncode=1))
        self.assertEqual(resp.status, ERROR)
        self.assertIsNone(resp.cost_usd)                  # unreadable price is unpriced
        self.assertEqual(resp.usage["input_fresh"], 52)   # the rest of the telemetry survives
        self.assertEqual(resp.usage["output"], 27978)
        self.assertIn("session limit", resp.error)        # the provider still speaks
        self.assertIn("session limit", failures.operator_reason(resp))
        self.assertIn("resets 7:50pm", resp.failure.raw)  # raw evidence retained
        self.assertEqual(resp.failure.http_status, 429)
        self.assertEqual(resp.text, "")                   # and it is still not an answer

    def test_a_partially_unreadable_usage_block_keeps_what_it_can(self):
        body = json.dumps({"result": "the answer",
                           "usage": {"input_tokens": 5, "output_tokens": "many"}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.usage["input_fresh"], 5)
        self.assertIsNone(resp.usage["output"])
        self.assertIsNone(resp.usage["total"])     # never inferred from a lost class

    def test_a_boolean_is_not_a_token_count(self):
        u = _normalize_claude_usage({"input_tokens": True, "output_tokens": 4})
        self.assertIsNone(u["input_fresh"])
        self.assertEqual(u["output"], 4)

    def test_model_usage_of_the_wrong_shape_falls_back(self):
        from orchestrator.adapters import _normalize_claude_model_usage
        self.assertIsNone(_normalize_claude_model_usage(["opus"]))
        self.assertIsNone(_normalize_claude_model_usage("opus"))
        body = json.dumps({"result": "the answer", "modelUsage": [],
                           "usage": {"input_tokens": 7, "output_tokens": 1}})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.usage["input_fresh"], 7)

    def test_a_structured_result_is_not_an_answer(self):
        """`result` must be a string to be an assistant message; a dict is not
        coerced into one (and an empty answer fails safe, per the contract)."""
        body = json.dumps({"result": {"text": "PASS"}, "total_cost_usd": 0.01})
        resp = _run_cli(_completed(stdout=body, returncode=0))
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertAlmostEqual(resp.cost_usd, 0.01)

    def test_an_unreadable_codex_footer_leaves_usage_null(self):
        ad = CliAdapter("codex", {"command": ["codex"], "usage_format": "codex_text"})
        parsed = ad._parse_output("some answer\ntokens used: ,\n")
        self.assertIsNone(parsed.usage)
        self.assertIn("some answer", parsed.text)

    def test_openai_usage_of_the_wrong_type_degrades(self):
        from orchestrator.adapters import _normalize_openai_usage
        self.assertIsNone(_normalize_openai_usage({"usage": "12 tokens"}))
        u = _normalize_openai_usage({"usage": {"prompt_tokens": 100,
                                               "completion_tokens": "20"}})
        self.assertEqual(u["input"], 100)
        self.assertIsNone(u["output"])
        self.assertIsNone(u["total"])


class TestStructuredClassification(unittest.TestCase):
    """Classification driven by codes and numeric status, not by prose."""

    def test_http_statuses(self):
        cases = {401: (failures.AUTHENTICATION, failures.SCOPE_ACCOUNT),
                 403: (failures.AUTHENTICATION, failures.SCOPE_ACCOUNT),
                 402: (failures.BILLING, failures.SCOPE_ACCOUNT),
                 429: (failures.RATE_LIMIT, failures.SCOPE_ACCOUNT),
                 500: (failures.CAPACITY, failures.SCOPE_PROVIDER),
                 503: (failures.CAPACITY, failures.SCOPE_PROVIDER),
                 529: (failures.CAPACITY, failures.SCOPE_PROVIDER)}
        for status, expected in cases.items():
            f = failures.classify(http_status=status, stderr="(no prose at all)")
            self.assertEqual((f.kind, f.scope), expected, status)
            self.assertEqual(f.classification_source, failures.SRC_HTTP_STATUS, status)

    def test_529_needs_no_overloaded_prose(self):
        f = failures.classify(http_status=529, stderr="")
        self.assertEqual(f.kind, failures.CAPACITY)
        self.assertTrue(f.retry_same_agent)
        self.assertTrue(f.roster_unavailable)

    def test_structured_code_beats_generic_status(self):
        f = failures.classify(http_status=429,
                              body={"error": {"code": "insufficient_quota",
                                              "message": "You exceeded your quota"}})
        self.assertEqual(f.kind, failures.QUOTA)
        self.assertEqual(f.provider_code, "insufficient_quota")
        self.assertEqual(f.reason, "You exceeded your quota")

    def test_invalid_request_is_fatal_not_a_roster_shift(self):
        f = failures.classify(http_status=400,
                              body={"error": {"type": "invalid_request_error",
                                              "message": "bad model name"}})
        self.assertEqual(f.kind, failures.INVALID_REQUEST)
        self.assertFalse(f.retry_same_agent)
        self.assertFalse(f.roster_unavailable)

    def test_unknown_failure_is_fatal(self):
        f = failures.classify(stderr="segmentation fault")
        self.assertEqual(f.kind, failures.UNKNOWN)
        self.assertFalse(f.roster_unavailable)

    def test_every_kind_has_an_explicit_policy_row(self):
        """A3/A4: retry_same_agent and roster_unavailable are tabulated, not derived."""
        for kind in (failures.QUOTA, failures.RATE_LIMIT, failures.CAPACITY,
                     failures.AUTHENTICATION, failures.BILLING, failures.TIMEOUT,
                     failures.TRANSPORT, failures.CONFIGURATION,
                     failures.INVALID_REQUEST, failures.UNKNOWN):
            self.assertIn(kind, failures.POLICY, kind)
        self.assertEqual(failures.POLICY[failures.CONFIGURATION],
                         (failures.SCOPE_AGENT, False, True))


class TestRetryAt(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertIsNotNone(failures.parse_retry_at(retry_after="120"))

    def test_retry_after_http_date(self):
        self.assertEqual(failures.parse_retry_at(retry_after="Wed, 21 Oct 2026 07:28:00 GMT"),
                         "2026-10-21T07:28:00Z")

    def test_malformed_retry_after_yields_no_deadline(self):
        """A header is untrusted input arriving exactly when an agent has already
        failed: reading it must never raise, or the failure record being built
        around it is lost. `isdigit()` accepts characters `int()` refuses, and a
        long enough delta overflows the clock — all of them mean "no deadline"."""
        for header in ("9" * 400,                    # overflows the addition
                       "99999999999999999999",       # out of range for the clock
                       "\u00b2",                     # isdigit() true, int() refuses
                       "-30", "nonsense", ""):
            with self.subTest(header=header):
                self.assertIsNone(failures.parse_retry_at(retry_after=header))

    def test_an_out_of_range_reset_is_refused(self):
        """Year 1 renders as "1-01-01", not the fixed-width stamp consumers expect."""
        self.assertIsNone(failures.parse_retry_at(body={"retry_at": "0001-01-01T00:00:00+00:00"}))

    def test_an_unusable_header_does_not_suppress_the_body(self):
        """Degrading the header must not also throw away a deadline the body states."""
        self.assertEqual(failures.parse_retry_at(retry_after="9" * 400,
                                                 body={"reset_at": "2026-10-21T07:28:00Z"}),
                         "2026-10-21T07:28:00Z")

    def test_ambiguous_prose_is_refused(self):
        self.assertIsNone(failures.parse_retry_at(
            body={"result": "You've hit your session limit · resets 7:50pm (Europe/Istanbul)"}))


class TestLegacyCompatibility(unittest.TestCase):
    """Acceptance 4: classification may only get stronger, never weaker."""

    def test_marker_at_the_tail_of_a_long_body_still_classifies(self):
        # codex echoes the whole prompt to stderr; the real line lands at the END.
        noise = "x" * (failures.RAW_BOUND * 4)
        f = failures.classify(stderr=noise + "\nYou've reached your usage limit")
        self.assertTrue(f.roster_unavailable, "a tail marker was truncated away")
        self.assertIn("usage limit", f.raw)

    def test_marker_at_the_head_of_a_long_body_still_classifies(self):
        noise = "x" * (failures.RAW_BOUND * 4)
        f = failures.classify(stderr="socket hang up\n" + noise)
        self.assertTrue(f.retry_same_agent)
        self.assertTrue(f.roster_unavailable)

    def test_every_legacy_unavailable_marker_still_shifts(self):
        from orchestrator.roster import UNAVAILABLE_MARKERS
        for marker in UNAVAILABLE_MARKERS:
            f = failures.classify(stderr=f"agent said: {marker} happened")
            self.assertTrue(f.roster_unavailable, marker)

    def test_every_legacy_transient_marker_still_retries(self):
        from orchestrator.roster import TRANSIENT_MARKERS
        for marker in TRANSIENT_MARKERS:
            f = failures.classify(stderr=f"agent said: {marker} happened")
            self.assertTrue(f.retry_same_agent, marker)

    def test_classifier_sees_stdout_as_well_as_stderr(self):
        """The old seam looked at `resp.error or resp.text` — one of the two."""
        f = failures.classify(stdout="You've reached your usage limit", stderr="")
        self.assertTrue(f.roster_unavailable)

    def test_legacy_response_without_a_failure_record_is_classified(self):
        resp = AgentResponse("gemini", ERROR, text="", error="503 Service Unavailable")
        f = failures.failure_for_response(resp)
        self.assertTrue(f.retry_same_agent)
        self.assertTrue(f.roster_unavailable)

    def test_existing_failure_record_is_authoritative(self):
        made = failures.classify(http_status=402)
        resp = AgentResponse("deepseek", ERROR, failure=made)
        self.assertIs(failures.failure_for_response(resp), made)

    def test_helpers_remain_importable_and_behaviourally_unchanged(self):
        self.assertTrue(is_unavailable("HTTP Error 402: Payment Required"))
        self.assertFalse(is_unavailable("NCC FL control message msg=402 handled"))
        self.assertTrue(is_transient("socket hang up"))


class TestApiAdapterErrorPath(unittest.TestCase):
    def _adapter(self):
        ad = ApiAdapter("deepseek", {"api_base": "https://example.invalid/v1",
                                     "api_env": "TEST_KEY", "model": "m"})
        ad.availability = lambda: (True, "ok")
        return ad

    def test_http_error_keeps_body_status_and_retry_after(self):
        import io
        import urllib.error
        import orchestrator.adapters as mod
        payload = json.dumps({"error": {"message": "Rate limit reached", "type": "rate_limit_error"},
                              "usage": {"prompt_tokens": 7, "completion_tokens": 0,
                                        "total_tokens": 7}}).encode()

        def boom(*a, **kw):
            raise urllib.error.HTTPError("u", 429, "Too Many Requests",
                                         {"Retry-After": "30"}, io.BytesIO(payload))
        real, mod.urllib.request.urlopen = mod.urllib.request.urlopen, boom
        import os
        os.environ["TEST_KEY"] = "x"
        try:
            resp = self._adapter().run("sys", "prompt")
        finally:
            mod.urllib.request.urlopen = real
            os.environ.pop("TEST_KEY", None)
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.failure.http_status, 429)
        self.assertEqual(resp.failure.kind, failures.RATE_LIMIT)
        self.assertEqual(resp.error, "Rate limit reached")
        self.assertIsNotNone(resp.failure.retry_at)
        self.assertEqual(resp.usage["input"], 7)

    def _run_http_error(self, status, phrase, body: bytes, headers=None):
        """Drive the adapter over an HTTPError carrying `body`."""
        import io
        import os
        import urllib.error
        import orchestrator.adapters as mod

        def boom(*a, **kw):
            raise urllib.error.HTTPError("u", status, phrase, headers or {},
                                         io.BytesIO(body))
        real, mod.urllib.request.urlopen = mod.urllib.request.urlopen, boom
        os.environ["TEST_KEY"] = "x"
        try:
            return self._adapter().run("sys", "prompt")
        finally:
            mod.urllib.request.urlopen = real
            os.environ.pop("TEST_KEY", None)

    def test_a_plain_text_http_error_body_is_the_operator_reason(self):
        """A non-JSON error response is still the provider SPEAKING (reason rung 5).
        urllib's own description is ours, not theirs (rung 6): passed as `stderr` it
        ranked at 4 and the abort quoted "HTTP Error 429" instead of the real words."""
        resp = self._run_http_error(
            429, "Too Many Requests",
            b"upstream is over capacity for this model, retry in a moment")
        self.assertEqual(resp.status, ERROR)
        self.assertIn("upstream is over capacity", resp.error)
        self.assertNotIn("HTTP Error 429", resp.error)
        self.assertIn("upstream is over capacity", failures.operator_reason(resp))
        self.assertEqual(resp.failure.http_status, 429)   # classification unaffected
        self.assertEqual(resp.failure.kind, failures.RATE_LIMIT)

    def test_a_malformed_retry_after_keeps_the_structured_failure(self):
        """The header is the provider's, so it can be anything. An unreadable one
        costs us the deadline and nothing else: status, kind, the provider's own
        words and the raw evidence all still reach the operator."""
        for header in ("9" * 400, "99999999999999999999", "later"):
            with self.subTest(header=header):
                resp = self._run_http_error(429, "Too Many Requests",
                                            b"upstream is over capacity, retry in a moment",
                                            headers={"Retry-After": header})
                self.assertEqual(resp.status, ERROR)
                self.assertIsNone(resp.failure.retry_at)
                self.assertEqual(resp.failure.http_status, 429)
                self.assertEqual(resp.failure.kind, failures.RATE_LIMIT)
                self.assertIn("over capacity", resp.error)
                self.assertIn("over capacity", resp.failure.raw)

    def test_an_empty_http_error_body_falls_back_to_the_local_exception(self):
        """Nothing was said by the provider, so the local description is the reason —
        `returned ERROR: no detail` stays impossible."""
        resp = self._run_http_error(402, "Payment Required", b"")
        self.assertEqual(resp.status, ERROR)
        self.assertIn("HTTP Error 402", resp.error)
        self.assertNotEqual(failures.operator_reason(resp), "no detail")

    def test_the_legacy_402_shift_decision_is_unchanged(self):
        """The exception string keeps reaching the legacy markers even though it no
        longer speaks for the provider: this body carries no marker of its own, so
        only "HTTP Error 402: Payment Required" can produce today's roster shift."""
        body = b"This account cannot be served until its balance is topped up."
        self.assertFalse(is_unavailable(body.decode()))    # no marker in the body
        resp = self._run_http_error(402, "Payment Required", body)
        self.assertIn("balance is topped up", resp.error)  # provider still speaks
        self.assertTrue(resp.failure.compat_roster_unavailable)
        self.assertFalse(resp.failure.compat_retry_same_agent)
        self.assertEqual(resp.failure.kind, failures.BILLING)
        # the record still reproduces the applied decision on its own
        self.assertEqual(resp.failure.compat_roster_unavailable,
                         is_unavailable(resp.failure.raw))

    def test_structured_error_in_a_200_body(self):
        import io
        import os
        import orchestrator.adapters as mod
        payload = json.dumps({"error": {"message": "Insufficient Balance",
                                        "code": "insufficient_balance"}}).encode()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        real, mod.urllib.request.urlopen = mod.urllib.request.urlopen, lambda *a, **kw: _Resp(payload)
        os.environ["TEST_KEY"] = "x"
        try:
            resp = self._adapter().run("sys", "prompt")
        finally:
            mod.urllib.request.urlopen = real
            os.environ.pop("TEST_KEY", None)
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertEqual(resp.failure.kind, failures.BILLING)
        self.assertEqual(resp.error, "Insufficient Balance")

    def _run_ok_body(self, payload: bytes):
        """Drive the adapter over a 200 response carrying `payload`."""
        import io
        import os
        import orchestrator.adapters as mod

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        real, mod.urllib.request.urlopen = mod.urllib.request.urlopen, lambda *a, **kw: _Resp(payload)
        os.environ["TEST_KEY"] = "x"
        try:
            return self._adapter().run("sys", "prompt")
        finally:
            mod.urllib.request.urlopen = real
            os.environ.pop("TEST_KEY", None)

    def test_shape_error_quotes_the_provider_not_the_parser(self):
        """Reason precedence: a LOCAL parser diagnostic is the last rung (6), so
        the provider's own body (5) still gets to speak. Passing the diagnostic as
        `stderr` (4) ranked it above the body and hid the real words behind a
        Python exception message."""
        resp = self._run_ok_body(b"upstream connect error or disconnect/reset before headers")
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.text, "")
        self.assertIn("upstream connect error", resp.error)
        self.assertNotIn("not a JSON object", resp.error)
        self.assertIn("upstream connect error", resp.failure.raw)

    def test_shape_error_falls_back_to_the_diagnostic_when_the_body_is_silent(self):
        """`returned ERROR: no detail` stays impossible when nobody said anything."""
        resp = self._run_ok_body(b"")
        self.assertEqual(resp.status, ERROR)
        self.assertIn("not a JSON object", resp.error)
        self.assertNotEqual(failures.operator_reason(resp), "no detail")

    def test_an_unexpected_message_shape_is_a_structured_failure(self):
        """A provider variant answering with a non-object `message` must not raise
        out of the adapter (AttributeError was uncaught) — it degrades."""
        for choices in ([{"message": "just a string"}],
                        [{"message": {"content": {"parts": ["hi"]}}}],
                        ["not an object"], [], {"0": {}}):
            with self.subTest(choices=choices):
                resp = self._run_ok_body(json.dumps({"choices": choices}).encode())
                self.assertEqual(resp.status, ERROR)
                self.assertEqual(resp.text, "")
                self.assertTrue(resp.error.strip())
                for role in ROLE_VERDICTS:
                    self.assertIsNone(extract_verdict(role, resp.text), role)

    def test_usage_survives_an_unexpected_message_shape(self):
        resp = self._run_ok_body(json.dumps(
            {"choices": [{"message": None}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 2}}).encode())
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(resp.usage["input"], 11)


if __name__ == "__main__":
    unittest.main()
