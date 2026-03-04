from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase, override_settings

from core.models import Repository
from analyzer.models import QueueRuleSet, PRRevision
from syncer.models import PullRequest, CheckRun, CommitCheckRun
from analyzer.services.queue_windows import is_on_queue_at


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


@override_settings(ANALYZER_CI_SHA_READ_PRIMARY=False, ANALYZER_CI_SHA_READ_FALLBACK_PR=True)
class TestQueueWindowsPRRevision(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        # Rules: require CI success for context "lint".
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
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
        )

    def test_ci_gates_by_head_sha_when_revisions_present(self) -> None:
        pr = self._mk_pr(1)
        # One revision window from Sep 1 onward on SHA "sha1".
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha1",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        at = _dt(2024, 9, 5)

        # No CI snapshots yet: required context is missing, so PR is not on queue.
        self.assertFalse(is_on_queue_at(pr, at=at))

        # CI success exists, but for a different head SHA ("sha2") -> should not count.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR1",
            head_sha="sha2",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        self.assertFalse(is_on_queue_at(pr, at=at))

        # Add a failing run for the correct head SHA "sha1" -> still not ok.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR2",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        self.assertFalse(is_on_queue_at(pr, at=at))

        # Add a later successful run for "sha1" and check after it completes.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR3",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 6),
            gh_completed_at=_dt(2024, 9, 6),
        )
        later = _dt(2024, 9, 7)
        self.assertTrue(is_on_queue_at(pr, at=later))

    def test_revisions_without_window_at_time_treat_ci_as_unknown(self) -> None:
        pr = self._mk_pr(2)
        # Revision window starts after creation; at the creation time we cannot
        # resolve a head SHA from PRRevision.
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="shaX",
            from_ts=_dt(2024, 9, 5),
            to_ts=None,
            seq=0,
        )

        # CI snapshot exists for the later head, but at Sep 2 we should still
        # treat CI as unknown and keep the PR off the queue.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CRX",
            head_sha="shaX",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 5),
            gh_completed_at=_dt(2024, 9, 5),
        )

        before = _dt(2024, 9, 2)
        self.assertFalse(is_on_queue_at(pr, at=before))

        after = _dt(2024, 9, 7)
        # Once we're inside the revision window and CI is green for that head, the
        # PR should be on the queue.
        self.assertTrue(is_on_queue_at(pr, at=after))

    @override_settings(ANALYZER_CI_SHA_READ_PRIMARY=True, ANALYZER_CI_SHA_READ_FALLBACK_PR=False)
    def test_sha_primary_reads_commit_checkrun_rows(self) -> None:
        pr = self._mk_pr(3)
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="shaC",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CCR1",
            head_sha="shaC",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        self.assertTrue(is_on_queue_at(pr, at=_dt(2024, 9, 5)))

    @override_settings(ANALYZER_CI_SHA_READ_PRIMARY=True, ANALYZER_CI_SHA_READ_FALLBACK_PR=False)
    def test_sha_primary_without_fallback_ignores_pr_ci_rows(self) -> None:
        pr = self._mk_pr(4)
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="shaD",
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            seq=0,
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CRD",
            head_sha="shaD",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        self.assertFalse(is_on_queue_at(pr, at=_dt(2024, 9, 5)))
