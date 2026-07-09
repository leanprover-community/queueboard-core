from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import AssignmentProposal, QueueRuleSet, ReviewerAssignmentSnapshot
from analyzer.tasks.reviewer_assignment_propose import propose_reviewer_assignments_task
from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ProposeReviewerAssignmentsTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")

    def _seed_confirm_reviewer_snapshot(self) -> None:
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, is_default=True, is_active=True)
        now = timezone.now()
        user = User.objects.create(github_login="bob", zulip_user_id=4242)
        ReviewerPreference.objects.create(
            repository=self.repo, user=user, auto_assign=True, assignment_acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM
        )
        ReviewerAssignmentSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(rule_set.id),
            generated_at=now,
            payload={"meta": {}, "automatic_assignments": {"101": "bob"}},
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

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False)
    def test_skips_when_disabled(self) -> None:
        res = propose_reviewer_assignments_task.apply().get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "feature_disabled")
        self.assertFalse(AssignmentProposal.objects.exists())

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False)
    def test_enabled_creates_proposal(self) -> None:
        self._seed_confirm_reviewer_snapshot()

        res = propose_reviewer_assignments_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["repos"], 1)
        self.assertEqual(res["totals"]["proposed"], 1)
        self.assertTrue(AssignmentProposal.objects.filter(repository=self.repo, pr_number=101, reviewer_login="bob").exists())

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True)
    def test_dry_run_no_side_effects(self) -> None:
        self._seed_confirm_reviewer_snapshot()

        res = propose_reviewer_assignments_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["totals"]["skipped_dry_run"], 1)
        self.assertEqual(res["totals"]["proposed"], 0)
        self.assertFalse(AssignmentProposal.objects.exists())

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True)
    def test_repo_filter_miss_returns_not_found(self) -> None:
        res = propose_reviewer_assignments_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False)
    def test_one_repo_failure_does_not_abort_sweep(self) -> None:
        other = Repository.objects.create(owner="leanprover-community", name="other", default_branch="master")

        def fake_propose(repo, **kwargs):
            if repo.id == self.repo.id:
                raise RuntimeError("boom")
            return {"repo": f"{repo.owner}/{repo.name}", "repo_id": repo.id, "status": "ok", "stats": {"proposed": 1}}

        with patch("analyzer.tasks.reviewer_assignment_propose.propose_assignments_for_repo", side_effect=fake_propose):
            res = propose_reviewer_assignments_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["repos"], 2)
        self.assertEqual(res["repos_errored"], 1)
        self.assertEqual(res["totals"].get("proposed"), 1)
        statuses = {r["repo"]: r["status"] for r in res["per_repo"]}
        self.assertEqual(statuses[f"{self.repo.owner}/{self.repo.name}"], "error")
        self.assertEqual(statuses[f"{other.owner}/{other.name}"], "ok")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True)
    def test_command_honors_dry_run_when_run_bare(self) -> None:
        self._seed_confirm_reviewer_snapshot()

        call_command("propose_reviewer_assignments", stdout=StringIO())

        self.assertFalse(AssignmentProposal.objects.exists())
