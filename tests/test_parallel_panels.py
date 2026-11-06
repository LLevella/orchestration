"""Panel steps run at the same time, and nothing but the elapsed time changes.

Workflows opt in through a `parallel` block (review, implreview stage 1, mrreview). The
tests pin the properties that one-at-a-time execution gave for free, because each of
them would fail silently rather than loudly:

  1. The steps really overlap: a barrier only opens if every seat is in flight at once.
  2. Output order is the config's, never the finishing order, so Reviewer numbering and
     what the arbiter reads do not depend on which vendor happened to be fast.
  3. The journal survives concurrent writers: unique step numbers, no lost file.
  4. A resume replays what finished and runs only the rest, on the agents not taken.
  5. A shifting seat never lands on an agent that a peer is still running.
  6. A failing seat does not discard its peers, whose results stay resumable.
  7. A signal takes the running CLIs down with the orchestrator.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

import orchestrate
from orchestrator import adapters as adapters_mod
from orchestrator import engine as engine_mod
from orchestrator import failures
from orchestrator import mr as mrlib
from orchestrator.adapters import ERROR, OK, AgentResponse
from orchestrator.config import ConfigError, _validate_parallel, load_config
from orchestrator.engine import (
    HUMAN_REQUIRED,
    IMPLREVIEW_COMPLETE,
    REVIEW_COMPLETE,
    AgentError,
    AvailabilityPause,
    Engine,
    _BatchCall,
)
from orchestrator.guard import GitGuard, GuardViolation
from orchestrator.transcript import Transcript

ORCH = Path(__file__).resolve().parent.parent
BARRIER_TIMEOUT_S = 20
MR_OK = "## MR Review Verdict\nAPPROVE\n\n## Summary\n- from {name}\n"
MODERATED = "## Review Summary Verdict\nCONSOLIDATED\n\n## Remarks\n- merged\n"
REVIEW_OK = "## Review Verdict\nSOLID\n\n- from {name}\n"
ARBITER_OK = "## Review Arbiter Verdict\nSTRONG\n\n- synthesized\n"
FEASIBLE = {"impl.dev_review": "## Implementation Feasibility Verdict\nFEASIBLE\n",
            "impl.devops_review": "## Operational Feasibility Verdict\nFEASIBLE\n"}


def _quota_failure():
    return failures.classify(stderr="You've hit your usage limit")


class _Agent:
    """A scripted roster candidate that records whether it ever ran twice at once."""

    def __init__(self, name, cfg, script):
        self.name = name
        self.cfg = {"roles": list(cfg.get("roles") or []), "can_edit": False,
                    "availability_group": name, "provider": name}
        self.script = script          # step_id -> callable(agent) -> AgentResponse
        self.calls = []
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()

    def availability(self):
        return True, "ok"

    def run(self, system, prompt, allow_edit=False, step_id=None, timeout=900):
        with self._lock:
            self.calls.append(step_id)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            return self.script(self, step_id, prompt)
        finally:
            with self._lock:
                self.running -= 1


class _Guard:
    def __init__(self, violate_step=None):
        self.violate_step = violate_step

    def branches(self):
        return None

    def snapshot(self):
        return None

    def assert_branch_unchanged(self, *a):
        return None

    def assert_unchanged(self, before, after, who, step):
        if step == self.violate_step:
            raise GuardViolation(f"Read-only agent '{who}' altered the working tree "
                                 f"during step '{step}'.")


class _Ctx:
    def __init__(self, tx, adapters, workflows, guard=None, resume_cache=None):
        self.adapters = adapters
        self.tx = tx
        self.guard = guard or _Guard()
        self.resume_cache = dict(resume_cache or {})
        self.resume_degradations = []
        self.workflows = workflows
        self.live_calls = 0
        self.last_call_replayed = False
        self.last_call_final_replay = False
        self.human_answer = None
        self.project_context = ""

    def resume_lookup(self, step_id, phase, iteration):
        return self.resume_cache.pop((step_id, phase, iteration), None)

    def load_role(self, role, wrapper=None):
        return "system prompt"

    def repo_scope_line(self):
        return "Scope."

    def repo_scope_prompt(self):
        return "Scope."


def _mr():
    return mrlib.MRContext(subject="s", repo_label="example/service",
                           repo_path="/w/example/service", base="origin/main", head="feat/x",
                           base_sha="a" * 40, head_sha="b" * 40,
                           diff="--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n",
                           stat=" x.py | 2 +-", commits="deadbee add retry")


def _events(tx, kind=None):
    rows = [json.loads(l) for l in (tx.dir / "events.jsonl").read_text("utf-8").splitlines()]
    return [e for e in rows if kind is None or e["kind"] == kind]


class _Case(unittest.TestCase):
    workflow = "mrreview"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        backoff, engine_mod.TRANSIENT_RETRY_BACKOFF_S = engine_mod.TRANSIENT_RETRY_BACKOFF_S, 0
        self.addCleanup(setattr, engine_mod, "TRANSIENT_RETRY_BACKOFF_S", backoff)

    def build(self, script, *, max_workers=None, guard=None, resume_cache=None):
        agents_cfg, workflows = load_config()
        workflows = copy.deepcopy(workflows)
        if max_workers is not None:
            workflows[self.workflow]["parallel"]["max_workers"] = max_workers
        adapters = {name: _Agent(name, cfg, script)
                    for name, cfg in agents_cfg["agents"].items() if not name.startswith("_")}
        tx = Transcript(self.tmp, f"run-{time.monotonic_ns()}", self.workflow, "task", False,
                        None, {})
        self.addCleanup(lambda: tx._events.closed or tx._events.close())
        ctx = _Ctx(tx, adapters, workflows, guard=guard, resume_cache=resume_cache)
        return Engine(ctx, self.workflow), tx, adapters


class TestStepsReallyOverlap(_Case):
    def _barrier_script(self, seats, reply):
        barrier = threading.Barrier(seats, timeout=BARRIER_TIMEOUT_S)

        def script(agent, step_id, prompt):
            if step_id in self.panel_steps:
                barrier.wait()  # BrokenBarrierError unless every seat is in flight
                text = reply[step_id] if isinstance(reply, dict) else reply.format(name=step_id)
                return AgentResponse(agent.name, OK, text=text)
            return AgentResponse(agent.name, OK, text=self.later_steps[step_id])
        return script

    def test_mrreview_runs_all_four_experts_at_once(self):
        self.workflow = "mrreview"
        self.panel_steps = {f"mrreview.expert_{i}" for i in range(1, 5)}
        self.later_steps = {"mrreview.moderator": MODERATED}
        eng, tx, _ = self.build(self._barrier_script(4, MR_OK))
        self.assertEqual(eng.run("task", mr=_mr()), REVIEW_COMPLETE)
        self.assertEqual(len(_events(tx, "panel_summary")), 1)
        self.assertEqual(_events(tx, "panel_summary")[0]["opinions"], 4)
        end = _events(tx, "parallel_end")[0]
        self.assertEqual(end["failed"], [])
        self.assertEqual(len(end["steps"]), 4)

    def test_review_runs_its_lenses_at_once(self):
        self.workflow = "review"
        self.panel_steps = {"review.reviewer_correctness", "review.reviewer_completeness",
                            "review.reviewer_risk"}
        self.later_steps = {"review.arbiter": ARBITER_OK}
        eng, tx, _ = self.build(self._barrier_script(3, REVIEW_OK))
        self.assertEqual(eng.run("task", target="the artifact"), REVIEW_COMPLETE)
        self.assertEqual(len(_events(tx, "parallel_start")), 1)

    def test_implreview_runs_stage_one_at_once_and_only_stage_one(self):
        self.workflow = "implreview"
        self.panel_steps = {"impl.dev_review", "impl.devops_review"}
        self.later_steps = {
            "impl.author": "the draft",
            "impl.verifier": "## Implementation Tech Lead Verdict\nREADY_FOR_HUMAN_REVIEW\n",
            "impl.gating": "## Delivery Gate Verdict\nAPPROVED\n"}
        eng, tx, _ = self.build(self._barrier_script(2, FEASIBLE))
        self.assertEqual(eng.run("task", target="the artifact"), IMPLREVIEW_COMPLETE)
        batches = _events(tx, "parallel_start")
        self.assertEqual([b["steps"] for b in batches],
                         [["impl.dev_review", "impl.devops_review"]])
        self.assertTrue((tx.dir / "artifacts" / "implreview-dev-01.md").exists())
        self.assertTrue((tx.dir / "artifacts" / "implreview-devops-01.md").exists())


class TestOrderIsTheConfigs(_Case):
    def test_reviewer_numbering_ignores_who_finished_first(self):
        # Seats finish in reverse: expert_4 first, expert_1 last. Each seat waits for the
        # next one to be JOURNALED, so the order below is deterministic, not a race.
        gates = {f"mrreview.expert_{i}": threading.Event() for i in range(1, 6)}
        gates["mrreview.expert_5"].set()

        def script(agent, step_id, prompt):
            if step_id == "mrreview.moderator":
                self.moderator_prompt = prompt
                return AgentResponse(agent.name, OK, text=MODERATED)
            n = int(step_id[-1])
            self.assertTrue(gates[f"mrreview.expert_{n + 1}"].wait(BARRIER_TIMEOUT_S))
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, _ = self.build(script)
        real_step = tx.step

        def step_then_release(step_id, *a, **kw):
            real_step(step_id, *a, **kw)
            if step_id in gates:
                gates[step_id].set()
        tx.step = step_then_release
        self.assertEqual(eng.run("task", mr=_mr()), REVIEW_COMPLETE)
        finished = [e["step"] for e in _events(tx, "step")][:4]
        self.assertEqual(finished, [f"mrreview.expert_{i}" for i in (4, 3, 2, 1)],
                         "the fixture must really finish in reverse for this to prove anything")
        positions = [self.moderator_prompt.index(f"from mrreview.expert_{i}") for i in (1, 2, 3, 4)]
        self.assertEqual(positions, sorted(positions))
        for i in range(1, 5):
            self.assertIn(f"## Reviewer {i} ", self.moderator_prompt)
        doc = (tx.dir / "artifacts" / "mr-review.md").read_text("utf-8")
        self.assertLess(doc.index("from mrreview.expert_1"), doc.index("from mrreview.expert_4"))


class TestOffSwitch(_Case):
    workflow = "review"

    def test_max_workers_one_runs_in_config_order_with_no_overlap(self):
        running = []

        def script(agent, step_id, prompt):
            if step_id == "review.arbiter":
                return AgentResponse(agent.name, OK, text=ARBITER_OK)
            running.append(step_id)
            return AgentResponse(agent.name, OK, text=REVIEW_OK.format(name=step_id))

        eng, tx, _ = self.build(script, max_workers=1)
        self.assertEqual(eng.run("task", target="artifact"), REVIEW_COMPLETE)
        self.assertEqual(running, ["review.reviewer_correctness", "review.reviewer_completeness",
                                   "review.reviewer_risk"])
        self.assertEqual(_events(tx, "parallel_start"), [])

    def test_one_at_a_time_still_stops_at_the_first_failure(self):
        started = []

        def script(agent, step_id, prompt):
            started.append(step_id)
            return AgentResponse(agent.name, ERROR, error="bad request",
                                 failure=failures.classify(stderr="invalid request body"))

        eng, _tx, _ = self.build(script, max_workers=1)
        self.assertEqual(eng.run("task", target="artifact"), HUMAN_REQUIRED)
        self.assertEqual(set(started), {"review.reviewer_correctness"})


class TestJournalUnderConcurrentWriters(_Case):
    def test_every_step_gets_its_own_number_and_file(self):
        tx = Transcript(self.tmp, "hammer", "mrreview", "task", False, None, {})
        self.addCleanup(lambda: tx._events.closed or tx._events.close())
        resp = AgentResponse("a", OK, text="x" * 2000, duration=0.1)

        def writer(k):
            for j in range(40):
                tx.attempt(step=f"s.{k}.{j}", phase="p", iteration=1, attempt=1,
                           attempt_kind="primary", agent="a", role="r", status=OK, duration=0.1)
                tx.step(f"s.{k}.{j}", "a", "r", "sys", "prompt", resp, "APPROVE", 1, "p")
                tx.update_meta(**{f"k{k}": j})
                tx.artifact(f"a-{k}-{j}.md", "body")

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        steps = _events(tx, "step")
        self.assertEqual(len(steps), 240)
        self.assertEqual(len({e["file"] for e in steps}), 240, "two steps shared a number")
        self.assertEqual(len(list((tx.dir / "iterations").iterdir())), 240)
        self.assertEqual(len(tx.usage_rows), 240)
        json.loads((tx.dir / "run.json").read_text("utf-8"))


class TestResume(_Case):
    def test_finished_seats_replay_and_only_the_rest_run_live(self):
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        replayed = {("mrreview.expert_1", "panel", 1): {
                        "agent": "gemini", "role": "MR Review Expert", "verdict": "APPROVE",
                        "text": MR_OK.format(name="journaled expert_1")},
                    ("mrreview.expert_3", "panel", 1): {
                        "agent": "codex", "role": "MR Review Expert", "verdict": "APPROVE",
                        "text": MR_OK.format(name="journaled expert_3")}}

        def script(agent, step_id, prompt):
            if step_id == "mrreview.moderator":
                return AgentResponse(agent.name, OK, text=MODERATED)
            barrier.wait()  # the two seats left still run together
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, adapters = self.build(script, resume_cache=replayed)
        self.assertEqual(eng.run("task", mr=_mr()), REVIEW_COMPLETE)
        all_calls = [c for a in adapters.values() for c in a.calls]
        self.assertEqual(sorted(all_calls), ["mrreview.expert_2", "mrreview.expert_4",
                                             "mrreview.moderator"])
        # The journaled agents hold their seats: nobody live may repeat them.
        live_seat_agents = [n for n, a in adapters.items() if set(a.calls) & {
            "mrreview.expert_2", "mrreview.expert_4"}]
        self.assertNotIn("gemini", live_seat_agents)
        self.assertNotIn("codex", live_seat_agents)
        self.assertEqual(_events(tx, "parallel_start")[0]["steps"],
                         ["mrreview.expert_2", "mrreview.expert_4"])


class TestShiftWhilePeersRun(_Case):
    def test_two_failing_seats_shift_to_different_free_agents(self):
        # codex and gemini fail together while their peers are mid-flight, on a roster
        # whose positional rule would otherwise hand both the same next agent, or a peer's.
        failing = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        replacements = threading.Barrier(4, timeout=BARRIER_TIMEOUT_S)
        dead = {"codex", "gemini"}

        def script(agent, step_id, prompt):
            if step_id == "mrreview.moderator":
                return AgentResponse(agent.name, OK, text=MODERATED)
            if agent.name in dead:
                failing.wait()
                return AgentResponse(agent.name, ERROR, error="quota",
                                     failure=_quota_failure())
            replacements.wait()  # four live seats, in flight together, after the shifts
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, adapters = self.build(script)
        self.assertEqual(eng.run("task", mr=_mr()), REVIEW_COMPLETE)
        seats = {e["step"]: e["agent"] for e in _events(tx, "step") if "expert" in e["step"]}
        self.assertEqual(len(seats), 4)
        self.assertEqual(len(set(seats.values())), 4, f"a model sat twice: {seats}")
        self.assertFalse(dead & set(seats.values()))
        for name, a in adapters.items():
            self.assertLessEqual(a.max_running, 1, f"{name} ran two seats at once")


class TestFailureKeepsPeers(_Case):
    def test_guard_violation_ends_the_run_but_peers_stay_replayable(self):
        def script(agent, step_id, prompt):
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, adapters = self.build(script, guard=_Guard("mrreview.expert_2"))
        self.assertEqual(eng.run("task", mr=_mr()), HUMAN_REQUIRED)
        self.assertEqual(adapters["claude_sonnet"].calls, [],
                         "a guard violation must stop the run before the moderator")
        cache = orchestrate._load_resume_cache(tx.dir)
        for i in (1, 3, 4):
            self.assertIn((f"mrreview.expert_{i}", "panel", 1), cache)
        reason = json.loads((tx.dir / "run.json").read_text("utf-8"))["reason"]
        self.assertIn("parallel batch with", reason, "must not blame one agent alone")
        self.assertIn("mrreview.expert_1", reason)

    def test_lost_seat_keeps_the_panel(self):
        def script(agent, step_id, prompt):
            if step_id == "mrreview.expert_2":
                return AgentResponse(agent.name, ERROR, error="bad",
                                     failure=failures.classify(stderr="invalid request body"))
            if step_id == "mrreview.moderator":
                return AgentResponse(agent.name, OK, text=MODERATED)
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, _ = self.build(script)
        self.assertEqual(eng.run("task", mr=_mr()), REVIEW_COMPLETE)
        self.assertEqual([e["cell"] for e in _events(tx, "panel_seat_lost")], ["expert_2"])
        self.assertEqual(_events(tx, "panel_summary")[0]["opinions"], 3)
        self.assertEqual(_events(tx, "parallel_end")[0]["failed"], ["mrreview.expert_2"])


class TestLensOutOfAgents(_Case):
    workflow = "review"

    def _script(self, dead):
        def script(agent, step_id, prompt):
            if step_id == "review.arbiter":
                return AgentResponse(agent.name, OK, text=ARBITER_OK)
            if agent.name in dead:
                return AgentResponse(agent.name, ERROR, error="quota", failure=_quota_failure())
            return AgentResponse(agent.name, OK, text=REVIEW_OK.format(name=step_id))
        return script

    def test_a_lens_with_no_agent_left_thins_the_panel(self):
        # Everything but codex and the arbiter's pin is gone: the failing lenses have
        # nowhere to shift, and one at a time this ended with a thinner panel too.
        dead = {"claude_reviewer", "deepseek", "claude_sonnet", "claude_haiku"}
        eng, tx, _ = self.build(self._script(dead))
        self.assertEqual(eng.run("task", target="artifact"), REVIEW_COMPLETE)
        steps = [e["step"] for e in _events(tx, "step") if e["status"] == OK]
        self.assertIn("review.reviewer_correctness", steps)
        self.assertIn("review.arbiter", steps)
        self.assertIn("no live agent for review lens", (tx.dir / "transcript.md").read_text("utf-8"))

    def test_no_lens_at_all_still_pauses_resumably(self):
        everyone = {"codex", "claude_reviewer", "gemini", "deepseek", "claude_sonnet",
                    "claude_haiku"}
        eng, tx, _ = self.build(self._script(everyone))
        self.assertEqual(eng.run("task", target="artifact"), engine_mod.AWAITING_HUMAN)


class TestLostSeatEvidence(_Case):
    def test_it_names_the_agent_that_failed(self):
        def script(agent, step_id, prompt):
            if step_id == "mrreview.expert_2":
                return AgentResponse(agent.name, ERROR, error="bad",
                                     failure=failures.classify(stderr="invalid request body"))
            if step_id == "mrreview.moderator":
                return AgentResponse(agent.name, OK, text=MODERATED)
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, adapters = self.build(script)
        eng.run("task", mr=_mr())
        failed = next(n for n, a in adapters.items() if "mrreview.expert_2" in a.calls)
        self.assertEqual(_events(tx, "panel_seat_lost")[0]["agent"], failed)


class TestGuardUnderConcurrentLoad(unittest.TestCase):
    """Concurrent `git status` and `git diff` must not read as a change: they refresh the
    index opportunistically, and a lost race for index.lock has to stay invisible."""

    def test_concurrent_snapshots_of_an_unchanged_repo_all_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*a):
                subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t",
                                *a], check=True, capture_output=True)
            git("init", "-q")
            for i in range(300):
                (Path(tmp) / f"f{i}.txt").write_text(str(i))
            git("add", ".")
            git("commit", "-qm", "base")
            (Path(tmp) / "f0.txt").write_text("dirty")
            guard = GitGuard([("repo", tmp)], "orchestration")
            want = guard.snapshot()
            seen, errors = [], []

            def worker():
                try:
                    for j in range(25):
                        # A fresh mtime with the same content makes every status and
                        # diff want to rewrite the index, which is the contended path.
                        os.utime(Path(tmp) / f"f{j + 1}.txt")
                        seen.append(guard.snapshot()[0])
                        guard.branches()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(seen), 100)
            self.assertEqual(set(seen), {want[0]})
            self.assertFalse((Path(tmp) / ".git" / "index.lock").exists())


class TestWorstFailureDecides(unittest.TestCase):
    def test_ranking_is_independent_of_finishing_order(self):
        guard, bug = GuardViolation("g"), ValueError("bug")
        agent, pause = AgentError("a"), AvailabilityPause("p")
        for outcomes, want in (([pause, agent], agent), ([agent, bug], bug),
                               ([pause, bug, agent, guard], guard),
                               ([None, ("resp", "v"), pause], pause)):
            with self.assertRaises(type(want)) as cm:
                Engine._raise_worst(outcomes)
            self.assertIs(cm.exception, want)

    def test_all_successes_raise_nothing(self):
        Engine._raise_worst([("r", "v"), None])


class TestChildren(unittest.TestCase):
    def test_terminate_children_stops_a_running_cli(self):
        self.addCleanup(setattr, adapters_mod, "_terminating", False)
        result = {}
        t = threading.Thread(target=lambda: result.update(
            p=adapters_mod._run_child(["sleep", "60"])))
        t.start()
        deadline = time.monotonic() + BARRIER_TIMEOUT_S
        while not adapters_mod._children and time.monotonic() < deadline:
            time.sleep(0.01)
        adapters_mod.terminate_children()
        t.join(BARRIER_TIMEOUT_S)
        self.assertFalse(t.is_alive())
        self.assertLess(result["p"].returncode, 0)
        self.assertEqual(adapters_mod._children, set())

    def test_timeout_fires_even_if_an_escaped_descendant_holds_the_pipe(self):
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            adapters_mod._run_child(["sh", "-c", "setsid sleep 5 & sleep 5"], timeout=0.3)
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(adapters_mod._children, set())

    def _pid_of_grandchild(self, marker):
        deadline = time.monotonic() + BARRIER_TIMEOUT_S
        while time.monotonic() < deadline:
            r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
            if r.stdout.split():
                return r.stdout.split()[0]
            time.sleep(0.02)
        self.fail("the descendant never started")

    def _wait_gone(self, marker):
        deadline = time.monotonic() + BARRIER_TIMEOUT_S
        while time.monotonic() < deadline:
            if not subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout:
                return
            time.sleep(0.02)
        self.fail(f"a descendant outlived its CLI: {marker}")

    def test_timeout_also_kills_what_the_cli_started(self):
        marker = f"sleep {1000 + time.monotonic_ns() % 8000}.25"
        with self.assertRaises(subprocess.TimeoutExpired):
            adapters_mod._run_child(["sh", "-c", f"{marker} & wait"], timeout=0.5)
        self._wait_gone(marker)

    def test_terminate_also_stops_what_the_cli_started(self):
        marker = f"sleep {1000 + time.monotonic_ns() % 8000}.75"
        self.addCleanup(setattr, adapters_mod, "_terminating", False)
        t = threading.Thread(target=lambda: adapters_mod._run_child(
            ["sh", "-c", f"{marker} & wait"]))
        t.start()
        self._pid_of_grandchild(marker)
        adapters_mod.terminate_children()
        t.join(BARRIER_TIMEOUT_S)
        self.assertFalse(t.is_alive())
        self._wait_gone(marker)

    def test_nothing_starts_once_shutdown_began(self):
        self.addCleanup(setattr, adapters_mod, "_terminating", False)
        adapters_mod.terminate_children()
        with self.assertRaises(OSError):
            adapters_mod._run_child(["true"])

    def test_handler_on_a_thread_already_holding_the_lock_does_not_hang(self):
        self.addCleanup(setattr, adapters_mod, "_terminating", False)
        with adapters_mod._children_lock:
            adapters_mod.terminate_children()  # what a signal mid-registration amounts to

    def test_stdin_stdout_and_exit_code_match_subprocess_run(self):
        p = adapters_mod._run_child(["sh", "-c", "cat; echo err >&2; exit 3"], input="hello")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (3, "hello", "err\n"))

    def test_signal_finalizes_the_run_and_takes_the_clis_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = textwrap.dedent(f"""
                import sys, threading, time
                sys.path.insert(0, {str(ORCH)!r})
                import orchestrate
                from orchestrator import adapters
                from orchestrator.transcript import Transcript
                tx = Transcript({tmp!r}, "sig", "mrreview", "t", False, None, {{}})
                orchestrate._install_interrupt_handler(tx)
                ts = [threading.Thread(target=adapters._run_child, args=(["sleep", "60"],),
                                       daemon=True) for _ in range(3)]
                [t.start() for t in ts]
                while len(adapters._children) < 3:
                    time.sleep(0.01)
                print(" ".join(str(p.pid) for p in adapters._children), flush=True)
                [t.join() for t in ts]
            """)
            p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, text=True)
            pids = [int(x) for x in p.stdout.readline().split()]
            self.assertEqual(len(pids), 3)
            p.terminate()
            self.assertEqual(p.wait(BARRIER_TIMEOUT_S), 2)
            p.stdout.close()
            status = (Path(tmp) / "sig" / "STATUS").read_text("utf-8")
            self.assertTrue(status.startswith(HUMAN_REQUIRED))
            deadline = time.monotonic() + BARRIER_TIMEOUT_S
            alive = pids
            while alive and time.monotonic() < deadline:
                alive = [pid for pid in alive if Path(f"/proc/{pid}").exists()
                         and "Z" not in Path(f"/proc/{pid}/stat").read_text().split(")")[1][:3]]
                time.sleep(0.02)
            self.assertEqual(alive, [], "agent CLIs outlived the orchestrator")


class TestConfig(unittest.TestCase):
    def test_shipped_panels_opt_in_with_one_worker_per_seat(self):
        _, wf = load_config()
        self.assertEqual(wf["mrreview"]["parallel"]["max_workers"], 4)
        self.assertEqual(wf["review"]["parallel"]["max_workers"], 3)
        self.assertEqual(wf["implreview"]["parallel"]["max_workers"], 2)

    def test_chains_of_gates_cannot_carry_the_block(self):
        for wf in ("architecture", "development", "investigation"):
            with self.assertRaises(ConfigError):
                _validate_parallel(wf, {"parallel": {"max_workers": 2}})

    def test_bad_values_are_refused(self):
        for bad in (0, -1, True, "2", None, 1.5):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                _validate_parallel("review", {"parallel": {"max_workers": bad}})
        with self.assertRaises(ConfigError):
            _validate_parallel("review", {"parallel": 2})
        with self.assertRaises(ConfigError):
            _validate_parallel("review", {"parallel": {"max_workers": 2, "max_worker": 4}})
        _validate_parallel("review", {"parallel": {"_comment": "why", "max_workers": 2}})
        _validate_parallel("review", {})
        _validate_parallel("review", {"parallel": {"max_workers": 1}})

    def test_no_block_means_one_at_a_time(self):
        self.assertEqual(Engine._parallel_limit(type("E", (), {
            "ctx": type("C", (), {"workflows": {"development": {}}}), "workflow": "development"
        })), 1)


class TestBatchShape(_Case):
    def test_outcomes_align_with_calls_and_a_single_step_needs_no_pool(self):
        def script(agent, step_id, prompt):
            return AgentResponse(agent.name, OK, text=MR_OK.format(name=step_id))

        eng, tx, _ = self.build(script)
        eng._setup_roster()
        spec = eng.ctx.workflows["mrreview"]["expert_1"]
        out = eng._call_batch([_BatchCall("mrreview.expert_1", spec, "p", "panel", 1)])
        self.assertEqual(out[0][1], "APPROVE")
        self.assertEqual(_events(tx, "parallel_start"), [], "one step is not a batch")


if __name__ == "__main__":
    unittest.main()
