from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevision
from syncer.models import PullRequest
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueWindowGating(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr(self, number: int, *, state: str = "open", timeline_done: bool = False) -> PullRequest:
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
            timeline_backfill_done=timeline_done,
        )

    def test_no_windows_when_timeline_not_backfilled(self) -> None:
        pr = self._mk_pr(1, timeline_done=False)
        rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
        )
        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.deleted, 0)
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=rules).count(), 0)

    def test_ci_gated_rules_require_revisions(self) -> None:
        pr = self._mk_pr(2, timeline_done=True)
        rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )
        # No PRRevision rows exist yet; rebuild should not create any windows.
        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.deleted, 0)
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=rules).count(), 0)

        # Once a revision exists, rebuild is allowed (even if CI is missing; CI
        # gating will keep the PR off the queue until snapshots arrive).
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha1",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        res2 = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rules, as_of=_dt(2024, 9, 10))
        # Still no windows because CI is missing, but the call is now permitted.
        self.assertEqual(res2.created, 0)
        self.assertEqual(res2.updated, 0)
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=rules).count(), 0)
