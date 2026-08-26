"""Run history: every prompt, every agent output, every verdict, preserved.

Layout per run:
    runs/<run-id>/
        run.json          metadata + final status
        events.jsonl       append-only machine log (one line per step/event)
        transcript.md      human-readable chronological narrative
        iterations/NN-*.md full prompt + full output for each step (never overwritten)
        artifacts/         drafts, diffs, AQ docs produced during the run
        disagreements.md   accumulated unresolved-issue dossier
        STATUS             one-glance terminal status + reason
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Transcript:
    def __init__(self, runs_dir, run_id, workflow, task, mock, scenario_name, config_snapshot):
        self.dir = Path(runs_dir) / run_id
        (self.dir / "iterations").mkdir(parents=True, exist_ok=True)
        (self.dir / "artifacts").mkdir(exist_ok=True)
        self.run_id = run_id
        self.n = 0
        self.usage_rows = []  # per-step token accounting, aggregated in finish()
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
        if not usage:
            return "n/a"
        parts = []
        if usage.get("input") is not None or usage.get("output") is not None:
            parts.append(f"in {usage.get('input')} / out {usage.get('output')}")
        if usage.get("total") is not None:
            parts.append(f"total {usage.get('total')}")
        if cost_usd is not None:
            parts.append(f"${cost_usd:.4f}")
        return ", ".join(parts) or "n/a"

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
        (self.dir / "iterations" / fn).write_text(body, encoding="utf-8")
        self.event(kind="step", step=step_id, agent=agent, role=role,
                   status=resp.status, verdict=verdict, iteration=iteration,
                   phase=phase, file=f"iterations/{fn}", duration=round(resp.duration, 3),
                   usage=resp.usage, cost_usd=resp.cost_usd)
        self.usage_rows.append({"step": step_id, "agent": agent, "phase": phase,
                                "iteration": iteration, "usage": resp.usage,
                                "cost_usd": resp.cost_usd, "duration": round(resp.duration, 3)})
        self.md(f"### [{self.n}] {phase} · iter {iteration} · `{step_id}` "
                f"→ **{verdict or resp.status}**\n"
                f"`{agent}` as *{role}* — [details](iterations/{fn})\n")

    def artifact(self, name: str, content: str):
        (self.dir / "artifacts" / name).write_text(content, encoding="utf-8")
        self.event(kind="artifact", name=name)

    def disagreements(self, content: str):
        (self.dir / "disagreements.md").write_text(content, encoding="utf-8")
        self.event(kind="disagreements", chars=len(content))

    def _write_usage_summary(self):
        """Aggregate per-step token usage into usage.json + a transcript table; return
        the totals dict. Agents that don't report usage (gemini/mock) contribute 0;
        codex reports a total only. 'metered' = number of steps that carried a figure."""
        def acc():
            return {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0,
                    "steps": 0, "duration": 0.0, "metered": 0}

        def add(b, r, u):
            b["steps"] += 1
            b["duration"] += r.get("duration") or 0.0
            b["input"] += u.get("input") or 0
            b["output"] += u.get("output") or 0
            b["total"] += u.get("total") or 0
            b["cost_usd"] += r.get("cost_usd") or 0.0
            b["metered"] += 1 if r.get("usage") else 0

        totals, by_agent, by_phase = acc(), {}, {}
        for r in self.usage_rows:
            u = r.get("usage") or {}
            add(totals, r, u)
            add(by_agent.setdefault(r["agent"], acc()), r, u)
            add(by_phase.setdefault(r["phase"], acc()), r, u)
        summary = {"run_id": self.run_id, "workflow": self.meta.get("workflow"),
                   "totals": totals, "by_agent": by_agent, "by_phase": by_phase,
                   "note": ("cost_usd is claude-only (deepseek/codex unpriced); codex "
                            "reports total only; gemini/mock report nothing — 'metered' "
                            "counts steps that carried a token figure.")}
        (self.dir / "usage.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        def row(name, b):
            return (f"| {name} | {b['steps']} | {b['input']} | {b['output']} | "
                    f"{b['total']} | ${b['cost_usd']:.4f} | {round(b['duration'])}s |")
        lines = ["\n## Token usage\n",
                 "| scope | steps | input | output | total | cost(claude) | wall |",
                 "|---|---|---|---|---|---|---|", row("**TOTAL**", totals)]
        lines += [row(f"agent · {a}", b) for a, b in sorted(by_agent.items())]
        lines += [row(f"phase · {p}", b) for p, b in sorted(by_phase.items())]
        self.md("\n".join(lines) + "\n")
        return totals

    def finish(self, status: str, reason: str):
        if getattr(self, "_finished", False):
            return self.meta.get("status")
        self._finished = True
        self.meta.update(status=status, reason=reason, finished=now_utc())
        self._write_meta()
        totals = self._write_usage_summary()
        usage_line = (f"tokens: in {totals['input']} / out {totals['output']} / "
                      f"total {totals['total']} · cost(claude) ${totals['cost_usd']:.4f} · "
                      f"{totals['steps']} steps ({totals['metered']} metered)")
        (self.dir / "STATUS").write_text(f"{status}\n{reason}\n\n{usage_line}\n", encoding="utf-8")
        self.event(kind="status", status=status, reason=reason, usage_total=totals)
        self.md(f"\n---\n\n## FINAL STATUS: **{status}**\n\n{reason}\n")
        self._events.close()
        return status
