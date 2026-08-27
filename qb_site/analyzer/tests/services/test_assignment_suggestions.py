from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase, override_settings

from analyzer.models import AssignmentProposal, QueueRuleSet, QueueSnapshot, ReviewerOptOut
from analyzer.services.assignment_suggestions import (
    SKIP_ALREADY_ASSIGNED,
    SKIP_AUTHORED,
    SKIP_CONFLICT_OF_INTEREST,
    SKIP_EXCLUDED,
    SKIP_NO_AREA_MATCH,
    SKIP_NO_TOPIC_LABEL,
    SKIP_OUTRANKED,
    STATUS_NO_LABELS,
    STATUS_NO_SNAPSHOT,
    STATUS_NONE_ELIGIBLE,
    STATUS_NOT_A_REVIEWER,
    STATUS_OK,
    suggest_prs_for_reviewer,
)
from analyzer.services.reviewer_assignment_engine import ReviewerProfile, suggest_reviewer_for_pr_with_trace
from core.models import Repository, ReviewerPreference, User
from syncer.models import LabelDef

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=dt_timezone.utc)


def _pr(
    *,
    author: str = "zed",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    title: str = "chore: a change",
    queue_age: float = 1000.0,
) -> dict:
    return {
        "author": author,
        "title": title,
        "labels": [{"name": name} for name in (labels or [])],
        "assignees": assignees or [],
        "pr_status": "AwaitingReview",
        "total_queue_time": {"status": "valid", "value_td": queue_age},
    }


class SuggestionServiceTestCase(TestCase):
    """Shared fixture: a repo with a default rule set, a label catalog, and two reviewers."""

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
            is_active=True,
        )
        for name in ("t-algebra", "t-topology", "t-order", "easy"):
            LabelDef.objects.create(repository=self.repo, name=name, color="ededed")
        alice = User.objects.create(github_login="alice", zulip_user_id=101)
        bob = User.objects.create(github_login="bob", zulip_user_id=102)
        self.alice_pref = ReviewerPreference.objects.create(
            user=alice, repository=self.repo, maximum_capacity=10, preferred_labels=["t-algebra"]
        )
        self.bob_pref = ReviewerPreference.objects.create(
            user=bob, repository=self.repo, maximum_capacity=10, preferred_labels=["t-algebra", "t-topology"]
        )

    def _seed_snapshot(self, prs: dict[str, dict], *, queue: list[int] | None = None) -> None:
        queue_prs = queue if queue is not None else [int(n) for n in prs]
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(self.rules.id),
            generated_at=datetime(2026, 8, 1, tzinfo=dt_timezone.utc),
            payload={
                "meta": {"generated_at": "2026-08-01T00:00:00+00:00"},
                "prs": prs,
                "lists": {"dashboards": {"Queue": queue_prs}},
            },
            etag="etag",
            pr_count=len(prs),
            queue_count=len(queue_prs),
        )

    def _suggest(self, login: str = "alice", **kwargs):
        kwargs.setdefault("now", NOW)
        return suggest_prs_for_reviewer(self.repo, login, **kwargs)


class TestStatuses(SuggestionServiceTestCase):
    def test_no_snapshot_returns_no_suggestions_not_an_empty_success(self) -> None:
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NO_SNAPSHOT)
        self.assertEqual(result.suggestions, [])
        self.assertIsNone(result.snapshot_generated_at)
        self.assertIsNone(result.load)

    def test_not_a_reviewer(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest("stranger")
        self.assertEqual(result.status, STATUS_NOT_A_REVIEWER)
        self.assertEqual(result.suggestions, [])

    def test_no_labels_when_reviewer_has_none_and_no_override(self) -> None:
        self.alice_pref.preferred_labels = []
        self.alice_pref.save(update_fields=["preferred_labels"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NO_LABELS)
        self.assertEqual(result.suggestions, [])
        # The load line is still available for rendering.
        self.assertIsNotNone(result.load)

    def test_no_labels_when_override_is_entirely_unknown(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=["t-nonexistent"])
        self.assertEqual(result.status, STATUS_NO_LABELS)
        self.assertTrue(result.label_override)
        self.assertEqual(result.unknown_labels, ["t-nonexistent"])
        self.assertEqual(result.effective_labels, [])

    def test_none_eligible_carries_the_skip_tally(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-order"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.suggestions, [])
        self.assertEqual(result.skipped, {SKIP_NO_AREA_MATCH: 1})

    def test_ok_result_shape(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"], title="feat: rings", queue_age=123.0)})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.reviewer_login, "alice")
        self.assertEqual(result.repository_id, self.repo.id)
        self.assertIsNotNone(result.snapshot_generated_at)
        [pr] = result.suggestions
        self.assertEqual(pr.pr_number, 1)
        self.assertEqual(pr.title, "feat: rings")
        self.assertEqual(pr.url, "https://github.com/leanprover-community/mathlib4/pull/1")
        self.assertEqual(pr.author_login, "zed")
        self.assertEqual(pr.topic_labels, ["t-algebra"])
        self.assertEqual(pr.matched_labels, ["t-algebra"])
        self.assertEqual(pr.queue_age_seconds, 123.0)
        # alice and bob both match t-algebra and both have room.
        self.assertEqual(pr.available_reviewer_count, 2)
        # AwaitingReview -> claiming adds a full slot.
        self.assertEqual(pr.load_weight, 1.0)


class TestLabelOverrideCap(SuggestionServiceTestCase):
    """Labels past MAX_LABELS are reported, not silently honoured-in-part."""

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS=2)
    def test_labels_past_the_cap_are_reported(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=["t-algebra", "t-topology", "t-order"])
        self.assertEqual(result.effective_labels, ["t-algebra", "t-topology"])
        self.assertEqual(result.dropped_labels, ["t-order"])
        self.assertEqual(result.unknown_labels, [])

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS=2)
    def test_dropped_is_distinct_from_unknown(self) -> None:
        # A typo inside the cap is `unknown`; a good label past the cap is `dropped`. Collapsing
        # the two would tell a reviewer their own label was wrong when it was merely refused.
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=["t-algebra", "t-nonexistent", "t-topology"])
        self.assertEqual(result.effective_labels, ["t-algebra"])
        self.assertEqual(result.unknown_labels, ["t-nonexistent"])
        self.assertEqual(result.dropped_labels, ["t-topology"])

    def test_nothing_is_dropped_within_the_cap(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=["t-algebra"])
        self.assertEqual(result.dropped_labels, [])


class TestSnapshotStaleness(SuggestionServiceTestCase):
    """A stale snapshot is refused like a missing one — it must not be served as live."""

    def test_snapshot_older_than_the_ceiling_is_refused(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(now=datetime(2026, 9, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(result.status, STATUS_NO_SNAPSHOT)
        self.assertEqual(result.suggestions, [])
        # Still reported, so an operator can see *how* stale rather than just "missing".
        self.assertIsNotNone(result.snapshot_generated_at)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_SNAPSHOT_AGE_SECONDS=0)
    def test_zero_disables_the_ceiling(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(now=datetime(2026, 9, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(result.status, STATUS_OK)

    def test_a_fresh_snapshot_is_served(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        self.assertEqual(self._suggest().status, STATUS_OK)


class TestEngineTraceContract(TestCase):
    """`_classify_skip` reads `trace["potential"]`; every trace shape must carry the key.

    Without it, the three early-return paths were indistinguishable from "was contested and
    lost", which would silently misattribute skips to `outranked` — the one reason a reviewer
    cannot verify from the PR itself.
    """

    def _profile(self, login: str, labels: list[str]) -> ReviewerProfile:
        return ReviewerProfile(
            github_login=login,
            maximum_capacity=10,
            auto_assign=True,
            temporary_break=False,
            preferred_labels=labels,
            preferred_labels_lower={lab.lower() for lab in labels},
            free_form="",
            conflict_of_interest=[],
            conflict_of_interest_lower=set(),
        )

    def _trace(self, pr_entry: dict, reviewers: list[ReviewerProfile]) -> dict:
        _result, trace = suggest_reviewer_for_pr_with_trace(
            pr_entry=pr_entry, reviewers=reviewers, assignment_stats={}, rng=random.Random(0)
        )
        return trace

    def test_missing_topic_label_path_carries_potential(self) -> None:
        trace = self._trace(_pr(labels=[]), [self._profile("alice", ["t-algebra"])])
        self.assertEqual(trace["reason"], "missing-topic-label")
        self.assertIn("potential", trace)
        self.assertEqual(trace["potential"], [])

    def test_no_match_path_carries_potential(self) -> None:
        trace = self._trace(_pr(labels=["t-order"]), [self._profile("alice", ["t-algebra"])])
        self.assertEqual(trace["reason"], "no-match")
        self.assertIn("potential", trace)
        self.assertEqual(trace["potential"], [])

    def test_success_path_still_reports_real_potential(self) -> None:
        trace = self._trace(_pr(labels=["t-algebra"]), [self._profile("alice", ["t-algebra"])])
        self.assertEqual(trace["potential"], ["alice"])
        self.assertEqual(trace["available"], ["alice"])


class TestScarcityCount(SuggestionServiceTestCase):
    """`available_reviewer_count` reports the REAL supply, not the requester's override."""

    def test_unavailable_requester_is_not_counted_as_available_supply(self) -> None:
        # Alice is off auto-assign: the request overrides that for *her* eligibility (she still
        # gets the suggestion), but the nightly run would not count her as available supply, so
        # neither may the rendered scarcity number. Only bob really matches and has room.
        self.alice_pref.auto_assign = False
        self.alice_pref.save(update_fields=["auto_assign"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        [pr] = result.suggestions
        self.assertEqual(pr.available_reviewer_count, 1)

    def test_requester_at_capacity_is_not_counted_as_available_supply(self) -> None:
        # Same for the capacity override — the other half of Invariant 7's "reported, never
        # enforced": alice is suggested the PR, but she is not supply for it.
        self.alice_pref.maximum_capacity = 0
        self.alice_pref.save(update_fields=["maximum_capacity"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        [pr] = result.suggestions
        self.assertEqual(pr.available_reviewer_count, 1)

    def test_available_requester_is_counted(self) -> None:
        # The control: with alice genuinely available, she counts alongside bob.
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        [pr] = result.suggestions
        self.assertEqual(pr.available_reviewer_count, 2)

    def test_scarcity_is_zero_when_no_one_is_really_available(self) -> None:
        # A PR only the unavailable requester matches reports 0 available reviewers — the signal
        # the old reading could never produce, since the override always counted at least one.
        self.alice_pref.auto_assign = False
        self.alice_pref.preferred_labels = ["t-order"]
        self.alice_pref.save(update_fields=["auto_assign", "preferred_labels"])
        self._seed_snapshot({"1": _pr(labels=["t-order"])})
        result = self._suggest()
        [pr] = result.suggestions
        self.assertEqual(pr.available_reviewer_count, 0)


class TestOrderingAndLimits(SuggestionServiceTestCase):
    def _seed_six_algebra_prs(self) -> None:
        # Same label/scarcity for all six -> the ranking is decided by queue age (oldest first).
        self._seed_snapshot({str(n): _pr(labels=["t-algebra"], queue_age=float(1000 * (7 - n))) for n in range(1, 7)})

    def test_ordering_matches_the_scheduled_ranking_and_limit_is_respected(self) -> None:
        self._seed_six_algebra_prs()
        result = self._suggest(limit=4)
        self.assertEqual([s.pr_number for s in result.suggestions], [1, 2, 3, 4])

    def test_prefix_property(self) -> None:
        # The same request at limit=3 returns exactly the first three of limit=6 (Invariant 2):
        # this is the promise the Zulip footer's console link makes.
        self._seed_six_algebra_prs()
        short = self._suggest(limit=3)
        long = self._suggest(limit=6)
        self.assertEqual(
            [s.pr_number for s in short.suggestions],
            [s.pr_number for s in long.suggestions][:3],
        )
        self.assertEqual(len(long.suggestions), 6)

    def test_determinism_engine_random_draw_never_leaks_in(self) -> None:
        self._seed_six_algebra_prs()
        first = self._suggest(limit=6)
        second = self._suggest(limit=6)
        self.assertEqual(first.suggestions, second.suggestions)
        self.assertEqual(first.skipped, second.skipped)

    def test_queue_age_ties_break_by_pr_number(self) -> None:
        self._seed_snapshot(
            {
                "9": _pr(labels=["t-algebra"], queue_age=500.0),
                "3": _pr(labels=["t-algebra"], queue_age=500.0),
            }
        )
        result = self._suggest()
        self.assertEqual([s.pr_number for s in result.suggestions], [3, 9])

    def test_skip_tally_covers_the_whole_pool_even_past_the_limit(self) -> None:
        self._seed_snapshot(
            {
                "1": _pr(labels=["t-algebra"], queue_age=3000.0),
                "2": _pr(labels=["t-algebra"], queue_age=2000.0),
                "3": _pr(labels=["t-order"], queue_age=1000.0),
            }
        )
        result = self._suggest(limit=1)
        self.assertEqual(len(result.suggestions), 1)
        self.assertEqual(result.skipped, {SKIP_NO_AREA_MATCH: 1})

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT=2)
    def test_default_limit_comes_from_settings(self) -> None:
        self._seed_six_algebra_prs()
        result = self._suggest()
        self.assertEqual(len(result.suggestions), 2)


class TestLabelOverride(SuggestionServiceTestCase):
    def test_override_replaces_stored_preferred_labels(self) -> None:
        self._seed_snapshot(
            {
                "1": _pr(labels=["t-algebra"]),
                "2": _pr(labels=["t-order"]),
            }
        )
        result = self._suggest(labels=["t-order"])
        self.assertTrue(result.label_override)
        self.assertEqual(result.effective_labels, ["t-order"])
        self.assertEqual([s.pr_number for s in result.suggestions], [2])
        # The stored t-algebra interest does not leak through: PR 1 is a no_area_match skip.
        self.assertEqual(result.skipped, {SKIP_NO_AREA_MATCH: 1})

    def test_no_override_uses_stored_labels(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        self.assertFalse(result.label_override)
        self.assertEqual(result.effective_labels, ["t-algebra"])
        self.assertEqual(result.unknown_labels, [])

    def test_unknown_labels_reported_known_ones_still_used(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        # "t-typo" matches the topic pattern but is not in the repo's label catalog;
        # "easy" is in the catalog but is not a topic label.
        result = self._suggest(labels=["t-typo", "easy", "t-algebra"])
        self.assertEqual(result.unknown_labels, ["t-typo", "easy"])
        self.assertEqual(result.effective_labels, ["t-algebra"])
        self.assertEqual([s.pr_number for s in result.suggestions], [1])

    def test_labels_are_normalized_and_deduped(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=[" T-Algebra ", "t-algebra", ""])
        self.assertEqual(result.effective_labels, ["t-algebra"])
        self.assertEqual(result.unknown_labels, [])

    def test_blank_only_override_is_no_override(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest(labels=["", "   "])
        self.assertFalse(result.label_override)
        self.assertEqual(result.effective_labels, ["t-algebra"])

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS=2)
    def test_max_labels_cap_is_enforced(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-order"])})
        result = self._suggest(labels=["t-algebra", "t-topology", "t-order"])
        # Only the first two survive the cap; the dropped t-order means PR 1 is not offered.
        self.assertEqual(result.effective_labels, ["t-algebra", "t-topology"])
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)


class TestOverridesAndCorrectnessRules(SuggestionServiceTestCase):
    def test_away_until_is_overridden(self) -> None:
        self.alice_pref.away_until = NOW + timedelta(days=30)
        self.alice_pref.save(update_fields=["away_until"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        self.assertEqual([s.pr_number for s in result.suggestions], [1])

    def test_auto_assign_off_is_overridden(self) -> None:
        self.alice_pref.auto_assign = False
        self.alice_pref.save(update_fields=["auto_assign"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        result = self._suggest()
        self.assertEqual([s.pr_number for s in result.suggestions], [1])

    def test_capacity_is_overridden_but_the_load_line_reports_the_real_capacity(self) -> None:
        # alice is pinned at her cap by an existing assignment; an explicit request still yields
        # suggestions (Invariant 7), the load line stays honest, and there is never an
        # at_capacity skip for the requester.
        self.alice_pref.maximum_capacity = 1
        self.alice_pref.auto_assign = False  # keep her assigned PR in the pool via inactivity
        self.alice_pref.save(update_fields=["maximum_capacity", "auto_assign"])
        self._seed_snapshot(
            {
                "1": _pr(labels=["t-algebra"], assignees=["alice"]),
                "2": _pr(labels=["t-algebra"]),
            }
        )
        result = self._suggest()
        self.assertEqual([s.pr_number for s in result.suggestions], [2])
        self.assertIsNotNone(result.load)
        self.assertEqual(result.load.capacity, 1)
        self.assertEqual(result.load.current_load, 1.0)
        self.assertTrue(result.load.at_capacity)
        self.assertNotIn("at_capacity", result.skipped)

    def test_own_assigned_pr_is_never_suggested_back(self) -> None:
        # The pool's active-assignee filter reads real availability, so an auto_assign-off
        # requester's own assigned PR survives into the pool; the walk must still skip it.
        self.alice_pref.auto_assign = False
        self.alice_pref.save(update_fields=["auto_assign"])
        self._seed_snapshot({"1": _pr(labels=["t-algebra"], assignees=["alice"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_ALREADY_ASSIGNED: 1})

    def test_authorship_is_not_overridden(self) -> None:
        self._seed_snapshot({"1": _pr(author="alice", labels=["t-algebra"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_AUTHORED: 1})

    def test_conflict_of_interest_is_not_overridden(self) -> None:
        self.alice_pref.conflict_of_interest = ["zed"]
        self.alice_pref.save(update_fields=["conflict_of_interest"])
        self._seed_snapshot({"1": _pr(author="zed", labels=["t-algebra"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_CONFLICT_OF_INTEREST: 1})

    def test_opt_out_is_not_overridden(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        ReviewerOptOut.objects.create(
            repository=self.repo, pr_number=1, reviewer_login="alice", active=True, opted_out_at=NOW - timedelta(days=1)
        )
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_EXCLUDED: 1})

    def test_expired_proposal_cooldown_is_not_overridden(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=1,
            reviewer_login="alice",
            state=AssignmentProposal.STATE_EXPIRED,
            expires_at=NOW - timedelta(days=2),
            decided_at=NOW - timedelta(days=2),
        )
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_EXCLUDED: 1})

    def test_pr_with_active_proposal_is_not_in_the_pool_at_all(self) -> None:
        self._seed_snapshot({"1": _pr(labels=["t-algebra"])})
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=1,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=NOW + timedelta(days=7),
        )
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        # Withheld by the shared pool filter, so it is not even a skip.
        self.assertEqual(result.skipped, {})


class TestSkipTally(SuggestionServiceTestCase):
    def test_no_topic_label(self) -> None:
        self._seed_snapshot({"1": _pr(labels=[])})
        result = self._suggest()
        self.assertEqual(result.skipped, {SKIP_NO_TOPIC_LABEL: 1})

    def test_outranked_on_a_multi_label_pr(self) -> None:
        # bob matches both labels, alice one: the engine's max_score contest drops alice.
        self._seed_snapshot({"1": _pr(labels=["t-algebra", "t-topology"])})
        result = self._suggest()
        self.assertEqual(result.status, STATUS_NONE_ELIGIBLE)
        self.assertEqual(result.skipped, {SKIP_OUTRANKED: 1})

    def test_broad_label_override_is_never_outranked(self) -> None:
        # Matching every label on the PR always ties max_score (Invariant 9).
        self._seed_snapshot({"1": _pr(labels=["t-algebra", "t-topology"])})
        result = self._suggest(labels=["t-algebra", "t-topology"])
        self.assertEqual([s.pr_number for s in result.suggestions], [1])
        self.assertNotIn(SKIP_OUTRANKED, result.skipped)
        self.assertEqual(result.suggestions[0].matched_labels, ["t-algebra", "t-topology"])
