from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet
from syncer.models import CommitCheckRun, CommitStatusContext, PullRequest
from analyzer.services.queue_windows import is_on_queue_at


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueWindowsCI(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        # Rules: require CI success for a specific context "lint".
        QueueRuleSet.objects.create(
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
            head_sha="h1",
        )

    def test_ci_required_contexts_gate_queue_membership(self) -> None:
        pr = self._mk_pr(1)
        at = _dt(2024, 9, 5)

        # No CI snapshots yet: required context is missing, so PR is not on queue.
        self.assertFalse(is_on_queue_at(pr, at=at))

        # Add a failing status for the required context.
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC1",
            head_sha="h1",
            name="lint",
            state="FAILURE",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 4),
        )
        self.assertFalse(is_on_queue_at(pr, at=at))

        # Add a successful status with the same context name; latest SUCCESS should make CI ok.
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC2",
            head_sha="h1",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 6),
        )
        # At Sep 5 we only consider snapshots up to that time; CI should still be failing.
        self.assertFalse(is_on_queue_at(pr, at=at))

        # At Sep 7, the SUCCESS snapshot is visible, so CI is ok and the PR is on the queue.
        later = _dt(2024, 9, 7)
        self.assertTrue(is_on_queue_at(pr, at=later))

    def test_required_contexts_require_all_matching_jobs(self) -> None:
        pr = self._mk_pr(2)
        at = _dt(2024, 9, 6)

        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC3",
            head_sha="h1",
            name="lint / linux",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 4),
        )
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC4",
            head_sha="h1",
            name="lint / mac",
            state="FAILURE",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 5),
        )
        self.assertFalse(is_on_queue_at(pr, at=at))

        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC5",
            head_sha="h1",
            name="lint / mac",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 7),
        )
        later = _dt(2024, 9, 8)
        self.assertTrue(is_on_queue_at(pr, at=later))

    def test_required_contexts_match_substrings(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["linux"],
        )
        pr = self._mk_pr(3)
        at = _dt(2024, 9, 6)

        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC6",
            head_sha="h1",
            name="lint / linux",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 5),
        )
        self.assertTrue(is_on_queue_at(pr, at=at))

    def test_no_required_failures_mode(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )
        pr = self._mk_pr(4)
        at = _dt(2024, 9, 5)

        # Missing required context is non-blocking in no-fail mode.
        self.assertTrue(is_on_queue_at(pr, at=at))

        # Running required context is also non-blocking.
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC7",
            head_sha="h1",
            name="lint",
            state="PENDING",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 4),
        )
        self.assertTrue(is_on_queue_at(pr, at=at))

        # Observed required failure blocks queue eligibility.
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC8",
            head_sha="h1",
            name="lint",
            state="FAILURE",
            target_url=None,
            description=None,
            gh_created_at=_dt(2024, 9, 6),
        )
        self.assertFalse(is_on_queue_at(pr, at=_dt(2024, 9, 7)))

        # A newer running check run makes the required context non-failing again.
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_RUNNING",
            head_sha="h1",
            name="lint",
            status="IN_PROGRESS",
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 8),
            gh_completed_at=None,
        )
        self.assertTrue(is_on_queue_at(pr, at=_dt(2024, 9, 9)))
