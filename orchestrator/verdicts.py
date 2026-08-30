"""Verdict extraction from agent output.

Every role file defines a fixed output format with a known header line and a
fixed verdict vocabulary. We parse the verdict token deterministically. The
contract is fail-safe: if no known token is found, we return None and the engine
treats that as "must go to human", never a silent PASS.
"""
from __future__ import annotations

import re

# role name -> (header text, [valid verdict tokens])
ROLE_VERDICTS = {
    "System Architecture Reviewer": ("Architecture Verdict", ["PASS", "NEEDS_FIX", "NOT_APPLICABLE"]),
    "System Architect": ("Architect Verdict", ["PASS", "NEEDS_FIX", "NOT_APPLICABLE", "ESCALATE_TO_TECH_LEAD"]),
    # Architecture-workflow author. It OWNS the proposal and is called parse=False (its
    # output IS the proposal document), so this verdict is NOMINAL — present only so the
    # config consistency guard (every configured role needs a verdict spec) is satisfied.
    "Architecture Author": ("Architecture Proposal", ["DRAFTED"]),
    "Software Engineering Code Reviewer": ("Code Review Verdict", ["PASS", "NEEDS_FIX"]),
    "Technical Lead": ("Tech Lead Verdict", [
        "READY_FOR_HUMAN_REVIEW", "DEVELOPER_NEEDS_FIX",
        "ARCHITECT_REVIEW_NEEDS_REVISION", "ESCALATE_TO_HUMAN",
    ]),
    "Software Engineering Developer": ("Developer Verdict", [
        "READY_FOR_ARCHITECT_REVIEW", "BLOCKED_BY_ARCHITECTURE_QUESTION",
    ]),
    "Responsible Developer": ("Developer Verdict", [
        "READY_FOR_ARCHITECT_REVIEW", "BLOCKED_BY_ARCHITECTURE_QUESTION",
    ]),
    # Architecture-workflow implementability gate (Claude, read-only). The final
    # arch reviewer: judges whether the proposal is buildable in scope, not whether
    # it is elegant. May block; never authors or redesigns.
    "Developer Verifier": ("Implementation Feasibility Verdict", [
        "FEASIBLE", "NEEDS_REVISION", "HUMAN_REQUIRED",
    ]),
    "Arbitration Reviewer": ("Arbitration Verdict", [
        "DEVELOPER_IS_RIGHT", "REVIEWER_IS_RIGHT", "ARCHITECT_IS_RIGHT",
        "TECH_LEAD_IS_RIGHT", "BOTH_PARTIALLY_RIGHT", "OVERENGINEERING_DETECTED",
        "NEEDS_SMALLER_FIX", "NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION",
    ]),
    # ---- review workflow ----
    # One adversarial reviewer per lens; the verdict is the severity of its WORST
    # finding. The engine collects these; it does not branch on them.
    "Adversarial Reviewer": ("Review Verdict", [
        "SOLID", "MINOR_CONCERNS", "MAJOR_CONCERNS", "CRITICAL_FLAWS",
    ]),
    # Consolidates the panel into a ranked punch-list + judgment on the artifact.
    # STRONG/NEEDS_REVISION/WEAK -> REVIEW_COMPLETE (success); INSUFFICIENT_INPUT
    # (or unparseable) -> HUMAN_REQUIRED.
    "Review Arbiter": ("Review Arbiter Verdict", [
        "STRONG", "NEEDS_REVISION", "WEAK", "INSUFFICIENT_INPUT",
    ]),
    # The reviser (refine loop, `review --iterate`) applies the arbiter's must-fix to
    # the artifact and returns the revised text. It is called parse=False (its output
    # IS the new artifact), so this verdict is NOMINAL — present only so the config
    # consistency guard (every configured role needs a verdict spec) is satisfied.
    "Review Reviser": ("Reviser Verdict", ["REVISED"]),
    # ---- implreview workflow (6 roles, 3-stage cyclic gate) ----
    # Author OWNS + revises the artifact; called parse=False (its output IS the new
    # artifact), so this verdict is NOMINAL — present only for the config consistency
    # guard (every configured role needs a verdict spec).
    "Implementation Architect": ("Architect Revision", ["REVISED"]),
    # Two peer feasibility reviewers (both must return FEASIBLE to converge stage 1).
    "Advanced Developer Reviewer": ("Implementation Feasibility Verdict", [
        "FEASIBLE", "NEEDS_REVISION", "HUMAN_REQUIRED",
    ]),
    "DevOps Reviewer": ("Operational Feasibility Verdict", [
        "FEASIBLE", "NEEDS_REVISION", "HUMAN_REQUIRED",
    ]),
    # Technical control gate after the two peers converge.
    "Implementation Tech Lead": ("Implementation Tech Lead Verdict", [
        "READY_FOR_HUMAN_REVIEW", "NEEDS_REVISION", "ESCALATE_TO_HUMAN",
    ]),
    # Delivery gate (Project Manager) — the last gate before ship/send.
    "Project Manager Gate": ("Delivery Gate Verdict", [
        "APPROVED", "NEEDS_REVISION", "ESCALATE_TO_HUMAN",
    ]),
    # Independent arbiter — breaks a deadlock / escalation. APPROVED and NEEDS_REVISION
    # reopen or close the loop; NEEDS_HUMAN_DECISION / INSUFFICIENT_INFORMATION are in
    # ARB_STOP (engine ends HUMAN_REQUIRED).
    "Implementation Review Arbiter": ("Implementation Arbiter Verdict", [
        "APPROVED", "NEEDS_REVISION", "NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION",
    ]),
    # ---- mrreview workflow (merge-request panel) ----
    # Four experts review the same MR independently; the tally of these verdicts is
    # what "the panel's opinion" means, so the vocabulary is the one a human uses in
    # a merge request, not a gate token.
    "MR Review Expert": ("MR Review Verdict", [
        "APPROVE", "APPROVE_WITH_COMMENTS", "REQUEST_CHANGES",
    ]),
    # The moderator does not judge the code - it judges whether it received enough
    # usable opinions to produce one consolidated remarks list.
    "Review Moderator": ("Review Summary Verdict", [
        "CONSOLIDATED", "INSUFFICIENT_INPUT",
    ]),
    # ---- investigation workflow ----
    "Root Cause Investigator": ("Investigator Verdict", [
        "HYPOTHESIS_READY_FOR_REFUTATION", "BLOCKED_NEED_MORE_DATA", "NO_DEFECT_FOUND",
        # An over-eager Investigator may self-declare ROOT_CAUSE_ESTABLISHED; we
        # recognize it only so the engine can route it INTO the adversarial gate
        # (the Investigator cannot self-certify — see _inv_investigator_terminal).
        "ROOT_CAUSE_ESTABLISHED",
    ]),
    "Hypothesis Refuter": ("Refutation Verdict", [
        "REFUTED", "NOT_REFUTED", "INSUFFICIENT_EVIDENCE",
    ]),
    "Adversarial Verifier": ("Verification Verdict", [
        "CONFIRMED", "REFUTED", "NEEDS_ARBITRATION",
    ]),
    "Investigation Arbiter": ("Arbitration Verdict", [
        "HYPOTHESIS_STANDS", "HYPOTHESIS_REFUTED", "INVESTIGATOR_IS_RIGHT",
        "REFUTER_IS_RIGHT", "VERIFIER_IS_RIGHT", "NEEDS_MORE_INVESTIGATION",
        "NEEDS_HUMAN_DECISION", "INSUFFICIENT_INFORMATION",
    ]),
}


def extract_verdict(role: str, text: str):
    """Return the verdict token for `role` found in `text`, or None.

    Strategy: (1) prefer a token that appears right after the role's header line;
    (2) otherwise the first known token anywhere in the text. Longest tokens are
    matched first so substrings can't shadow them.
    """
    spec = ROLE_VERDICTS.get(role)
    if not spec or not text:
        return None
    header, tokens = spec
    toks = sorted(tokens, key=len, reverse=True)

    # 1) anchored on the header
    m = re.search(re.escape(header), text, re.IGNORECASE)
    if m:
        tail = text[m.end():m.end() + 300]
        for t in toks:
            if re.search(r"\b" + re.escape(t) + r"\b", tail):
                return t

    # 2) first occurrence anywhere
    best, best_pos = None, None
    for t in toks:
        mm = re.search(r"\b" + re.escape(t) + r"\b", text)
        if mm and (best_pos is None or mm.start() < best_pos):
            best, best_pos = t, mm.start()
    return best
