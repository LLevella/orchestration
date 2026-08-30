"""P2: one attempt record per adapter invocation, and honest usage aggregation.

Counting only logical steps loses the invocations that failed and then recovered
— a same-agent transient retry, a shift to another vendor, a verdict repair —
even though every one of them spent real tokens and, on Claude, real money. This
suite pins that each invocation is journaled exactly once, that usage.json sums
those attempts rather than the logical `step` event, and that an unpriced
transport is reported as unmetered instead of as $0.00 of spend.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import failures
from orchestrator.adapters import ERROR, OK, AgentResponse
from orchestrator.engine import Engine, AgentError
from orchestrator.transcript import Transcript


class _Guard:
    def branches(self):
        return None

    def snapshot(self):
        return None

    def assert_branch_unchanged(self, *a):
        return None

    def assert_unchanged(self, *a):
        return None


class _Adapter:
    """Scripted responses, one per run() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        self.calls += 1
        return self._responses.pop(0)


class _Ctx:
    def __init__(self, tx, adapters):
        self.adapters = adapters
        self.tx = tx
        self.guard = _Guard()
        self.resume_cache = {}
        self.workflows = {"investigation": {}}
        self.live_calls = 0
        self.last_call_replayed = False
        self.last_call_final_replay = False

    def resume_lookup(self, *a):
        return None

    def load_role(self, role, wrapper=None):
        return "system prompt"


ROLE = "Root Cause Investigator"
GOOD = "## Investigator Verdict\nNO_DEFECT_FOUND\n"


def _tx(tmp):
    return Transcript(tmp, "run-1", "investigation", "task", False, None, {})


def _close(tx):
    """finish() closes the journal; tests that stop short of it close it here."""
    if not tx._events.closed:
        tx._events.close()


def _usage(**kw):
    from orchestrator.adapters import USAGE_KEYS
    return {k: kw.get(k) for k in USAGE_KEYS}


def _events(tx, kind):
    lines = (tx.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(l) for l in lines) if e.get("kind") == kind]


class TestAttemptRecording(unittest.TestCase):
    def setUp(self):
        # The transient-retry backoff is a real sleep; drop it to zero for the
        # duration of the test rather than patching the shared time module.
        import orchestrator.engine as mod
        self._backoff, mod.TRANSIENT_RETRY_BACKOFF_S = mod.TRANSIENT_RETRY_BACKOFF_S, 0
        self.addCleanup(setattr, mod, "TRANSIENT_RETRY_BACKOFF_S", self._backoff)

    def _call(self, responses, tmp):
        tx = _tx(tmp)
        ctx = _Ctx(tx, {"claude": _Adapter(responses)})
        return tx, Engine(ctx, "investigation"), ctx

    def test_one_attempt_per_invocation_including_the_retry(self):
        transient = AgentResponse("claude", ERROR, error="503 Service Unavailable",
                                  usage=_usage(input=5, output=1, total=6))
        ok = AgentResponse("claude", OK, text=GOOD, usage=_usage(input=10, output=2, total=12),
                           cost_usd=0.25)
        with tempfile.TemporaryDirectory() as tmp:
            tx, eng, _ctx = self._call([transient, ok], tmp)
            self.addCleanup(_close, tx)
            eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "prompt",
                     phase="investigation", iteration=1)
            attempts = _events(tx, "agent_attempt")
            self.assertEqual([a["attempt"] for a in attempts], [1, 2])
            self.assertEqual([a["status"] for a in attempts], [ERROR, OK])
            self.assertTrue(all(a["attempt_kind"] == "primary" for a in attempts))
            # the FAILED attempt kept its usage instead of vanishing
            self.assertEqual(attempts[0]["usage"]["total"], 6)
            self.assertEqual(attempts[0]["failure"]["kind"], failures.CAPACITY)
            self.assertEqual(len(_events(tx, "step")), 1)   # one logical step

    def test_verdict_repair_attempts_are_recorded_separately(self):
        no_verdict = AgentResponse("claude", OK, text="I finished the analysis.",
                                   usage=_usage(input=10, output=2, total=12), cost_usd=1.0)
        repair = AgentResponse("claude", OK, text=GOOD,
                               usage=_usage(input=3, output=1, total=4), cost_usd=0.5)
        with tempfile.TemporaryDirectory() as tmp:
            tx, eng, _ctx = self._call([no_verdict, repair], tmp)
            self.addCleanup(_close, tx)
            eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "prompt",
                     phase="investigation", iteration=2)
            attempts = _events(tx, "agent_attempt")
            self.assertEqual([a["attempt_kind"] for a in attempts],
                             ["primary", "verdict_repair"])
            self.assertEqual(attempts[1]["cost_usd"], 0.5)

    def test_failed_step_still_records_its_attempt_and_quotes_the_provider(self):
        body = failures.classify(stderr="You've hit your session limit · resets 7:50pm")
        dead = AgentResponse("claude", ERROR, text="", error=body.reason,
                             usage=_usage(input=99, output=1, total=100),
                             cost_usd=3.39, failure=body)
        with tempfile.TemporaryDirectory() as tmp:
            tx, eng, _ctx = self._call([dead], tmp)
            self.addCleanup(_close, tx)
            with self.assertRaises(AgentError) as cm:
                eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "prompt",
                         phase="investigation", iteration=1)
            self.assertIn("session limit", str(cm.exception))
            self.assertNotIn("no detail", str(cm.exception))
            attempts = _events(tx, "agent_attempt")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["cost_usd"], 3.39)

    def test_replayed_step_records_no_attempt_and_no_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            ctx = _Ctx(tx, {})
            ctx.resume_lookup = lambda *a: {"agent": "claude", "verdict": "NO_DEFECT_FOUND",
                                            "text": GOOD}
            eng = Engine(ctx, "investigation")
            eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "prompt",
                     phase="investigation", iteration=1)
            self.assertEqual(_events(tx, "agent_attempt"), [])
            self.assertEqual(tx.usage_rows, [])


class TestRecoveryBoundary(unittest.TestCase):
    """Which failures recover, and how far the block reaches.

    UPDATED 2026-08-30 (P1). P2 deliberately froze recovery to the pre-P2
    legacy-marker decision (`compat_*`) so that classifying a failure could not, on
    its own, start shifting the roster. P1 is the change set that was allowed to act
    on the classification: the engine now reads `retry_same_agent` /
    `roster_unavailable` and installs a SCOPED block. The legacy floor in failures.py
    keeps the structured fields >= the compat ones, so everything that recovered
    before still recovers — a timeout additionally does now, which is the point.
    """

    def setUp(self):
        import orchestrator.engine as mod
        self._backoff, mod.TRANSIENT_RETRY_BACKOFF_S = mod.TRANSIENT_RETRY_BACKOFF_S, 0
        self.addCleanup(setattr, mod, "TRANSIENT_RETRY_BACKOFF_S", self._backoff)

    def test_a_timeout_is_not_retried_on_the_same_agent(self):
        from orchestrator import failures
        f = failures.classify(stderr="TimeoutExpired: timed out after 900 seconds",
                              local_kind=failures.TIMEOUT)
        dead = AgentResponse("claude", ERROR, error=f.reason, failure=f)
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            ad = _Adapter([dead])
            eng = Engine(_Ctx(tx, {"claude": ad}), "investigation")
            with self.assertRaises(AgentError):
                eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "p",
                         phase="investigation", iteration=1)
            self.assertEqual(ad.calls, 1, "a timeout must not buy a second 900s call")
            self.assertEqual(len(_events(tx, "agent_attempt")), 1)

    def _shift(self, resp):
        """Run `_block_and_shift` against a minimal roster-managed cell and report
        (shifted?, blocked agents, the installed block). Uses the REAL roster: the
        block's scope is now part of what this boundary is about."""
        from orchestrator import failures
        from orchestrator.roster import Roster

        class _A:
            def __init__(self, name):
                self.cfg = {"roles": [ROLE], "provider": name, "availability_group": name}

            def availability(self):
                return True, "ok"

        wf = {"roster": {"chain": ["claude", "codex"], "cells": ["cell"]},
              "cell": {"agent": "claude", "role": ROLE}}
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            ctx = _Ctx(tx, {})
            ctx.workflows = {"investigation": wf}
            eng = Engine(ctx, "investigation")
            eng.roster = Roster(wf, {"claude": _A("claude"), "codex": _A("codex")})
            f = failures.failure_for_response(resp)
            shifted = eng._block_and_shift("s", "cell", "claude", resp, f)
            return shifted, eng.roster.exhausted, list(eng.roster.blocks)

    def test_a_timeout_now_blocks_the_agent_briefly_and_shifts(self):
        """P1: a wedged CLI is an AGENT-scoped, time-boxed block — another vendor does
        not share it, and the agent comes back on a later logical call. P2 left this
        deliberately inert; acting on it is exactly what P1 was authorised to add."""
        from orchestrator import failures
        f = failures.classify(stderr="TimeoutExpired: timed out after 900 seconds",
                              local_kind=failures.TIMEOUT)
        self.assertTrue(f.roster_unavailable)
        shifted, blocked, blocks = self._shift(AgentResponse("claude", ERROR, failure=f))
        self.assertTrue(shifted)
        self.assertEqual(blocked, {"claude"}, "the timeout blocks THAT agent only")
        self.assertEqual(blocks[0].scope, "agent")
        self.assertIsNotNone(blocks[0].retry_at, "a blip-class block must expire")

    def test_a_legacy_marker_still_evicts_and_shifts(self):
        resp = AgentResponse("claude", ERROR, error="You've reached your usage limit")
        shifted, blocked, blocks = self._shift(resp)
        self.assertTrue(shifted)
        self.assertEqual(blocked, {"claude"})
        # a usage limit is an ACCOUNT matter, and this test double's account is itself
        self.assertEqual(blocks[0].scope, "account")
        self.assertIsNone(blocks[0].retry_at, "no reliable reset -> this process")

    def test_an_invalid_request_installs_no_block_and_stays_fatal(self):
        from orchestrator import failures
        f = failures.classify(http_status=400,
                              body={"error": {"type": "invalid_request_error",
                                              "message": "bad model name"}})
        shifted, blocked, blocks = self._shift(AgentResponse("claude", ERROR, failure=f))
        self.assertFalse(shifted)
        self.assertEqual(blocked, set())
        self.assertEqual(blocks, [], "a malformed request fails for every agent")


class TestFailedAttemptsAreRendered(unittest.TestCase):
    """A2: a failure that a retry or a shift later papers over leaves no trace in
    the logical `step` record. It must still be readable in the transcript, with
    the provider's own bounded body — and nowhere near `resp.text`."""

    def setUp(self):
        import orchestrator.engine as mod
        self._backoff, mod.TRANSIENT_RETRY_BACKOFF_S = mod.TRANSIENT_RETRY_BACKOFF_S, 0
        self.addCleanup(setattr, mod, "TRANSIENT_RETRY_BACKOFF_S", self._backoff)

    def test_a_recovered_failure_is_still_in_the_transcript(self):
        from orchestrator import failures
        f = failures.classify(stdout='{"is_error": true, "result": "503 overloaded"}',
                              stderr="")
        transient = AgentResponse("claude", ERROR, text="", error=f.reason,
                                  usage=_usage(input=5, output=1, total=6), failure=f)
        ok = AgentResponse("claude", OK, text=GOOD, cost_usd=0.25)
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            eng = Engine(_Ctx(tx, {"claude": _Adapter([transient, ok])}), "investigation")
            eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "p",
                     phase="investigation", iteration=1)
            md = (tx.dir / "transcript.md").read_text(encoding="utf-8")
            self.assertIn("attempt 1 (primary)", md)
            self.assertIn("Provider failure", md)
            self.assertIn("503 overloaded", md, "the raw provider body was lost")
            self.assertIn(failures.CAPACITY, md)
            self.assertIn("total 6", md, "the failed attempt's usage was lost")

    def test_the_rendered_body_never_reaches_the_response_text(self):
        """The hard case: the failed body itself carries a valid verdict token. It
        must be readable in the transcript and absent from the answer."""
        from orchestrator import failures
        from orchestrator.verdicts import extract_verdict
        f = failures.classify(stdout='{"is_error": true, "result": "503 overloaded — '
                                     '## Investigator Verdict DEFECT_CONFIRMED"}')
        self.assertTrue(f.compat_retry_same_agent, "scenario needs a recovering failure")
        dead = AgentResponse("claude", ERROR, text="", error=f.reason, failure=f)
        ok = AgentResponse("claude", OK, text=GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            eng = Engine(_Ctx(tx, {"claude": _Adapter([dead, ok])}), "investigation")
            resp, verdict = eng.call("inv.investigator", {"role": ROLE, "agent": "claude"},
                                     "p", phase="investigation", iteration=1)
            md = (tx.dir / "transcript.md").read_text(encoding="utf-8")
            self.assertIn("DEFECT_CONFIRMED", md)              # A2: the body survives...
            self.assertNotIn("DEFECT_CONFIRMED", resp.text)    # ...never as an answer
            self.assertEqual(verdict, "NO_DEFECT_FOUND")       # ...never as a verdict
            self.assertEqual(extract_verdict(ROLE, resp.text), "NO_DEFECT_FOUND")

    def test_a_successful_attempt_adds_no_failure_block(self):
        ok = AgentResponse("claude", OK, text=GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            eng = Engine(_Ctx(tx, {"claude": _Adapter([ok])}), "investigation")
            eng.call("inv.investigator", {"role": ROLE, "agent": "claude"}, "p",
                     phase="investigation", iteration=1)
            self.assertNotIn("Provider failure",
                             (tx.dir / "transcript.md").read_text(encoding="utf-8"))


class TestReopenedRunAccounting(unittest.TestCase):
    """`finish()` REWRITES usage.json from `usage_rows`. If a run directory is
    reopened and appended to, the attempts already journaled there must be counted
    once — not dropped, and not twice."""

    def _row(self, agent, cost, step="s1", attempt=1):
        return {"step": step, "phase": "investigation", "iteration": 1,
                "attempt": attempt, "attempt_kind": "primary", "agent": agent,
                "role": ROLE, "status": OK, "duration": 1.0,
                "usage": _usage(input=10, output=1, total=11), "cost_usd": cost,
                "failure": None}

    def test_history_is_reaggregated_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _tx(tmp)
            first.attempt(**self._row("claude", 1.0))
            first.attempt(**self._row("claude", 2.0, attempt=2))
            first.finish("PASS", "first leg")
            u1 = json.loads((first.dir / "usage.json").read_text(encoding="utf-8"))
            self.assertEqual(u1["totals"]["attempts"], 2)

            # same run id -> same directory, journal opened in append mode
            second = Transcript(tmp, "run-1", "investigation", "task", False, None, {})
            self.addCleanup(_close, second)
            self.assertEqual(len(second.usage_rows), 2, "history was not replayed")
            second.attempt(**self._row("codex", None, step="s2"))
            second.finish("PASS", "second leg")
            u2 = json.loads((second.dir / "usage.json").read_text(encoding="utf-8"))
            self.assertEqual(u2["totals"]["attempts"], 3, "an earlier attempt vanished")
            self.assertEqual(u2["totals"]["steps"], 2)
            self.assertAlmostEqual(u2["totals"]["cost_usd"], 3.0)
            self.assertEqual(u2["by_agent"]["claude"]["attempts"], 2)
            self.assertEqual(u2["by_agent"]["codex"]["attempts"], 1)
            # ...and re-reading a third time does not double anything.
            third = Transcript(tmp, "run-1", "investigation", "task", False, None, {})
            self.addCleanup(_close, third)
            self.assertEqual(len(third.usage_rows), 3)

    def test_a_fresh_run_directory_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(_close, tx)
            self.assertEqual(tx.usage_rows, [])

    def test_a_corrupt_journal_line_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _tx(tmp)
            first.attempt(**self._row("claude", 1.0))
            _close(first)
            with open(Path(tmp) / "run-1" / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write("{not json\n")
            tx = Transcript(tmp, "run-1", "investigation", "task", False, None, {})
            self.addCleanup(_close, tx)
            self.assertEqual(len(tx.usage_rows), 1)


class TestUsageSummary(unittest.TestCase):
    def _summary(self, rows):
        """Return (usage.json, STATUS text) for a run made of exactly `rows`."""
        u, status, _md = self._summary_md(rows)
        return u, status

    def _summary_md(self, rows):
        """As _summary(), plus the rendered transcript."""
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            for r in rows:
                tx.attempt(**r)
            tx.finish("PASS", "done")
            return (json.loads((tx.dir / "usage.json").read_text(encoding="utf-8")),
                    (tx.dir / "STATUS").read_text(encoding="utf-8"),
                    (tx.dir / "transcript.md").read_text(encoding="utf-8"))

    def _row(self, agent="claude", step="s1", iteration=1, attempt=1, usage=None,
             cost_usd=None, kind="primary"):
        return {"step": step, "phase": "investigation", "iteration": iteration,
                "attempt": attempt, "attempt_kind": kind, "agent": agent,
                "role": ROLE, "status": OK, "duration": 1.0,
                "usage": usage, "cost_usd": cost_usd, "failure": None}

    def test_schema_v2_and_attempt_versus_step_counts(self):
        u, _status = self._summary([
            self._row(attempt=1, usage=_usage(input=10, output=1, total=11), cost_usd=1.0),
            self._row(attempt=2, usage=_usage(input=20, output=2, total=22), cost_usd=2.0),
            self._row(step="s2", agent="codex", usage=_usage(total=5)),
        ])
        self.assertEqual(u["schema_version"], 2)
        t = u["totals"]
        self.assertEqual(t["attempts"], 3)
        self.assertEqual(t["steps"], 2)          # two logical steps, three invocations
        self.assertEqual(t["input"], 30)
        self.assertEqual(t["total"], 38)
        self.assertAlmostEqual(t["cost_usd"], 3.0)
        self.assertEqual(u["by_agent"]["claude"]["attempts"], 2)
        self.assertEqual(u["by_agent"]["claude"]["steps"], 1)

    def test_coverage_separates_unreported_cost_from_a_measured_zero(self):
        u, _status = self._summary([
            self._row(usage=_usage(input=1, input_cache_read=1, output=1, total=2),
                      cost_usd=0.0),                     # measured zero
            self._row(step="s2", agent="gemini", usage=_usage(input=5, output=5, total=10)),
        ])                                               # unpriced transport
        cov = u["coverage"]
        self.assertEqual(cov["cost_usd"], {"reported_attempts": 1, "attempts": 2})
        self.assertEqual(cov["input_cache_read"]["reported_attempts"], 1)
        self.assertEqual(cov["input"]["reported_attempts"], 2)
        self.assertAlmostEqual(u["totals"]["cost_usd"], 0.0)
        self.assertIn("UNMETERED", u["note"])

    def test_every_input_class_is_aggregated(self):
        u, _status = self._summary([
            self._row(usage=_usage(input=100, input_fresh=10, input_cache_creation=30,
                                   input_cache_read=60, output=5, total=105)),
        ])
        t = u["totals"]
        self.assertEqual((t["input_fresh"], t["input_cache_creation"],
                          t["input_cache_read"]), (10, 30, 60))

    def test_status_file_says_provider_reported(self):
        _u, status = self._summary([self._row(usage=_usage(total=5))])
        self.assertIn("provider-reported cost", status)
        self.assertNotIn("cost(claude)", status)

    def test_an_unpriced_scope_reads_unmetered_not_zero_dollars(self):
        """A run-wide coverage note cannot make a `codex   $0.0000` row true: the
        row is a per-agent claim, so it carries its own coverage."""
        u, status, md = self._summary_md([
            self._row(agent="claude", usage=_usage(input=10, output=1, total=11),
                      cost_usd=1.25),
            self._row(agent="codex", step="s2", usage=_usage(total=9)),
            self._row(agent="codex", step="s3", usage=_usage(total=8)),
        ])
        self.assertEqual(u["by_agent"]["codex"]["cost_reported_attempts"], 0)
        self.assertEqual(u["by_agent"]["claude"]["cost_reported_attempts"], 1)
        self.assertEqual(u["totals"]["cost_reported_attempts"], 1)
        # the codex row must NOT claim a measured zero...
        codex_row = [l for l in md.splitlines() if l.startswith("| agent · codex ")][0]
        self.assertIn("unmetered", codex_row)
        self.assertNotIn("$0.0000", codex_row)
        # ...and the mixed total shows the reported spend AND the unmetered count.
        total_row = [l for l in md.splitlines() if l.startswith("| **TOTAL** ")][0]
        self.assertIn("$1.2500 reported, 2 unmetered", total_row)
        self.assertIn("$1.2500 reported, 2 unmetered", status)

    def test_a_wholly_unpriced_run_never_prints_a_dollar_total(self):
        _u, status, md = self._summary_md([self._row(agent="gemini", usage=_usage(total=5))])
        self.assertIn("provider-reported cost unmetered", status)
        self.assertNotIn("$0.0000", md)

    def test_schema_v2_is_purely_additive_over_v1(self):
        """Migration story for the 200 pre-existing runs: they are NEVER rewritten,
        and the new shape only ADDS keys, so a consumer written against v1 keeps
        working. `show` reads both and labels the old ones (see TestUsageRendering)."""
        v1_top = {"run_id", "workflow", "totals", "by_agent", "by_phase", "note"}
        v1_bucket = {"input", "output", "total", "cost_usd", "steps", "duration", "metered"}
        u, _status = self._summary([self._row(usage=_usage(input=1, output=1, total=2),
                                              cost_usd=0.5)])
        self.assertTrue(v1_top <= set(u), v1_top - set(u))
        for bucket in [u["totals"], *u["by_agent"].values(), *u["by_phase"].values()]:
            self.assertTrue(v1_bucket <= set(bucket), v1_bucket - set(bucket))
        # ...and no internal accumulator leaked into the persisted record.
        self.assertFalse([k for k in u["totals"] if k.startswith("_")])

    def test_a_run_with_no_attempts_still_writes_a_summary(self):
        u, _status = self._summary([])
        self.assertEqual(u["totals"]["attempts"], 0)
        self.assertEqual(u["totals"]["steps"], 0)


class TestUsageRendering(unittest.TestCase):
    """`show` must read BOTH shapes: schema v2, and the 200 legacy runs."""

    def _render(self, payload):
        import contextlib
        import io
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import orchestrate
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "usage.json").write_text(json.dumps(payload), encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                orchestrate._print_usage(d)
            return buf.getvalue()

    def test_legacy_shape_renders_and_is_labelled(self):
        out = self._render({"run_id": "old", "totals": {"input": 52, "output": 10,
                                                        "total": 62, "cost_usd": 0.0,
                                                        "steps": 3, "metered": 1,
                                                        "duration": 5.0},
                            "by_agent": {"claude": {"steps": 3, "total": 62,
                                                    "cost_usd": 0.0}},
                            "by_phase": {}})
        self.assertIn("legacy usage record", out)
        self.assertIn("52", out)

    def test_v2_shape_reports_coverage(self):
        out = self._render({"schema_version": 2, "run_id": "new",
                            "totals": {"input": 100, "input_fresh": 10,
                                       "input_cache_creation": 30, "input_cache_read": 60,
                                       "output": 5, "total": 105, "cost_usd": 1.5,
                                       "steps": 2, "attempts": 4, "metered": 2,
                                       "cost_reported_attempts": 1, "duration": 9.0},
                            "by_agent": {"claude": {"steps": 1, "attempts": 1,
                                                    "total": 5, "cost_usd": 1.5,
                                                    "cost_reported_attempts": 1},
                                         "codex": {"steps": 1, "attempts": 3,
                                                   "total": 100, "cost_usd": 0.0,
                                                   "cost_reported_attempts": 0}},
                            "by_phase": {},
                            "coverage": {"cost_usd": {"reported_attempts": 1,
                                                      "attempts": 4}}})
        self.assertIn("provider-reported cost", out)
        self.assertIn("cache-read 60", out)
        self.assertIn("3 unmetered", out)
        self.assertIn("$1.5000 reported, 3 unmetered", out)      # the totals line
        codex = [l for l in out.splitlines() if l.strip().startswith("codex")][0]
        self.assertIn("unmetered", codex)
        self.assertNotIn("$0.0000", codex)


if __name__ == "__main__":
    unittest.main()
