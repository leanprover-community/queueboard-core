from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from analyzer.models import QueueRuleSet, QueueSnapshot
from analyzer.services.reviewer_assignment_engine import ReviewerProfile
from analyzer.services.reviewer_load import (
    ReviewerLoad,
    build_reviewer_loads,
    compute_reviewer_loads,
    format_load_line,
    reviewer_load_for,
)
from core.models import Repository, ReviewerPreference, User


def _profile(login: str, capacity: int) -> ReviewerProfile:
    return ReviewerProfile(
        github_login=login,
        maximum_capacity=capacity,
        auto_assign=True,
        temporary_break=False,
        preferred_labels=[],
        preferred_labels_lower=set(),
        free_form="",
        conflict_of_interest=[],
        conflict_of_interest_lower=set(),
    )


class TestComputeReviewerLoads(TestCase):
    def test_weighted_load_and_remaining(self) -> None:
        loads = compute_reviewer_loads(
            repository_id=7,
            assignments={"alice": ([1, 2, 3], 3.0, 3)},
            reviewers=[_profile("alice", 10)],
        )
        load = loads["alice"]
        self.assertEqual(load.repository_id, 7)
        self.assertEqual(load.assigned_open, 3)
        self.assertEqual(load.current_load, 3.0)
        self.assertEqual(load.capacity, 10)
        self.assertEqual(load.remaining, 7.0)
        self.assertFalse(load.at_capacity)

    def test_assigned_count_can_exceed_weighted_load(self) -> None:
        # 5 assigned PRs but only 3.0 counts toward capacity (zero-weight/self-authored PRs).
        loads = compute_reviewer_loads(
            repository_id=1,
            assignments={"alice": ([1, 2, 3, 4, 5], 3.0, 5)},
            reviewers=[_profile("alice", 10)],
        )
        self.assertEqual(loads["alice"].assigned_open, 5)
        self.assertEqual(loads["alice"].current_load, 3.0)

    def test_case_insensitive_login_matching(self) -> None:
        loads = compute_reviewer_loads(
            repository_id=1,
            assignments={"AliCe": ([9], 1.0, 1)},
            reviewers=[_profile("alice", 4)],
        )
        self.assertIn("alice", loads)
        self.assertEqual(loads["alice"].assigned_open, 1)
        self.assertEqual(loads["alice"].current_load, 1.0)

    def test_reviewer_with_nothing_assigned_is_zero_not_at_capacity(self) -> None:
        loads = compute_reviewer_loads(repository_id=1, assignments={}, reviewers=[_profile("alice", 10)])
        load = loads["alice"]
        self.assertEqual(load.assigned_open, 0)
        self.assertEqual(load.current_load, 0.0)
        self.assertEqual(load.remaining, 10.0)
        self.assertFalse(load.at_capacity)

    def test_at_capacity_when_load_meets_cap(self) -> None:
        loads = compute_reviewer_loads(
            repository_id=1,
            assignments={"alice": ([1, 2], 2.0, 2)},
            reviewers=[_profile("alice", 2)],
        )
        self.assertTrue(loads["alice"].at_capacity)
        self.assertEqual(loads["alice"].remaining, 0.0)

    def test_over_capacity_is_at_capacity(self) -> None:
        loads = compute_reviewer_loads(
            repository_id=1,
            assignments={"alice": ([1, 2, 3], 3.0, 3)},
            reviewers=[_profile("alice", 2)],
        )
        self.assertTrue(loads["alice"].at_capacity)
        self.assertEqual(loads["alice"].remaining, -1.0)

    def test_no_reviewers_yields_empty(self) -> None:
        self.assertEqual(compute_reviewer_loads(repository_id=1, assignments={"x": ([1], 1.0, 1)}, reviewers=[]), {})


class TestFormatLoadLine(TestCase):
    def _load(self, *, used: float, cap: int, assigned: int) -> ReviewerLoad:
        remaining = cap - used
        return ReviewerLoad(
            repository_id=1,
            reviewer_login="alice",
            assigned_open=assigned,
            current_load=used,
            capacity=cap,
            remaining=remaining,
            at_capacity=remaining <= 1e-9,
        )

    def test_normal_line_shows_free(self) -> None:
        self.assertEqual(format_load_line(self._load(used=3.0, cap=10, assigned=5)), "Load: 3 / 10 (7 free)")

    def test_at_capacity_replaces_free(self) -> None:
        self.assertEqual(format_load_line(self._load(used=10.0, cap=10, assigned=12)), "Load: 10 / 10 ⚠ at capacity")

    def test_fractional_uses_one_decimal(self) -> None:
        self.assertEqual(format_load_line(self._load(used=4.5, cap=10, assigned=5)), "Load: 4.5 / 10 (5.5 free)")

    def test_include_assigned_count_appends_suffix(self) -> None:
        self.assertEqual(
            format_load_line(self._load(used=9.0, cap=10, assigned=9), include_assigned_count=True),
            "Load: 9 / 10 (1 free) · 9 assigned",
        )

    def test_at_capacity_with_count(self) -> None:
        self.assertEqual(
            format_load_line(self._load(used=10.0, cap=10, assigned=12), include_assigned_count=True),
            "Load: 10 / 10 ⚠ at capacity · 12 assigned",
        )

    def test_over_capacity_shows_true_load(self) -> None:
        # The engine can push a reviewer past capacity (no look-ahead); show the real figure.
        self.assertEqual(format_load_line(self._load(used=10.5, cap=10, assigned=12)), "Load: 10.5 / 10 ⚠ at capacity")

    def test_near_capacity_with_real_room_is_not_rendered_as_full(self) -> None:
        # 9.96/10 is NOT at capacity (remaining 0.04 > 0 -> still assignable); it must not render as
        # the contradictory "10 / 10 (0 free)".
        self.assertEqual(format_load_line(self._load(used=9.96, cap=10, assigned=12)), "Load: 9.9 / 10 (0.1 free)")

    def test_used_and_free_always_sum_to_capacity(self) -> None:
        # Independent rounding used to drift (3.6 + 6.3 = 9.9); free is now derived from used.
        self.assertEqual(format_load_line(self._load(used=3.65, cap=10, assigned=5)), "Load: 3.6 / 10 (6.4 free)")


class TestBuildReviewerLoads(TestCase):
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
        alice = User.objects.create(github_login="alice", zulip_user_id=101)
        erin = User.objects.create(github_login="erin", zulip_user_id=102)
        ReviewerPreference.objects.create(user=alice, repository=self.repo, maximum_capacity=10)
        ReviewerPreference.objects.create(user=erin, repository=self.repo, maximum_capacity=1)

    def _seed_snapshot(self, prs: dict) -> None:
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(self.rules.id),
            generated_at=datetime(2026, 7, 1, tzinfo=dt_timezone.utc),
            payload={"meta": {"generated_at": "2026-07-01T00:00:00+00:00"}, "prs": prs},
            etag="etag",
            pr_count=len(prs),
            queue_count=0,
        )

    def test_returns_empty_without_snapshot(self) -> None:
        self.assertEqual(build_reviewer_loads(self.repo), {})

    def test_computes_weighted_load_from_snapshot(self) -> None:
        self._seed_snapshot(
            {
                "1": {"assignees": ["alice"], "author": "bob", "pr_status": "AwaitingReview"},
                # Self-authored: counts toward assigned_open but contributes zero weight.
                "2": {"assignees": ["alice"], "author": "alice", "pr_status": "AwaitingReview"},
                "3": {"assignees": ["erin"], "author": "frank", "pr_status": "AwaitingReview"},
            }
        )
        loads = build_reviewer_loads(self.repo)

        self.assertEqual(loads["alice"].assigned_open, 2)
        self.assertEqual(loads["alice"].current_load, 1.0)
        self.assertEqual(loads["alice"].capacity, 10)
        self.assertFalse(loads["alice"].at_capacity)

        # erin has one weight-1 PR against a capacity of 1 -> at capacity.
        self.assertTrue(loads["erin"].at_capacity)
        self.assertEqual(loads["erin"].current_load, 1.0)

    def test_reviewer_load_for_convenience(self) -> None:
        self._seed_snapshot({"1": {"assignees": ["alice"], "author": "bob", "pr_status": "AwaitingReview"}})
        load = reviewer_load_for(self.repo, "AliCe")  # case-insensitive
        self.assertIsNotNone(load)
        self.assertEqual(load.current_load, 1.0)
        self.assertIsNone(reviewer_load_for(self.repo, "nobody"))
