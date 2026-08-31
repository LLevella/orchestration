"""Run history: every prompt, every agent output, every verdict, preserved.

Layout per run:
    runs/<run-id>/
        run.json          metadata + final status
        events.jsonl       append-only machine log (one line per step/event)
        transcript.md      human-readable chronological narrative
        iterations/NN-*.md full prompt + full output for each step (never overwritten)
        artifacts/         drafts, diffs, AQ docs produced during the run
        disagreements.md   accumulated unresolved-issue dossier
        quality.json       derived quality signal (architecture runs, schema_version 1);
                           staged during the run, published only by a normal finish
        STATUS             one-glance terminal status + reason
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# `quality.json` is published in two phases (see `write_quality` / `finish`): the
# derived summary lands in the pending path first and is renamed into place only
# when the run finalizes normally.
QUALITY_FILE = "quality.json"
QUALITY_PENDING = ".quality.json.pending"


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fmt_scope_cost(bucket) -> str:
    """Cost of ONE rendered scope (the run total, an agent row, a phase row).

    An unpriced transport must never print as `$0.0000` — that is a measurement
    claim this tool cannot make, since it holds no price tables and only ever
    repeats what a provider reported. So a scope in which nothing reported a cost
    reads `unmetered`, and a mixed scope shows the reported spend AND how many of
    its attempts were unmetered, because the bare sum reads as the whole bill.
    A legacy (schema-v1) bucket carries no per-scope coverage and is rendered as
    the plain figure it recorded; `show` labels the record as legacy separately."""
    cost = bucket.get("cost_usd") or 0.0
    reported, attempts = bucket.get("cost_reported_attempts"), bucket.get("attempts")
    if reported is None or attempts is None:
        return f"${cost:.4f}"
    if reported == 0:
        return "unmetered"
    if reported < attempts:
        return f"${cost:.4f} reported, {attempts - reported} unmetered"
    return f"${cost:.4f} reported"


def fmt_version_line(versions) -> str:
    """One line naming the code a run ran ON: the executing tool, the project layer in
    force, and each watched repo's run-start baseline (see RunContext.versions).

    A part that could not be read prints the reason the run recorded for it, never a
    blank and never a plausible-looking substitute. Returns "" when a run recorded no
    versions at all - every run written before this was captured - so those STATUS
    files keep exactly the shape they have."""
    if not versions:
        return ""
    if "tool" not in versions:
        return ("code: version not recorded ("
                + (versions.get("unavailable") or "no reason recorded") + ")")

    def short(value, n=7):
        return value[:n] if value else None

    tool = versions.get("tool") or {}
    if tool.get("commit"):
        state = {True: "dirty", False: "clean"}.get(tool.get("dirty"), "dirtiness unknown")
        t = (f"tool {short(tool['commit'])} "
             f"({tool.get('branch') or 'unknown branch'}, {state})")
    else:
        t = f"tool unavailable ({tool.get('unavailable') or 'no reason recorded'})"
    proj = versions.get("project_layer") or {}
    p = f"project layer {short(proj.get('digest'), 12) or 'unavailable'}"
    repos = ", ".join(
        f"{label}@{short((v or {}).get('commit')) or 'no-HEAD'}"
        f" ({(v or {}).get('branch') or '?'})"
        for label, v in sorted((versions.get("repos") or {}).items()))
    return f"code: {t} · {p} · repos: {repos or '(none watched)'}"


class Transcript:
    def __init__(self, runs_dir, run_id, workflow, task, mock, scenario_name, config_snapshot):
        self.dir = Path(runs_dir) / run_id
        (self.dir / "iterations").mkdir(parents=True, exist_ok=True)
        (self.dir / "artifacts").mkdir(exist_ok=True)
        self.run_id = run_id
        self.n = 0
        # Per-ATTEMPT token accounting, aggregated in finish(). Seeded from any
        # attempts already journaled in this directory BEFORE the append handle is
        # opened, so reopening a run directory re-aggregates the whole history
        # instead of overwriting usage.json with only this process's invocations.
        # Read exactly once, and nothing writes to the journal before this, so no
        # attempt can be counted twice.
        self.usage_rows = self._journaled_attempts()
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._tpath = self.dir / "transcript.md"
        self.meta = {
            "run_id": run_id, "workflow": workflow, "task": task,
            "mock": mock, "scenario": scenario_name,
            "started": now_utc(), "status": None, "reason": None,
            "config": config_snapshot,
        }
        self._write_meta()
        self.md(
            f"# Orchestration run `{run_id}`\n\n"
            f"- workflow: **{workflow}**\n"
            f"- mock: `{mock}`  scenario: `{scenario_name}`\n"
            f"- started: {self.meta['started']}\n\n"
            f"## Task\n\n```\n{task}\n```\n"
        )

    def _journaled_attempts(self):
        """Attempt rows already recorded in this run directory's journal.

        Usually empty (a fresh run id). It matters when a run directory is reopened
        and appended to: `finish()` REWRITES usage.json from `usage_rows`, so
        without replaying the journal the earlier invocations — and their cost —
        would silently vanish from the totals of the very run that incurred them.
        A malformed or unreadable journal degrades to "no history": losing an old
        row is bad, refusing to start the run is worse."""
        f = self.dir / "events.jsonl"
        if not f.exists():
            return []
        rows = []
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") == "agent_attempt":
                rows.append({k: v for k, v in e.items() if k not in ("kind", "ts")})
        return rows

    def _write_meta(self):
        (self.dir / "run.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def update_meta(self, **kw):
        """Persist extra fields into run.json (e.g. arch_ref, so an interrupted
        run can be resumed with the same inputs)."""
        self.meta.update(kw)
        self._write_meta()

    def event(self, **kw):
        kw["ts"] = now_utc()
        self._events.write(json.dumps(kw) + "\n")
        self._events.flush()

    def md(self, text: str):
        with open(self._tpath, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    @staticmethod
    def _fmt_usage(usage, cost_usd):
        """One-line rendering of a normalized usage dict. Every input class the
        transport reported is shown separately — a Claude step whose input is 99%
        cache-read is not the same event as one that paid for it fresh — and an
        unreported cost prints as `cost unmetered`, never as $0.0000."""
        if not usage and cost_usd is None:
            return "n/a"
        usage = usage or {}
        parts = []
        if usage.get("input") is not None or usage.get("output") is not None:
            parts.append(f"in {usage.get('input')} / out {usage.get('output')}")
        classes = [(k, usage.get(f"input_{k}")) for k in ("fresh", "cache_creation", "cache_read")]
        if any(v is not None for _k, v in classes):
            parts.append("input " + " + ".join(f"{k} {v}" for k, v in classes if v is not None))
        if usage.get("total") is not None:
            parts.append(f"total {usage.get('total')}")
        parts.append(f"${cost_usd:.4f} reported" if cost_usd is not None else "cost unmetered")
        return ", ".join(parts) or "n/a"

    @staticmethod
    def _failure_dict(failure):
        """FailureInfo (or an already-serialized dict, or None) -> dict|None."""
        if failure is None or isinstance(failure, dict):
            return failure
        return failure.to_dict()

    @staticmethod
    def _fmt_failure(failure, level="##"):
        """Render the structured failure, INCLUDING the bounded raw provider body.

        Amendment A2: an error body may never become `resp.text` (it could be read
        as an answer or a verdict), but it must not be lost either — losing it is
        defect P2. This is where a human reads the provider's own words back.
        `level` keeps the heading below its host document's own headings (`##` in
        an iteration file, `####` inside the chronological transcript)."""
        f = Transcript._failure_dict(failure)
        if f is None:
            return ""
        return (
            f"\n{level} Provider failure\n\n"
            f"- kind: `{f.get('kind')}` · scope: `{f.get('scope')}` · "
            f"source: `{f.get('classification_source')}`\n"
            f"- http_status: `{f.get('http_status')}` · provider_code: "
            f"`{f.get('provider_code')}` · retry_at: `{f.get('retry_at')}`\n"
            # Both pairs are shown: what P2 DID (compat_*, the pre-P2 marker
            # decision) and what the failure kind MEANS for P1 recovery.
            f"- applied: retry_same_agent `{f.get('compat_retry_same_agent')}` · "
            f"roster_unavailable `{f.get('compat_roster_unavailable')}`\n"
            f"- kind policy (not applied in P2): retry_same_agent "
            f"`{f.get('retry_same_agent')}` · roster_unavailable "
            f"`{f.get('roster_unavailable')}`\n\n"
            # Usually the provider's own words, but it also carries the explicitly
            # labelled `[local]` evidence when the transport itself failed, so the
            # heading says evidence rather than promising a provider body.
            f"{level}# Raw failure evidence (bounded head+tail; NOT parsed as an answer)\n\n"
            f"```\n{f.get('raw') or '(provider returned nothing)'}\n```\n")

    def step(self, step_id, agent, role, system, prompt, resp, verdict, iteration, phase):
        self.n += 1
        fn = f"{self.n:02d}-{step_id.replace('.', '-')}-{phase}-i{iteration}.md"
        body = (
            f"# Step {self.n}: `{step_id}`\n\n"
            f"- agent: **{agent}**\n- role: **{role}**\n"
            f"- phase: {phase}  iteration: {iteration}\n"
            f"- adapter status: `{resp.status}`\n- verdict: `{verdict}`\n"
            f"- duration: {round(resp.duration, 3)}s\n"
            f"- tokens: {self._fmt_usage(resp.usage, resp.cost_usd)}\n\n"
            f"## System Prompt (role + wrapper)\n\n<details><summary>expand</summary>\n\n"
            f"```\n{system}\n```\n\n</details>\n\n"
            f"## User Prompt\n\n```\n{prompt}\n```\n\n"
            f"## Agent Output\n\n{resp.text or '(no output)'}\n"
        )
        if resp.error:
            body += f"\n## Error\n\n```\n{resp.error}\n```\n"
        failure = getattr(resp, "failure", None)
        body += self._fmt_failure(failure)
        (self.dir / "iterations" / fn).write_text(body, encoding="utf-8")
        # The logical step keeps reporting its own usage for readability, but it is
        # NOT what usage.json sums — attempts are (see attempt()), so a retry or a
        # cross-agent shift inside this step cannot be double-counted or dropped.
        self.event(kind="step", step=step_id, agent=agent, role=role,
                   status=resp.status, verdict=verdict, iteration=iteration,
                   phase=phase, file=f"iterations/{fn}", duration=round(resp.duration, 3),
                   usage=resp.usage, cost_usd=resp.cost_usd,
                   failure=self._failure_dict(failure))
        self.md(f"### [{self.n}] {phase} · iter {iteration} · `{step_id}` "
                f"→ **{verdict or resp.status}**\n"
                f"`{agent}` as *{role}* — [details](iterations/{fn})\n")

    def attempt(self, *, step, phase, iteration, attempt, attempt_kind, agent, role,
                status, duration, usage=None, cost_usd=None, failure=None):
        """Journal ONE adapter invocation. Additive: never replayed on resume.

        Every provider call is recorded exactly once here — primary, same-agent
        retry, post-shift call on another vendor, and verdict repair — each keeping
        the agent that actually incurred its usage. This is the row set usage.json
        aggregates, which is why a failed-then-recovered step no longer reports the
        cost of only its last attempt."""
        row = {"step": step, "phase": phase, "iteration": iteration,
               "attempt": attempt, "attempt_kind": attempt_kind, "agent": agent,
               "role": role, "status": status, "duration": duration,
               "usage": usage, "cost_usd": cost_usd, "failure": failure}
        self.usage_rows.append(row)
        self.event(kind="agent_attempt", **row)
        if status != "OK":
            self.md(self._fmt_failed_attempt(row))

    @staticmethod
    def _fmt_failed_attempt(row):
        """A failed invocation, rendered into the human transcript.

        A failure that is followed by a successful retry or a shift to another
        vendor leaves NO trace in the logical `step` record — only the winning
        response is written there — so without this the operator cannot see that
        an agent died, why, or what it cost on the way. Amendment A2 also requires
        the provider's own bytes to reach the transcript: they are rendered here
        (bounded head+tail, inside a fenced block) and nowhere near `resp.text`,
        so nothing here can be read back as an answer or a verdict."""
        head = (f"\n> ✖ attempt {row.get('attempt')} ({row.get('attempt_kind')}) on "
                f"`{row.get('step')}` · {row.get('phase')} iter {row.get('iteration')} — "
                f"`{row.get('agent')}` as *{row.get('role')}* → **{row.get('status')}**"
                f" · {Transcript._fmt_usage(row.get('usage'), row.get('cost_usd'))}\n")
        return head + Transcript._fmt_failure(row.get("failure"), level="####")

    def artifact(self, name: str, content: str):
        (self.dir / "artifacts" / name).write_text(content, encoding="utf-8")
        self.event(kind="artifact", name=name)

    def disagreements(self, content: str):
        (self.dir / "disagreements.md").write_text(content, encoding="utf-8")
        self.event(kind="disagreements", chars=len(content))

    def write_quality(self, summary: dict) -> None:
        """Stage the run's derived quality signal for publication as `quality.json`.

        Deterministic formatting (sorted keys, fixed indent) so two runs of the same
        cohort differ only where their measurements differ. This is a DERIVED artifact:
        every number in it comes from gate and author responses already in this run's
        journal, so it adds no agent call and no new telemetry source.

        Staged, not published, and deliberately so. The engine derives the metric
        BEFORE `finish()`, because the journal this event goes to is still open here —
        which leaves a window in which a signal can arrive with the summary already on
        disk. So the bytes land in the pending path, and only `finish()` renames them
        into place, atomically, and only for a run that finalized normally: the signal
        handler in orchestrate.py reaches `finish(interrupted=True)` and discards them
        instead. A run interrupted anywhere in that window therefore has NO
        `quality.json` at all, which is how incomplete evidence stays out of comparison
        cohorts rather than contributing a partial value."""
        (self.dir / QUALITY_PENDING).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        overall = summary.get("overall") or {}
        self.event(kind="quality", metric=summary.get("metric"),
                   schema_version=summary.get("schema_version"),
                   accepted=overall.get("accepted"), eligible=overall.get("eligible"),
                   rate=overall.get("rate"))

    def _publish_quality(self, interrupted: bool) -> None:
        """Resolve a staged quality signal: publish it, or drop it.

        Last act of a normal `finish()`, so `quality.json` existing implies the run
        also has its STATUS — and `os.replace` is atomic, so a reader never sees a
        half-written one. An interrupted finalization deletes the staging file, and a
        finalization that is itself cut short simply never gets here: the pending path
        is hidden and no cohort rule looks at it."""
        pending = self.dir / QUALITY_PENDING
        if not pending.exists():
            return
        if interrupted:
            pending.unlink()
        else:
            os.replace(pending, self.dir / QUALITY_FILE)

    # usage.json shape version. v1 recorded one row per logical step, summed only
    # `input`/`output`/`total`, and reported an unpriced transport as $0.00. v2 sums
    # ATTEMPTS, carries every input class, and states coverage so "nobody reported a
    # cost" is distinguishable from "the cost was measured and it was zero".
    SCHEMA_VERSION = 2
    _USAGE_KEYS = ("input", "input_fresh", "input_cache_creation",
                   "input_cache_read", "output", "total")

    def _write_usage_summary(self):
        """Aggregate per-ATTEMPT usage into usage.json + a transcript table; return
        the totals dict.

        Numeric aggregates are sums of REPORTED values only — nothing is estimated
        and no price table exists here, so an agent whose transport prices nothing
        (codex/gemini/deepseek) is unmetered spend, not $0.00. `coverage` says how
        many attempts actually reported each figure, which is the only honest way
        to read the sums. `steps` counts distinct live logical steps and `metered`
        the subset that carried a token figure, both unchanged in meaning from v1;
        `attempts` is the new, true invocation count."""
        def acc():
            b = {k: 0 for k in self._USAGE_KEYS}
            b.update(cost_usd=0.0, cost_reported_attempts=0, steps=0, attempts=0,
                     duration=0.0, metered=0, _steps=set(), _metered=set())
            return b

        def add(b, r, u):
            key = (r.get("step"), r.get("phase"), r.get("iteration"))
            b["_steps"].add(key)
            if r.get("usage"):
                b["_metered"].add(key)
            b["attempts"] += 1
            b["duration"] += r.get("duration") or 0.0
            for k in self._USAGE_KEYS:
                b[k] += u.get(k) or 0
            b["cost_usd"] += r.get("cost_usd") or 0.0
            # Cost coverage is tracked PER SCOPE, not only run-wide: a run-wide note
            # cannot make a `codex  $0.0000` row true. A row whose transport priced
            # nothing must read `unmetered`, and a mixed row must say how much of it
            # was actually priced.
            if r.get("cost_usd") is not None:
                b["cost_reported_attempts"] += 1

        def close(b):
            b["steps"], b["metered"] = len(b.pop("_steps")), len(b.pop("_metered"))
            return b

        totals, by_agent, by_phase = acc(), {}, {}
        coverage = {k: {"reported_attempts": 0, "attempts": len(self.usage_rows)}
                    for k in self._USAGE_KEYS + ("cost_usd",)}
        for r in self.usage_rows:
            u = r.get("usage") or {}
            add(totals, r, u)
            add(by_agent.setdefault(r["agent"], acc()), r, u)
            add(by_phase.setdefault(r["phase"], acc()), r, u)
            for k in self._USAGE_KEYS:
                if u.get(k) is not None:
                    coverage[k]["reported_attempts"] += 1
            if r.get("cost_usd") is not None:
                coverage["cost_usd"]["reported_attempts"] += 1
        close(totals)
        for b in list(by_agent.values()) + list(by_phase.values()):
            close(b)
        priced, attempts = coverage["cost_usd"]["reported_attempts"], totals["attempts"]
        summary = {"schema_version": self.SCHEMA_VERSION, "run_id": self.run_id,
                   "workflow": self.meta.get("workflow"),
                   "totals": totals, "by_agent": by_agent, "by_phase": by_phase,
                   "coverage": coverage,
                   "note": ("all figures are PROVIDER-REPORTED per invocation and are "
                            "never estimated: sums include only attempts that reported "
                            "the figure, and 'coverage' says how many did. cost_usd is "
                            "therefore reported spend, not total spend — the "
                            f"{attempts - priced} of {attempts} attempt(s) whose "
                            "transport prices nothing are UNMETERED, not $0.00. "
                            "'steps'/'metered' count distinct logical steps; 'attempts' "
                            "counts real invocations (retries, cross-agent shifts and "
                            "verdict repairs each cost tokens and are counted here).")}
        (self.dir / "usage.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        def row(name, b):
            return (f"| {name} | {b['steps']} | {b['attempts']} | {b['input']} | "
                    f"{b['input_cache_creation']} | {b['input_cache_read']} | "
                    f"{b['output']} | {b['total']} | {fmt_scope_cost(b)} | "
                    f"{round(b['duration'])}s |")
        lines = ["\n## Token usage\n",
                 "| scope | steps | attempts | input | cache-create | cache-read | "
                 "output | total | cost(reported) | wall |",
                 "|---|---|---|---|---|---|---|---|---|---|", row("**TOTAL**", totals)]
        lines += [row(f"agent · {a}", b) for a, b in sorted(by_agent.items())]
        lines += [row(f"phase · {p}", b) for p, b in sorted(by_phase.items())]
        lines += ["",
                  f"Cost is PROVIDER-REPORTED for {priced} of {attempts} attempt(s); "
                  "the rest ran on transports that report no price and are UNMETERED "
                  "(not $0.00). No price table is used anywhere in this tool."]
        self.md("\n".join(lines) + "\n")
        return totals

    def finish(self, status: str, reason: str, interrupted: bool = False):
        """Finalize the run. `interrupted=True` marks a finalization forced by a signal
        rather than by the engine reaching a terminal state; it publishes no staged
        quality signal (see `_publish_quality`). A second call is a no-op that touches
        nothing — a signal landing mid-finish must not undo a publication that already
        happened, nor publish one the engine never resolved."""
        if getattr(self, "_finished", False):
            return self.meta.get("status")
        self._finished = True
        self.meta.update(status=status, reason=reason, finished=now_utc())
        self._write_meta()
        totals = self._write_usage_summary()
        # STATUS is the one line an operator reads without opening anything else, so
        # it carries the coverage too: "$12.30 reported, 4 unmetered" is the truth,
        # "$12.30" alone silently claims the run cost that much in total.
        usage_line = (f"tokens: in {totals['input']} / out {totals['output']} / "
                      f"total {totals['total']} · provider-reported cost "
                      f"{fmt_scope_cost(totals)} · {totals['steps']} steps "
                      f"({totals['metered']} metered) · {totals['attempts']} attempts")
        # ... and the code it ran on, for the same reason: a cost or a verdict that
        # cannot be attributed to a version cannot be compared with another run's.
        # Absent (a run recorded before this existed) -> the footer keeps its old shape.
        version_line = fmt_version_line(self.meta.get("versions"))
        body = f"{status}\n{reason}\n\n{usage_line}\n"
        if version_line:
            body += f"{version_line}\n"
        (self.dir / "STATUS").write_text(body, encoding="utf-8")
        self.event(kind="status", status=status, reason=reason, usage_total=totals)
        self.md(f"\n---\n\n## FINAL STATUS: **{status}**\n\n{reason}\n")
        self._events.close()
        self._publish_quality(interrupted)
        return status
