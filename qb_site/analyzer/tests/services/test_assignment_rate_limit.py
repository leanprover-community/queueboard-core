"""Reviewer assignment rate limit — design doc 054.

Four layers, matching the doc's validation plan: the count service over the durable history, the
pure engine gate (including the boundary the probe's own cross-check tripped on), the catalog wiring
that carries counts and limits to both the gate and the surfacing, and the end-to-end withholding.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.test import SimpleTestCase, TestCase, override_settings

from analyzer.models import QueueRuleSet, ReviewerAssignmentApplication
from analyzer.services.assignment_rate_limit import assignment_rate_window_days, recent_assignment_counts
from analyzer.services.reviewer_assignment import build_reviewer_catalog, prepare_assignment_inputs, suggest_reviewers_many
from analyzer.services.reviewer_assignment_engine import (
    ReviewerProfile,
    SimulationInputs,
    _reviewer_candidate_state,
    _within_rate_limit,
    run_assignment_simulation,
    suggest_reviewer_for_pr_with_trace,
)
from core.models import Repository, ReviewerPreference, User

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=dt_timezone.utc)


def _profile(
    login: str,
    *,
    capacity: int = 10,
    weekly_limit: int | None = None,
    recent: int = 0,
    simulated: int = 0,
    labels: tuple[str, ...] = ("t-algebra",),
    auto_assign: bool = True,
    temporary_break: bool = False,
) -> ReviewerProfile:
    return ReviewerProfile(
        github_login=login,
        maximum_capacity=capacity,
        auto_assign=auto_assign,
        temporary_break=temporary_break,
        preferred_labels=list(labels),
        preferred_labels_lower={lab.lower() for lab in labels},
        free_form="",
        conflict_of_interest=[],
        conflict_of_interest_lower=set(),
        weekly_limit=weekly_limit,
        recent_assignment_count=recent,
        simulated_this_run=simulated,
    )


def _pr(*, author: str = "zed", labels: tuple[str, ...] = ("t-algebra",), assignees: list[str] | None = None) -> dict:
    return {
        "author": author,
        "title": "chore: a change",
        "labels": [{"name": name} for name in labels],
        "assignees": assignees or [],
        "pr_status": "AwaitingReview",
        "total_queue_time": {"status": "valid", "value_td": 1000.0},
    }


class RecentAssignmentCountsTests(TestCase):
    """The count service over ``ReviewerAssignmentApplication`` (design doc 046's history)."""

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.other_repo = Repository.objects.create(owner="leanprover-community", name="batteries", default_branch="main")

    def _application(
        self,
        *,
        login: str,
        pr_number: int,
        applied_at: datetime | None = NOW,
        status: str = ReviewerAssignmentApplication.STATUS_APPLIED,
        repository: Repository | None = None,
        run_date: date | None = None,
    ) -> ReviewerAssignmentApplication:
        return ReviewerAssignmentApplication.objects.create(
            run_date=run_date or (applied_at or NOW).date(),
            repository=repository or self.repo,
            pr_number=pr_number,
            reviewer_login=login,
            status=status,
            applied_at=applied_at,
        )

    def test_counts_distinct_prs_in_window(self) -> None:
        for pr_number in (1, 2, 3):
            self._application(login="alice", pr_number=pr_number, applied_at=NOW - timedelta(days=1))
        counts = recent_assignment_counts(self.repo, ["alice"], window_days=7, now=NOW)
        self.assertEqual(counts, {"alice": 3})

    def test_repeat_assignment_of_one_pr_counts_once(self) -> None:
        # The attention sweep can auto-unassign a PR that is later re-assigned, writing a second
        # applied row for the same (PR, reviewer). The limit counts *new PRs*, so this is one.
        self._application(login="alice", pr_number=42, applied_at=NOW - timedelta(days=5), run_date=date(2026, 8, 23))
        self._application(login="alice", pr_number=42, applied_at=NOW - timedelta(days=1), run_date=date(2026, 8, 27))
        self.assertEqual(ReviewerAssignmentApplication.objects.filter(pr_number=42).count(), 2)
        counts = recent_assignment_counts(self.repo, ["alice"], window_days=7, now=NOW)
        self.assertEqual(counts, {"alice": 1})

    def test_window_boundary_is_inclusive_at_the_edge(self) -> None:
        self._application(login="alice", pr_number=1, applied_at=NOW - timedelta(days=7))
        self._application(login="alice", pr_number=2, applied_at=NOW - timedelta(days=7, seconds=1))
        self._application(login="alice", pr_number=3, applied_at=NOW - timedelta(days=6, hours=23))
        counts = recent_assignment_counts(self.repo, ["alice"], window_days=7, now=NOW)
        # PR 2 fell out the far side of the window; PR 1 is exactly on the boundary and counts.
        self.assertEqual(counts, {"alice": 2})

    def test_only_applied_rows_count(self) -> None:
        self._application(login="alice", pr_number=1, applied_at=NOW - timedelta(days=1))
        self._application(
            login="alice",
            pr_number=2,
            applied_at=NOW - timedelta(days=1),
            status=ReviewerAssignmentApplication.STATUS_SKIPPED_RECENTLY_APPLIED,
            run_date=date(2026, 8, 26),
        )
        self._application(
            login="alice",
            pr_number=3,
            applied_at=None,
            status=ReviewerAssignmentApplication.STATUS_PENDING,
            run_date=date(2026, 8, 26),
        )
        counts = recent_assignment_counts(self.repo, ["alice"], window_days=7, now=NOW)
        self.assertEqual(counts, {"alice": 1})

    def test_login_matching_is_case_insensitive_on_both_sides(self) -> None:
        """The history column stores login casing verbatim; a case-sensitive count reads zero.

        Measured on production: 11 of 41 reviewers are stored capitalized, and because no login
        appears under two spellings the failure is not a partial undercount but a total one — their
        limits would silently never fire. This is the test that keeps ``lower()`` in place.
        """
        self._application(login="MichaelStollBayreuth", pr_number=1, applied_at=NOW - timedelta(days=1))
        self._application(login="michaelstollbayreuth", pr_number=2, applied_at=NOW - timedelta(days=2))
        counts = recent_assignment_counts(self.repo, ["MichaelStollBayreuth"], window_days=7, now=NOW)
        self.assertEqual(counts, {"michaelstollbayreuth": 2})
        # And the same answer when the caller asks in the other casing.
        self.assertEqual(
            recent_assignment_counts(self.repo, ["michaelstollbayreuth"], window_days=7, now=NOW),
            {"michaelstollbayreuth": 2},
        )

    def test_every_requested_login_is_present_even_with_no_intake(self) -> None:
        self._application(login="alice", pr_number=1, applied_at=NOW - timedelta(days=1))
        counts = recent_assignment_counts(self.repo, ["alice", "bob"], window_days=7, now=NOW)
        self.assertEqual(counts, {"alice": 1, "bob": 0})

    def test_other_repositories_and_reviewers_do_not_leak(self) -> None:
        self._application(login="alice", pr_number=1, applied_at=NOW - timedelta(days=1))
        self._application(login="alice", pr_number=2, applied_at=NOW - timedelta(days=1), repository=self.other_repo)
        self._application(login="bob", pr_number=3, applied_at=NOW - timedelta(days=1))
        self.assertEqual(recent_assignment_counts(self.repo, ["alice"], window_days=7, now=NOW), {"alice": 1})

    def test_empty_and_disabled_window(self) -> None:
        self._application(login="alice", pr_number=1, applied_at=NOW - timedelta(days=1))
        self.assertEqual(recent_assignment_counts(self.repo, [], window_days=7, now=NOW), {})
        self.assertEqual(recent_assignment_counts(self.repo, [""], window_days=7, now=NOW), {})
        # A non-positive window counts nothing rather than degenerating into "all of history",
        # which would block every limited reviewer outright.
        self.assertEqual(recent_assignment_counts(self.repo, ["alice"], window_days=0, now=NOW), {"alice": 0})

    @override_settings(ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS=14)
    def test_window_days_helper_reads_the_setting(self) -> None:
        self.assertEqual(assignment_rate_window_days(), 14)


class RateLimitGateTests(SimpleTestCase):
    """The pure engine gate: ``recent + simulated < weekly_limit``."""

    def test_no_limit_is_a_no_op(self) -> None:
        self.assertTrue(_within_rate_limit(_profile("alice", weekly_limit=None, recent=999)))

    def test_reviewer_may_reach_their_limit_but_not_exceed_it(self) -> None:
        """ "Max 5 per week" means at most 5, not at most 4 — the strict ``<``.

        The probe's §3b/§4 cross-check disagreed until this boundary was pinned down, so it is
        pinned here too: a reviewer whose worst week was exactly N is never blocked at N.
        """
        self.assertTrue(_within_rate_limit(_profile("alice", weekly_limit=5, recent=4)))
        self.assertFalse(_within_rate_limit(_profile("alice", weekly_limit=5, recent=5)))
        self.assertFalse(_within_rate_limit(_profile("alice", weekly_limit=5, recent=6)))

    def test_this_runs_picks_count_against_the_budget(self) -> None:
        self.assertTrue(_within_rate_limit(_profile("alice", weekly_limit=5, recent=3, simulated=1)))
        self.assertFalse(_within_rate_limit(_profile("alice", weekly_limit=5, recent=3, simulated=2)))

    def test_candidate_state_withholds_a_rate_limited_reviewer(self) -> None:
        _, available, _, _ = _reviewer_candidate_state(
            pr_entry=_pr(),
            reviewers=[_profile("alice", weekly_limit=2, recent=2), _profile("bob")],
            assignment_stats={},
        )
        self.assertEqual(available, ["bob"])

    def test_gates_compose_and_neither_replaces_the_other(self) -> None:
        # Under the weekly limit but at concurrent capacity -> blocked by stock.
        _, available, _, _ = _reviewer_candidate_state(
            pr_entry=_pr(),
            reviewers=[_profile("alice", capacity=1, weekly_limit=5, recent=0)],
            assignment_stats={"alice": ([1], 1.0, 1)},
        )
        self.assertEqual(available, [])
        # Free concurrent capacity but at the weekly limit -> blocked by flow. This is the whole
        # point of 054: the fast-clearing reviewer the stock cap never bound.
        _, available, _, _ = _reviewer_candidate_state(
            pr_entry=_pr(),
            reviewers=[_profile("alice", capacity=10, weekly_limit=5, recent=5)],
            assignment_stats={},
        )
        self.assertEqual(available, [])

    def test_trace_records_at_rate_limit_distinctly_from_at_capacity(self) -> None:
        _result, trace = suggest_reviewer_for_pr_with_trace(
            pr_entry=_pr(),
            reviewers=[_profile("alice", capacity=10, weekly_limit=2, recent=2)],
            assignment_stats={},
            rng=random.Random(0),
        )
        self.assertEqual(trace["filtered"].get("at_rate_limit"), ["alice"])
        self.assertNotIn("at_capacity", trace["filtered"])
        self.assertEqual(trace["available"], [])

    def test_a_single_run_cannot_overrun_the_weekly_cap(self) -> None:
        """Five assignable PRs, one reviewer, limit 2 — the run stops at 2, not 5.

        Without ``simulated_this_run`` the durable window count would still read 0 for every pick
        in the run and one night could spend a whole week's budget several times over.
        """
        all_prs = {n: _pr() for n in range(1, 6)}
        result = run_assignment_simulation(
            inputs=SimulationInputs(
                reviewers=[_profile("alice", capacity=10, weekly_limit=2, recent=0)],
                assignments={},
                prs_to_assign=list(all_prs),
                all_prs=all_prs,
            ),
            rng=random.Random(0),
        )
        self.assertEqual(len(result.suggestions), 2)
        self.assertEqual(set(result.suggestions.values()), {"alice"})

    def test_a_run_fills_only_the_remaining_budget(self) -> None:
        all_prs = {n: _pr() for n in range(1, 6)}
        result = run_assignment_simulation(
            inputs=SimulationInputs(
                reviewers=[_profile("alice", capacity=10, weekly_limit=5, recent=3)],
                assignments={},
                prs_to_assign=list(all_prs),
                all_prs=all_prs,
            ),
            rng=random.Random(0),
        )
        self.assertEqual(len(result.suggestions), 2)

    def test_unlimited_reviewer_is_unchanged_by_the_feature(self) -> None:
        all_prs = {n: _pr() for n in range(1, 6)}
        result = run_assignment_simulation(
            inputs=SimulationInputs(
                reviewers=[_profile("alice", capacity=10, weekly_limit=None, recent=99)],
                assignments={},
                prs_to_assign=list(all_prs),
                all_prs=all_prs,
            ),
            rng=random.Random(0),
        )
        self.assertEqual(len(result.suggestions), 5)


class RateLimitCatalogTests(TestCase):
    """``build_reviewer_catalog`` carries both the limit and the measured window count."""

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        # Deliberately capitalized: the catalog must normalize before looking the count up.
        self.user = User.objects.create(github_login="MichaelStollBayreuth")
        self.pref = ReviewerPreference.objects.create(
            user=self.user, repository=self.repo, maximum_capacity=10, preferred_labels=["t-algebra"]
        )

    def _seed_intake(self, count: int) -> None:
        for pr_number in range(1, count + 1):
            ReviewerAssignmentApplication.objects.create(
                run_date=NOW.date(),
                repository=self.repo,
                pr_number=pr_number,
                reviewer_login=self.user.github_login,
                status=ReviewerAssignmentApplication.STATUS_APPLIED,
                applied_at=NOW - timedelta(days=1),
            )

    def test_unset_limit_leaves_the_profile_inert(self) -> None:
        self._seed_intake(4)
        profile = build_reviewer_catalog(self.repo, now=NOW)[0]
        self.assertIsNone(profile.weekly_limit)
        self.assertEqual(profile.recent_assignment_count, 4)
        self.assertTrue(_within_rate_limit(profile))

    def test_capitalized_login_still_resolves_its_window_count(self) -> None:
        self._seed_intake(3)
        self.pref.max_new_assignments_per_week = 3
        self.pref.save(update_fields=["max_new_assignments_per_week"])
        profile = build_reviewer_catalog(self.repo, now=NOW)[0]
        self.assertEqual(profile.weekly_limit, 3)
        self.assertEqual(profile.recent_assignment_count, 3)
        self.assertFalse(_within_rate_limit(profile))


class RateLimitEndToEndTests(TestCase):
    """A rate-limited reviewer is withheld from the push and still served by the pull (053)."""

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
        self.alice = User.objects.create(github_login="alice")
        self.alice_pref = ReviewerPreference.objects.create(
            user=self.alice, repository=self.repo, maximum_capacity=10, preferred_labels=["t-algebra"]
        )
        self.payload = {
            "meta": {"generated_at": "2026-08-28T00:00:00+00:00"},
            "prs": {str(n): _pr() for n in range(1, 5)},
            "lists": {"dashboards": {"Queue": [1, 2, 3, 4]}},
        }

    def _seed_intake(self, count: int, *, login: str = "alice") -> None:
        for pr_number in range(100, 100 + count):
            ReviewerAssignmentApplication.objects.create(
                run_date=NOW.date(),
                repository=self.repo,
                pr_number=pr_number,
                reviewer_login=login,
                status=ReviewerAssignmentApplication.STATUS_APPLIED,
                applied_at=NOW - timedelta(days=1),
            )

    def _suggestions(self) -> dict[int, str]:
        inputs = prepare_assignment_inputs(self.repo, payload=self.payload, now=NOW, rule_set=self.rules)
        return suggest_reviewers_many(
            reviewers=inputs.reviewers,
            assignments=inputs.assignments,
            prs_to_assign=inputs.assignable_queue_prs,
            all_prs=self.payload["prs"],
            rng=random.Random(0),
            excluded_by_pr=inputs.excluded_by_pr,
        )

    def test_reviewer_at_their_limit_gets_nothing_despite_free_capacity(self) -> None:
        self._seed_intake(5)
        self.alice_pref.max_new_assignments_per_week = 5
        self.alice_pref.save(update_fields=["max_new_assignments_per_week"])
        # Concurrent capacity is entirely free (nothing assigned in the payload) — only the flow
        # gate is holding them back, which is exactly the case maximum_capacity never covered.
        self.assertEqual(self._suggestions(), {})

    def test_reviewer_under_their_limit_receives_only_the_remainder(self) -> None:
        self._seed_intake(3)
        self.alice_pref.max_new_assignments_per_week = 5
        self.alice_pref.save(update_fields=["max_new_assignments_per_week"])
        self.assertEqual(len(self._suggestions()), 2)

    def test_no_limit_means_no_change(self) -> None:
        self._seed_intake(20)
        self.assertEqual(len(self._suggestions()), 4)

    def test_on_demand_suggestions_override_the_limit(self) -> None:
        """Design doc 053 Invariant 4: the pull side ignores every push throttle, this one included.

        This is what makes the short window acceptable — a reviewer cannot save up unused weekly
        budget, so catch-up has to live somewhere, and it lives here.
        """
        from analyzer.models import QueueSnapshot
        from analyzer.services.assignment_suggestions import STATUS_OK, suggest_prs_for_reviewer
        from syncer.models import LabelDef

        LabelDef.objects.create(repository=self.repo, name="t-algebra", color="ededed")
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(self.rules.id),
            generated_at=NOW - timedelta(hours=1),
            payload=self.payload,
            etag="etag",
            pr_count=4,
            queue_count=4,
        )
        self._seed_intake(5)
        self.alice_pref.max_new_assignments_per_week = 5
        self.alice_pref.save(update_fields=["max_new_assignments_per_week"])

        # The push gives them nothing...
        self.assertEqual(self._suggestions(), {})
        # ...while asking directly still does.
        result = suggest_prs_for_reviewer(self.repo, "alice", now=NOW)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual([pr.pr_number for pr in result.suggestions], [1, 2, 3, 4])
        # And the load line reports the limit honestly rather than hiding it (Invariant 7).
        self.assertIsNotNone(result.load)
        self.assertEqual(result.load.weekly_count, 5)
        self.assertEqual(result.load.weekly_limit, 5)
        self.assertTrue(result.load.at_weekly_limit)
