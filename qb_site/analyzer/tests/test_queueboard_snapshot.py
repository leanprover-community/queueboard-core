from __future__ import annotations

from datetime import datetime, timezone

from django.test import TestCase

from analyzer.models import PRDependency
from analyzer.models.queue_snapshot import QueueSnapshot
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository, User
from syncer.models import LabelDef, PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState
from syncer.models.check_run import CheckRun, CheckRunConclusion, CheckRunStatus


class QueueboardSnapshotBuilderTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _make_pr(
        self,
        number: int,
        *,
        is_draft: bool = False,
        base: str = "master",
        labels: tuple[str, ...] = (),
        author: User | None = None,
        body: str = "description",
    ) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=author,
            state=PullRequestState.OPEN,
            is_draft=is_draft,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            closed_at=None,
            merged_at=None,
            base_ref_name=base,
            head_ref_name=f"feature/{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body=body,
            additions=1,
            deletions=1,
            changed_files_count=1,
            files=["src/file.py"],
            assignees=[],
            approvals=[],
            commenters=[],
            number_total_comments=0,
            last_synced_at=self.now,
            engagement_synced_at=self.now,
            files_incomplete=False,
            assignees_incomplete=False,
            reviews_incomplete=False,
            comments_incomplete=False,
        )
        for label_name in labels:
            label_def, _ = LabelDef.objects.get_or_create(repository=self.repo, name=label_name, defaults={"color": "123456"})
            PRLabel.objects.create(pull_request=pr, label_def=label_def)
        return pr

    def test_builds_snapshot_with_queue_filters(self):
        pr1 = self._make_pr(1, author=self.user, labels=("t-analysis",))
        pr2 = self._make_pr(2, labels=("awaiting-zulip",))
        pr3 = self._make_pr(3, is_draft=True)

        # CI success for pr1
        CheckRun.objects.create(
            pull_request=pr1,
            github_node_id="cr1",
            head_sha="a" * 40,
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        # Dependency edge for pr1
        PRDependency.objects.create(
            pull_request=pr1,
            depends_on_repository=self.repo,
            depends_on_number=42,
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)

        self.assertEqual(snapshot["meta"]["repository"], "leanprover-community/mathlib4")
        self.assertEqual(snapshot["meta"]["schema_version"], "v1-draft")

        prs = snapshot["prs"]
        self.assertIn(1, prs)
        self.assertIn(2, prs)
        self.assertEqual(prs[1]["ci_status"], "pass")
        self.assertEqual(prs[2]["ci_status"], "missing")
        self.assertEqual(prs[1]["pr_status"], "AwaitingReview")
        self.assertEqual(prs[2]["pr_status"], "NotReady")
        self.assertEqual(prs[1]["direct_dependencies"], [42])
        self.assertEqual(prs[1]["labels"][0]["name"], "t-analysis")
        self.assertEqual(prs[1]["labels"][0]["url"], "https://github.com/leanprover-community/mathlib4/labels/t-analysis")

        self.assertEqual(set(snapshot["lists"]["nondraft_prs"]), {1, 2})
        self.assertEqual(set(snapshot["lists"]["draft_prs"]), {3})
        self.assertEqual(snapshot["lists"]["dashboards"]["Queue"], [1])
        self.assertEqual(snapshot["lists"]["dashboards"]["NeedsDecision"], [2])

    def test_build_and_store_creates_snapshot_row(self):
        pr1 = self._make_pr(10, labels=("t-analysis",))
        CheckRun.objects.create(
            pull_request=pr1,
            github_node_id="cr10",
            head_sha="a" * 40,
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        builder = QueueboardSnapshotBuilder(chunk_size=5)
        obj = builder.build_and_store(self.repo, cache_key="default")

        self.assertEqual(obj.repository, self.repo)
        self.assertEqual(obj.cache_key, "default")
        self.assertEqual(obj.pr_count, 1)
        self.assertEqual(obj.queue_count, 1)
        self.assertIsNone(obj.expires_at)
        self.assertEqual(QueueSnapshot.objects.count(), 1)

        # Etag should be consistent with the payload content
        from hashlib import sha256
        import json as _json

        expected_etag = sha256(_json.dumps(obj.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(obj.etag, expected_etag)

    def test_build_and_store_updates_existing_snapshot(self):
        pr1 = self._make_pr(20, labels=("t-analysis",))
        builder = QueueboardSnapshotBuilder(chunk_size=2)
        first = builder.build_and_store(self.repo, cache_key="k")
        self.assertEqual(QueueSnapshot.objects.count(), 1)
        self.assertEqual(first.queue_count, 0)  # no CI yet

        # Add CI and a second PR off-queue
        CheckRun.objects.create(
            pull_request=pr1,
            github_node_id="cr20",
            head_sha="b" * 40,
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )
        self._make_pr(21, labels=("awaiting-zulip",))

        updated = builder.build_and_store(self.repo, cache_key="k")
        self.assertEqual(QueueSnapshot.objects.count(), 1)
        self.assertEqual(updated.pr_count, 2)
        self.assertEqual(updated.queue_count, 1)
        self.assertGreater(updated.generated_at, first.generated_at)
