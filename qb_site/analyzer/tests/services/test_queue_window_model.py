from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevision
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestPRQueueWindowModel(TestCase):
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

    def _mk_pr(self, number: int, *, state: str = "open") -> PullRequest:
        created = _dt(2024, 9, 1)
        updated = _dt(2024, 9, 2)
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state=state,
            is_draft=False,
            gh_created_at=created,
            gh_updated_at=updated,
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

    def test_rebuild_persists_queue_windows_with_cycles(self) -> None:
        pr = self._mk_pr(1)
        # For CI-gated rulesets, we require PRRevision to exist; for this
        # label-only ruleset we do not, but we add one here to exercise the
        # gating logic symmetrically.
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha1",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        # Blocked from Sep 1–6, then unblocked, then blocked again on Sep 12.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 12),
            label_name="blocked-by-other-PR",
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 20))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # Expect a single cycle window: [Sep 6, Sep 12)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 6))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 12))
        self.assertEqual(windows[0].cycle_index, 0)
        self.assertEqual(windows[0].duration_seconds_closed, 6 * 24 * 60 * 60)
        self.assertEqual(windows[0].cumulative_seconds_closed, 6 * 24 * 60 * 60)
        self.assertEqual(windows[0].window_count, 1)
        self.assertEqual(windows[0].first_on_queue_ts, _dt(2024, 9, 6))

    def test_rebuild_persists_open_ended_queue_window(self) -> None:
        pr = self._mk_pr(2)
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha1",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        # Blocked from Sep 1–6, then unblocked and still on queue at as_of.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 20))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 6))
        self.assertIsNone(windows[0].to_ts)
        self.assertEqual(windows[0].cycle_index, 0)
        self.assertEqual(windows[0].duration_seconds_closed, 0)
        self.assertEqual(windows[0].cumulative_seconds_closed, 0)
        self.assertEqual(windows[0].window_count, 1)
        self.assertEqual(windows[0].first_on_queue_ts, _dt(2024, 9, 6))

    def test_rebuild_deletes_all_windows_when_pr_never_on_queue(self) -> None:
        pr = self._mk_pr(3)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 2),
            to_ts=_dt(2024, 9, 3),
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=_dt(2024, 9, 2),
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 20))

        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.deleted, 1)
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).count(), 0)

    def test_rebuild_bulk_path_mixed_create_update_delete(self) -> None:
        pr = self._mk_pr(4)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 2),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 4),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        # Existing row to update (same start, wrong end/rollups).
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 2),
            to_ts=_dt(2024, 9, 3),
            cycle_index=9,
            duration_seconds_closed=1,
            cumulative_seconds_closed=1,
            window_count=9,
            first_on_queue_ts=_dt(2024, 9, 3),
        )
        # Existing stale row to delete.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 5),
            to_ts=_dt(2024, 9, 5),
            cycle_index=1,
            window_count=2,
            first_on_queue_ts=_dt(2024, 9, 2),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 8))

        self.assertEqual(res.created, 1)
        self.assertEqual(res.updated, 1)
        self.assertEqual(res.deleted, 1)
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual([w.from_ts for w in windows], [_dt(2024, 9, 2), _dt(2024, 9, 6)])
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 4))
        self.assertIsNone(windows[1].to_ts)
        self.assertEqual([w.cycle_index for w in windows], [0, 1])
        self.assertEqual([w.window_count for w in windows], [2, 2])
        self.assertEqual([w.first_on_queue_ts for w in windows], [_dt(2024, 9, 2), _dt(2024, 9, 2)])

    def test_rebuild_updates_existing_rows_when_only_rollups_change(self) -> None:
        pr = self._mk_pr(5)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 2),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 4),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        # Correct starts and ends, but bad rollup metadata.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 2),
            to_ts=_dt(2024, 9, 4),
            cycle_index=9,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=9,
            first_on_queue_ts=_dt(2024, 9, 6),
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 6),
            to_ts=None,
            cycle_index=9,
            duration_seconds_closed=9,
            cumulative_seconds_closed=9,
            window_count=9,
            first_on_queue_ts=_dt(2024, 9, 6),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 8))

        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 2)
        self.assertEqual(res.deleted, 0)
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual([w.cycle_index for w in windows], [0, 1])
        self.assertEqual([w.window_count for w in windows], [2, 2])
        self.assertEqual([w.first_on_queue_ts for w in windows], [_dt(2024, 9, 2), _dt(2024, 9, 2)])

    def test_rebuild_preserves_open_ended_window_without_write_churn(self) -> None:
        pr = self._mk_pr(6)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        from analyzer.models.queue_window import QueueWindowEventType
        from syncer.models import PRTimelineEvent as _TLE

        ev_unblock = _TLE.objects.get(pull_request=pr, type=PRTimelineEventType.UNLABELED)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=_dt(2024, 9, 6),
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=_dt(2024, 9, 6),
            opened_by_event_type=QueueWindowEventType.FORBIDDEN_LABEL_REMOVED,
            opened_by_timeline_event=ev_unblock,
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 20))

        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.deleted, 0)
        win = PRQueueWindow.objects.get(pull_request=pr, rule_set=self.rules, from_ts=_dt(2024, 9, 6))
        self.assertIsNone(win.to_ts)
