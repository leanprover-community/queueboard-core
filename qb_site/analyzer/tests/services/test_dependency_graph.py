from __future__ import annotations

from datetime import datetime, timezone

from django.test import TestCase

from analyzer.models import PRDependency
from analyzer.services.dependency_graph import DependencyGraphBuilder
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository, User
from syncer.models import LabelDef, PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState


class DependencyGraphBuilderTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _make_pr(
        self,
        number: int,
        *,
        is_draft: bool = False,
        labels: tuple[str, ...] = (),
    ) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=self.user,
            state=PullRequestState.OPEN,
            is_draft=is_draft,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            closed_at=None,
            merged_at=None,
            base_ref_name="master",
            head_ref_name=f"feature/{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body="desc",
            additions=1,
            deletions=1,
            changed_files_count=1,
            files=[],
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
            label_def, _ = LabelDef.objects.get_or_create(
                repository=self.repo,
                name=label_name,
                defaults={"color": "123456"},
            )
            PRLabel.objects.create(pull_request=pr, label_def=label_def)
        return pr

    def test_build_filters_missing_dependencies_and_marks_drafts(self):
        pr1 = self._make_pr(1, is_draft=True, labels=("t-alpha",))
        pr2 = self._make_pr(2, labels=("wip",))
        self._make_pr(3)

        PRDependency.objects.create(
            pull_request=pr1,
            depends_on_repository=self.repo,
            depends_on_number=pr2.number,
            depends_on_pull_request=pr2,
        )
        # This dependency points to a PR not present in the snapshot and should be ignored.
        PRDependency.objects.create(
            pull_request=pr1,
            depends_on_repository=self.repo,
            depends_on_number=99,
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=2).build(self.repo)
        graph = DependencyGraphBuilder().build(repository=self.repo, snapshot=snapshot)

        nodes_by_id = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(len(nodes_by_id), 3)
        self.assertTrue(nodes_by_id[1]["is_draft"])  # draft flag
        self.assertTrue(nodes_by_id[2]["is_draft"])  # WIP label
        self.assertFalse(nodes_by_id[3]["is_draft"])
        self.assertEqual(nodes_by_id[2]["url"], f"https://github.com/{self.repo.owner}/{self.repo.name}/pull/2")
        self.assertEqual(nodes_by_id[2]["state"], "open")
        self.assertIn("wip", [name.lower() for name in nodes_by_id[2]["labels"]])

        links = graph["links"]
        self.assertEqual(links, [{"source": 1, "target": 2, "source_state": "open", "target_state": "open"}])

        metadata = graph["metadata"]
        self.assertEqual(metadata["total_prs"], 3)
        self.assertEqual(metadata["prs_with_dependencies"], 1)
        self.assertEqual(metadata["prs_that_are_dependencies"], 1)
        self.assertEqual(metadata["dependency_links"], 1)
