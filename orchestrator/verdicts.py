"""Verdict extraction from agent output.

Every role file defines a fixed output format with a known header line and a
fixed verdict vocabulary. We parse the verdict token deterministically. The
contract is fail-safe: if no known token is found, we return None and the engine
treats that as "must go to human", never a silent PASS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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


# ===================== ARCH-02: semantic consistency assessment =====================
# The `architecture` arbiter seat has ONE extra, conditional capability: judging whether
# a Developer-Verifier objection is the SAME objection it already made, restated. It is
# not a fifth role and not a fifth seat — the engine selects the mode with an exact
# first-line marker and parses the reply under the contract below instead of the role's
# ordinary `## Arbitration Verdict` vocabulary.
#
# Why an arbiter and not a string/embedding distance: a paraphrase needs judgment and an
# accountable, citable ruling. The parser here is therefore strict about STRUCTURE (which
# is orchestration plumbing) and says nothing about similarity (which is the agent's job).

SEMANTIC_MODE_REPEAT = "REPEAT"
# The second conditional capability of the same seat: judging how far a Tech-Lead
# non-approval DEPARTS from the Developer-Verifier FEASIBLE result it overturns, and
# whether the evidence it cites justifies the departure. Same seat, same marker family,
# its own vocabulary and its own required ID partition.
SEMANTIC_MODE_GATE = "GATE_DEPARTURE"
SEMANTIC_MARKER = "[[ARCH_SEMANTIC_AUDIT_V1:{mode}]]"
SEMANTIC_HEADER = "Semantic Consistency Verdict"

# A ruling that the current objection IS the earlier one, plus which side it favours.
SAME_OBJECTION_TOKENS = frozenset({
    "SAME_OBJECTION_REVIEWER_RIGHT",
    "SAME_OBJECTION_AUTHOR_RIGHT",
    "SAME_OBJECTION_BOTH_PARTIALLY_RIGHT",
})
REPEAT_TOKENS = tuple(sorted(SAME_OBJECTION_TOKENS)) + (
    "DISTINCT_OR_MATERIALLY_PROGRESSING",
    "NEEDS_HUMAN_DECISION",
    "INSUFFICIENT_INFORMATION",
    "SELF_CONTRADICTION",
)
# The three gate-departure rulings, plus the three shared inconclusive tokens. Each of
# the first three fixes a REQUIRED partition of the Tech Lead's own findings, which is
# what makes "how far did the gate depart" answerable about named evidence.
GATE_RULING_TOKENS = ("JUSTIFIED_GATE_DEPARTURE",
                      "PARTIALLY_JUSTIFIED_GATE_DEPARTURE",
                      "UNJUSTIFIED_GATE_DEPARTURE")
GATE_TOKENS = GATE_RULING_TOKENS + ("NEEDS_HUMAN_DECISION",
                                    "INSUFFICIENT_INFORMATION",
                                    "SELF_CONTRADICTION")
# mode -> the only tokens that mode may return. Unknown mode = unparseable, never PASS.
SEMANTIC_TOKENS = {SEMANTIC_MODE_REPEAT: REPEAT_TOKENS,
                   SEMANTIC_MODE_GATE: GATE_TOKENS}

# Gate reports whose verdict is an approval carry no blocking findings by definition.
NON_BLOCKING_VERDICTS = frozenset({"FEASIBLE", "READY_FOR_HUMAN_REVIEW", "PASS"})
# source -> the H2 section that holds that gate's machine-readable blocking findings.
# `Architecture Blocking Findings` is the Tech Lead's canonical PROJECTION of the
# sections it always wrote, not a third source of judgment: a blocking claim left out
# of it is inactive and cannot support routing.
BLOCKING_SECTION = {"DV": "Blocking Implementation Gaps",
                    "TL": "Architecture Blocking Findings"}
# Sources whose synthetic fallback finding holds the WHOLE report rather than just the
# blocking section. A Tech-Lead non-approval carries its reasoning across several
# sections (`Architect Blocking Issues Assessment`, `Additional Tech Lead Findings`,
# `Final Instructions`), so an unstructured one collapses to the complete report; a
# Developer-Verifier report keeps its own section when it has one.
SYNTHETIC_WHOLE_REPORT = frozenset({"TL"})

# ---- the author's disposition transport block ----------------------------------
DISPOSITION_HEADER = "Blocker Dispositions"
ACCEPTED, DISAGREED, UNCLASSIFIED = "ACCEPTED", "DISAGREED", "UNCLASSIFIED"
DISPOSITION_TOKENS = frozenset({ACCEPTED, DISAGREED})
DISAGREEMENT_SECTION = "Disagreements"

_ID_RE = re.compile(r"\b[A-Z]{2,4}-\d+-B\d+\b")
_B_HEADING_RE = re.compile(r"^#{2,6}[ \t]*B(\d+)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class BlockingFinding:
    """One independently-addressable blocking objection, with an ID stable for the run.

    `synthetic` marks the fallback identity (`B0`) the engine assigns when a gate report
    is malformed, legacy, or unstructured: one indivisible finding holding the whole
    section, so Python never invents semantic boundaries the agent did not draw."""
    finding_id: str
    source: str
    iteration: int
    text: str
    synthetic: bool = False


@dataclass(frozen=True)
class SemanticAssessment:
    """A structurally VALID semantic ruling. `None` in its place means unparseable, and
    unparseable never means PASS (the fail-safe the whole verdict layer is built on).

    `retained_ids` / `rejected_ids` are the gate-departure partition of the Tech Lead's
    own findings: what survives as an active blocker, and what becomes historical
    evidence. They are empty in repeat mode, which has nothing to partition."""
    mode: str
    verdict: str
    current_ids: tuple[str, ...] = ()
    matched_prior_ids: tuple[str, ...] = ()
    retained_ids: tuple[str, ...] = ()
    rejected_ids: tuple[str, ...] = ()
    cited_evidence: str = ""
    binding_ruling: str = ""

    @property
    def named_ids(self) -> frozenset:
        """Every finding ID this assessment spoke about — what makes a later assessment
        "related" to it."""
        return frozenset(self.current_ids + self.matched_prior_ids
                         + self.retained_ids + self.rejected_ids)


@dataclass(frozen=True)
class BlockerDisposition:
    """What the author said it DID with one blocking finding. Transport only: it is
    stripped from the proposal, never repaired, and never affects routing — a
    measurement may not alter the thing it measures."""
    finding_id: str
    disposition: str
    target_section: str | None = None
    reason: str = ""            # why an UNCLASSIFIED entry could not be classified


@dataclass(frozen=True)
class SemanticVerdictContract:
    """What the engine SUPPLIED to the assessment. The parser validates every returned
    ID against it, so a ruling can only be about evidence the arbiter actually saw."""
    mode: str
    supplied_current_ids: frozenset[str] = frozenset()
    supplied_prior_ids: frozenset[str] = frozenset()


def semantic_marker(mode: str) -> str:
    return SEMANTIC_MARKER.format(mode=mode)


def _heading_level(line: str) -> int:
    m = re.match(r"^(#{1,6})[ \t]", line)
    return len(m.group(1)) if m else 0


def section_body(text: str, title: str):
    """Body of the markdown section titled `title`, or None if there is no such heading.

    Ends at the next heading of the same or a higher level, so `### B1` sub-blocks stay
    inside their `## Blocking Implementation Gaps` parent."""
    if not text:
        return None
    lines = (text or "").splitlines()
    pat = re.compile(r"^(#{1,6})[ \t]*" + re.escape(title) + r"[ \t]*:?[ \t]*$",
                     re.IGNORECASE)
    for idx, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        level = len(m.group(1))
        out = []
        for nxt in lines[idx + 1:]:
            lvl = _heading_level(nxt)
            if lvl and lvl <= level:
                break
            out.append(nxt)
        return "\n".join(out).strip("\n")
    return None


def _ids(section_text):
    """The finding IDs named in a section, in order of appearance, deduplicated.
    `None` (section absent) and an explicit `NONE` both yield an empty tuple."""
    if not section_text:
        return ()
    out, seen = [], set()
    for m in _ID_RE.finditer(section_text):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(m.group(0))
    return tuple(out)


def extract_blocking_findings(text: str, *, source: str, iteration: int, verdict: str):
    """Split a gate report into individually-identified blocking findings.

    The IDs (`DV-<iteration>-B<n>`) are what makes "the same objection, restated" a
    question that can be asked and answered about named evidence. A report that does not
    carry the `### B<n>` structure — legacy, malformed, or with a duplicated number —
    collapses to ONE synthetic `B0` holding the whole section: an indivisible finding is
    honest, whereas guessing where one objection ends and the next begins is not."""
    if verdict in NON_BLOCKING_VERDICTS or not (text or "").strip():
        return []
    title = BLOCKING_SECTION.get(source)
    section = section_body(text, title) if title else None
    body = section if (section and section.strip()) else (text or "")
    blocks, numbers = [], []
    heads = list(_B_HEADING_RE.finditer(body))
    for pos, m in enumerate(heads):
        end = heads[pos + 1].start() if pos + 1 < len(heads) else len(body)
        numbers.append(int(m.group(1)))
        blocks.append(body[m.start():end].strip())
    structured = (bool(heads) and all(n >= 1 for n in numbers)
                  and len(set(numbers)) == len(numbers))
    if not structured:
        # An indivisible finding is honest; guessing boundaries is not. For the Tech
        # Lead the indivisible unit is the WHOLE non-approval report, which is also
        # what makes PARTIALLY_JUSTIFIED_GATE_DEPARTURE structurally unavailable
        # against it: one finding cannot be split into two non-empty halves.
        whole = (text or "") if source in SYNTHETIC_WHOLE_REPORT else body
        return [BlockingFinding(f"{source}-{iteration}-B0", source, iteration,
                                whole.strip(), True)]
    return [BlockingFinding(f"{source}-{iteration}-B{n}", source, iteration, blk, False)
            for n, blk in zip(numbers, blocks)]


def _is_none(value: str) -> bool:
    """An explicitly empty section. `NONE` is a real answer ("no IDs", "no ruling"),
    which is not the same as the section being missing."""
    return value.strip().strip("`*_ .").upper() == "NONE"


def _final_semantic_block(text: str):
    """(token, body-after-the-header) for the LAST `## Semantic Consistency Verdict`
    heading, or None. Anchored on purpose: a token quoted in prose is not a ruling, and
    a repaired reply is APPENDED to the original, so the last block is the valid one."""
    lines = (text or "").splitlines()
    pat = re.compile(r"^#{1,6}[ \t]*" + re.escape(SEMANTIC_HEADER) + r"[ \t]*:?[ \t]*$",
                     re.IGNORECASE)
    last = None
    for idx, line in enumerate(lines):
        if pat.match(line):
            last = idx
    if last is None:
        return None
    token = None
    for line in lines[last + 1:]:
        s = line.strip().strip("`*_ ")
        if s:
            token = s
            break
    return token, "\n".join(lines[last + 1:])


def extract_semantic_assessment(text: str, *, contract: SemanticVerdictContract):
    """Parse a semantic-consistency reply, or return None if it is not structurally
    valid under `contract`. None is the fail-safe: the engine repairs it (live only) and,
    if repair fails, falls back to the ordinary route — it never reads as agreement."""
    tokens = SEMANTIC_TOKENS.get(contract.mode)
    if not tokens or not text:
        return None
    found = _final_semantic_block(text)
    if not found:
        return None
    token, body = found
    if token not in tokens:
        return None
    current = _ids(section_body(body, "Current Finding IDs"))
    prior = _ids(section_body(body, "Matched Prior Finding IDs"))
    retained = _ids(section_body(body, "Retained Tech-Lead Finding IDs"))
    rejected = _ids(section_body(body, "Rejected Tech-Lead Finding IDs"))
    evidence = (section_body(body, "Cited Evidence") or "").strip()
    ruling = (section_body(body, "Binding Ruling") or "").strip()
    supplied = set(contract.supplied_current_ids)
    # A ruling may only speak about evidence the engine actually supplied.
    if not set(current) <= supplied:
        return None
    if not set(prior) <= set(contract.supplied_prior_ids):
        return None
    if not (set(retained) | set(rejected)) <= supplied:
        return None
    if token in SAME_OBJECTION_TOKENS:
        # "The same objection" is only true if it accounts for EVERY current blocker:
        # one uncovered blocker means the draft is still being asked something new.
        if not current or not prior:
            return None
        if set(current) != supplied:
            return None
        if not evidence or _is_none(evidence) or not ruling or _is_none(ruling):
            return None
    if token in GATE_RULING_TOKENS:
        # A departure ruling is a PARTITION of the Tech Lead's own findings: what stays
        # an active blocker and what becomes historical evidence. An incomplete or
        # overlapping partition would leave a finding whose status nobody decided, so
        # it is unparseable — and unparseable falls back to the original route.
        if set(retained) & set(rejected):
            return None
        if set(retained) | set(rejected) != supplied:
            return None
        if token == "JUSTIFIED_GATE_DEPARTURE" and rejected:
            return None
        if token == "UNJUSTIFIED_GATE_DEPARTURE" and retained:
            return None
        if token == "PARTIALLY_JUSTIFIED_GATE_DEPARTURE" and not (retained and rejected):
            return None
        if not evidence or _is_none(evidence) or not ruling or _is_none(ruling):
            return None
    return SemanticAssessment(mode=contract.mode, verdict=token, current_ids=current,
                              matched_prior_ids=prior, retained_ids=retained,
                              rejected_ids=rejected, cited_evidence=evidence,
                              binding_ruling=ruling)


# ===================== ARCH-02: the author's blocker dispositions ====================
# The one quality signal this tool records is `author_blocker_acceptance_rate`: the share
# of a gate's blocking findings the author explicitly accepts in the next revision. It is
# computed from a transport block the author appends AFTER the proposal, which the engine
# parses and then STRIPS — the dispositions are orchestration metadata, so the document a
# human (or a later gate) reads never contains the author's self-classification.
#
# Everything here is deliberately unforgiving and deliberately inert: a defective entry
# becomes UNCLASSIFIED (counted, never guessed), it is never repaired with another author
# call, and it can never change a route. A measurement must not alter what it measures.

# Deliberately LENIENT about the rest of the heading line, unlike every other section
# match in this module. Being strict here would leak orchestration metadata into a
# human-facing proposal the moment an author wrote `## Blocker Dispositions (transport)`,
# and the phrase is this tool's own coinage — no real architecture section is called that.
_DISPOSITION_HEADING_RE = re.compile(
    r"^(#{1,6})[ \t]*" + re.escape(DISPOSITION_HEADER) + r"\b.*$",
    re.IGNORECASE | re.MULTILINE)


def _h2_titles(text: str) -> set:
    """The normalized H2 section titles of a proposal — the only targets an `ACCEPTED`
    disposition may name."""
    out = set()
    for m in re.finditer(r"^##[ \t]+(.+?)[ \t]*$", text or "", re.MULTILINE):
        out.add(_norm_section(m.group(1)))
    return out


def _norm_section(name: str) -> str:
    """Compare section names the way a reader does: without decoration or case."""
    cleaned = re.sub(r"<!--.*?-->", "", name or "")
    return cleaned.strip().strip("#").strip().strip("`*_ ").casefold()


def _split_disposition_block(text: str):
    """(clean_text, block_body) — the proposal without its trailing disposition block.

    Only the block and its own body are removed (it ends at the next heading of the same
    or a higher level), so an author that put something after it does not lose it. The
    LAST occurrence wins, for the same reason the semantic parser anchors on the last
    verdict block: a re-emitted block is appended, not substituted."""
    text = text or ""
    matches = list(_DISPOSITION_HEADING_RE.finditer(text))
    if not matches:
        return text.strip(), None
    m = matches[-1]
    level = len(m.group(1))
    tail = text[m.end():]
    end = len(text)
    for nxt in re.finditer(r"^(#{1,6})[ \t]+\S", tail, re.MULTILINE):
        if len(nxt.group(1)) <= level:
            end = m.end() + nxt.start()
            break
    body = text[m.end():end]
    clean = (text[:m.start()].rstrip() + "\n" + text[end:].lstrip()).strip()
    return clean, body


def _disposition_rows(body: str):
    """{finding_id: [(token, target), ...]} for every parseable row, and the ids that
    appeared more than once. A row is `- <ID> | <TOKEN> | <target>`."""
    rows: dict = {}
    for line in (body or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip().strip("`*_") for p in line.split("|")]
        m = _ID_RE.search(parts[0])
        if not m or m.group(0) != parts[0]:
            continue
        token = parts[1].strip().upper() if len(parts) > 1 else ""
        target = parts[2].strip() if len(parts) > 2 else ""
        rows.setdefault(m.group(0), []).append((token, target))
    return rows


def extract_blocker_dispositions(text: str, *, expected_ids):
    """(clean_proposal, {expected_id: BlockerDisposition}).

    Every expected ID gets exactly one disposition, so the denominator is the manifest
    the author was actually given and nothing else. Classification is deterministic:

      * `ACCEPTED` only when the target names an existing, non-`Disagreements` H2 section
        of the CLEAN proposal — "I accepted it" is only a claim if it points at the text
        that answers it;
      * `DISAGREED` only when the clean proposal really has a `## Disagreements` section;
      * missing, duplicated, unknown-token, malformed or mistargeted -> `UNCLASSIFIED`.

    Unknown IDs are ignored: an author cannot enlarge its own denominator."""
    clean, body = _split_disposition_block(text)
    expected = set(expected_ids or ())
    if not expected:
        return clean, {}
    rows = _disposition_rows(body) if body is not None else {}
    titles = _h2_titles(clean)
    disagreements_present = _norm_section(DISAGREEMENT_SECTION) in titles
    out = {}
    for fid in sorted(expected):
        entries = rows.get(fid) or []
        if not entries:
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, None,
                                          "no disposition was returned for this finding")
            continue
        if len(entries) > 1:
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, None,
                                          "the finding was disposed of more than once")
            continue
        token, target = entries[0]
        if token not in DISPOSITION_TOKENS:
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, target or None,
                                          f"unknown disposition {token!r}")
        elif token == ACCEPTED and not target:
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, None,
                                          "ACCEPTED named no target section")
        elif token == ACCEPTED and _norm_section(target) == _norm_section(DISAGREEMENT_SECTION):
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, target,
                                          "ACCEPTED targeted the Disagreements section")
        elif token == ACCEPTED and _norm_section(target) not in titles:
            out[fid] = BlockerDisposition(
                fid, UNCLASSIFIED, target,
                f"ACCEPTED targeted '{target}', which is not an H2 section of the proposal")
        elif token == DISAGREED and not disagreements_present:
            out[fid] = BlockerDisposition(fid, UNCLASSIFIED, target or None,
                                          "DISAGREED but the proposal has no "
                                          "'## Disagreements' section")
        else:
            out[fid] = BlockerDisposition(fid, token, target or None)
    return clean, out
