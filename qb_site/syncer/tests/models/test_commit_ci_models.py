from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from syncer.models import CommitCheckRun, CommitStatusContext
from syncer.tests.factories import make_repo


class CommitCIModelsTests(TestCase):
    def test_commit_check_run_unique_github_node_id(self) -> None:
        repo = make_repo()
        CommitCheckRun.objects.create(
            repository=repo,
            github_node_id="CR_node_1",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CommitCheckRun.objects.create(
                    repository=repo,
                    github_node_id="CR_node_1",
                    head_sha="b" * 40,
                    name="build",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                )

    def test_commit_check_run_unique_external_id_per_repo_sha_and_name(self) -> None:
        repo = make_repo()
        CommitCheckRun.objects.create(
            repository=repo,
            head_sha="a" * 40,
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            external_id="ext-1",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CommitCheckRun.objects.create(
                    repository=repo,
                    head_sha="a" * 40,
                    name="lint",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                    external_id="ext-1",
                )

    def test_commit_status_context_unique_provider_ids(self) -> None:
        repo = make_repo()
        now = timezone.now()
        CommitStatusContext.objects.create(
            repository=repo,
            github_node_id="SC_node_1",
            rest_id=101,
            head_sha="a" * 40,
            name="ci/status",
            state="SUCCESS",
            gh_created_at=now,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CommitStatusContext.objects.create(
                    repository=repo,
                    github_node_id="SC_node_1",
                    rest_id=102,
                    head_sha="b" * 40,
                    name="ci/status",
                    state="SUCCESS",
                    gh_created_at=now,
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CommitStatusContext.objects.create(
                    repository=repo,
                    github_node_id="SC_node_2",
                    rest_id=101,
                    head_sha="b" * 40,
                    name="ci/status-2",
                    state="PENDING",
                    gh_created_at=now,
                )

    def test_commit_ci_rows_are_queryable_by_repository_and_sha(self) -> None:
        repo = make_repo()
        other_repo = make_repo(owner="other", name="repo")
        now = timezone.now()
        target_sha = "abc" * 13 + "a"

        CommitCheckRun.objects.create(
            repository=repo,
            head_sha=target_sha,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
        )
        CommitCheckRun.objects.create(
            repository=other_repo,
            head_sha=target_sha,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
        )
        CommitStatusContext.objects.create(
            repository=repo,
            head_sha=target_sha,
            name="ci/status",
            state="SUCCESS",
            gh_created_at=now,
        )
        CommitStatusContext.objects.create(
            repository=repo,
            head_sha="b" * 40,
            name="ci/status",
            state="PENDING",
            gh_created_at=now,
        )

        self.assertEqual(CommitCheckRun.objects.filter(repository=repo, head_sha=target_sha).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=repo, head_sha=target_sha).count(), 1)
