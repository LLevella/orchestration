"""P1: running out of agents PAUSES the run; it never discards it.

The defect this pins. A roster cell with no live agent left raised an ordinary
`AgentError`, `Engine.run` caught it, and the run ended HUMAN_REQUIRED — terminal,
with every completed iteration thrown away. That killed 16 corpus runs; the one on
2026-08-29 had 13 finished steps and $15.19 of provider-reported cost in it. None of
that work was wrong: one agent was unavailable.

So the exhausted cell now raises `AvailabilityPause`, the run ends AWAITING_HUMAN,
and the journal it leaves behind is the one `resume` reads. The three properties
that make that safe are pinned here:

  * the pause happens AFTER `_finalize` — the guard and branch checks still run and
    the failed step is still journaled — and a `GuardViolation` is NEVER translated
    into a pause;
  * an unassigned OPENING cell pauses before any provider is called at all;
  * MR-review's local `except AgentError` handlers still catch it, because
    `AvailabilityPause` is an `AgentError`: a lost panel seat and a lost moderator
    stay local decisions and never become a top-level availability pause.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import orchestrate
from orchestrator import failures, mr as mrlib
from orchestrator.adapters import ERROR, OK, AgentResponse
from orchestrator.config import load_config
from orchestrator.engine import (AWAITING_HUMAN, HUMAN_REQUIRED, AgentError,
                                 AvailabilityPause, Engine)
from orchestrator.guard import GuardViolation
from orchestrator.transcript import Transcript

DEV = "Root Cause Investigator"        # only the writer holds it, in this fixture
REF = "Hypothesis Refuter"
GOOD = "## Investigator Verdict\nNO_DEFECT_FOUND\n"
REF_OK = "## Refuter Verdict\nREFUTED\n"


class _Guard:
    def __init__(self, violate=False):
        self.violate = violate

    def branches(self):
        return None

    def snapshot(self):
        return None

    def assert_branch_unchanged(self, *a):
        return None

    def assert_unchanged(self, *a):
        if self.violate:
            raise GuardViolation("read-only agent modified the working tree")


class _Agent:
    """A scripted agent that is also a roster candidate."""

    def __init__(self, roles, responses=(), live=True, can_edit=False, group=None):
        self.cfg = {"roles": list(roles), "can_edit": can_edit,
                    "availability_group": group or "own", "provider": group or "own"}
        self._responses = list(responses)
        self.live = live
        self.calls = 0

    def availability(self):
        return (self.live, "ok" if self.live else "'cli' not found in PATH")

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        self.calls += 1
        return self._responses.pop(0) if self._responses else AgentResponse(
            "x", OK, text=GOOD, duration=0.0)


class _Ctx:
    def __init__(self, tx, adapters, workflows, guard=None, resume_cache=None,
                 resume_degradations=()):
        self.adapters = adapters
        self.tx = tx
        self.guard = guard or _Guard()
        self.resume_cache = dict(resume_cache or {})
        self.resume_degradations = list(resume_degradations)
        self.workflows = workflows
        self.live_calls = 0
        self.last_call_replayed = False
        self.last_call_final_replay = False
        self.human_answer = None

    def resume_lookup(self, step_id, phase, iteration):
        """The real RunContext contract: consumed once, then the step runs live."""
        if not self.resume_cache:
            return None
        return self.resume_cache.pop((step_id, phase, iteration), None)

    def load_role(self, role, wrapper=None):
        return "system prompt"

    def repo_scope_line(self):
        return "Scope — WRITABLE: (none); READ-ONLY: example/service."


def _wf(dev_agent="claude"):
    """A two-cell roster whose writing cell only `claude` may hold — the shipped
    development/investigation shape, in miniature."""
    return {"investigation": {
        "roster": {"chain": ["claude", "codex"], "cells": ["investigator", "refuter"]},
        "investigator": {"agent": dev_agent, "role": DEV, "requires_edit": True},
        "refuter": {"agent": "codex", "role": REF},
    }}


def _tx(tmp, run_id="run-1"):
    return Transcript(tmp, run_id, "investigation", "task", False, None, {})


def _events(tx, kind):
    lines = (tx.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(l) for l in lines) if e.get("kind") == kind]


def _quota():
    return failures.classify(
        http_status=429,
        body={"is_error": True,
              "result": "You've hit your session limit · resets 7:50pm (Europe/Istanbul)"})


class TestPauseInsteadOfAbort(unittest.TestCase):
    def setUp(self):
        import orchestrator.engine as mod
        self._b, mod.TRANSIENT_RETRY_BACKOFF_S = mod.TRANSIENT_RETRY_BACKOFF_S, 0
        self.addCleanup(setattr, mod, "TRANSIENT_RETRY_BACKOFF_S", self._b)

    def test_an_unassigned_opening_cell_pauses_before_any_provider_call(self):
        wf = _wf()
        agents = {"claude": _Agent([DEV], live=False, can_edit=True),
                  "codex": _Agent([REF])}
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(tx._events.close)
            eng = Engine(_Ctx(tx, agents, wf), "investigation")
            eng._setup_roster()
            with self.assertRaises(AvailabilityPause) as e:
                eng.call("inv.investigator", wf["investigation"]["investigator"], "p",
                         phase="investigation", iteration=1)
            self.assertEqual(e.exception.cell, "investigator")
            self.assertEqual(e.exception.candidates, ["claude"])
            self.assertEqual(agents["claude"].calls + agents["codex"].calls, 0,
                             "an unassigned opening cell must cost zero provider calls")

    def test_an_exhausted_cell_pauses_after_the_failed_step_is_journaled(self):
        wf = _wf()
        f = _quota()
        dead = AgentResponse("claude", ERROR, error=f.reason, failure=f)
        agents = {"claude": _Agent([DEV], [dead], can_edit=True, group="anthropic"),
                  "codex": _Agent([REF])}
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(tx._events.close)
            eng = Engine(_Ctx(tx, agents, wf), "investigation")
            eng._setup_roster()
            with self.assertRaises(AvailabilityPause) as e:
                eng.call("inv.investigator", wf["investigation"]["investigator"], "p",
                         phase="investigation", iteration=1)
            self.assertEqual(e.exception.cell, "investigator")
            self.assertEqual(e.exception.failure.scope, failures.SCOPE_ACCOUNT)
            # _finalize still ran: the failed logical step and its attempt are on record
            self.assertEqual([s["status"] for s in _events(tx, "step")], [ERROR])
            self.assertEqual(len(_events(tx, "agent_attempt")), 1)
            self.assertEqual(agents["claude"].calls, 1,
                             "a session limit is not retried on the same agent")

    def _paused_run(self, tmp, guard=None, second=None):
        """A run whose first step succeeds and whose second cell has no live agent."""
        wf = _wf()
        f = _quota()
        agents = {"claude": _Agent([DEV, REF], [AgentResponse("claude", OK, text=GOOD)],
                                   can_edit=True, group="anthropic"),
                  "codex": _Agent([REF], [second or AgentResponse("codex", ERROR,
                                                                  error=f.reason, failure=f)])}
        tx = _tx(tmp)
        self.addCleanup(tx._events.close)
        eng = Engine(_Ctx(tx, agents, wf, guard=guard), "investigation")

        def loop(task):
            blocks = eng.ctx.workflows["investigation"]
            eng.call("inv.investigator", blocks["investigator"], "p",
                     phase="investigation", iteration=1)
            eng.call("inv.refuter", blocks["refuter"], "p", phase="investigation", iteration=1)
            return eng._finish("NO_DEFECT_FOUND", "should not be reached")

        eng._run_investigation = loop
        return eng, tx, agents

    def test_the_run_ends_awaiting_human_with_a_resumable_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx, _agents = self._paused_run(tmp)
            status = eng.run("task")
            self.assertEqual(status, AWAITING_HUMAN)

            meta = json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))
            pause = meta["pause"]
            self.assertEqual(pause["kind"], "availability")
            self.assertEqual(pause["cell"], "refuter")
            self.assertIn("session limit", pause["reason"])
            self.assertEqual(pause["failure"]["scope"], failures.SCOPE_ACCOUNT)
            self.assertIsNone(pause["retry_at"], "no date in the reset prose, no deadline")
            self.assertIn("resume run-1", pause["resume_command"])
            self.assertTrue(any(b["scope"] == "account" for b in pause["blocked_selectors"]))

            status_file = (tx.dir / "STATUS").read_text(encoding="utf-8")
            self.assertTrue(status_file.startswith(AWAITING_HUMAN))
            self.assertIn("resume", status_file)

            # THE point of the change: the finished step survives and resume replays it.
            cache = orchestrate._load_resume_cache(tx.dir)
            self.assertEqual(list(cache), [("inv.investigator", "investigation", 1)])
            self.assertIn("NO_DEFECT_FOUND", cache[("inv.investigator", "investigation", 1)]["text"])

    def test_a_guard_violation_is_never_turned_into_a_pause(self):
        """A read-only agent that wrote to the tree is a safety failure, not an
        availability one: it stays terminal HUMAN_REQUIRED."""
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx, _ = self._paused_run(tmp, guard=_Guard(violate=True))
            self.assertEqual(eng.run("task"), HUMAN_REQUIRED)
            meta = json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))
            self.assertNotIn("pause", meta)

    def test_an_unrecoverable_failure_still_ends_human_required(self):
        """A malformed request fails for every agent: no block, no shift, no pause."""
        bad = failures.classify(http_status=400,
                                body={"error": {"type": "invalid_request_error",
                                                "message": "bad model name"}})
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx, _ = self._paused_run(
                tmp, second=AgentResponse("codex", ERROR, error=bad.reason, failure=bad))
            self.assertEqual(eng.run("task"), HUMAN_REQUIRED)
            meta = json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))
            self.assertNotIn("pause", meta)

    def test_the_pause_is_an_agent_error_so_local_handlers_still_catch_it(self):
        self.assertTrue(issubclass(AvailabilityPause, AgentError))


class TestCrossFamilyDegradationIsRecorded(unittest.TestCase):
    def test_the_run_says_which_cell_crossed_families_and_what_it_cost(self):
        agents_cfg, workflows = load_config()
        from orchestrator.adapters import build_adapters
        adapters = build_adapters(agents_cfg, mock=True, scenario=None)
        with tempfile.TemporaryDirectory() as tmp:
            tx = Transcript(tmp, "run-arch", "architecture", "task", False, None, {})
            self.addCleanup(tx._events.close)
            eng = Engine(_Ctx(tx, adapters, workflows), "architecture")
            eng._setup_roster()
            eng.roster.block_for_failure("claude_reviewer", _quota())
            eng._apply_roster_assignment()

            recs = _events(tx, "roster_degradation")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["degradation"]["selected"], "deepseek")
            meta = json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["degradations"][0]["cell"], "developer_verifier")
            self.assertIn("cross_family_pin_fallback", (tx.dir / "run.json").read_text("utf-8"))
            eng._finish("PASS", "done")
            status = (tx.dir / "STATUS").read_text(encoding="utf-8")
            self.assertIn("DEGRADED", status)
            self.assertIn("claude_reviewer", status)
            self.assertIn("deepseek", status)


class TestResumeKeepsRosterContinuity(unittest.TestCase):
    """A6 across a resume: a resumed run must not re-derive the assignment from scratch.

    The run being resumed ran a cell with a FALLBACK, because the preferred agent was
    blocked at the time. The resume starts with a fresh block registry (by design — a
    cap may have reset), so the preferred agent is eligible again. Without replaying the
    journal's cell bindings the role changes hands mid-run: iteration 1 was written by
    one agent, iteration 2 by another, and the two halves of the same logical run stop
    being comparable.
    """

    def _wf(self):
        return {"investigation": {
            "roster": {"chain": ["a", "b"], "cells": ["refuter"]},
            "refuter": {"agent": "a", "role": REF},
        }}

    def _cached(self, agent, iteration):
        return {("inv.refuter", "investigation", iteration): {
            "agent": agent, "role": REF, "verdict": "REFUTED", "text": REF_OK}}

    def test_a_replayed_step_binds_its_cell_to_the_agent_that_ran_it(self):
        wf = self._wf()
        agents = {"a": _Agent([REF], [AgentResponse("a", OK, text=REF_OK)]),
                  "b": _Agent([REF], [AgentResponse("b", OK, text=REF_OK)])}
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(tx._events.close)
            eng = Engine(_Ctx(tx, agents, wf, resume_cache=self._cached("b", 1)),
                         "investigation")
            eng._setup_roster()
            self.assertEqual(eng.roster.assign()["refuter"], "a",
                             "chain order alone would hand the cell back to 'a'")

            eng.call("inv.refuter", wf["investigation"]["refuter"], "p",
                     phase="investigation", iteration=1)          # replayed
            self.assertEqual(eng.roster.sticky, {"refuter": "b"})
            self.assertEqual(agents["a"].calls + agents["b"].calls, 0, "a replay calls nobody")

            eng.call("inv.refuter", wf["investigation"]["refuter"], "p",
                     phase="investigation", iteration=2)          # live
            self.assertEqual((agents["b"].calls, agents["a"].calls), (1, 0),
                             "the role must not change hands halfway through a resume")

    def test_a_stale_binding_is_ignored_when_that_agent_is_no_longer_eligible(self):
        """Stickiness is a preference, never a trap: if the journaled agent is gone,
        the cell is re-filled instead of pausing on a name from an older run."""
        wf = self._wf()
        agents = {"a": _Agent([REF], [AgentResponse("a", OK, text=REF_OK)]),
                  "b": _Agent([REF], live=False)}
        with tempfile.TemporaryDirectory() as tmp:
            tx = _tx(tmp)
            self.addCleanup(tx._events.close)
            eng = Engine(_Ctx(tx, agents, wf, resume_cache=self._cached("b", 1)),
                         "investigation")
            eng._setup_roster()
            eng.call("inv.refuter", wf["investigation"]["refuter"], "p",
                     phase="investigation", iteration=1)
            eng.call("inv.refuter", wf["investigation"]["refuter"], "p",
                     phase="investigation", iteration=2)
            self.assertEqual(agents["a"].calls, 1)


class TestResumeKeepsTheDegradationLedger(unittest.TestCase):
    """A resume replays the work a cross-family fallback produced, so it inherits what
    that fallback cost. The ledger is re-journaled into the new run and never shrinks."""

    def _inherited(self):
        return {"kind": "cross_family_pin_fallback", "cell": "developer_verifier",
                "configured_primary": "claude_reviewer", "selected": "deepseek",
                "trigger": {"kind": "quota", "scope": "account",
                            "selector": "anthropic-default-account",
                            "source": "provider_decoder", "reason": "session limit"},
                "lost_assurance": "cannot independently inspect the workspace"}

    def _engine(self, tmp, degradations):
        from orchestrator.adapters import build_adapters
        agents_cfg, workflows = load_config()
        adapters = build_adapters(agents_cfg, mock=True, scenario=None)
        tx = Transcript(tmp, "run-resume", "architecture", "task", False, None, {})
        self.addCleanup(tx._events.close)
        eng = Engine(_Ctx(tx, adapters, workflows, resume_degradations=degradations),
                     "architecture")
        eng._setup_roster()
        return eng, tx

    def _meta(self, tx):
        return json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))

    def test_an_inherited_degradation_is_re_journaled_and_reaches_the_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx = self._engine(tmp, [self._inherited()])
            self.assertEqual(len(_events(tx, "roster_degradation_inherited")), 1)
            self.assertEqual(self._meta(tx)["degradations"][0]["selected"], "deepseek")
            eng._finish("PASS", "done")
            status = (tx.dir / "STATUS").read_text(encoding="utf-8")
            self.assertIn("DEGRADED", status)
            self.assertIn("developer_verifier", status)
            self.assertIn("cannot independently inspect", status)

    def test_re_selecting_the_same_fallback_live_does_not_duplicate_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx = self._engine(tmp, [self._inherited()])
            eng.roster.block_for_failure("claude_reviewer", _quota())
            eng._apply_roster_assignment()
            self.assertEqual(eng.roster.assign()["developer_verifier"], "deepseek")
            self.assertEqual(len(eng.degradations), 1, "the same loss, recorded once")
            self.assertEqual(len(self._meta(tx)["degradations"]), 1)

    def test_a_live_degradation_is_appended_to_the_inherited_one(self):
        """The ledger only ever grows: a NEW cell crossing families in the resumed run
        joins the inherited record instead of replacing it."""
        other = dict(self._inherited(), cell="some_other_cell", selected="gemini")
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx = self._engine(tmp, [other])
            eng.roster.block_for_failure("claude_reviewer", _quota())
            eng._apply_roster_assignment()
            cells = [d["cell"] for d in self._meta(tx)["degradations"]]
            self.assertEqual(cells, ["some_other_cell", "developer_verifier"])

    def test_a_malformed_inherited_record_is_dropped_before_it_reaches_the_engine(self):
        meta = {"degradations": [self._inherited(), {"cell": "x"}, "junk",
                                 {"cell": "y", "selected": "z", "trigger": None}]}
        self.assertEqual([d["cell"] for d in orchestrate._resume_degradations(meta)],
                         ["developer_verifier"])


class TestMrReviewKeepsItsLocalHandling(unittest.TestCase):
    """An MR-review seat that cannot be filled is a LOST OPINION, not a paused run."""

    def _run_panel(self, tmp):
        agents_cfg, workflows = load_config()
        opinion = AgentResponse("x", OK, text="## MR Review Verdict\nAPPROVE\n\n## Summary\n- ok\n")
        adapters = {}
        for name, cfg in agents_cfg["agents"].items():
            if name.startswith("_"):
                continue
            live = name in ("codex", "gemini")
            adapters[name] = _Agent(cfg.get("roles") or [], [opinion, opinion], live=live,
                                    group=cfg.get("availability_group"))
        tx = Transcript(tmp, "run-mr", "mrreview", "task", False, None, {})
        self.addCleanup(tx._events.close)
        ctx = _Ctx(tx, adapters, workflows)
        eng = Engine(ctx, "mrreview")
        eng._setup_roster()
        mr = mrlib.MRContext(subject="s", repo_label="example/service",
                             repo_path="/w/example/service", base="origin/main", head="feat/x",
                             base_sha="a" * 40, head_sha="b" * 40,
                             diff="--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n",
                             stat=" x.py | 2 +-", commits="deadbee add retry")
        return eng, tx, eng._run_mrreview("task", mr)

    def test_lost_seats_and_a_lost_moderator_never_become_a_top_level_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, tx, status = self._run_panel(tmp)
            self.assertEqual(status, HUMAN_REQUIRED, "an unmerged document is not a success")
            lost = _events(tx, "panel_seat_lost")
            self.assertEqual([e["cell"] for e in lost], ["expert_3", "expert_4"])
            meta = json.loads((tx.dir / "run.json").read_text(encoding="utf-8"))
            self.assertNotIn("pause", meta, "a lost seat is a local decision, not a pause")
            self.assertIn("unmerged", meta["reason"])
            self.assertTrue((tx.dir / "artifacts" / "mr-review.md").exists(),
                            "the two collected opinions are still delivered")


class TestResumeOfAnAvailabilityPause(unittest.TestCase):
    """`--human-answer` is a binding RULING for an arbiter that could not decide. An
    availability pause asked no question, so an answer supplied there would be silently
    dropped — the resume is refused instead, and the decision pauses keep working.

    Also pins what `cmd_resume` hands the engine besides the replay cache: the prior
    run's degradation ledger."""

    def _resume(self, meta, human_answer=None):
        import types
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runs = Path(tmp.name)
        (runs / "r1").mkdir()
        (runs / "r1" / "run.json").write_text(json.dumps(meta), encoding="utf-8")

        seen = {}

        class _StubEngine:
            def __init__(self, ctx, workflow):
                seen["human_answer"] = ctx.human_answer
                seen["resume_degradations"] = ctx.resume_degradations
                seen["workflow"] = workflow

            def run(self, *a, **kw):
                return "PASS"

        for name, value in (("_runs_dir", lambda cfg: runs), ("Engine", _StubEngine)):
            orig = getattr(orchestrate, name)
            setattr(orchestrate, name, value)
            self.addCleanup(setattr, orchestrate, name, orig)
        args = types.SimpleNamespace(run_id="r1", human_answer=human_answer,
                                     allow_unguarded=True, arch=None)
        return args, seen

    def test_an_availability_pause_refuses_a_human_answer(self):
        meta = {"workflow": "architecture", "task": "t", "mock": False,
                "pause": {"kind": "availability", "cell": "developer_verifier"}}
        args, _ = self._resume(meta, human_answer="do it my way")
        with self.assertRaises(SystemExit) as e:
            orchestrate.cmd_resume(args)
        msg = str(e.exception)
        self.assertIn("developer_verifier", msg)
        self.assertIn("resume r1", msg)

    def test_a_decision_pause_still_accepts_one(self):
        import contextlib
        import io
        meta = {"workflow": "architecture", "task": "t", "mock": False}
        args, seen = self._resume(meta, human_answer="the arbiter's answer")
        with contextlib.redirect_stdout(io.StringIO()):   # cmd_resume narrates to stdout
            orchestrate.cmd_resume(args)
        self.assertEqual(seen["human_answer"], "the arbiter's answer")

    def test_the_resume_hands_the_engine_the_prior_degradation_ledger(self):
        import contextlib
        import io
        rec = {"kind": "cross_family_pin_fallback", "cell": "developer_verifier",
               "configured_primary": "claude_reviewer", "selected": "deepseek",
               "trigger": {"kind": "quota", "scope": "account",
                           "selector": "anthropic-default-account", "source": "decoder"},
               "lost_assurance": "judged only the inlined task"}
        meta = {"workflow": "architecture", "task": "t", "mock": False,
                "degradations": [rec]}
        args, seen = self._resume(meta)
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrate.cmd_resume(args)
        self.assertEqual(seen["resume_degradations"], [rec])


if __name__ == "__main__":
    unittest.main()
