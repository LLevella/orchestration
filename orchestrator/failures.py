"""Provider-neutral failure telemetry for agent invocations.

Why this module exists: an agent that fails still *says* why it failed, and the
orchestrator used to throw that away. A non-zero CLI exit returned before the
JSON body was parsed, so a complete `claude -p --output-format json` result —
carrying `usage`, `total_cost_usd` and the literal reason "You've hit your
session limit · resets 7:50pm" — was discarded and the operator saw
`agent 'claude_reviewer' (Developer Verifier) returned ERROR: no detail`
(28 corpus steps in that state, 23 of them still holding $26.46 of
provider-reported cost and 15.2M uncounted input tokens).

Everything here is a pure function over already-captured bytes: no network, no
provider price tables, no clock-based guessing. The two hard rules are

  1. an error body is NEVER an answer — `AgentResponse.text` is emptied, and the
     raw body survives ONLY inside `FailureInfo.raw` (bounded head+tail), where
     verdict extraction cannot see it (A2);
  2. classification can only get STRONGER — whatever `is_unavailable()` /
     `is_transient()` recognise today still shifts / retries tomorrow, because
     the legacy markers are re-applied as a FLOOR on top of the structured
     decision (see `_apply_legacy_floor`).

A third rule governs the SEPARATION between describing a failure and acting on
one. `FailureInfo.retry_same_agent` / `.roster_unavailable` are the DESCRIPTIVE
policy of the failure kind (the A3/A4 table below): what failure-domain-aware
recovery will do once P1 lands. P2 is telemetry only and must not change WHICH
failures recover, so the engine consumes `compat_retry_same_agent` /
`compat_roster_unavailable` instead — the unchanged pre-P2 legacy-marker
decision. Without that split, merely classifying a timeout as `timeout/agent`
would silently start shifting the roster on timeouts, which no shipped run has
ever done.
"""
from __future__ import annotations

import email.utils
import json
import re
import time
from dataclasses import dataclass

from .roster import is_transient, is_unavailable

# ---- vocabulary --------------------------------------------------------
QUOTA = "quota"
RATE_LIMIT = "rate_limit"
CAPACITY = "capacity"
AUTHENTICATION = "authentication"
BILLING = "billing"
TIMEOUT = "timeout"
TRANSPORT = "transport"
CONFIGURATION = "configuration"
INVALID_REQUEST = "invalid_request"
UNKNOWN = "unknown"

SCOPE_AGENT = "agent"
SCOPE_ACCOUNT = "account"
SCOPE_PROVIDER = "provider"
SCOPE_UNKNOWN = "unknown"

# where the classification came from, in precedence order
SRC_LOCAL = "local"
SRC_STRUCTURED = "structured"
SRC_PROVIDER_DECODER = "provider_decoder"
SRC_HTTP_STATUS = "http_status"
SRC_LEGACY_TEXT = "legacy_text"

# kind -> (default scope, retry_same_agent, roster_unavailable).
#
# `retry_same_agent` is tabulated here explicitly rather than derived from the
# kind at the call site (amendment A4), and `configuration` has its own row
# (amendment A3) instead of being folded into `authentication`.
#
# THIS TABLE IS DESCRIPTIVE IN P2. It states what each kind means for recovery so
# P1 can act on it; the engine does NOT read it (it reads the `compat_*` fields).
# Two rows would otherwise change live behaviour on their own: `timeout` and
# `configuration` are newly recognised kinds that no legacy marker matches, so
# acting on this table here would start shifting the roster on a wedged CLI or a
# missing binary — recovery P2 is explicitly not authorised to add.
#
# retry_same_agent=True only where an IMMEDIATE second call plausibly succeeds:
# capacity (a momentary 503/529/overloaded) and transport (a socket blip). That
# is exactly today's TRANSIENT_MARKERS set, so the single same-agent retry keeps
# firing where it fires now. Quota/rate-limit/auth/billing/config persist for
# minutes-to-hours: retrying just burns another call.
#
# roster_unavailable=True means "this is infrastructure, not the task — a
# different-vendor agent would not share it", i.e. shift-left instead of
# aborting the run. False for invalid_request/unknown: a malformed prompt fails
# for every agent, so walking the chain would only multiply the damage.
POLICY = {
    QUOTA:           (SCOPE_ACCOUNT,  False, True),
    RATE_LIMIT:      (SCOPE_ACCOUNT,  False, True),
    CAPACITY:        (SCOPE_PROVIDER, True,  True),
    AUTHENTICATION:  (SCOPE_ACCOUNT,  False, True),
    BILLING:         (SCOPE_ACCOUNT,  False, True),
    # A per-agent wall-clock timeout: the agent is wedged, another vendor is not.
    # Not retried on the same agent — a 900 s timeout that repeats costs 900 s.
    TIMEOUT:         (SCOPE_AGENT,    False, True),
    TRANSPORT:       (SCOPE_AGENT,    True,  True),
    # Missing binary / unset API key. Agent-local and another vendor has its own
    # credentials, so shifting recovers; a missing command will not appear in 3 s.
    CONFIGURATION:   (SCOPE_AGENT,    False, True),
    INVALID_REQUEST: (SCOPE_UNKNOWN,  False, False),
    UNKNOWN:         (SCOPE_UNKNOWN,  False, False),
}

# Head+tail bound for any raw provider body kept in the failure record. Same size
# as the stderr bound it generalises: codex echoes the ENTIRE prompt to stderr, so
# a head-only slice pushes the real failure line out of view (observed 2026-07-09:
# a codex usage-limit marker was cut by a [:4000] head slice and the run aborted
# instead of shifting).
RAW_BOUND = 4000
# Shorter bound for the one-line operator/abort message.
REASON_BOUND = 600


def bound_text(text: str, limit: int = RAW_BOUND, label: str = "output") -> str:
    """Head+tail slice of `text`, announced in-band. Never a head-only cut: the
    marker that matters (a usage-limit line, a stack tail) usually sits at the end."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    half = limit // 2
    return f"{t[:half]}\n…[{label} truncated]…\n{t[-half:]}"


@dataclass
class FailureInfo:
    """Everything known about one failed invocation, provider-neutral.

    `raw` is the bounded head+tail of the provider's own bytes (stdout and
    stderr, or the HTTP body). It is the record that amendment A2 forbids
    discarding — it reaches run history, the transcript and the abort message,
    but never `AgentResponse.text`, so it can never be read as an answer."""
    kind: str = UNKNOWN
    scope: str = SCOPE_UNKNOWN
    reason: str = ""
    http_status: int | None = None
    provider_code: str | None = None
    retry_at: str | None = None          # UTC "%Y-%m-%dT%H:%M:%SZ", or None
    # Descriptive policy of `kind` (the POLICY table): what P1 recovery WILL do.
    # Recorded, never acted on in P2 — see the two fields below.
    retry_same_agent: bool = False
    roster_unavailable: bool = False
    # The decision the engine actually applies in P2: the pre-P2 legacy-marker
    # answer, so no failure starts (or stops) recovering because of this change
    # set. Strictly the old `is_transient()` / `is_unavailable()` verdict, over
    # the wider head+tail-of-both-streams evidence acceptance criterion 4
    # requires — evidence may grow, policy may not.
    compat_retry_same_agent: bool = False
    compat_roster_unavailable: bool = False
    classification_source: str = SRC_LEGACY_TEXT
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "scope": self.scope, "reason": self.reason,
            "http_status": self.http_status, "provider_code": self.provider_code,
            "retry_at": self.retry_at, "retry_same_agent": self.retry_same_agent,
            "roster_unavailable": self.roster_unavailable,
            "compat_retry_same_agent": self.compat_retry_same_agent,
            "compat_roster_unavailable": self.compat_roster_unavailable,
            "classification_source": self.classification_source, "raw": self.raw,
        }


# ---- structured decoders ------------------------------------------------
def _is_claude_body(body) -> bool:
    """A `claude -p --output-format json` result object, recognised structurally
    (no provider string has to be threaded through the adapters)."""
    return isinstance(body, dict) and (
        "is_error" in body or "api_error_status" in body or "total_cost_usd" in body)


def _openai_error(body):
    """The OpenAI-compatible `{"error": {...}}` envelope, or None."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
        if isinstance(err, str) and err:
            return {"message": err}
    return None


# Narrow prose decoders, applied ONLY to a known provider field (Claude's
# `result`) at structured precedence, and to free stdout/stderr prose at the
# lower message-decoder precedence.
_CLAUDE_QUOTA_RE = re.compile(
    r"session limit|spending cap|usage limit|weekly limit|reached your limit",
    re.IGNORECASE)


def _decode_message(msg):
    """(kind, provider_code) for a provider message we recognise precisely, else None."""
    if not msg:
        return None
    if _CLAUDE_QUOTA_RE.search(msg):
        return QUOTA, "claude_session_limit"
    return None


# Generic structured error codes/types shared by the OpenAI-compatible APIs.
_CODE_MAP = {
    "insufficient_quota": (QUOTA, SCOPE_ACCOUNT),
    "quota_exceeded": (QUOTA, SCOPE_ACCOUNT),
    "rate_limit_exceeded": (RATE_LIMIT, SCOPE_ACCOUNT),
    "rate_limit_error": (RATE_LIMIT, SCOPE_ACCOUNT),
    "too_many_requests": (RATE_LIMIT, SCOPE_ACCOUNT),
    "insufficient_balance": (BILLING, SCOPE_ACCOUNT),
    "invalid_api_key": (AUTHENTICATION, SCOPE_ACCOUNT),
    "authentication_error": (AUTHENTICATION, SCOPE_ACCOUNT),
    "invalid_authentication": (AUTHENTICATION, SCOPE_ACCOUNT),
    "permission_error": (AUTHENTICATION, SCOPE_ACCOUNT),
    "overloaded_error": (CAPACITY, SCOPE_PROVIDER),
    "server_error": (CAPACITY, SCOPE_PROVIDER),
    "api_error": (CAPACITY, SCOPE_PROVIDER),
    "invalid_request_error": (INVALID_REQUEST, SCOPE_UNKNOWN),
    "not_found_error": (INVALID_REQUEST, SCOPE_UNKNOWN),
}

# Numeric HTTP status -> classification. 529 (Anthropic "overloaded") is decided
# NUMERICALLY here, so it no longer depends on the word "overloaded" appearing.
_STATUS_MAP = {
    401: (AUTHENTICATION, SCOPE_ACCOUNT), 403: (AUTHENTICATION, SCOPE_ACCOUNT),
    402: (BILLING, SCOPE_ACCOUNT),
    429: (RATE_LIMIT, SCOPE_ACCOUNT),
    500: (CAPACITY, SCOPE_PROVIDER), 502: (CAPACITY, SCOPE_PROVIDER),
    503: (CAPACITY, SCOPE_PROVIDER), 529: (CAPACITY, SCOPE_PROVIDER),
    400: (INVALID_REQUEST, SCOPE_UNKNOWN), 404: (INVALID_REQUEST, SCOPE_UNKNOWN),
    422: (INVALID_REQUEST, SCOPE_UNKNOWN),
}


def _status_of(body, http_status):
    """Numeric status from the transport, else from the provider body."""
    if http_status is not None:
        return http_status
    if isinstance(body, dict):
        for k in ("api_error_status", "status", "status_code"):
            v = body.get(k)
            if isinstance(v, int):
                return v
        err = _openai_error(body) or {}
        for k in ("status", "status_code", "code"):
            v = err.get(k)
            if isinstance(v, int):
                return v
    return None


# ---- reason extraction --------------------------------------------------
def _structured_reason(body):
    """Reason precedence steps 1-3: provider-specific field, OpenAI-style
    `error.message`, then any other structured error field."""
    if not isinstance(body, dict):
        return ""
    if _is_claude_body(body):
        r = body.get("result")
        if isinstance(r, str) and r.strip():
            return r.strip()
    err = _openai_error(body)
    if err:
        for k in ("message", "msg", "detail", "description"):
            v = err.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    for k in ("message", "detail", "error_message", "reason"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _reason(body, stderr, stdout, local_reason):
    """Best human-readable reason. Precedence: structured provider reason, then
    bounded stderr, then bounded stdout, then the local exception text."""
    for candidate in (_structured_reason(body), stderr, stdout, local_reason):
        if candidate and candidate.strip():
            return bound_text(candidate, REASON_BOUND, "reason")
    return ""


# ---- retry_at ------------------------------------------------------------
def _utc(epoch) -> str | None:
    """The documented `%Y-%m-%dT%H:%M:%SZ` stamp for an epoch, or None.

    Every epoch reaching here is derived from untrusted provider metadata, so a
    value the platform clock cannot represent (a twenty-digit `Retry-After`, a
    negative reset) degrades to None instead of raising. The year bound is part
    of the same contract: `%Y` renders year 1 as "1-01-01", which is not the
    fixed-width UTC stamp `FailureInfo.retry_at` promises its consumers."""
    try:
        tm = time.gmtime(epoch)
    except (OverflowError, OSError, ValueError):
        return None
    if not 1970 <= tm.tm_year <= 9999:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", tm)


def _utc_of(dt) -> str | None:
    """The same stamp for an aware datetime, or None when it is out of range."""
    try:
        return _utc(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def parse_retry_at(retry_after=None, body=None):
    """UTC timestamp the agent may be tried again, or None.

    Populated ONLY from an unambiguous signal: an HTTP `Retry-After` (delta
    seconds or HTTP-date), or a provider timestamp that already carries a date
    AND a timezone. A date-less message such as
    "You've hit your session limit · resets 7:50pm (Europe/Istanbul)" yields
    None on purpose — guessing today's date or the host timezone would install a
    wrong deadline, and a wrong deadline is worse than no deadline.

    No conversion in here may raise. A header is attacker-shaped input that
    arrives at the very moment an agent has already failed: if reading it throws,
    the structured failure being built around it — reason, usage, cost, raw body
    — is lost and the operator is back to a traceback. So every malformed or
    out-of-range value simply yields no deadline, and an unusable header falls
    through to the body rather than suppressing it."""
    if retry_after is not None:
        s = str(retry_after).strip()
        if s.isdigit():
            # `isdigit()` is true for characters `int()` refuses (superscripts),
            # and a delta of 10**400 overflows the addition itself: both are
            # unreadable deltas, which means no known deadline.
            try:
                at = _utc(time.time() + int(s))
            except (ValueError, OverflowError):
                at = None
        else:
            try:
                dt = email.utils.parsedate_to_datetime(s)
            except (TypeError, ValueError):
                dt = None
            at = _utc_of(dt) if dt is not None and dt.tzinfo is not None else None
        if at:
            return at
    if isinstance(body, dict):
        for k in ("retry_at", "reset_at", "resets_at", "retryAt", "resetAt"):
            v = body.get(k)
            if isinstance(v, str):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is not None:
                    at = _utc_of(dt)
                    if at:
                        return at
    return None


# ---- classification ------------------------------------------------------
def _classify_structured(body):
    """Precedence 2-3: provider-specific structured fields, then generic codes."""
    if not isinstance(body, dict):
        return None
    if _is_claude_body(body):
        # `result` is a named field of a known provider body, so decoding it is
        # STRUCTURED evidence — which is what lets the b343 session limit win
        # over its own generic HTTP 429 and land on quota/account.
        decoded = _decode_message(body.get("result") if isinstance(body.get("result"), str) else "")
        if decoded:
            kind, code = decoded
            return kind, POLICY[kind][0], SRC_STRUCTURED, code
    err = _openai_error(body) or {}
    for key in ("code", "type"):
        v = err.get(key)
        if isinstance(v, str) and v.lower() in _CODE_MAP:
            kind, scope = _CODE_MAP[v.lower()]
            return kind, scope, SRC_STRUCTURED, v
    for key in ("code", "type", "error_type"):
        v = body.get(key)
        if isinstance(v, str) and v.lower() in _CODE_MAP:
            kind, scope = _CODE_MAP[v.lower()]
            return kind, scope, SRC_STRUCTURED, v
    return None


def _classify_status(status):
    """Precedence 4: the numeric HTTP status, no prose involved."""
    if status is None or status in (0, 200):
        return None
    mapped = _STATUS_MAP.get(status)
    if mapped:
        return mapped[0], mapped[1], SRC_HTTP_STATUS, str(status)
    if 500 <= status < 600:
        return CAPACITY, SCOPE_PROVIDER, SRC_HTTP_STATUS, str(status)
    return None


def _classify_message(evidence):
    """Precedence 5: a narrow provider message decoder over free prose."""
    decoded = _decode_message(evidence)
    if decoded:
        kind, code = decoded
        return kind, POLICY[kind][0], SRC_PROVIDER_DECODER, code
    return None


def _classify_legacy(evidence):
    """Precedence 6: the historical substring markers. Kept as the LAST word on
    kind and (via `_apply_legacy_floor`) as a floor on recoverability, so every
    marker that shifts or retries a cell today keeps doing so."""
    if is_transient(evidence):
        return CAPACITY, SCOPE_PROVIDER, SRC_LEGACY_TEXT, None
    if is_unavailable(evidence):
        return QUOTA, SCOPE_ACCOUNT, SRC_LEGACY_TEXT, None
    return None


def _apply_legacy_floor(retry_same_agent, roster_unavailable, evidence):
    """Never classify WEAKER than the legacy markers did (acceptance criterion 4).

    The structured path decides the kind; the markers can only ADD recoverability
    that the structured path missed. A structured `invalid_request` whose body
    happens to contain the word "quota" therefore still shifts left — the old
    code shifted on it, and P2 is not allowed to take that away."""
    if is_transient(evidence):
        retry_same_agent = True
    if is_unavailable(evidence):
        roster_unavailable = True
    return retry_same_agent, roster_unavailable


def classify(*, stdout="", stderr="", body=None, http_status=None, retry_after=None,
             local_kind=None, local_reason="", local_evidence="") -> FailureInfo:
    """Build a FailureInfo from everything captured about one failed invocation.

    `stdout`/`stderr` are raw and are bounded here (head AND tail, separately, so
    a marker at either end survives). `body` is the already-decoded structured
    payload when the transport produced one. `local_kind` short-circuits to the
    launch/configuration facts the adapter knows first-hand.

    `local_evidence` is text WE generated about the failure, not something the
    provider said — urllib's "HTTP Error 402: Payment Required" is the live case.
    It is classification evidence only: the legacy markers must keep seeing it
    (that string is exactly what makes a DeepSeek 402 shift the roster today), but
    it must NOT be mistaken for provider stderr, because rung 4 of the reason
    precedence would then outrank the provider's own plain-text body and the abort
    message would quote a Python exception instead of the provider's words. Pass
    the same string as `local_reason` to keep it as the LAST rung of the reason."""
    out = bound_text(stdout, RAW_BOUND, "stdout")
    err = bound_text(stderr, RAW_BOUND, "stderr")
    loc = bound_text(local_evidence, RAW_BOUND, "local")
    # The classifier sees BOTH streams, unlike the old `resp.error or resp.text`
    # which looked at exactly one of them, plus any locally known description. The
    # evidence pool and `raw` are kept identical on purpose, so `f.compat_*` stays
    # reproducible from the record alone (`is_unavailable(f.raw)`).
    evidence = "\n".join(x for x in (err, out, loc) if x)
    status = _status_of(body, http_status)

    decision = None
    if local_kind:
        decision = (local_kind, POLICY[local_kind][0], SRC_LOCAL, None)
    for resolver in (lambda: _classify_structured(body),
                     lambda: _classify_status(status),
                     lambda: _classify_message(evidence),
                     lambda: _classify_legacy(evidence)):
        if decision:
            break
        decision = resolver()
    if not decision:
        decision = (UNKNOWN, SCOPE_UNKNOWN, SRC_LEGACY_TEXT, None)

    kind, scope, source, code = decision
    _default_scope, retry_same, roster_unavail = POLICY.get(kind, POLICY[UNKNOWN])
    retry_same, roster_unavail = _apply_legacy_floor(retry_same, roster_unavail, evidence)
    raw = "\n".join(p for p in (f"[stdout]\n{out}" if out else "",
                                f"[stderr]\n{err}" if err else "",
                                f"[local]\n{loc}" if loc else "") if p)
    return FailureInfo(
        kind=kind, scope=scope,
        reason=_reason(body, err, out, local_reason),
        http_status=status, provider_code=code,
        retry_at=parse_retry_at(retry_after, body),
        retry_same_agent=retry_same, roster_unavailable=roster_unavail,
        # The live decision stays EXACTLY the pre-P2 one: the legacy markers and
        # nothing else. A structured 500 or a local timeout is now described
        # precisely, but it recovers only if it already did before this change.
        compat_retry_same_agent=is_transient(evidence),
        compat_roster_unavailable=is_unavailable(evidence),
        classification_source=source, raw=raw)


def failure_for_response(resp) -> FailureInfo:
    """The structured failure for ANY response — the compatibility seam that
    replaces `is_unavailable(resp.error or resp.text)` in the engine.

    A response from a P2 adapter carries `resp.failure` and it is authoritative.
    An older adapter or a test double carries only text, and is classified from
    the bounded combination of BOTH its error and its output."""
    existing = getattr(resp, "failure", None)
    if existing is not None:
        return existing
    return classify(stdout=getattr(resp, "text", "") or "",
                    stderr=getattr(resp, "error", "") or "")


def operator_reason(resp) -> str:
    """The provider's own words for the abort message. `returned ERROR: no
    detail` is only possible now when the provider genuinely said nothing."""
    failure = getattr(resp, "failure", None)
    for candidate in ((failure.reason if failure else ""),
                      getattr(resp, "error", "") or "",
                      (failure.raw if failure else ""),
                      getattr(resp, "text", "") or ""):
        if candidate and candidate.strip():
            return bound_text(candidate, REASON_BOUND, "detail")
    return "no detail"


def json_or_none(raw):
    """Decode a structured body, or None if it is not JSON. Never raises."""
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return d if isinstance(d, dict) else None
