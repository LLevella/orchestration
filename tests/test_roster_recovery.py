"""P1: the roster survives losing an agent, an account, or a provider.

Why this file exists. Three shipped cells had a fallback depth of ONE, because a
single agent in the workflow's chain declared the cell's role. One recognised
unavailability emptied the cell, `assign()` returned None, and the run ABORTED —
discarding every completed iteration. Sixteen corpus runs died that way (28% of all
HUMAN_REQUIRED outcomes); one of them lost 13 finished steps and $15.19 on
2026-08-29. In 11 of the 16 the block was the shared Anthropic ACCOUNT limit, which
every `claude_*` alias hits identically, so no same-family depth could have saved
them — the roster had to learn the difference between "this agent is gone" and
"this account is gone".

What is pinned here:

  1. The fallback depth of EVERY cell of EVERY shipped workflow, walked to
     exhaustion. A future allowlist edit that silently creates a depth-one cell
     fails this suite instead of a run six weeks later.
  2. The all-live assignment of every workflow — the compatibility invariant that
     none of this changes who reviews what on a healthy day.
  3. Account/provider-scoped blocks: one failure takes out every alias that shares
     the domain, and only THAT evidence unlocks a cross-family pin fallback.
  4. Re-admission on a `retry_at` deadline, and its boundary: nothing is re-admitted
     while a single logical call is still searching for a fallback.
  5. Preflight: one frozen availability snapshot, the real allocator in every
     simulation, and the depth-one writer cells reported before the run starts.

Everything asserts against the SHIPPED config or synthetic doubles — never a model.
"""
from __future__ import annotations

import copy
import unittest

from orchestrator import config as cfgmod
from orchestrator import failures
from orchestrator.adapters import build_adapters
from orchestrator.config import WORKFLOWS, ConfigError, load_config
from orchestrator.roster import (BLOCK_POLICY, DEGRADED, READY, UNASSIGNED, Block, Roster,
                                 SCOPE_ACCOUNT, SCOPE_AGENT, SCOPE_PROVIDER,
                                 availability_snapshot, format_preflight)


def _all_live():
    """Adapters for the shipped config with every agent live (mock transport), so the
    assertions describe the CONFIG and not the machine the suite runs on."""
    agents_cfg, wf = load_config()
    return build_adapters(agents_cfg, mock=True, scenario=None), wf


def _walk(wf_block, adapters, cell, snapshot):
    """Fill `cell`, evict that agent (agent-scope), repeat — the cell's real fallback
    depth under repeated AGENT-specific loss."""
    r = Roster(wf_block, adapters, snapshot=snapshot)
    seq = []
    while True:
        agent = r.assign().get(cell)
        if not agent:
            return seq
        seq.append(agent)
        r.mark_exhausted(agent)


# Fallback depth of every shipped cell, walked to exhaustion (agent-scope loss).
# depth 1 == "one unavailability empties this cell". The two that remain depth one
# are the WRITING cells: `claude` is the only agent with can_edit, and granting edit
# capability to another agent is a safety-model change, not a resilience fix — so the
# risk is reported by preflight and the loss pauses the run resumably instead.
SHIPPED_DEPTH = {
    "architecture": {"author": 3, "developer_verifier": 2, "tech_lead": 3, "arbiter": 2},
    "development": {"developer": 1, "architect": 2, "tech_lead": 3, "arbiter": 2},
    "investigation": {"investigator": 1, "refuter": 4, "verifier": 4, "arbiter": 2},
    "review": {"reviewer_correctness": 4, "reviewer_completeness": 3, "reviewer_risk": 2,
               "arbiter": 4, "reviser": 4},
    "implreview": {"author": 5, "dev_reviewer": 4, "devops_reviewer": 3, "verifier": 3,
                   "gating": 3, "arbiter": 3},
    "mrreview": {"expert_1": 7, "expert_2": 6, "expert_3": 5, "expert_4": 4, "moderator": 3},
}

# The all-live assignment of every workflow: an explicit compatibility invariant. P1
# adds a fallback and a failure domain; it does not change who does the work when
# everyone is up.
ALL_LIVE = {
    "architecture": {"author": "codex", "developer_verifier": "claude_reviewer",
                     "tech_lead": "gemini", "arbiter": "deepseek"},
    "development": {"developer": "claude", "architect": "codex",
                    "tech_lead": "gemini", "arbiter": "deepseek"},
    "investigation": {"investigator": "claude", "refuter": "codex",
                      "verifier": "gemini", "arbiter": "deepseek"},
    "review": {"reviewer_correctness": "codex", "reviewer_completeness": "claude_reviewer",
               "reviewer_risk": "claude_sonnet", "arbiter": "gemini", "reviser": "deepseek"},
    "implreview": {"author": "claude_author", "dev_reviewer": "claude_reviewer",
                   "devops_reviewer": "claude_devops", "verifier": "codex",
                   "gating": "deepseek", "arbiter": "gemini"},
    "mrreview": {"expert_1": "codex", "expert_2": "claude_reviewer", "expert_3": "gemini",
                 "expert_4": "deepseek", "moderator": "claude_sonnet"},
}


class TestShippedFallbackDepth(unittest.TestCase):
    def test_every_cell_has_the_depth_this_config_claims(self):
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        for w in WORKFLOWS:
            for cell, expected in SHIPPED_DEPTH[w].items():
                seq = _walk(wf[w], adapters, cell, snap)
                self.assertEqual(
                    len(seq), expected,
                    f"{w}.{cell}: fallback depth {len(seq)} ({seq}), expected {expected}. "
                    "If this dropped to 1, one unavailability now discards the cell — "
                    "extend the role allowlist or the pin, do not just edit the number.")

    def test_the_only_depth_one_cells_are_the_ones_that_must_write(self):
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        thin = {f"{w}.{c}" for w in WORKFLOWS for c in SHIPPED_DEPTH[w]
                if len(_walk(wf[w], adapters, c, snap)) == 1}
        self.assertEqual(thin, {"development.developer", "investigation.investigator"},
                         "a depth-one cell that is not a writing cell has no reason to "
                         "exist: some agent may hold the role, so let the roster reach it")
        for key in thin:
            w, c = key.split(".")
            self.assertTrue(Roster(wf[w], adapters, snapshot=snap).requires_edit[c],
                            f"{key} is depth one but does not declare requires_edit")

    def test_the_verifier_pin_is_no_longer_depth_one(self):
        """The defect itself: claude_reviewer was the ONLY 'Developer Verifier' in the
        architecture chain, so one session limit killed the run."""
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        self.assertEqual(_walk(wf["architecture"], adapters, "developer_verifier", snap),
                         ["claude_reviewer", "claude"])


class TestAllLiveAssignmentIsUnchanged(unittest.TestCase):
    def test_every_workflow_assigns_exactly_what_it_did_before(self):
        adapters, wf = _all_live()
        snap = availability_snapshot(adapters)
        for w in WORKFLOWS:
            self.assertEqual(Roster(wf[w], adapters, snapshot=snap).assign(), ALL_LIVE[w], w)

    def test_only_the_pin_primary_is_reserved_from_the_positional_pass(self):
        """The new `deepseek` verifier fallback must NOT displace the all-live DeepSeek
        arbiter: a pin reserves its PRIMARY, never its fallbacks."""
        adapters, wf = _all_live()
        r = Roster(wf["architecture"], adapters)
        out = r.assign()
        self.assertEqual(out["developer_verifier"], "claude_reviewer")
        self.assertEqual(out["arbiter"], "deepseek")
        self.assertEqual(r.degradations, {}, "an all-live run trades nothing away")


class _A:
    """A synthetic agent. Declares no failure domain on purpose: a test double must
    default to a domain of its own, so unrelated doubles never share a block."""

    def __init__(self, roles, live=True, can_edit=False, group=None, provider=None):
        self.cfg = {"roles": roles, "can_edit": can_edit}
        if group:
            self.cfg["availability_group"] = group
        if provider:
            self.cfg["provider"] = provider
        self.live = live
        self.probes = 0

    def availability(self):
        self.probes += 1
        return (self.live, "ok" if self.live else "down")


def _quota_account():
    """The failure that killed 11 of the 16 runs, classified from the real body."""
    return failures.classify(
        http_status=429,
        body={"is_error": True, "type": "result",
              "result": "You've hit your session limit · resets 7:50pm (Europe/Istanbul)"})


class TestScopedBlocks(unittest.TestCase):
    def test_every_failure_kind_has_a_block_row_or_is_deliberately_fatal(self):
        """BLOCK_POLICY mirrors failures.py's vocabulary; a new kind must be decided
        explicitly, not fall into the no-block default by accident."""
        for kind in failures.POLICY:
            _scope, _retry, roster_unavailable = failures.POLICY[kind]
            if roster_unavailable:
                self.assertIn(kind, BLOCK_POLICY,
                              f"'{kind}' recovers the roster but has no block scope")
            else:
                self.assertNotIn(kind, BLOCK_POLICY,
                                 f"'{kind}' is fatal by policy and must install no block")

    def test_an_account_failure_blocks_every_alias_that_shares_the_account(self):
        adapters = {
            "claude_reviewer": _A(["V"], group="anthropic-default-account", provider="anthropic"),
            "claude": _A(["V"], group="anthropic-default-account", provider="anthropic"),
            "gemini": _A(["V"], group="google-default-account", provider="google"),
        }
        wf = {"roster": {"chain": ["claude_reviewer", "claude", "gemini"], "cells": ["v"]},
              "v": {"role": "V"}}
        r = Roster(wf, adapters)
        block = r.block_for_failure("claude_reviewer", _quota_account())
        self.assertEqual((block.scope, block.selector),
                         (SCOPE_ACCOUNT, "anthropic-default-account"))
        self.assertEqual(r.exhausted, {"claude_reviewer", "claude"},
                         "the second alias shares the cap: trying it costs a call and fails")
        self.assertEqual(r.assign()["v"], "gemini")

    def test_the_observed_claude_reset_prose_yields_no_deadline(self):
        """'resets 7:50pm' carries no date: inventing one would re-admit the agent into
        the same wall. Those runs come back by explicit resume, not by a timer."""
        f = _quota_account()
        self.assertEqual((f.kind, f.scope), (failures.QUOTA, failures.SCOPE_ACCOUNT))
        self.assertIsNone(f.retry_at)
        r = Roster({"roster": {"chain": ["a"], "cells": ["c"]}, "c": {}}, {"a": _A([])})
        self.assertIsNone(r.block_for_failure("a", f).retry_at)

    def test_a_capacity_failure_blocks_the_provider_but_expires(self):
        adapters = {"g": _A(["V"], provider="google"), "d": _A(["V"], provider="deepseek")}
        wf = {"roster": {"chain": ["g", "d"], "cells": ["v"]}, "v": {"role": "V"}}
        r = Roster(wf, adapters, clock=lambda: 1_000_000.0)
        f = failures.classify(http_status=503, stderr="Service Unavailable")
        b = r.block_for_failure("g", f)
        self.assertEqual((b.scope, b.selector), (SCOPE_PROVIDER, "google"))
        self.assertIsNotNone(b.retry_at, "a provider blip must be re-admitted, not evicted")
        self.assertEqual(r.assign()["v"], "d")

    def test_an_invalid_request_installs_no_block(self):
        r = Roster({"roster": {"chain": ["a"], "cells": ["c"]}, "c": {}}, {"a": _A([])})
        f = failures.classify(http_status=400,
                              body={"error": {"type": "invalid_request_error",
                                              "message": "bad model"}})
        self.assertIsNone(r.block_for_failure("a", f))
        self.assertEqual(r.blocks, [])


class TestCrossFamilyPin(unittest.TestCase):
    """The architecture verifier pin is the only cell allowed to cross families, and
    only on classified account/provider evidence."""

    def _arch(self):
        adapters, wf = _all_live()
        return Roster(wf["architecture"], adapters), adapters

    def test_an_account_block_crosses_families_and_records_what_was_lost(self):
        r, _ = self._arch()
        r.block_for_failure("claude_reviewer", _quota_account())
        out = r.assign()
        self.assertEqual(out["developer_verifier"], "deepseek")
        self.assertEqual(out["author"], "codex")
        self.assertEqual(out["tech_lead"], "gemini")
        self.assertIsNone(out["arbiter"],
                          "DeepSeek cannot verify AND arbitrate: reaching arbitration pauses")
        d = r.degradations["developer_verifier"]
        self.assertEqual(d["kind"], "cross_family_pin_fallback")
        self.assertEqual((d["configured_primary"], d["selected"]), ("claude_reviewer", "deepseek"))
        self.assertEqual(d["trigger"]["scope"], SCOPE_ACCOUNT)
        self.assertEqual(d["trigger"]["selector"], "anthropic-default-account")
        _, wf = _all_live()
        configured = wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        self.assertEqual(d["lost_assurance"], configured["lost_assurance"])
        self.assertIn("cannot independently inspect", d["lost_assurance"])
        self.assertIn("7:50pm", d["trigger"]["reason"], "the provider's own words survive")

    def test_no_other_claude_is_tried_first(self):
        """The whole point of the scope: `claude` shares the account, so walking to it
        buys another failed call and no recovery."""
        r, _ = self._arch()
        r.block_for_failure("claude_reviewer", _quota_account())
        for alias in ("claude", "claude_author", "claude_sonnet", "claude_haiku"):
            self.assertIsNotNone(r.blocking(alias), f"{alias} shares the blocked account")

    def test_an_agent_specific_loss_stays_in_the_family_and_is_no_degradation(self):
        r, _ = self._arch()
        f = failures.classify(stderr="TimeoutExpired: timed out after 2400 seconds",
                              local_kind=failures.TIMEOUT)
        b = r.block_for_failure("claude_reviewer", f)
        self.assertEqual(b.scope, SCOPE_AGENT)
        self.assertEqual(r.assign()["developer_verifier"], "claude",
                         "an agent-specific loss is covered by the same-family fallback")
        self.assertEqual(r.degradations, {}, "same-family fallback loses no assurance")

    def test_local_absence_alone_never_unlocks_the_cross_family_fallback(self):
        """A missing CLI is not evidence that the Anthropic account is gone. Without
        that evidence the pin fails CLOSED — the run pauses rather than quietly
        downgrading the review that the pin exists to guarantee."""
        adapters, wf = _all_live()
        for name in ("claude_reviewer", "claude"):
            adapters[name].availability = lambda: (False, "'claude' not found in PATH")
        r = Roster(wf["architecture"], adapters)
        self.assertIsNone(r.assign()["developer_verifier"])
        self.assertEqual(r.degradations, {})


class TestReadmission(unittest.TestCase):
    def _setup(self, now):
        adapters = {"g": _A(["V"], provider="google"), "d": _A(["V"], provider="deepseek")}
        wf = {"roster": {"chain": ["g", "d"], "cells": ["v"]}, "v": {"role": "V"}}
        return Roster(wf, adapters, clock=lambda: now[0]), adapters

    def _deadline_failure(self):
        return failures.FailureInfo(kind=failures.CAPACITY, scope=failures.SCOPE_PROVIDER,
                                    reason="503 overloaded", retry_at="2030-01-01T00:00:00Z",
                                    retry_same_agent=True, roster_unavailable=True)

    def test_a_time_boxed_block_expires_at_its_deadline(self):
        now = [1_893_456_000.0 - 60]          # a minute before 2030-01-01T00:00:00Z
        r, _ = self._setup(now)
        r.block_for_failure("g", self._deadline_failure())
        self.assertEqual(r.assign()["v"], "d")
        self.assertEqual(r.begin_call(), [], "not due yet: still blocked")
        self.assertEqual(r.assign()["v"], "d")
        now[0] += 120
        readmitted = r.begin_call()
        self.assertEqual([b.selector for b in readmitted], ["google"])
        self.assertEqual(r.assign()["v"], "g", "the preferred agent is back")

    def test_a_deadline_never_re_admits_inside_one_logical_call(self):
        """Eligibility may only SHRINK while a call is still searching for a fallback:
        expiry happens at `begin_call()` and nowhere else, so the search cannot loop
        back onto the selector that just failed."""
        now = [1_893_456_000.0 - 60]
        r, _ = self._setup(now)
        r.block_for_failure("g", self._deadline_failure())
        now[0] += 3600                        # the deadline passes mid-call
        self.assertEqual(r.assign()["v"], "d", "no re-entry without a refresh point")
        self.assertIsNotNone(r.blocking("g"))

    def test_an_executed_cell_keeps_its_agent_when_a_stronger_one_returns(self):
        """A6: re-admission must not make a role change hands between iterations."""
        now = [1_893_456_000.0 - 60]
        r, _ = self._setup(now)
        r.block_for_failure("g", self._deadline_failure())
        r.note_executed("v", r.assign()["v"])          # 'd' ran the cell
        now[0] += 120
        r.begin_call()
        self.assertIsNone(r.blocking("g"), "google is eligible again")
        self.assertEqual(r.assign()["v"], "d", "but the cell that already ran does not move")


class TestPreflight(unittest.TestCase):
    def test_one_frozen_snapshot_probes_each_adapter_exactly_once(self):
        agents_cfg, wf = load_config()
        adapters = {n: _A(c.get("roles") or [], can_edit=c.get("can_edit", False),
                          group=c.get("availability_group"), provider=c.get("provider"))
                    for n, c in agents_cfg["agents"].items() if not n.startswith("_")}
        snap = availability_snapshot(adapters)
        for w in WORKFLOWS:
            Roster(wf[w], adapters, snapshot=snap).preflight()
        self.assertEqual({n: a.probes for n, a in adapters.items() if a.probes != 1}, {},
                         "a simulation re-probed availability: the snapshot is not frozen")

    def test_writer_cells_report_their_depth_one_risk(self):
        adapters, wf = _all_live()
        report = Roster(wf["development"], adapters).preflight()
        cell = next(c for c in report["cells"] if c["cell"] == "developer")
        self.assertTrue(cell["requires_edit"])
        self.assertEqual(cell["candidates"], ["claude"])
        self.assertTrue(any("depth-one" in w for w in cell["warnings"]), cell["warnings"])
        self.assertIn("developer", report["fragile_cells"])
        self.assertEqual(report["status"], DEGRADED,
                         "assigned everywhere, but a domain loss empties a cell")

    def test_the_verifier_simulations_name_the_real_fallbacks(self):
        adapters, wf = _all_live()
        report = Roster(wf["architecture"], adapters).preflight()
        cell = next(c for c in report["cells"] if c["cell"] == "developer_verifier")
        self.assertEqual(cell["on_agent_loss"]["assignment"]["developer_verifier"], "claude")
        self.assertEqual(cell["on_account_loss"]["assignment"]["developer_verifier"], "deepseek")
        self.assertEqual(cell["on_provider_loss"]["assignment"]["developer_verifier"], "deepseek")
        self.assertIn("arbiter", cell["on_account_loss"]["lost"],
                      "the account simulation must also report the arbiter it empties")

    def test_an_unassigned_cell_makes_the_report_unassigned(self):
        adapters, wf = _all_live()
        adapters["claude"].availability = lambda: (False, "'claude' not found in PATH")
        report = Roster(wf["development"], adapters).preflight()
        self.assertEqual(report["status"], UNASSIGNED)
        self.assertEqual(report["unassigned"], ["developer"])

    def test_a_locked_cross_family_fallback_is_never_counted_as_live(self):
        """The report must not contradict itself. With both Claude verifiers merely
        MISSING LOCALLY there is no account evidence, so the pin stays closed and the
        cell is empty — while DeepSeek sits in the candidate list, live and capable but
        locked. Counting it as a live candidate printed 'NONE (no live agent)' and
        'live 1/3' about the same cell in the same line."""
        adapters, wf = _all_live()
        for name in ("claude_reviewer", "claude"):
            adapters[name].availability = lambda: (False, "'claude' not found in PATH")
        report = Roster(wf["architecture"], adapters).preflight()
        cell = next(c for c in report["cells"] if c["cell"] == "developer_verifier")
        self.assertIsNone(cell["assigned"])
        self.assertEqual(cell["candidates"], ["claude_reviewer", "claude", "deepseek"])
        self.assertEqual(cell["live_candidates"], [], "nothing is selectable right now")
        self.assertEqual(cell["conditional_candidates"], ["deepseek"])
        self.assertTrue(any("locked until an account- or provider-scoped failure" in w
                            for w in cell["warnings"]), cell["warnings"])

        rendered = format_preflight("architecture", report).splitlines()
        line = next(t for t in rendered if "developer_verifier" in t)
        self.assertIn("NONE (no live agent)", line)
        self.assertIn("live 0/3", line)
        self.assertIn("+1 locked cross-family", line)

    def test_the_cross_family_fallback_becomes_selectable_once_the_account_is_blocked(self):
        """The other half of the same rule: with the classified account block in place
        DeepSeek is selectable, not conditional, and the report says so."""
        adapters, wf = _all_live()
        r = Roster(wf["architecture"], adapters)
        r.block_for_failure("claude_reviewer", _quota_account())
        cell = next(c for c in r.preflight()["cells"] if c["cell"] == "developer_verifier")
        self.assertEqual(cell["assigned"], "deepseek")
        self.assertEqual(cell["live_candidates"], ["deepseek"])
        self.assertEqual(cell["conditional_candidates"], [])

    def test_a_live_primary_keeps_its_cross_family_fallback_conditional(self):
        adapters, wf = _all_live()
        cell = next(c for c in Roster(wf["architecture"], adapters).preflight()["cells"]
                    if c["cell"] == "developer_verifier")
        self.assertEqual(cell["live_candidates"], ["claude_reviewer", "claude"])
        self.assertEqual(cell["conditional_candidates"], ["deepseek"],
                         "deepseek may hold the cell, but not while a Claude can")

    def test_a_workflow_with_no_thin_cell_is_ready(self):
        adapters = {"a": _A(["V"]), "b": _A(["V"]), "c": _A(["V"])}
        wf = {"roster": {"chain": ["a", "b", "c"], "cells": ["v"]}, "v": {"role": "V"}}
        self.assertEqual(Roster(wf, adapters).preflight()["status"], READY)


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        agents, wf = load_config()
        self.agents, self.wf = copy.deepcopy(agents), copy.deepcopy(wf)

    def _err(self):
        with self.assertRaises(ConfigError) as e:
            cfgmod._validate(self.agents, self.wf)
        return str(e.exception)

    def test_the_shipped_config_validates(self):
        cfgmod._validate(self.agents, self.wf)

    def test_a_shipped_agent_must_declare_its_failure_domain(self):
        del self.agents["agents"]["gemini"]["availability_group"]
        self.assertIn("availability_group", self._err())

    def test_a_pin_candidate_must_be_allowed_to_hold_the_cell(self):
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["agent_fallbacks"] = ["gemini"]          # gemini is no Developer Verifier
        self.assertIn("Developer Verifier", self._err())

    def test_a_pin_may_not_repeat_its_primary_as_a_fallback(self):
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["agent_fallbacks"] = ["claude", "claude_reviewer"]
        self.assertIn("repeats the primary", self._err())

    def test_a_different_provider_may_not_hide_in_agent_fallbacks(self):
        """`agent_fallbacks` is taken on ANY agent-specific loss — no account evidence,
        no degradation record. A different-provider candidate there would cross vendor
        families silently, which is exactly what the structured pin exists to prevent."""
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["agent_fallbacks"] = ["deepseek"]        # capable of the role, wrong family
        err = self._err()
        self.assertIn("agent_fallbacks", err)
        self.assertIn("account_provider_fallbacks", err)

    def test_a_cross_family_fallback_may_not_share_the_blocked_account(self):
        """It would be blocked by the very account-scoped failure that unlocks it."""
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["agent_fallbacks"] = []
        pin["account_provider_fallbacks"] = ["claude"]
        self.assertIn("availability_group", self._err())

    def test_the_two_fallback_lists_must_be_disjoint(self):
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["account_provider_fallbacks"] = ["claude", "deepseek"]
        self.assertIn("BOTH", self._err())

    def test_crossing_families_without_saying_what_is_lost_is_refused(self):
        pin = self.wf["architecture"]["roster"]["pinned"]["developer_verifier"]
        pin["lost_assurance"] = ""
        self.assertIn("lost_assurance", self._err())

    def test_a_writing_cell_may_not_be_bound_to_a_read_only_agent(self):
        self.wf["development"]["developer"]["agent"] = "codex"
        self.assertIn("can_edit", self._err())

    def test_a_legacy_string_pin_is_still_valid(self):
        self.assertEqual(self.wf["review"]["roster"]["pinned"]["arbiter"], "gemini")
        cfgmod._validate(self.agents, self.wf)


class TestLegacyPinBehaviourIsUnchanged(unittest.TestCase):
    def test_a_string_pin_still_falls_back_to_the_next_live_claude(self):
        adapters = {
            "claude_reviewer": _A(["Developer Verifier"], live=False),
            "claude_sonnet": _A(["Adversarial Reviewer"]),
            "claude_haiku": _A(["Developer Verifier"]),
        }
        wf = {"roster": {"chain": ["claude_reviewer", "claude_sonnet", "claude_haiku"],
                         "cells": ["v"], "pinned": {"v": "claude_reviewer"}},
              "v": {"role": "Developer Verifier"}}
        self.assertEqual(Roster(wf, adapters).assign()["v"], "claude_haiku")


if __name__ == "__main__":
    unittest.main()
