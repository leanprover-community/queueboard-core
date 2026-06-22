from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import QueueRuleSet, ReviewerAssignmentApplication, ReviewerAssignmentSnapshot
from analyzer.tasks.reviewer_assignment_apply import apply_reviewer_assignments_task
from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ApplyReviewerAssignmentsTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")

    def _seed_repo_with_proposal(self) -> None:
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, is_default=True, is_active=True)
        now = timezone.now()
        user = User.objects.create(github_login="alice")
        ReviewerPreference.objects.create(repository=self.repo, user=user, auto_assign=True)
        ReviewerAssignmentSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(rule_set.id),
            generated_at=now,
            payload={"meta": {}, "automatic_assignments": {"101": "alice"}},
            etag="etag",
            assignment_count=1,
        )
        PullRequest.objects.create(
            repository=self.repo,
            number=101,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="PR 101",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )

    @override_settings(
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=False,
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=False,
    )
    def test_skips_when_disabled(self) -> None:
        res = apply_reviewer_assignments_task.apply().get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "feature_disabled")
        self.assertFalse(ReviewerAssignmentApplication.objects.exists())

    @override_settings(
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=False,
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=True,
    )
    def test_dry_run_records_without_mutating(self) -> None:
        self._seed_repo_with_proposal()

        res = apply_reviewer_assignments_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["repos"], 1)
        self.assertEqual(res["totals"]["skipped_dry_run"], 1)
        self.assertEqual(res["totals"]["applied"], 0)
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_SKIPPED_DRY_RUN)

    @override_settings(ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=True)
    def test_repo_filter_miss_returns_not_found(self) -> None:
        res = apply_reviewer_assignments_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")
