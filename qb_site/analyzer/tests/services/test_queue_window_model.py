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
