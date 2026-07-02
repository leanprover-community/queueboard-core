from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import TestCase

from django.db.models.query import QuerySet

from analyzer.models import PRDependency, PRQueueWindow, PRQueueWindowBuildState, QueueRuleSet, ReviewerOptOut
from analyzer.services.dependencies import rebuild_pr_dependencies
from analyzer.services.queue_window_build_state import record_queue_window_build_states
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset
from analyzer.services.reviewer_opt_out_backfill import backfill_reviewer_opt_outs
from core.models import Repository
from syncer.models import PRTimelineEvent, PRTimelineEventType, PullRequest


def flaky_queryset_get():
    """Patch target for QuerySet.get that misses exactly once, then delegates.

    Simulates the get_or_create race: the initial get sees no row (the winner has
    not committed yet), the create then conflicts with the winner's committed row,
    and Django's conflict-retry get must recover it. That retry only succeeds when
    the lookup kwargs cover the unique constraint — which is the property these
    tests pin.
    """
    real_get = QuerySet.get
    state = {"missed": False}

    def flaky(self, *args, **kwargs):
        if not state["missed"]:
            state["missed"] = True
            raise self.model.DoesNotExist
        return real_get(self, *args, **kwargs)

    return flaky


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestConcurrentWriteRaces(TestCase):
    """Losing an insert race against a concurrent rebuild must upsert, not raise.

    analyzer.process_pr and the queue-window sweep can rebuild the same PR at the
    same time: both snapshot the existing rows, both compute the same new row, and
    the loser's bulk_create used to raise IntegrityError (prqwin_pr_ruleset_from_unique)
    and abort the whole sweep run. These tests simulate the loser's view by making
    the snapshot read return empty while the "winner's" row already exists.
    """

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=["blocked-by-other-pr"],
        )
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=_dt(2024, 9, 1),
            gh_updated_at=_dt(2024, 9, 2),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )

    def _empty_first_filter(self, manager):
        """Return a filter() side-effect whose first call sees no rows (the loser's
        stale snapshot) and delegates to the real manager afterwards."""
        real_filter = manager.filter
        state = {"first": True}

        def fake_filter(*args, **kwargs):
            if state["first"]:
                state["first"] = False
                return manager.none()
            return real_filter(*args, **kwargs)

        return fake_filter

    def test_queue_window_rebuild_upserts_on_lost_insert_race(self) -> None:
        # The "winner" (a concurrent process_pr rebuild) already inserted the
        # window this rebuild is about to create, with different rollup values.
        PRQueueWindow.objects.create(
            pull_request=self.pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 6),
            to_ts=None,
            cycle_index=7,
            duration_seconds_closed=123,
            cumulative_seconds_closed=123,
            window_count=9,
            first_on_queue_ts=_dt(2024, 9, 1),
        )

        with mock.patch.object(
            PRQueueWindow.objects,
            "filter",
            side_effect=self._empty_first_filter(PRQueueWindow.objects),
        ):
            res = rebuild_queue_windows_for_ruleset(pr=self.pr, rule_set=self.rules, as_of=_dt(2024, 9, 20))

        self.assertEqual(res.status, "rebuilt")
        windows = list(PRQueueWindow.objects.filter(pull_request=self.pr, rule_set=self.rules))
        self.assertEqual(len(windows), 1)
        # The loser's freshly-computed values overwrote the conflicting row.
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 6))
        self.assertIsNone(windows[0].to_ts)
        self.assertEqual(windows[0].cycle_index, 0)
        self.assertEqual(windows[0].window_count, 1)
        self.assertEqual(windows[0].first_on_queue_ts, _dt(2024, 9, 6))

    def test_build_state_recording_upserts_on_lost_insert_race(self) -> None:
        # The "winner" already recorded build state for this (PR, ruleset) pair.
        PRQueueWindowBuildState.objects.create(
            pull_request=self.pr,
            rule_set=self.rules,
            revision_version_built=1,
            windows_built_at=_dt(2024, 9, 10),
            last_status="rebuilt",
            last_reason=None,
        )

        with mock.patch.object(
            PRQueueWindowBuildState.objects,
            "filter",
            side_effect=self._empty_first_filter(PRQueueWindowBuildState.objects),
        ):
            record_queue_window_build_states(
                pr=self.pr,
                rule_sets=[self.rules],
                per_ruleset={int(self.rules.id): {"status": "rebuilt", "reason": None}},
                revision_version=2,
                built_at=_dt(2024, 9, 12),
            )

        rows = list(PRQueueWindowBuildState.objects.filter(pull_request=self.pr, rule_set=self.rules))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].revision_version_built, 2)
        self.assertEqual(rows[0].windows_built_at, _dt(2024, 9, 12))

    def test_dependency_rebuild_converges_on_lost_insert_race(self) -> None:
        self.pr.body = "- [ ] depends on: #99"
        self.pr.save(update_fields=["body"])
        # The "winner" (a concurrent rebuild) already inserted the same edge.
        PRDependency.objects.create(
            pull_request=self.pr,
            depends_on_repository=self.repo,
            depends_on_number=99,
            depends_on_pull_request=None,
        )

        with mock.patch.object(
            PRDependency.objects,
            "filter",
            side_effect=self._empty_first_filter(PRDependency.objects),
        ):
            res = rebuild_pr_dependencies(self.pr)

        self.assertEqual(res.created, 0)
        self.assertEqual(res.deleted, 0)
        edges = PRDependency.objects.filter(pull_request=self.pr)
        self.assertEqual(edges.count(), 1)
        self.assertEqual(edges.get().depends_on_number, 99)

    def test_opt_out_backfill_converges_on_lost_insert_race(self) -> None:
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=_dt(2024, 9, 10),
            assignee_login="Rev",
        )
        # The "winner" (a live sync) already inserted the opt-out row.
        ReviewerOptOut.objects.create(
            repository=self.repo,
            pr_number=self.pr.number,
            reviewer_login="rev",
            active=True,
            opted_out_at=_dt(2024, 9, 5),
        )

        with mock.patch.object(QuerySet, "get", flaky_queryset_get()):
            res = backfill_reviewer_opt_outs(repository=self.repo, dry_run=False)

        self.assertEqual(res.opt_outs_created, 0)
        rows = list(ReviewerOptOut.objects.filter(repository=self.repo, pr_number=self.pr.number))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].active)
        self.assertEqual(rows[0].cleared_at, _dt(2024, 9, 10))
