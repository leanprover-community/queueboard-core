from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
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

    @override_settings(
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=True,
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=False,
        ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True,
    )
    def test_skips_when_proposals_pipeline_also_enabled(self) -> None:
        # Both pipelines on is a misconfiguration: the proposal-unaware apply task must yield to
        # the acceptance gate instead of direct-assigning confirm-mode reviewers past it.
        self._seed_repo_with_proposal()

        res = apply_reviewer_assignments_task.apply().get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "superseded_by_proposals_pipeline")
        self.assertFalse(ReviewerAssignmentApplication.objects.exists())

    @override_settings(ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=True)
    def test_repo_filter_miss_returns_not_found(self) -> None:
        res = apply_reviewer_assignments_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")

    @override_settings(
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=True,
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=False,
    )
    def test_one_repo_failure_does_not_abort_sweep(self) -> None:
        # A failure applying one repo must be isolated: the sweep records it and
        # continues to the remaining repos rather than aborting the whole run.
        other = Repository.objects.create(owner="leanprover-community", name="other", default_branch="master")

        def fake_apply(repo, **kwargs):
            if repo.id == self.repo.id:
                raise RuntimeError("boom")
            return {"repo": f"{repo.owner}/{repo.name}", "repo_id": repo.id, "status": "ok", "stats": {"applied": 1}}

        with patch("analyzer.tasks.reviewer_assignment_apply.apply_assignments_for_repo", side_effect=fake_apply):
            res = apply_reviewer_assignments_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["repos"], 2)
        self.assertEqual(res["repos_errored"], 1)
        # The healthy repo (ordered after the failing one) is still processed.
        self.assertEqual(res["totals"].get("applied"), 1)
        statuses = {r["repo"]: r["status"] for r in res["per_repo"]}
        self.assertEqual(statuses[f"{self.repo.owner}/{self.repo.name}"], "error")
        self.assertEqual(statuses[f"{other.owner}/{other.name}"], "ok")

    @override_settings(
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=True,
        ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN=True,
    )
    def test_command_honors_dry_run_setting_when_run_bare(self) -> None:
        # ENABLED=True + DRY_RUN=True in settings; the command run with no flags must
        # honor the dry-run safety net (record skipped_dry_run, perform no mutation).
        self._seed_repo_with_proposal()

        call_command("apply_reviewer_assignments", stdout=StringIO())

        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_SKIPPED_DRY_RUN)
