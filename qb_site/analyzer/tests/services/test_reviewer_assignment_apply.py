from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from analyzer.models import (
    QueueRuleSet,
    ReviewerAssignmentApplication,
    ReviewerAssignmentSnapshot,
    ReviewerOptOut,
)
from analyzer.services.reviewer_assignment_apply import apply_assignments_for_repo
from core.models import Repository, ReviewerPreference, User
from core.services.github_assignment import AssignmentMutationError
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ApplyAssignmentsForRepoTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, is_default=True, is_active=True)
        self.cache_key = str(self.rule_set.id)
        self.now = timezone.now()
        self.run_date = self.now.date()
        # Eligible reviewers (must have an auto_assign ReviewerPreference to count).
        self._make_reviewer("alice")
        self._make_reviewer("bob")

    # ---- helpers -------------------------------------------------------

    def _make_reviewer(self, login: str, *, auto_assign: bool = True) -> User:
        user = User.objects.create(github_login=login)
        ReviewerPreference.objects.create(repository=self.repo, user=user, auto_assign=auto_assign)
        return user

    def _make_pr(self, number: int, *, assignees=None, state=PullRequestState.OPEN) -> PullRequest:
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            state=state,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            base_ref_name="master",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title=f"PR {number}",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=list(assignees or []),
        )

    def _make_snapshot(self, assignments: dict[int, str], *, cache_key=None, generated_at=None) -> ReviewerAssignmentSnapshot:
        return ReviewerAssignmentSnapshot.objects.create(
            repository=self.repo,
            cache_key=cache_key or self.cache_key,
            generated_at=generated_at or self.now,
            payload={"meta": {}, "automatic_assignments": {str(k): v for k, v in assignments.items()}},
            etag="etag",
            assignment_count=len(assignments),
        )

    def _apply(self, *, enabled=True, dry_run=False, max_per_repo=0, dedupe_days=7, max_age_hours=48, client=None, token="tok"):
        if client is None:
            client = MagicMock()
            # Mirror GitHub's "add assignees" response: the login lands in the assignee set.
            client.assign.side_effect = lambda **kwargs: (kwargs["github_login"],)
        sync = MagicMock()
        result = apply_assignments_for_repo(
            self.repo,
            run_date=self.run_date,
            now=self.now,
            enabled=enabled,
            dry_run=dry_run,
            dedupe_days=dedupe_days,
            max_age_hours=max_age_hours,
            max_per_repo=max_per_repo,
            token_resolver=lambda **kwargs: token,
            assignment_client=client,
            sync_enqueuer=sync,
        )
        return result, client, sync

    # ---- tests ---------------------------------------------------------

    def test_applies_proposal_and_records(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        result, client, sync = self._apply()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stats"]["applied"], 1)
        client.assign.assert_called_once_with(owner="leanprover-community", repo="mathlib4", number=101, github_login="alice")
        sync.assert_called_once_with("leanprover-community", "mathlib4", 101)
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101, reviewer_login="alice")
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_APPLIED)
        self.assertIsNotNone(record.applied_at)

    def test_skips_when_active_reviewer_already_assigned(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=["bob"])  # bob is an active reviewer

        result, client, _sync = self._apply()

        self.assertEqual(result["stats"]["skipped_already_assigned"], 1)
        self.assertEqual(result["stats"]["applied"], 0)
        client.assign.assert_not_called()
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_SKIPPED_ALREADY_ASSIGNED)

    def test_skips_when_not_open(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[], state=PullRequestState.MERGED)

        result, client, _sync = self._apply()

        self.assertEqual(result["stats"]["skipped_already_assigned"], 1)
        client.assign.assert_not_called()

    def test_skips_opted_out(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])
        ReviewerOptOut.objects.create(
            repository=self.repo, pr_number=101, reviewer_login="alice", active=True, opted_out_at=self.now
        )

        result, client, _sync = self._apply()

        self.assertEqual(result["stats"]["skipped_opted_out"], 1)
        client.assign.assert_not_called()

    def test_skips_ineligible_reviewer(self) -> None:
        self._make_snapshot({101: "carol"})  # carol has no ReviewerPreference
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply()

        self.assertEqual(result["stats"]["skipped_ineligible"], 1)
        client.assign.assert_not_called()

    def test_skips_recently_applied(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])
        ReviewerAssignmentApplication.objects.create(
            run_date=self.run_date - timedelta(days=1),
            repository=self.repo,
            pr_number=101,
            reviewer_login="alice",
            status=ReviewerAssignmentApplication.STATUS_APPLIED,
            applied_at=self.now - timedelta(days=1),
        )

        result, client, _sync = self._apply(dedupe_days=7)

        self.assertEqual(result["stats"]["skipped_recently_applied"], 1)
        client.assign.assert_not_called()

    def test_dry_run_records_without_mutating(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        result, client, sync = self._apply(enabled=True, dry_run=True)

        self.assertEqual(result["stats"]["skipped_dry_run"], 1)
        self.assertEqual(result["stats"]["applied"], 0)
        client.assign.assert_not_called()
        sync.assert_not_called()
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_SKIPPED_DRY_RUN)

    def test_dry_run_takes_precedence_over_disabled(self) -> None:
        # Preview mode is enabled=False, dry_run=True; the proposal must record
        # skipped_dry_run (not skipped_disabled).
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply(enabled=False, dry_run=True)

        self.assertEqual(result["stats"]["skipped_dry_run"], 1)
        self.assertEqual(result["stats"]["skipped_disabled"], 0)
        client.assign.assert_not_called()
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_SKIPPED_DRY_RUN)

    def test_disabled_without_dry_run_records_skipped_disabled(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply(enabled=False, dry_run=False)

        self.assertEqual(result["stats"]["skipped_disabled"], 1)
        self.assertEqual(result["stats"]["skipped_dry_run"], 0)
        client.assign.assert_not_called()

    def test_skips_when_no_token(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply(token=None)

        self.assertEqual(result["stats"]["skipped_no_token"], 1)
        client.assign.assert_not_called()

    def test_records_failure(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])
        client = MagicMock()
        client.assign.side_effect = AssignmentMutationError("github_transient", "boom")

        result, _client, sync = self._apply(client=client)

        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["applied"], 0)
        sync.assert_not_called()
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_FAILED)
        self.assertIn("boom", record.error)

    def test_records_failure_when_login_absent_from_result(self) -> None:
        # GitHub's add-assignees endpoint silently ignores an unassignable login: it
        # returns 200 without the login in the resulting set. That must record as a
        # failure (not a successful application) and must not enqueue a post-apply sync.
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])
        client = MagicMock()
        client.assign.return_value = ()  # login absent from the resulting assignee set

        result, _client, sync = self._apply(client=client)

        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["applied"], 0)
        sync.assert_not_called()
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_FAILED)
        self.assertIn("alice", record.error)
        self.assertIsNone(record.applied_at)

    def test_per_repo_cap_limits_mutations(self) -> None:
        self._make_snapshot({101: "alice", 102: "bob"})
        self._make_pr(101, assignees=[])
        self._make_pr(102, assignees=[])

        result, client, _sync = self._apply(max_per_repo=1)

        self.assertEqual(result["stats"]["applied"], 1)
        self.assertTrue(result["stats"]["capped"])
        self.assertEqual(result["stats"]["capped_remaining"], 1)
        self.assertEqual(client.assign.call_count, 1)
        # The capped proposal is left unrecorded so a later run can pick it up.
        self.assertFalse(ReviewerAssignmentApplication.objects.filter(repository=self.repo, pr_number=102).exists())

    def test_stale_snapshot_skipped(self) -> None:
        self._make_snapshot({101: "alice"}, generated_at=self.now - timedelta(days=3))
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply(max_age_hours=48)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "stale_snapshot")
        client.assign.assert_not_called()
        self.assertFalse(ReviewerAssignmentApplication.objects.filter(repository=self.repo).exists())

    def test_no_snapshot_for_default_ruleset_skipped(self) -> None:
        # A snapshot exists, but only under the wrong cache key (not the default ruleset id).
        self._make_snapshot({101: "alice"}, cache_key="default")
        self._make_pr(101, assignees=[])

        result, client, _sync = self._apply()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_snapshot")
        self.assertEqual(result["cache_key"], self.cache_key)
        client.assign.assert_not_called()

    def test_rerun_is_idempotent_within_day(self) -> None:
        self._make_snapshot({101: "alice"})
        self._make_pr(101, assignees=[])

        first, client1, _ = self._apply()
        self.assertEqual(first["stats"]["applied"], 1)

        # Second run on the same run_date: the row already exists -> already_recorded, no new mutation.
        second, client2, sync2 = self._apply()
        self.assertEqual(second["stats"]["applied"], 0)
        self.assertEqual(second["stats"]["skipped_already_recorded"], 1)
        client2.assign.assert_not_called()
        sync2.assert_not_called()
        self.assertEqual(ReviewerAssignmentApplication.objects.filter(repository=self.repo, pr_number=101).count(), 1)
