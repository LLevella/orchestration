"""The deterministic state machines for both workflows.

Control flow lives here; agent reasoning lives in the role files. Both loops are
hard-bounded (10 + 10 iterations), fire arbitration on repeated disagreement
(> trigger_after_cycles), and terminate with PASS / READY_FOR_HUMAN_REVIEW or
HUMAN_REQUIRED. Every step is logged via the transcript before any decision.
"""
from __future__ import annotations

import hashlib
import re

from .adapters import OK, AgentResponse
from .guard import GuardViolation
from .roster import Roster, is_unavailable
from .verdicts import ROLE_VERDICTS, extract_verdict

PASS = "PASS"
READY = "READY_FOR_HUMAN_REVIEW"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
# Investigation workflow terminal states (both are clean conclusions, exit 0).
ROOT_CAUSE_ESTABLISHED = "ROOT_CAUSE_ESTABLISHED"
NO_DEFECT_FOUND = "NO_DEFECT_FOUND"

# Terminal statuses that count as success (exit code 0). orchestrate.py imports
# this so the exit-code mapping never drifts from the engine's vocabulary.
SUCCESS_STATUSES = {PASS, READY, ROOT_CAUSE_ESTABLISHED, NO_DEFECT_FOUND}

# Arbiter verdicts that mean "stop, a human must decide". Shared by the dev,
# architecture, and investigation arbitration loops (the investigation arbiter
# uses exactly the same two terminal-stop tokens).
ARB_STOP = {"NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION", None}
# Investigation-arbiter verdicts that mean "the hypothesis is proven, stop with
# success". Everything not in this set and not in ARB_STOP reopens the loop.
INV_ARB_SUCCESS = {"HYPOTHESIS_STANDS", "INVESTIGATOR_IS_RIGHT"}

# How many times to re-prompt an agent that returned a successful response with
# no machine-readable verdict header before falling through to the fail-safe.
# A headless agent that did the work but ended on a prose summary (e.g. the
# investigator opening with "Investigation complete." instead of the
# "## Investigator Verdict" block) is nudged to re-emit the verdict rather than
# wasting the whole step to HUMAN_REQUIRED. 0 disables repair (old behaviour).
VERDICT_REPAIR_ATTEMPTS = 2


class AgentError(Exception):
    pass


def _norm_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()


def _sum_opt(a, b):
    """Sum two Optional[int] token counts; None only if both are None."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _merge_usage(a, b):
    """Element-wise sum of two normalized usage dicts (input/output/total)."""
    if not a and not b:
        return None
    a, b = a or {}, b or {}
    return {k: _sum_opt(a.get(k), b.get(k)) for k in set(a) | set(b)}


class Engine:
    def __init__(self, ctx, workflow: str):
        self.ctx = ctx
        self.workflow = workflow
        self.roster = None

    # ---- roster (availability-resolved role assignment) --------------
    def _apply_roster_assignment(self):
        """Recompute the cell->agent assignment for the current liveness and write
        it onto the workflow's per-cell spec blocks (so every call() resolves
        `spec["agent"]` from the live roster). Returns the assignment dict."""
        wf = self.ctx.workflows[self.workflow]
        assignment = self.roster.assign()
        for cell, agent in assignment.items():
            blk = wf.get(cell)
            if isinstance(blk, dict):
                blk["_cell"] = cell
                blk["agent"] = agent
        return assignment

    def _setup_roster(self):
        """Build the roster from the workflow's `roster` block and resolve the
        opening assignment. No-op (static per-cell `agent` bindings stand) when the
        workflow declares no roster."""
        wf = self.ctx.workflows[self.workflow]
        self.roster = Roster(wf, self.ctx.adapters)
        if not self.roster.enabled:
            return
        assignment = self._apply_roster_assignment()
        lines = [f"  {c:22s} -> {assignment.get(c) or 'NONE (no live agent)'}"
                 for c in self.roster.cells]
        self.ctx.tx.md("\n#### Roster — availability-resolved assignment\n"
                       "```\n" + "\n".join(lines) + "\n```\n")
        self.ctx.tx.event(kind="roster_assignment", workflow=self.workflow,
                          assignment=assignment, exhausted=sorted(self.roster.exhausted))

    # ---- core step caller --------------------------------------------
    def call(self, step_id, spec, user_prompt, *, phase, iteration,
             allow_edit=False, parse=True):
        ctx = self.ctx
        role = spec["role"]
        cell = spec.get("_cell")

        # Resume: if this exact step (step_id, phase, iteration) already completed
        # in the run we are resuming from, replay its journaled result instead of
        # re-invoking the agent (regardless of which agent produced it). For the
        # editing developer step this is also why we do NOT re-edit — its changes
        # are already persisted in the working tree. The first un-journaled step
        # and everything after it runs live.
        cached = ctx.resume_lookup(step_id, phase, iteration)
        if cached is not None:
            resp = AgentResponse(cached["agent"], OK, text=cached["text"],
                                 role=role, duration=0.0)
            verdict = cached["verdict"] if parse else None
            ctx.tx.step(step_id, cached["agent"], role,
                        "(resumed — system prompt not re-sent)",
                        "(resumed — user prompt not re-sent)",
                        resp, verdict, iteration, phase)
            return resp, verdict

        # Shift-left loop: on an EXHAUSTED-class failure for a roster-managed cell,
        # drop the agent, recompute the assignment, and retry with the next live
        # agent in the chain. Terminates because each failure marks one more agent
        # exhausted -> the chain eventually yields None -> a clear AgentError.
        while True:
            agent = spec["agent"]
            if agent is None:
                raise AgentError(
                    f"no live agent for role '{role}'"
                    + (f" (roster cell '{cell}')" if cell else "")
                    + " — the fallback chain is exhausted.")
            system = ctx.load_role(role, spec.get("wrapper"))
            before = ctx.guard.snapshot() if not allow_edit else None
            # Branches are captured for EVERY step, editable ones included: a
            # `git checkout` changes no file, so the snapshot above cannot see it.
            branches_before = ctx.guard.branches()
            resp = ctx.adapters[agent].run(system, user_prompt,
                                           allow_edit=allow_edit, step_id=step_id)
            resp.role = role
            if self._shift_if_exhausted(step_id, cell, agent, resp):
                continue  # retry with the shifted agent
            # Verdict repair: a successful step that must be parsed but carries no
            # known verdict token is nudged to re-emit it (bounded) BEFORE finalize,
            # so the read-only guard's before/after bracket still spans every
            # invocation. Without this a complete analysis that merely forgot the
            # verdict header wastes the whole step to HUMAN_REQUIRED.
            if (parse and resp.status == OK and role in ROLE_VERDICTS
                    and extract_verdict(role, resp.text) is None):
                resp = self._repair_verdict(step_id, agent, role, system,
                                            user_prompt, resp, allow_edit)
            return self._finalize(step_id, agent, role, system, user_prompt,
                                   resp, before, parse, iteration, phase, allow_edit,
                                   branches_before)

    def _shift_if_exhausted(self, step_id, cell, agent, resp) -> bool:
        """If `resp` is an EXHAUSTED-class failure for a roster-managed cell, drop
        the agent, recompute the assignment, and report whether a DIFFERENT live
        agent is now in the cell (so the caller should retry)."""
        if resp.status == OK or not cell or not (self.roster and self.roster.enabled):
            return False
        if not is_unavailable(resp.error or resp.text):
            return False
        self.roster.mark_exhausted(agent)
        self._apply_roster_assignment()
        nxt = self.ctx.workflows[self.workflow].get(cell, {}).get("agent")
        self.ctx.tx.event(kind="roster_shift", step=step_id, cell=cell, exhausted=agent,
                          next_agent=nxt, detail=(resp.error or resp.text or "")[:200])
        self.ctx.tx.md(f"\n> ⤵ `{agent}` exhausted on cell **{cell}** → shift-left to "
                       f"`{nxt or 'NONE'}`, retrying.\n")
        return bool(nxt and nxt != agent)

    def _finalize(self, step_id, agent, role, system, user_prompt, resp, before,
                  parse, iteration, phase, allow_edit, branches_before=None):
        """Guard-check a step, log it, and return (resp, verdict).

        Two independent checks: the working tree must be untouched by a read-only
        role, and the checked-out branch must be unmoved by ANY role.
        """
        ctx = self.ctx

        def _abort(e):
            ctx.tx.event(kind="guard_violation", step=step_id, detail=str(e))
            resp.error = ((resp.error or "") + "\n" + str(e)).strip()
            verdict = extract_verdict(role, resp.text) if parse else None
            ctx.tx.step(step_id, agent, role, system, user_prompt, resp, verdict, iteration, phase)
            raise e

        try:
            ctx.guard.assert_branch_unchanged(branches_before, ctx.guard.branches(),
                                              agent, step_id)
        except GuardViolation as e:
            _abort(e)
        if not allow_edit:
            after = ctx.guard.snapshot()
            try:
                ctx.guard.assert_unchanged(before, after, agent, step_id)
            except GuardViolation as e:
                _abort(e)
        verdict = extract_verdict(role, resp.text) if parse else None
        ctx.tx.step(step_id, agent, role, system, user_prompt, resp, verdict, iteration, phase)
        if resp.status != OK:
            raise AgentError(f"agent '{agent}' ({role}) returned {resp.status}: "
                             f"{resp.error or 'no detail'}")
        return resp, verdict

    def _repair_verdict(self, step_id, agent, role, system, user_prompt, resp, allow_edit):
        """`resp` succeeded but carries no parseable verdict. Re-prompt the SAME
        agent (bounded by VERDICT_REPAIR_ATTEMPTS) to re-emit ONLY its verdict
        block, then append it to the original text so the analysis is preserved
        AND the verdict parses. Returns the (mutated) resp; if every attempt
        fails, returns it unchanged so _finalize fail-safes as before.

        Called between the live invocation and _finalize, so the read-only guard
        still brackets these extra invocations (no edit can slip through)."""
        spec = ROLE_VERDICTS.get(role)
        if not spec or VERDICT_REPAIR_ATTEMPTS <= 0:
            return resp
        header, tokens = spec
        for attempt in range(1, VERDICT_REPAIR_ATTEMPTS + 1):
            self.ctx.tx.event(kind="verdict_repair", step=step_id, agent=agent,
                              role=role, attempt=attempt)
            repair_prompt = (
                "STOP — your previous reply is missing the required machine-readable "
                "verdict, so the orchestrator cannot process it and the entire step "
                "will be wasted.\n\n"
                "Reply with your role's output block and NOTHING before it. Its FIRST "
                f"line must be exactly:\n## {header}\n"
                "and the very next line must be EXACTLY ONE of these tokens, verbatim, "
                "alone on the line:\n"
                f"  {', '.join(tokens)}\n\n"
                "Then continue with the rest of your role's required sections. Do NOT "
                "open with a meta-summary (e.g. \"Investigation complete\") — the "
                "verdict header comes first. Re-answer the ORIGINAL TASK below, using "
                "the analysis/work you already did this session.\n\n"
                "===== ORIGINAL TASK =====\n"
                + (user_prompt or "")[:20000]
                + "\n\n----- YOUR PREVIOUS REPLY (may be empty; reproduce its substance) -----\n"
                + (resp.text or "")[:8000]
            )
            rresp = self.ctx.adapters[agent].run(system, repair_prompt,
                                                 allow_edit=allow_edit, step_id=step_id)
            if rresp.status == OK and extract_verdict(role, rresp.text):
                resp.text = ((resp.text or "").rstrip()
                             + f"\n\n----- verdict repair (attempt {attempt}) -----\n"
                             + rresp.text)
                resp.duration = (resp.duration or 0.0) + (rresp.duration or 0.0)
                resp.usage = _merge_usage(resp.usage, rresp.usage)
                resp.cost_usd = _sum_opt(resp.cost_usd, rresp.cost_usd)
                self.ctx.tx.md(f"\n> ♻ verdict-repair on **{step_id}** ({role}) "
                               f"succeeded on attempt {attempt}.\n")
                return resp
        self.ctx.tx.md(f"\n> ⚠ verdict-repair on **{step_id}** ({role}) exhausted "
                       f"{VERDICT_REPAIR_ATTEMPTS} attempt(s); fail-safe to human.\n")
        return resp

    def _finish(self, status, reason):
        # Gate-quality summary: how often a later gate blocked a diff the FIRST gate had
        # already passed. Written once, before the terminal status, so every dev run
        # carries its own miss count instead of needing a cross-run query to notice a
        # reviewer that has quietly stopped blocking.
        if self.workflow == "development" and not getattr(self, "_miss_reported", False):
            self._miss_reported = True
            misses = getattr(self, "_miss_count", 0)
            on = self._gate_enabled("code_reviewer")
            self.ctx.tx.event(kind="reviewer_miss_summary", misses=misses, code_review_enabled=on)
            self.ctx.tx.md("\n## Gate quality\n\n" + (
                f"Code-Reviewer misses this run: **{misses}** — times a later gate blocked a "
                "diff the Code Reviewer had passed.\n" if on else
                "Code-review gate **disabled** for this run; the Architect was the first gate, "
                "so no miss rate is measured.\n"))
        return self.ctx.tx.finish(status, reason)

    # ---- public entrypoint -------------------------------------------
    def run(self, task, arch_ref=None):
        try:
            self._setup_roster()
            if self.workflow == "architecture":
                return self._run_architecture(task)
            if self.workflow == "investigation":
                return self._run_investigation(task)
            return self._run_development(task, arch_ref)
        except (AgentError, GuardViolation) as e:
            return self._finish(HUMAN_REQUIRED, f"Aborted: {e}")
        except Exception as e:  # noqa: BLE001 - never leave a run without a STATUS
            import traceback
            self.ctx.tx.event(kind="error", error=f"{type(e).__name__}: {e}",
                              traceback=traceback.format_exc())
            return self._finish(HUMAN_REQUIRED,
                                f"Aborted by unexpected error: {type(e).__name__}: {e} "
                                "(see events.jsonl for traceback).")

    # ================= ARCHITECTURE ===================================
    # Chain (developer-centric, two escalation tiers):
    #   Architect (author) authors the proposal; the Developer (Developer Verifier)
    #   reviews EVERY draft as the primary implementability gate. The Developer's
    #   FEASIBLE is the only way to PASS. On an Architect<->Developer deadlock the
    #   Tech Lead is called to arbitrate (tier 1); on an Architect<->Tech-Lead
    #   deadlock the Arbiter is called (tier 2).
    def _arch_author_prompt(self, task, ctx_docs, draft, review, ruling=None, ruling_by=None):
        p = [f"## Task\n{task}", f"## Existing architecture docs (context)\n{ctx_docs}"]
        if draft:
            p.append(f"## Your previous draft\n{draft}")
        if review:
            p.append(f"## Developer review to address\n{review}\n\n"
                     "Apply what you agree with; document disagreements under '## Disagreements'.")
        if ruling:
            p.append(f"## {ruling_by or 'Arbiter'} ruling (binding — apply the smallest safe action)\n{ruling}")
        if not draft:
            p.append("Produce the first architecture proposal draft.")
        return "\n\n".join(p)

    def _arch_devreview_prompt(self, task, draft):
        return (f"## Task (the stated scope you must build within)\n{task}\n\n"
                f"## Architecture proposal you will have to implement\n{draft}\n\n"
                "You are the developer who will BUILD this next — this is the DEVELOPER REVIEW of "
                "the architecture, and you are the primary reviewer. Run your implementation-"
                "feasibility check per your role: is it buildable within the stated scope and a "
                "single dev run, are the interfaces/boundaries/tests clear, does it hide a "
                "migration / API-break / infra / concurrency / runtime-safety cost, and will "
                "you be forced to "
                "invent decisions later? You are READ-ONLY: do not edit any file. You may block on "
                "implementability/scope/testability but must not author or redesign the "
                "architecture. Return your exact output format ending in a verdict "
                "(FEASIBLE / NEEDS_REVISION / HUMAN_REQUIRED).")

    def _arch_techlead_prompt(self, task, draft, dev_review, disagreements):
        return (f"## Task\n{task}\n\n## Architecture proposal under dispute\n{draft}\n\n"
                f"## Developer review (the implementer's blocking feedback)\n{dev_review}\n\n"
                f"## Architect <-> Developer disagreement history\n{''.join(disagreements)}\n\n"
                "The Architect (Codex) and the Developer (the implementer) cannot resolve their "
                "dispute. Act as Tech Lead and ARBITRATE it: weigh proportionality and delivery "
                "reality, decide whose position holds (or a middle path), and issue a concrete, "
                "binding ruling the Architect must apply. Return your verdict: "
                "READY_FOR_HUMAN_REVIEW (your ruling stands — proceed) / DEVELOPER_NEEDS_FIX / "
                "ARCHITECT_REVIEW_NEEDS_REVISION (ruling for the Architect to apply) / "
                "ESCALATE_TO_HUMAN (you cannot arbitrate — defer to the Arbiter).")

    def _arbiter_prompt(self, task, draft, review, disagreements):
        return (f"## Original task\n{task}\n\n## Current architecture draft\n{draft}\n\n"
                f"## Latest feedback\n{review}\n\n"
                f"## Disagreement history (Architect <-> Developer <-> Tech Lead)\n{''.join(disagreements)}\n\n"
                "The Architect (Codex) and the Tech Lead (Gemini) cannot resolve the dispute even "
                "after Tech-Lead arbitration. Arbitrate per your role and return your verdict.")

    def _arch_devreview(self, task, draft, phase, i):
        """Developer review of the architecture (the primary gate). Returns (verdict, text);
        verdict is FEASIBLE / NEEDS_REVISION / HUMAN_REQUIRED / None."""
        wf = self.ctx.workflows["architecture"]
        dv, dvv = self.call("arch.dev_review", wf["developer_verifier"],
                            self._arch_devreview_prompt(task, draft),
                            phase=phase, iteration=i)
        return dvv, dv.text

    def _run_architecture(self, task):
        wf = self.ctx.workflows["architecture"]
        author = wf["author"]
        maxit = wf["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        ctx_docs = self.ctx.read_existing_arch()

        draft = last_review = None
        last_hash = None
        repeat = 0
        disagreements = []

        # Tier 0: Architect <-> Developer review.
        for i in range(1, maxit + 1):
            ap = self._arch_author_prompt(task, ctx_docs, draft, last_review)
            aresp, _ = self.call("arch.author", author, ap, phase="architecture",
                                 iteration=i, parse=False)
            draft = aresp.text
            self.ctx.tx.artifact(f"architecture-draft-{i:02d}.md", draft)

            dvv, dvtext = self._arch_devreview(task, draft, "architecture", i)
            last_review = dvtext
            if dvv == "FEASIBLE":
                self.ctx.tx.md(f"\n> Developer FEASIBLE at iteration {i} → Tech-Lead control review (final gate).\n")
                return self._arch_techlead_control(task, ctx_docs, draft, dvtext, disagreements)

            # NEEDS_REVISION / HUMAN_REQUIRED / unparseable -> Architect revises; deadlock -> Tech Lead.
            disagreements.append(f"### Architecture iteration {i} (Developer: {dvv})\n{dvtext}\n")
            h = _norm_hash(dvtext)
            repeat = repeat + 1 if h == last_hash else 1
            last_hash = h
            if dvv == HUMAN_REQUIRED or repeat > trig:
                self.ctx.tx.md(f"\n> Architect⇄Developer deadlock at iteration {i} → calling the Tech Lead.\n")
                break
        else:
            self.ctx.tx.md("\n> Architecture loop exhausted without FEASIBLE → calling the Tech Lead.\n")

        self.ctx.tx.disagreements("\n".join(disagreements))
        return self._arch_techlead_arbitration(task, ctx_docs, draft, last_review, disagreements)

    def _arch_techlead_control_prompt(self, task, draft, dev_review):
        return (f"## Task\n{task}\n\n"
                f"## Architecture proposal (Developer-Verifier already returned FEASIBLE)\n{draft}\n\n"
                f"## Developer-Verifier feasibility review\n{dev_review}\n\n"
                "This is the FINAL Tech-Lead control review before the proposal passes. Approve "
                "(READY_FOR_HUMAN_REVIEW) ONLY if it is architecturally sound, complete, and "
                "proportionate; otherwise return DEVELOPER_NEEDS_FIX / ARCHITECT_REVIEW_NEEDS_REVISION "
                "with specific blockers, or ESCALATE_TO_HUMAN. Return your exact Tech Lead verdict format.")

    def _author_dev_converge(self, task, ctx_docs, draft, seed_review, ruling, ruling_by,
                             phase, budget, disagreements, label):
        """Inner convergence loop: the Architect revises against a FIXED `ruling` and the
        Developer-Verifier ("глубокий девелопер") re-reviews, looping until the Developer
        returns FEASIBLE, an inner deadlock (the Developer repeats the SAME blocker
        > trigger_after_cycles — the author isn't making progress on it), a Developer
        HUMAN_REQUIRED, or `budget` author↔developer cycles are used. The judge (Tech Lead /
        Arbiter) is NOT called here — that is the caller's job, and only on a returned
        DEADLOCK/EXHAUSTED. This is what keeps the judge from being re-invoked on every
        author edit. Returns (outcome, draft, last_review, used) with outcome in
        {"FEASIBLE", "DEADLOCK", HUMAN_REQUIRED, "EXHAUSTED"}."""
        wf = self.ctx.workflows["architecture"]
        author = wf["author"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        last_review = seed_review
        last_hash = None
        repeat = 0
        used = 0
        for j in range(1, budget + 1):
            used = j
            ap = self._arch_author_prompt(task, ctx_docs, draft, last_review,
                                          ruling=ruling, ruling_by=ruling_by)
            aresp, _ = self.call("arch.author", author, ap, phase=phase, iteration=j, parse=False)
            draft = aresp.text
            self.ctx.tx.artifact(f"architecture-{label}-draft-{j:02d}.md", draft)
            dvv, dvtext = self._arch_devreview(task, draft, phase, j)
            last_review = dvtext
            disagreements.append(f"### {ruling_by} ruling → author revision {j} (Developer: {dvv})\n{dvtext}\n")
            self.ctx.tx.disagreements("\n".join(disagreements))
            if dvv == "FEASIBLE":
                return ("FEASIBLE", draft, dvtext, used)
            if dvv == HUMAN_REQUIRED:
                return (HUMAN_REQUIRED, draft, dvtext, used)
            # Inner deadlock only when the SAME blocker persists; a NEW blocker each cycle
            # is progress and keeps the author<->developer loop going without the judge.
            h = _norm_hash(dvtext)
            repeat = repeat + 1 if h == last_hash else 1
            last_hash = h
            if repeat > trig:
                return ("DEADLOCK", draft, dvtext, used)
        return ("EXHAUSTED", draft, last_review, used)

    def _arch_techlead_control(self, task, ctx_docs, draft, dev_review, disagreements):
        """Tech-Lead final CONTROL gate (Irina 2026-06-19). The Tech Lead reviews a proposal the
        Developer already marked FEASIBLE. READY_FOR_HUMAN_REVIEW -> PASS. If it demands changes,
        the Architect revises and the Developer must RE-REACH FEASIBLE (inner convergence) BEFORE
        the Tech Lead re-reviews — so the Tech Lead is engaged once per FEASIBLE checkpoint, not on
        every author edit. Inner deadlock / repeated identical Tech-Lead ruling / ESCALATE /
        unparseable -> Arbiter (tier 2). (Flow fixed 2026-07-09, Irina: judge no longer re-invoked
        every cycle; one ruling → author↔dev converge.)"""
        wf = self.ctx.workflows["architecture"]
        tech_lead = wf["tech_lead"]
        budget = wf["arbitration"]["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        last_review = dev_review
        last_hash = None
        repeat = 0
        round_no = 0

        while budget > 0:
            round_no += 1
            tl, tlv = self.call("arch.tech_lead", tech_lead,
                                self._arch_techlead_control_prompt(task, draft, last_review),
                                phase="techlead-control", iteration=round_no)
            if tlv == "READY_FOR_HUMAN_REVIEW":
                self.ctx.tx.artifact("architecture-final.md", draft)
                return self._finish(PASS, f"Tech-Lead control approved the FEASIBLE proposal at round {round_no}.")
            disagreements.append(f"### Tech-Lead control {round_no} (Tech Lead: {tlv})\n{tl.text}\n")
            self.ctx.tx.disagreements("\n".join(disagreements))
            if tlv == "ESCALATE_TO_HUMAN" or tlv is None:
                self.ctx.tx.md(f"\n> Tech-Lead control escalated at round {round_no} → calling the Arbiter.\n")
                return self._arch_arbiter(task, ctx_docs, draft, tl.text, disagreements)
            # A Tech-Lead ruling that repeats verbatim without landing -> Arbiter.
            h = _norm_hash(tl.text)
            repeat = repeat + 1 if h == last_hash else 1
            last_hash = h
            if repeat > trig:
                self.ctx.tx.md(f"\n> Tech-Lead control ruling repeats at round {round_no} → calling the Arbiter.\n")
                return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)
            # Tech Lead demands changes -> Architect revises until the Developer RE-REACHES FEASIBLE.
            outcome, draft, last_review, used = self._author_dev_converge(
                task, ctx_docs, draft, last_review, ruling=tl.text,
                ruling_by="Tech Lead (control review)", phase="techlead-control",
                budget=budget, disagreements=disagreements, label=f"tlc-r{round_no}")
            budget -= used
            if outcome == "FEASIBLE":
                continue  # Developer FEASIBLE again -> Tech Lead re-reviews the revised draft
            if outcome == HUMAN_REQUIRED:
                return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)
            # DEADLOCK / EXHAUSTED on this ruling -> Arbiter (tier 2).
            self.ctx.tx.md(f"\n> Developer could not re-reach FEASIBLE under Tech-Lead control ({outcome}) at round {round_no} → Arbiter.\n")
            return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)
        return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)

    def _arch_techlead_arbitration(self, task, ctx_docs, draft, last_review, disagreements):
        """Tier 1: the Tech Lead arbitrates an Architect<->Developer deadlock. It issues ONE
        ruling; the Architect then revises and the Developer re-reviews in the inner
        convergence loop (_author_dev_converge) until FEASIBLE or the inner loop deadlocks —
        the Tech Lead is NOT re-invoked on every author edit. Only when the inner loop cannot
        converge does the Tech Lead re-rule (informed by the persistent objection); a ruling
        that repeats verbatim, or shared-budget exhaustion, escalates to the Arbiter (tier 2).
        (Flow fixed 2026-07-09, Irina: one ruling → author↔dev converge, judge only on deadlock.)"""
        wf = self.ctx.workflows["architecture"]
        tech_lead = wf["tech_lead"]
        budget = wf["arbitration"]["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        last_hash = None
        repeat = 0
        round_no = 0

        while budget > 0:
            round_no += 1
            tl, tlv = self.call("arch.tech_lead", tech_lead,
                                self._arch_techlead_prompt(task, draft, last_review, disagreements),
                                phase="arbitration", iteration=round_no)
            if tlv == "ESCALATE_TO_HUMAN" or tlv is None:
                self.ctx.tx.md(f"\n> Tech Lead could not arbitrate at round {round_no} → calling the Arbiter.\n")
                disagreements.append(f"### Tech-Lead arbitration {round_no} (Tech Lead: {tlv})\n{tl.text}\n")
                self.ctx.tx.disagreements("\n".join(disagreements))
                return self._arch_arbiter(task, ctx_docs, draft, tl.text, disagreements)
            disagreements.append(f"### Tech-Lead arbitration {round_no} (ruling)\n{tl.text}\n")
            self.ctx.tx.disagreements("\n".join(disagreements))
            # A ruling that repeats verbatim never lands -> escalate to the Arbiter.
            h = _norm_hash(tl.text)
            repeat = repeat + 1 if h == last_hash else 1
            last_hash = h
            if repeat > trig:
                self.ctx.tx.md(f"\n> Architect⇄Tech-Lead deadlock (ruling repeats) at round {round_no} → calling the Arbiter.\n")
                return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)
            # Architect applies the ruling; Developer re-reviews in the inner convergence loop.
            outcome, draft, last_review, used = self._author_dev_converge(
                task, ctx_docs, draft, last_review, ruling=tl.text, ruling_by="Tech Lead",
                phase="arbitration", budget=budget, disagreements=disagreements, label=f"tl-r{round_no}")
            budget -= used
            if outcome == "FEASIBLE":
                self.ctx.tx.artifact("architecture-final.md", draft)
                return self._finish(PASS, f"Developer FEASIBLE after Tech-Lead arbitration (round {round_no}).")
            if outcome == HUMAN_REQUIRED:
                return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)
            # DEADLOCK / EXHAUSTED on this ruling -> loop: the Tech Lead re-rules with the
            # persistent objection in hand, until the ruling repeats or the budget runs out.
        return self._arch_arbiter(task, ctx_docs, draft, last_review, disagreements)

    def _arch_arbiter(self, task, ctx_docs, draft, last_review, disagreements):
        """Tier 2: the Arbiter rules on an Architect<->Tech-Lead deadlock. It issues ONE ruling;
        the Architect revises and the Developer re-reviews in the inner convergence loop until
        FEASIBLE — the Arbiter is not re-invoked on every author edit. A returned DEADLOCK lets
        the Arbiter re-rule (informed by the persistent objection); a ruling that repeats
        verbatim, or shared-budget exhaustion, ends the run HUMAN_REQUIRED. FEASIBLE is the only
        PASS. (Flow fixed 2026-07-09, Irina: one ruling → author↔dev converge.)"""
        wf = self.ctx.workflows["architecture"]
        arbiter = wf["arbiter"]
        budget = wf["arbitration"]["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        last_hash = None
        repeat = 0
        round_no = 0

        while budget > 0:
            round_no += 1
            ares, averdict = self.call("arch.arbiter", arbiter,
                                       self._arbiter_prompt(task, draft, last_review, disagreements),
                                       phase="arbitration", iteration=round_no)
            if averdict in ARB_STOP:
                return self._finish(HUMAN_REQUIRED,
                                    f"Arbiter returned {averdict} at arbiter round {round_no}.")
            disagreements.append(f"### Arbiter round {round_no} (ruling)\n{ares.text}\n")
            self.ctx.tx.disagreements("\n".join(disagreements))
            h = _norm_hash(ares.text)
            repeat = repeat + 1 if h == last_hash else 1
            last_hash = h
            if repeat > trig:
                return self._finish(HUMAN_REQUIRED,
                                    f"Arbiter ruling repeats without the Developer reaching FEASIBLE (round {round_no}).")
            outcome, draft, last_review, used = self._author_dev_converge(
                task, ctx_docs, draft, last_review, ruling=ares.text, ruling_by="Arbiter",
                phase="arbitration", budget=budget, disagreements=disagreements, label=f"arb-r{round_no}")
            budget -= used
            if outcome == "FEASIBLE":
                self.ctx.tx.artifact("architecture-final.md", draft)
                return self._finish(PASS, f"Developer FEASIBLE after Arbiter ruling (round {round_no}).")
            # DEADLOCK / EXHAUSTED / HUMAN_REQUIRED -> loop: Arbiter re-rules until it repeats
            # or the shared budget runs out, then HUMAN_REQUIRED.
        return self._finish(HUMAN_REQUIRED, "Arbiter exhausted without the Developer reaching FEASIBLE.")

    # ================= DEVELOPMENT ====================================
    _SCOPE_FENCE = (
        "## Scope discipline (mandatory)\n"
        "- Do ONLY what THIS task asks. Do not implement other phases, future work, or "
        "unrelated improvements, even if you notice them.\n"
        "- Touch ONLY the files this task requires. Do not edit files belonging to a "
        "different phase/task and do not refactor unrelated modules.\n"
        "- If finishing the task appears to need out-of-scope changes (another phase's "
        "files, a new dependency, an architecture change), STOP and report it in your "
        "summary rather than doing it; raise BLOCKED_BY_ARCHITECTURE_QUESTION if it is an "
        "architecture decision.\n"
        "- Any out-of-scope or unrelated edit will be rejected in review and wastes a cycle."
    )

    def _materialize_arch(self, arch_ref):
        """Persist the approved architecture ONCE to the run's artifacts and return its
        absolute path, so the Developer + arch/tech-lead reviewers READ it on demand
        (a pointer) instead of carrying the full text re-inlined on every iteration.
        Returns None when no architecture was supplied."""
        if not arch_ref or not arch_ref.strip():
            return None
        self.ctx.tx.artifact("approved-architecture.md", arch_ref)
        return str((self.ctx.tx.dir / "artifacts" / "approved-architecture.md").resolve())

    def _dev_prompt(self, task, arch_path, instruction, i, arbitration=False):
        arch = (f"Read it at: `{arch_path}` — read the sections your change touches. It is "
                "intentionally NOT inlined here (context economy); open the file."
                if arch_path else "(none provided)")
        p = [f"## Task\n{task}",
             self.ctx.repo_scope_prompt(),
             f"## Approved architecture\n{arch}",
             "## Build/test cadence\nRun the FULL build/test suite as a CLOSURE check once you "
             "believe the change is complete — NOT on every intermediate iteration; iterate with "
             "focused, cheap checks meanwhile. (Reviewers read the diff each cycle regardless.)"]
        if instruction:
            label = "Arbiter ruling — apply only the smallest safe valid fix" if arbitration \
                else "Review feedback — fix only valid blockers, nothing else"
            p.append(f"## {label}\n{instruction}")
        else:
            p.append("Implement the task with minimal, focused, production-quality changes.")
        p.append(self._SCOPE_FENCE)
        return "\n\n".join(p)

    # How much prior-gate history a re-review carries. Bounded so the reviewer prompt
    # cannot grow without limit across a long run: only the LAST few blockers matter
    # for deciding whether this iteration's diff actually fixed them.
    _REVIEW_HISTORY_ENTRIES = 3
    _REVIEW_HISTORY_CHARS = 12000

    def _review_history_block(self, history):
        """Render the blockers earlier gates raised in THIS run, newest last.

        WHY (Irina 2026-08-09): the code reviewer used to get a fresh prompt with no
        memory of what the Architect/Tech Lead had blocked on, so on iteration >= 2 it
        re-reviewed a diff whose contentious part it had already passed and passed it
        again — 32 of 33 re-reviews after an Architect NEEDS_FIX were PASS. Carrying the
        prior blockers makes verifying the fix part of the review instead of a re-run.
        """
        if not history:
            return ""
        tail = history[-self._REVIEW_HISTORY_ENTRIES:]
        body = "\n".join(tail)[-self._REVIEW_HISTORY_CHARS:]
        return ("## Blockers raised earlier in THIS run (by any gate)\n"
                "The developer was told to fix these. Part of your review is to confirm each one is "
                "ACTUALLY fixed in the diff below — a blocker that was papered over, or 'fixed' in "
                "the summary but not in the code, is itself a blocking issue. If a previous gate "
                "blocked on something you had passed, treat that as a signal that your bar was too "
                "low, not as a reason to defer.\n\n"
                f"{body}\n\n")

    def _review_prompt(self, task, diff, dev_summary, kind, arch_path=None, history=None):
        lens = ("as Code Reviewer (correctness, regressions, tests, security)"
                if kind == "code" else
                "as Architect (architecture impact, boundaries, ADR need)")
        # Both reviewers get the approved architecture. The code reviewer used to be
        # denied it, which made every "the diff does not do what the approved design
        # mandates" defect structurally invisible to the FIRST gate and left it for the
        # Architect (Irina 2026-08-09).
        arch_block = ""
        if arch_path:
            judge = ("Judge the diff against it — open the sections the change touches."
                     if kind == "arch" else
                     "Open the sections the change touches: a diff that does not implement what the "
                     "approved design mandates is a BLOCKING issue for you, not only for the Architect.")
            arch_block = f"## Approved architecture (read at `{arch_path}`)\n{judge}\n\n"
        return (f"## Task\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"{arch_block}"
                f"{self._review_history_block(history)}"
                f"## Developer summary\n{dev_summary}\n\n"
                f"## Implementation diff\n```diff\n{diff}\n```\n\n"
                f"Review ONLY the diff above {lens}. The diff already contains exactly the repos in "
                "scope; a change WITHIN a listed READ-ONLY repo is a blocking scope violation. Do "
                "NOT inspect or flag anything outside the listed repos — other repositories or "
                "directories that coexist in the workspace belong to separate work and are not your "
                "concern. Return your exact output format with a verdict.")

    def _techlead_prompt(self, task, diff, dev_summary, code_review, arch_review, arch_path=None):
        arch_block = f"## Approved architecture (read at `{arch_path}`)\n\n" if arch_path else ""
        # Name the agent that actually wrote each review. Both headers said "(Codex)",
        # which misattributed the Claude code review to the architect's vendor.
        wf = self.ctx.workflows["development"]
        wf_agents = (wf["code_reviewer"].get("agent") or "code reviewer",
                     wf["architect"].get("agent") or "architect")
        # With the code-review gate disabled, the Tech Lead is the ONLY reviewer after the
        # Architect — say so, so it does not assume a peer code review already happened.
        if code_review is None:
            cr_block = ("## Code Reviewer review\n(The code-review gate is disabled for this run. "
                        "No peer code review was performed — do not assume one. You are the last "
                        "gate before human review, and the only one after the Architect.)\n\n")
            closing = ("Review the Architect's review AND the diff itself per your Tech Lead role. "
                       "Because no code review ran, implementation-level risk (correctness, "
                       "regressions, concurrency, test strength) is yours to catch as well. "
                       "Validate proportionality, confirm/downgrade blockers, catch missed risks, "
                       "and return your verdict.")
        else:
            cr_block = f"## Code Reviewer ({wf_agents[0]}) review\n{code_review}\n\n"
            closing = ("Review the reviews per your Tech Lead role. Validate proportionality, "
                       "confirm/downgrade blockers, catch missed risks, and return your verdict.")
        return (f"## Task\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"{arch_block}"
                f"## Developer summary\n{dev_summary}\n\n"
                f"## Implementation diff\n```diff\n{diff}\n```\n\n"
                f"{cr_block}"
                f"## Architect ({wf_agents[1]}) review\n{arch_review}\n\n"
                + closing)

    def _dev_arbiter_prompt(self, task, diff, dev_summary, disagreements):
        return (f"## Original task\n{task}\n\n## Current diff\n```diff\n{diff}\n```\n\n"
                f"## Developer summary\n{dev_summary}\n\n"
                f"## Disagreement history\n{''.join(disagreements)}\n\n"
                "Developer, reviewers, and Tech Lead are stuck. Arbitrate and return your verdict.")

    def _arch_question_prompt(self, task, dev_summary, diff, arch_path=None):
        arch_block = f"## Approved architecture (read at `{arch_path}`)\n\n" if arch_path else ""
        return (f"## Task\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"{arch_block}"
                f"## The developer hit an architecture question / blocker mid-implementation\n{dev_summary}\n\n"
                f"## Work in progress (diff so far)\n```diff\n{diff}\n```\n\n"
                "Answer the developer's architecture question as the System Architect: give a clear, "
                "actionable decision and the minimal next step so implementation can continue. Resolve it "
                "within the approved architecture and the task scope; do not expand scope. If it genuinely "
                "cannot be decided at the architecture level, return ESCALATE_TO_TECH_LEAD. Return your "
                "exact Architect verdict format.")

    def _gate_enabled(self, cell) -> bool:
        """Is this review cell wired in for this run? A cell with "enabled": false in
        workflows.json is never invoked; the next stage becomes the first gate. The cell
        stays in the roster block on purpose — dropping it from roster.cells would shift
        the positional chain and silently reassign the cells after it."""
        spec = self.ctx.workflows[self.workflow].get(cell)
        return bool(isinstance(spec, dict) and spec.get("enabled", True))

    def _reviewer_miss(self, gate, verdict, blocker, i, phase):
        """Record that a LATER gate blocked on a diff the code reviewer had just passed.

        The code reviewer is stage 1 of 3; when it passes and the Architect or Tech Lead
        then blocks, that is a miss by the first gate — the defect was in the same diff it
        just read. Nothing measured this before, which is why a reviewer that had stopped
        blocking (2 % NEEDS_FIX in August vs a 63 % Architect block rate on the same diffs)
        went unnoticed for weeks. Logged to events.jsonl so miss rate is queryable per run
        and across runs; surfaced in the transcript so it is visible while the run is live.
        """
        self._miss_count = getattr(self, "_miss_count", 0) + 1
        self.ctx.tx.event(kind="reviewer_miss", gate=gate, gate_verdict=verdict,
                          iteration=i, phase=phase,
                          code_reviewer=self.ctx.workflows["development"]["code_reviewer"].get("agent"),
                          blocker=(blocker or "")[:2000])
        self.ctx.tx.md(f"\n> ⚠ **reviewer miss** — `{gate}` returned {verdict} at iteration {i} "
                       "on a diff the Code Reviewer had just PASSED.\n")

    def _dev_staged_review(self, task, diff, dev_summary, phase, i, arch_path=None, history=None):
        """Staged review gate (Irina 2026-06-22): Code Review (the peer 'second developer') runs FIRST;
        only when it PASSES does the Architect review run; only when the Architect passes does the Tech
        Lead (final gate) run. A failing earlier stage short-circuits, so later reviewers never see code
        an earlier gate already rejected. Returns (verdict, feedback, gate).

        `history` carries the blockers earlier gates raised in this run, so a re-review verifies
        the fix instead of re-reading the diff blind. Every later-gate block after a code-review
        PASS is logged as a reviewer_miss (Irina 2026-08-09)."""
        wf = self.ctx.workflows["development"]
        # Stage 1 — Code Review (second developer). Skippable: a cell with
        # "enabled": false is not invoked and the next stage becomes the first gate.
        # Disabling here does NOT touch the roster, so the cell's agent stays reserved
        # and the positional chain for architect/tech_lead/arbiter is unchanged.
        cr_text = None
        if self._gate_enabled("code_reviewer"):
            cr, crv = self.call("dev.code_review", wf["code_reviewer"],
                                self._review_prompt(task, diff, dev_summary, "code", arch_path, history),
                                phase=phase, iteration=i)
            if crv != "PASS":
                return "DEVELOPER_NEEDS_FIX", (
                    f"## Code Reviewer verdict: {crv}\n{cr.text}\n\n"
                    "Fix these blocking issues; architecture review runs once code review passes."), "code-review"
            cr_text = cr.text
        # Stage 2 — Architect (first gate when code review is disabled).
        ar, arv = self.call("dev.architect", wf["architect"],
                            self._review_prompt(task, diff, dev_summary, "arch", arch_path, history),
                            phase=phase, iteration=i)
        if arv not in ("PASS", "NOT_APPLICABLE", "ESCALATE_TO_TECH_LEAD"):
            # Only a miss if a gate actually ran before this one and let the diff through.
            if cr_text is not None:
                self._reviewer_miss("architect", arv, ar.text, i, phase)
            return "DEVELOPER_NEEDS_FIX", (
                f"## Architect verdict: {arv}\n{ar.text}\n\n"
                "Fix these architecture blockers; the Tech Lead final review runs once the Architect passes."), "architect"
        # Stage 3 — Tech Lead (final gate, only after the Architect passes).
        tl, tlv = self.call("dev.tech_lead", wf["tech_lead"],
                            self._techlead_prompt(task, diff, dev_summary, cr_text, ar.text, arch_path),
                            phase=phase, iteration=i)
        if tlv == READY:
            return READY, tl.text, "tech-lead"
        if tlv == "ESCALATE_TO_HUMAN" or tlv is None:
            return "ESCALATE_TO_HUMAN", tl.text, "tech-lead"
        if cr_text is not None:
            self._reviewer_miss("tech-lead", tlv, tl.text, i, phase)
        return "DEVELOPER_NEEDS_FIX", f"## Tech Lead verdict: {tlv}\n{tl.text}", "tech-lead"

    def _dev_break_deadlock(self, task, diff, dev_summary, disagreements, i):
        """Call the Arbiter ONCE to break a genuine deadlock (only reached when the same blocker
        repeats > trigger_after_cycles, or an architecture question keeps recurring). Returns a
        developer instruction carrying the arbiter's ruling, or None if a human must decide."""
        wf = self.ctx.workflows["development"]
        ares, averdict = self.call("dev.arbiter", wf["arbiter"],
                                   self._dev_arbiter_prompt(task, diff, dev_summary, disagreements),
                                   phase="arbitration", iteration=i)
        disagreements.append(f"### Arbiter ruling at iteration {i} ({averdict})\n{ares.text}\n")
        self.ctx.tx.disagreements("\n".join(disagreements))
        if averdict in ARB_STOP:
            return None
        return ("## Arbiter ruling (deadlock broken) — apply only the smallest safe valid fix, "
                f"then continue\n{ares.text}")

    def _run_development(self, task, arch_ref):
        """Dev workflow (Irina 2026-06-22): one loop of developer + STAGED review
        (code-review → architect → tech-lead). The Architect answers any architecture question the
        developer raises mid-implementation. The Arbiter is called ONLY to break a genuine deadlock
        (the same blocker repeats > trigger_after_cycles, or a recurring question)."""
        wf = self.ctx.workflows["development"]
        developer, architect = wf["developer"], wf["architect"]
        maxit = wf["max_iterations"] + wf["arbitration"]["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]
        arch_path = self._materialize_arch(arch_ref)

        self._dev_repeat, self._dev_last_sig = 0, None
        instruction, disagreements, diff, dev_summary = None, [], "", ""
        # Blockers raised by any gate so far, replayed to the reviewers on the next
        # iteration so a re-review verifies the fix instead of re-reading blind.
        review_history = []

        for i in range(1, maxit + 1):
            dresp, dverdict = self.call("dev.developer", developer,
                                        self._dev_prompt(task, arch_path, instruction, i),
                                        phase="development", iteration=i, allow_edit=True)
            dev_summary = dresp.text
            diff = self.ctx.capture_diff()
            self.ctx.tx.artifact(f"diff-{i:02d}.patch", diff)

            if dverdict == "BLOCKED_BY_ARCHITECTURE_QUESTION":
                # Architecture question mid-implementation -> the ARCHITECT answers (not the arbiter).
                instruction, stop = self._dev_answer_arch_question(
                    task, architect, dev_summary, diff, arch_path, disagreements, i, trig)
                self._dev_repeat, self._dev_last_sig = 0, None
                if stop:
                    return self._finish(HUMAN_REQUIRED,
                                        f"Architecture question needs a human decision (iteration {i}).")
                continue

            # Staged review: code-review -> architect -> tech-lead.
            verdict, feedback, gate = self._dev_staged_review(task, diff, dev_summary, "development", i,
                                                             arch_path, review_history)
            if verdict == READY:
                return self._finish(READY, f"Tech Lead returned READY_FOR_HUMAN_REVIEW at iteration {i}.")
            if verdict == "ESCALATE_TO_HUMAN":
                return self._finish(HUMAN_REQUIRED, f"Tech Lead escalated to human at iteration {i}.")

            review_history.append(f"### Iteration {i} — blocked by {gate}\n{feedback}\n")
            instruction, stop = self._dev_handle_blocker(gate, feedback, task, diff, dev_summary, disagreements, i, trig)
            if stop:
                return self._finish(HUMAN_REQUIRED, f"Arbiter stopped the run at iteration {i} (human decision needed).")

        return self._finish(HUMAN_REQUIRED, f"Development did not reach READY within {maxit} iterations.")

    def _dev_handle_blocker(self, gate, feedback, task, diff, dev_summary, disagreements, i, trig):
        """Record a gate's NEEDS_FIX, track repetition, and return the next developer instruction —
        or, on a genuine deadlock (the same blocker repeats > trig), call the Arbiter ONCE to break it.
        Returns (instruction, stop); stop=True => a human must decide."""
        disagreements.append(f"### Dev i{i} ({gate} NEEDS_FIX)\n{feedback}\n")
        self.ctx.tx.disagreements("\n".join(disagreements))
        sig = (gate, _norm_hash(feedback))
        self._dev_repeat = self._dev_repeat + 1 if sig == self._dev_last_sig else 1
        self._dev_last_sig = sig
        if self._dev_repeat > trig:
            self.ctx.tx.md(f"\n> Same {gate} blocker repeated {self._dev_repeat}× at iteration {i} → arbiter breaks the deadlock.\n")
            ruling = self._dev_break_deadlock(task, diff, dev_summary, disagreements, i)
            if ruling is None:
                return None, True
            self._dev_repeat, self._dev_last_sig = 0, None
            return ruling, False
        return feedback, False

    def _dev_answer_arch_question(self, task, architect, dev_summary, diff, arch_path, disagreements, i, trig):
        """The Architect answers a developer architecture question. Returns (instruction, stop):
        instruction = guidance to feed the developer next (the Architect's answer, or the Arbiter's
        ruling if it had to break a deadlock); stop = True means a human must decide."""
        aresp, av = self.call("dev.architect", architect,
                              self._arch_question_prompt(task, dev_summary, diff, arch_path),
                              phase="development", iteration=i)
        disagreements.append(
            f"### Dev i{i}: architecture question → Architect\n{dev_summary}\n--- answer ---\n{aresp.text}\n")
        self.ctx.tx.disagreements("\n".join(disagreements))
        # Architect cannot settle it, or the same question keeps recurring -> arbiter (deadlock).
        recurring = sum(1 for d in disagreements if "architecture question" in d) > trig
        if av == "ESCALATE_TO_TECH_LEAD" or recurring:
            self.ctx.tx.md(f"\n> Architecture question unresolved at iteration {i} → arbiter.\n")
            ruling = self._dev_break_deadlock(task, diff, dev_summary, disagreements, i)
            return ruling, (ruling is None)
        return (f"## Architect's answer to your architecture question\n{aresp.text}\n\n"
                "Apply this guidance and continue the implementation."), False

    # ================= INVESTIGATION ==================================
    # Investigator (Claude) reads all five repos on their named branches, reads
    # the named servers' logs, and produces falsifiable hypotheses. The Refuter
    # (Gemini) attacks every cycle; only when it cannot break a hypothesis does
    # the independent Verifier (Codex) — the final gate — attack it from a fresh
    # angle. The Arbiter is invoked only on escalation/deadlock. The
    # Investigator is the ONLY role that may edit/build, and only in the writable
    # repos; that scope is fenced by the prompt and surfaced to the skeptics via
    # the captured debug diff (a read-only-repo change is a disqualifying edit).
    _INV_FINAL_ARTIFACT = "root-cause-final.md"
    _INV_SCOPE_FENCE = (
        "## Investigation scope discipline (mandatory)\n"
        "- Read and analyse ALL five repos on the branches named in the problem; presume NO "
        "project innocent and excuse none — including a contract mismatch BETWEEN repos.\n"
        "- You may EDIT and BUILD only in the WRITABLE repos, "
        "and only as temporary, clearly-marked debug instrumentation to confirm or kill a "
        "hypothesis. Never edit a read-only repo, not even "
        "'to test' — any such change is a disqualifying scope violation.\n"
        "- Your deliverable is a PROVEN, falsifiable hypothesis, not a shipped fix and not a "
        "timing workaround (no sleep/After=/PartOf=/retry to paper over a race — name the "
        "code-level race: threads/lcores, shared state, missing lock or ordering).\n"
        "- Every hypothesis needs a complete cited chain (symptom log → repo/path:line on the "
        "stated branch → mechanism → falsifier) and a per-repo guilty/cleared verdict."
    )

    def _inv_investigator_prompt(self, task, instruction, arbitration=False):
        p = [f"## Problem description (names the affected repos, their branches, and the "
             f"hosts/servers — NOT the log paths, which you derive from the repos)\n{task}",
             self.ctx.repo_scope_prompt()]
        if instruction:
            label = ("Arbiter ruling — act on the smallest evidence-gathering step it names"
                     if arbitration else
                     "Skeptic feedback to address — fix the chain, gather the demanded evidence, "
                     "or DROP the broken hypothesis and propose a better one")
            p.append(f"## {label}\n{instruction}")
        else:
            p.append("Verify each repo is on its named branch; derive each service's log "
                     "location / journald unit from the repos themselves (service unit files, "
                     "logging/ConfD config, code, devtools collectors) — the problem gives you "
                     "the hosts, not the paths; then read those logs on the named hosts, "
                     "correlate them with the code, and produce your ranked, falsifiable "
                     "root-cause hypotheses.")
        p.append(self._INV_SCOPE_FENCE)
        return "\n\n".join(p)

    def _inv_refuter_prompt(self, task, hypothesis, debug_diff, is_no_defect=False):
        if is_no_defect:
            task_line = (
                "The Investigator concluded **NO_DEFECT_FOUND** — that the writable repos "
                "contain no defect and the cause is upstream / "
                "out of scope. Your job is to REFUTE that conclusion: prove a defect DOES exist in "
                "a writable repo, or that it was CLEARED on inadequate evidence — e.g. a cumulative "
                "counter read as a single snapshot instead of a delta over the symptom window, a "
                "drop/branch never instrumented, an upstream cause asserted without a time-correlated "
                "delta, or a writable repo exonerated by 'looks right'. REFUTED = you found such a "
                "gap (the no-defect does NOT hold). NOT_REFUTED = you genuinely tried and the "
                "no-defect conclusion stands. Return REFUTED / NOT_REFUTED / INSUFFICIENT_EVIDENCE.")
        else:
            task_line = (
                "Attack every hypothesis per your role. Re-verify each cited repo/path:line on the "
                "stated branch and re-check both biases (home-team excuse / read-only scapegoat). "
                "Flag any edit to a read-only repo as a disqualifying scope violation. Return your "
                "exact output format ending in REFUTED / NOT_REFUTED / INSUFFICIENT_EVIDENCE.")
        header = ("Investigator's NO-DEFECT conclusion to refute" if is_no_defect
                  else "Investigator's hypotheses and evidence chain")
        return (f"## Problem description\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"## {header}\n{hypothesis}\n\n"
                f"## Investigator's debug changes so far (must be confined to the writable repos)\n"
                f"```diff\n{debug_diff}\n```\n\n" + task_line)

    def _inv_verifier_prompt(self, task, hypothesis, debug_diff, refuter_text, is_no_defect=False):
        if is_no_defect:
            head = "NO-DEFECT conclusion the Refuter could not refute"
            task_line = (
                "Independently try to prove a DEFECT EXISTS in a writable repo, or that the "
                "no-defect rests on a blind spot the Investigator and Refuter share (a cumulative "
                "counter not delta-correlated, an uninstrumented path, an upstream claim without a "
                "time-aligned delta). CONFIRMED = no defect genuinely holds; REFUTED = a defect "
                "exists / it was cleared prematurely; NEEDS_ARBITRATION = honest impasse.")
        else:
            head = "Hypothesis the Refuter could not break"
            task_line = (
                "Independently re-derive the chain from the raw evidence, then run an attack the "
                "Refuter did not, hunting the blind spot the Investigator and Refuter may share. "
                "Return CONFIRMED / REFUTED / NEEDS_ARBITRATION.")
        return (f"## Problem description\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"## {head}\n{hypothesis}\n\n"
                f"## The Refuter's attacks — do NOT merely repeat them; find a new one\n"
                f"{refuter_text}\n\n"
                f"## Investigator's debug changes (must be confined to the writable repos)\n"
                f"```diff\n{debug_diff}\n```\n\n" + task_line)

    def _inv_arbiter_prompt(self, task, hypothesis, disagreements):
        return (f"## Problem description\n{task}\n\n## {self.ctx.repo_scope_line()}\n\n"
                f"## Current hypothesis under dispute\n{hypothesis}\n\n"
                f"## Disagreement history (Investigator <-> Refuter <-> Verifier)\n"
                f"{''.join(disagreements)}\n\n"
                "The investigation loop cannot converge. Rule on whether this root-cause "
                "hypothesis stands on the evidence, per your role, and return your verdict.")

    def _inv_investigator_terminal(self, iverdict, i, phase):
        """Map a non-actionable Investigator verdict to a terminal status, or None
        to continue to the adversarial gate. NOTE: NO_DEFECT_FOUND is NOT terminal
        here — it must still pass the Refuter/Verifier (who try to prove a defect
        DOES exist) before it can finish, so the Investigator cannot quietly clear
        the writable repos and bypass the team. Only 'blocked' and unparseable
        verdicts fail safe to a human."""
        if iverdict == "BLOCKED_NEED_MORE_DATA":
            return self._finish(HUMAN_REQUIRED,
                                f"Investigator is BLOCKED_NEED_MORE_DATA at {phase} iteration {i} "
                                "(needs server access / missing logs / an unbuildable branch).")
        if iverdict == ROOT_CAUSE_ESTABLISHED:
            # The Investigator self-declared the final verdict — but it cannot
            # self-certify. Route it through the Refuter/Verifier gate exactly as a
            # HYPOTHESIS_READY_FOR_REFUTATION handoff; the engine alone owns the
            # ROOT_CAUSE_ESTABLISHED terminal, and only after the skeptics agree.
            self.ctx.tx.md(f"\n> Investigator emitted ROOT_CAUSE_ESTABLISHED at {phase} "
                           f"iteration {i}; it cannot self-certify — routing the hypothesis "
                           "through the Refuter/Verifier gate.\n")
            return None
        if iverdict not in ("HYPOTHESIS_READY_FOR_REFUTATION", NO_DEFECT_FOUND):
            return self._finish(HUMAN_REQUIRED,
                                f"Investigator returned no parseable verdict at {phase} "
                                f"iteration {i} (fail-safe to human).")
        return None

    def _inv_gate(self, task, hypothesis, debug_diff, phase, i, is_no_defect=False):
        """Run the refutation gate. Returns (verdict, feedback, ran_verifier).

        The Refuter (Gemini) attacks every cycle. The Adversarial Verifier (Codex)
        is the SECOND, independent skeptic and runs ONLY once the Refuter returns
        NOT_REFUTED — mirroring the dev workflow's Tech-Lead final gate, so the
        Verifier never reacts to a hypothesis the Refuter already broke.

        When is_no_defect, the gate is attacking a NO_DEFECT_FOUND conclusion: the
        skeptics try to prove a defect DOES exist. A CONFIRMED here means the
        no-defect genuinely holds; a REFUTED means a defect was found / the writable
        repos were cleared prematurely, and the Investigator must keep digging.
        """
        wf = self.ctx.workflows["investigation"]
        rr, rv = self.call("inv.refuter", wf["refuter"],
                           self._inv_refuter_prompt(task, hypothesis, debug_diff, is_no_defect),
                           phase=phase, iteration=i)
        if rv != "NOT_REFUTED":
            # REFUTED / INSUFFICIENT_EVIDENCE / unparseable(None) -> back to the Investigator.
            return rv, rr.text, False
        vr, vv = self.call("inv.verifier", wf["verifier"],
                           self._inv_verifier_prompt(task, hypothesis, debug_diff, rr.text, is_no_defect),
                           phase=phase, iteration=i)
        return vv, vr.text, True

    def _run_investigation(self, task):
        wf = self.ctx.workflows["investigation"]
        investigator = wf["investigator"]
        maxit = wf["max_iterations"]
        trig = wf["arbitration"]["trigger_after_cycles"]

        instruction = None
        last_sig = None
        repeat = 0
        disagreements = []
        hypothesis = ""
        debug_diff = ""
        is_no_defect = False

        for i in range(1, maxit + 1):
            iresp, iverdict = self.call("inv.investigator", investigator,
                                        self._inv_investigator_prompt(task, instruction),
                                        phase="investigation", iteration=i, allow_edit=True)
            hypothesis = iresp.text
            debug_diff = self.ctx.capture_diff()
            self.ctx.tx.artifact(f"debug-diff-{i:02d}.patch", debug_diff)
            term = self._inv_investigator_terminal(iverdict, i, "investigation")
            if term:
                return term
            is_no_defect = (iverdict == NO_DEFECT_FOUND)

            verdict, feedback, ran_v = self._inv_gate(task, hypothesis, debug_diff,
                                                      "investigation", i, is_no_defect)
            if verdict == "CONFIRMED":
                if is_no_defect:
                    return self._finish(NO_DEFECT_FOUND,
                                        f"NO_DEFECT_FOUND validated — Refuter and Verifier could "
                                        f"not show a defect in the writable repos at iteration {i}.")
                self.ctx.tx.artifact(self._INV_FINAL_ARTIFACT, hypothesis)
                return self._finish(ROOT_CAUSE_ESTABLISHED,
                                    f"Verifier CONFIRMED the hypothesis at iteration {i}.")
            if verdict == "NEEDS_ARBITRATION":
                self.ctx.tx.md(f"\n> Verifier escalated at iteration {i} → arbitration.\n")
                disagreements.append(
                    f"### Investigation iteration {i} (Verifier: NEEDS_ARBITRATION)\n{feedback}\n")
                break
            if verdict is None:
                who = "Verifier" if ran_v else "Refuter"
                return self._finish(HUMAN_REQUIRED,
                                    f"{who} returned no parseable verdict at iteration {i}.")

            # REFUTED / INSUFFICIENT_EVIDENCE -> the Investigator reworks the hypothesis.
            instruction = feedback
            gate = "Verifier" if ran_v else "Refuter"
            sig = (verdict, _norm_hash(feedback))
            repeat = repeat + 1 if sig == last_sig else 1
            last_sig = sig
            disagreements.append(f"### Investigation iteration {i} ({gate}: {verdict})\n{feedback}\n")
            if repeat > trig:
                self.ctx.tx.md(f"\n> Same refutation repeated {repeat}× → triggering arbitration.\n")
                break
        else:
            self.ctx.tx.md("\n> Investigation loop exhausted without a confirmed root cause "
                           "→ arbitration.\n")

        self.ctx.tx.disagreements("\n".join(disagreements))
        return self._inv_arbitration(task, hypothesis, debug_diff, disagreements, is_no_defect)

    def _inv_arbitration(self, task, hypothesis, debug_diff, disagreements, is_no_defect=False):
        wf = self.ctx.workflows["investigation"]
        investigator, arbiter = wf["investigator"], wf["arbiter"]
        arbmax = wf["arbitration"]["max_iterations"]

        def _confirmed_finish(j, who):
            # On a confirmed conclusion, the terminal status depends on what was
            # being judged: a validated no-defect ends NO_DEFECT_FOUND, a confirmed
            # defect hypothesis ends ROOT_CAUSE_ESTABLISHED.
            if is_no_defect:
                return self._finish(NO_DEFECT_FOUND,
                                    f"NO_DEFECT_FOUND validated during arbitration iteration {j} ({who}).")
            self.ctx.tx.artifact(self._INV_FINAL_ARTIFACT, hypothesis)
            return self._finish(ROOT_CAUSE_ESTABLISHED,
                                f"{who} during arbitration iteration {j}.")

        for j in range(1, arbmax + 1):
            ares, averdict = self.call("inv.arbiter", arbiter,
                                       self._inv_arbiter_prompt(task, hypothesis, disagreements),
                                       phase="arbitration", iteration=j)
            if averdict in INV_ARB_SUCCESS:
                return _confirmed_finish(j, f"Arbiter returned {averdict}")
            if averdict in ARB_STOP:
                return self._finish(HUMAN_REQUIRED,
                                    f"Arbiter returned {averdict} at arbitration iteration {j}.")

            # HYPOTHESIS_REFUTED / REFUTER_IS_RIGHT / VERIFIER_IS_RIGHT /
            # NEEDS_MORE_INVESTIGATION -> the Investigator reworks under the ruling.
            iresp, iverdict = self.call("inv.investigator", investigator,
                                        self._inv_investigator_prompt(task, ares.text,
                                                                      arbitration=True),
                                        phase="arbitration", iteration=j, allow_edit=True)
            hypothesis = iresp.text
            debug_diff = self.ctx.capture_diff()
            self.ctx.tx.artifact(f"debug-diff-arb-{j:02d}.patch", debug_diff)
            term = self._inv_investigator_terminal(iverdict, j, "arbitration")
            if term:
                return term
            is_no_defect = (iverdict == NO_DEFECT_FOUND)

            verdict, feedback, ran_v = self._inv_gate(task, hypothesis, debug_diff,
                                                      "arbitration", j, is_no_defect)
            gate = "Verifier" if ran_v else "Refuter"
            disagreements.append(f"### Arbitration iteration {j} ({gate}: {verdict})\n{feedback}\n")
            self.ctx.tx.disagreements("\n".join(disagreements))
            if verdict == "CONFIRMED":
                return _confirmed_finish(j, "Verifier CONFIRMED")
            if verdict is None:
                who = "Verifier" if ran_v else "Refuter"
                return self._finish(HUMAN_REQUIRED,
                                    f"{who} returned no parseable verdict during arbitration "
                                    f"iteration {j}.")
            # NEEDS_ARBITRATION / REFUTED / INSUFFICIENT_EVIDENCE -> loop back to the arbiter.

        return self._finish(HUMAN_REQUIRED,
                            "Arbitration exhausted without an established root cause.")
