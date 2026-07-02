from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from syncer.models import CommitCheckRun, CommitStatusContext
from syncer.tasks.sync_tasks import expire_stale_ci_for_repo_task
from syncer.tests.factories import make_repo, make_pr


def _cr(
    repo, *, node_id, sha="sha1", name="build", status="IN_PROGRESS", conclusion=None, started_delta=None, created_delta=None
):
    now = timezone.now()
    return CommitCheckRun.objects.create(
        repository=repo,
        github_node_id=node_id,
        head_sha=sha,
        name=name,
        status=status,
        conclusion=conclusion,
        gh_started_at=now - started_delta if started_delta else None,
        gh_completed_at=None,
        last_synced_at=None,
    )


def _sc(repo, *, node_id, sha="sha1", name="bors", state="PENDING", created_delta=None, rest_id=None):
    now = timezone.now()
    return CommitStatusContext.objects.create(
        repository=repo,
        github_node_id=node_id if rest_id is None else None,
        rest_id=rest_id,
        head_sha=sha,
        name=name,
        state=state,
        target_url=None,
        description="",
        gh_created_at=now - created_delta if created_delta else now - timedelta(hours=1),
        last_synced_at=None,
    )


class TestExpireStaleCITask(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        make_pr(self.repo, 1)

    # ------------------------------------------------------------------ #
    # Pass 1: stale pending check runs                                    #
    # ------------------------------------------------------------------ #

    def test_pass1_deletes_old_pending_check_run(self) -> None:
        _cr(self.repo, node_id="OLD", started_delta=timedelta(days=40))
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitCheckRun.objects.count(), 0)
        self.assertEqual(result["deleted_stale_pending_check_runs"], 1)

    def test_pass1_keeps_recent_pending_check_run(self) -> None:
        _cr(self.repo, node_id="NEW", started_delta=timedelta(days=5))
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitCheckRun.objects.count(), 1)

    def test_pass1_keeps_completed_check_run_regardless_of_age(self) -> None:
        _cr(self.repo, node_id="DONE", status="COMPLETED", conclusion="SUCCESS", started_delta=timedelta(days=60))
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitCheckRun.objects.count(), 1)

    def test_pass1_skipped_when_stale_pending_days_zero(self) -> None:
        _cr(self.repo, node_id="OLD", started_delta=timedelta(days=40))
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertEqual(CommitCheckRun.objects.count(), 1)
        self.assertEqual(result["deleted_stale_pending_check_runs"], 0)

    # ------------------------------------------------------------------ #
    # Pass 2: stale pending status contexts                               #
    # ------------------------------------------------------------------ #

    def test_pass2_deletes_old_pending_status_context(self) -> None:
        _sc(self.repo, node_id="OLD_SC", created_delta=timedelta(days=40))
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitStatusContext.objects.count(), 0)
        self.assertEqual(result["deleted_stale_pending_status_contexts"], 1)

    def test_pass2_keeps_recent_pending_status_context(self) -> None:
        _sc(self.repo, node_id="NEW_SC", created_delta=timedelta(days=5))
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

    def test_pass2_keeps_non_pending_status_context(self) -> None:
        _sc(self.repo, node_id="SUCCESS_SC", state="SUCCESS", created_delta=timedelta(days=60))
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

    # ------------------------------------------------------------------ #
    # Pass 3: superseded check runs                                       #
    # ------------------------------------------------------------------ #

    def test_pass3_deletes_older_of_two_same_sha_name_rows(self) -> None:
        old = _cr(
            self.repo,
            node_id="CR_OLD",
            sha="sha1",
            name="build",
            status="COMPLETED",
            conclusion="FAILURE",
            started_delta=timedelta(hours=2),
        )
        new = _cr(
            self.repo,
            node_id="CR_NEW",
            sha="sha1",
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            started_delta=timedelta(hours=1),
        )
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertFalse(CommitCheckRun.objects.filter(pk=old.pk).exists())
        self.assertTrue(CommitCheckRun.objects.filter(pk=new.pk).exists())
        self.assertEqual(result["deleted_superseded_check_runs"], 1)

    def test_pass3_keeps_unique_sha_name_combinations(self) -> None:
        _cr(self.repo, node_id="CR_A", sha="sha1", name="build")
        _cr(self.repo, node_id="CR_B", sha="sha1", name="lint")
        _cr(self.repo, node_id="CR_C", sha="sha2", name="build")
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertEqual(CommitCheckRun.objects.count(), 3)
        self.assertEqual(result["deleted_superseded_check_runs"], 0)

    def test_pass3_superseded_pending_deleted_when_newer_completed_exists(self) -> None:
        old_pending = _cr(
            self.repo, node_id="CR_PEND", sha="sha1", name="build", status="IN_PROGRESS", started_delta=timedelta(hours=2)
        )
        new_done = _cr(
            self.repo,
            node_id="CR_DONE",
            sha="sha1",
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            started_delta=timedelta(hours=1),
        )
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertFalse(CommitCheckRun.objects.filter(pk=old_pending.pk).exists())
        self.assertTrue(CommitCheckRun.objects.filter(pk=new_done.pk).exists())

    # ------------------------------------------------------------------ #
    # Pass 4: superseded status contexts                                  #
    # ------------------------------------------------------------------ #

    def test_pass4_deletes_older_of_two_same_sha_name_rows(self) -> None:
        old = _sc(self.repo, node_id="SC_OLD", sha="sha1", name="bors", state="FAILURE", created_delta=timedelta(hours=2))
        new = _sc(self.repo, node_id="SC_NEW", sha="sha1", name="bors", state="SUCCESS", created_delta=timedelta(hours=1))
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertFalse(CommitStatusContext.objects.filter(pk=old.pk).exists())
        self.assertTrue(CommitStatusContext.objects.filter(pk=new.pk).exists())
        self.assertEqual(result["deleted_superseded_status_contexts"], 1)

    def test_pass4_preserves_rest_id_rows(self) -> None:
        """REST history rows (rest_id set) must never be deleted by superseded cleanup."""
        rest1 = _sc(
            self.repo, node_id=None, sha="sha1", name="bors", state="FAILURE", rest_id=101, created_delta=timedelta(hours=2)
        )
        rest2 = _sc(
            self.repo, node_id=None, sha="sha1", name="bors", state="SUCCESS", rest_id=102, created_delta=timedelta(hours=1)
        )
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)
        self.assertTrue(CommitStatusContext.objects.filter(pk=rest1.pk).exists())
        self.assertTrue(CommitStatusContext.objects.filter(pk=rest2.pk).exists())
        self.assertEqual(result["deleted_superseded_status_contexts"], 0)

    # ------------------------------------------------------------------ #
    # Batched deletion                                                    #
    # ------------------------------------------------------------------ #

    def test_batched_deletion_converges_with_tiny_batch_size(self) -> None:
        """All passes drain fully even when each batch holds a single row."""
        from unittest.mock import patch

        # Pass 1 fodder: two stale pending check runs.
        _cr(self.repo, node_id="STALE_A", sha="sA", started_delta=timedelta(days=40))
        _cr(self.repo, node_id="STALE_B", sha="sB", started_delta=timedelta(days=41))
        # Pass 3 fodder: three superseded + one latest in the same group.
        for i in range(4):
            _cr(self.repo, node_id=f"SUP_{i}", sha="shaX", name="build", status="COMPLETED", conclusion="SUCCESS")

        with patch("syncer.tasks.sync_tasks._EXPIRE_DELETE_BATCH_SIZE", 1):
            result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)

        self.assertEqual(result["deleted_stale_pending_check_runs"], 2)
        self.assertEqual(result["deleted_superseded_check_runs"], 3)
        remaining = CommitCheckRun.objects.filter(repository=self.repo)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.get().github_node_id, "SUP_3")

    # ------------------------------------------------------------------ #
    # Cross-repo isolation                                                #
    # ------------------------------------------------------------------ #

    def test_other_repo_rows_untouched(self) -> None:
        other_repo = make_repo(owner="other-owner", name="other-repo")
        make_pr(other_repo, 1)
        other_cr = _cr(other_repo, node_id="OTHER_CR", started_delta=timedelta(days=40))
        _cr(self.repo, node_id="THIS_CR", started_delta=timedelta(days=40))
        expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        self.assertTrue(CommitCheckRun.objects.filter(pk=other_cr.pk).exists())
        self.assertFalse(CommitCheckRun.objects.filter(repository=self.repo).exists())

    # ------------------------------------------------------------------ #
    # Return dict shape                                                   #
    # ------------------------------------------------------------------ #

    def test_result_contains_expected_keys(self) -> None:
        result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)
        for key in (
            "repo",
            "repo_id",
            "stale_pending_days",
            "deleted_stale_pending_check_runs",
            "deleted_stale_pending_status_contexts",
            "deleted_superseded_check_runs",
            "deleted_superseded_status_contexts",
        ):
            self.assertIn(key, result)
