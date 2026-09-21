from __future__ import annotations

from datetime import datetime, timezone

from django.test import TestCase

from analyzer.models import PRDependency, QueueRuleSet
from analyzer.services.dependency_graph import DependencyGraphBuilder
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository, User
from syncer.models import CommitCheckRun, LabelDef, PRLabel, PullRequest
from syncer.models.ci_enums import CheckRunConclusion, CheckRunStatus
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
            head_sha=f"sha{number}",
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

    def _add_ci(self, pr: PullRequest, *, name: str = "lint") -> None:
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"cr-{pr.number}-{name}",
            head_sha=pr.head_sha or "",
            name=name,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

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
        self.assertEqual(
            nodes_by_id[1]["labels"],
            [
                {
                    "name": "t-alpha",
                    "color": "123456",
                    "url": f"https://github.com/{self.repo.owner}/{self.repo.name}/labels/t-alpha",
                }
            ],
        )
        self.assertIn("wip", [label["name"].lower() for label in nodes_by_id[2]["labels"]])
        # Transitive counts: 1 depends on 2, and 3 is unrelated.
        self.assertEqual((nodes_by_id[1]["upstream_count"], nodes_by_id[1]["downstream_count"]), (1, 0))
        self.assertEqual((nodes_by_id[2]["upstream_count"], nodes_by_id[2]["downstream_count"]), (0, 1))
        self.assertEqual((nodes_by_id[3]["upstream_count"], nodes_by_id[3]["downstream_count"]), (0, 0))

        links = graph["links"]
        self.assertEqual(links, [{"source": 1, "target": 2, "source_state": "open", "target_state": "open"}])

        metadata = graph["metadata"]
        self.assertEqual(metadata["total_prs"], 3)
        self.assertEqual(metadata["prs_with_dependencies"], 1)
        self.assertEqual(metadata["prs_that_are_dependencies"], 1)
        self.assertEqual(metadata["dependency_links"], 1)

    def test_transitive_counts_follow_a_chain(self):
        # 3 depends on 2 depends on 1: 1 blocks two PRs, which is what the
        # queueboard's "blocks" column and the graph tooltip both report.
        pr1 = self._make_pr(1)
        pr2 = self._make_pr(2)
        pr3 = self._make_pr(3)
        for pr, dep in ((pr2, pr1), (pr3, pr2)):
            PRDependency.objects.create(
                pull_request=pr,
                depends_on_repository=self.repo,
                depends_on_number=dep.number,
                depends_on_pull_request=dep,
            )

        snapshot = QueueboardSnapshotBuilder(chunk_size=2).build(self.repo)
        nodes_by_id = {
            node["id"]: node for node in DependencyGraphBuilder().build(repository=self.repo, snapshot=snapshot)["nodes"]
        }

        self.assertEqual((nodes_by_id[1]["upstream_count"], nodes_by_id[1]["downstream_count"]), (0, 2))
        self.assertEqual((nodes_by_id[2]["upstream_count"], nodes_by_id[2]["downstream_count"]), (1, 1))
        self.assertEqual((nodes_by_id[3]["upstream_count"], nodes_by_id[3]["downstream_count"]), (2, 0))
        # Direct counts stay direct.
        self.assertEqual(nodes_by_id[1]["dependent_count"], 1)

    def test_marks_prs_on_the_review_queue(self):
        pr1 = self._make_pr(1, labels=("t-analysis",))
        pr2 = self._make_pr(2, labels=("awaiting-zulip",))
        self._make_pr(3, is_draft=True)
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
            forbidden_label_names=["awaiting-zulip"],
        )
        # Both get passing CI, so the only thing keeping PR 2 off the queue is its label.
        self._add_ci(pr1)
        self._add_ci(pr2)

        snapshot = QueueboardSnapshotBuilder(chunk_size=2).build(self.repo, rule_set=rule_set)
        graph = DependencyGraphBuilder().build(repository=self.repo, snapshot=snapshot)
        nodes_by_id = {node["id"]: node for node in graph["nodes"]}

        self.assertEqual(snapshot["lists"]["dashboards"]["Queue"], [1])
        self.assertTrue(nodes_by_id[1]["on_queue"])
        self.assertFalse(nodes_by_id[2]["on_queue"])
        self.assertFalse(nodes_by_id[3]["on_queue"])
        self.assertEqual(nodes_by_id[1]["ci_status"], "pass")
        # PR 1 is opened from a branch of the repository itself, so determine_PR_status
        # short-circuits to NotFromFork before it ever looks at labels or CI — while the PR is
        # nonetheless on the review queue. The graph must colour it by on_queue, not by
        # pr_status; see docs/design-decisions/055-dependency-graph-queue-status-colouring.md.
        # ... and PRs 2 and 3, which are *not* queueable, carry the very same pr_status, so
        # that field alone cannot tell the three apart.
        self.assertEqual(
            [nodes_by_id[number]["pr_status"] for number in (1, 2, 3)],
            ["NotFromFork", "NotFromFork", "NotFromFork"],
        )
        # The fork-blind status is what actually separates them, and is what the graph colours by.
        self.assertEqual(
            [nodes_by_id[number]["pr_status_ignoring_fork"] for number in (1, 2, 3)],
            ["AwaitingReview", "AwaitingDecision", "NotReady"],
        )
        self.assertEqual(graph["metadata"]["prs_on_queue"], 1)

    def test_maintainer_merge_prs_are_flagged_although_still_on_the_queue(self):
        # maintainer-merge means "reviewed; a maintainer must merge it". Such a PR stays on the
        # review queue, so on_queue alone would paint it as needing review.
        pr1 = self._make_pr(1, labels=("t-analysis", "maintainer-merge"))
        pr2 = self._make_pr(2, labels=("t-analysis",))
        # ready-to-merge takes over from maintainer-merge: that PR is bors' problem now, and
        # the AllMaintainerMerge list excludes it.
        pr3 = self._make_pr(3, labels=("maintainer-merge", "ready-to-merge"))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        for pr in (pr1, pr2, pr3):
            self._add_ci(pr)

        snapshot = QueueboardSnapshotBuilder(chunk_size=2).build(self.repo, rule_set=rule_set)
        nodes_by_id = {
            node["id"]: node for node in DependencyGraphBuilder().build(repository=self.repo, snapshot=snapshot)["nodes"]
        }

        self.assertEqual(snapshot["lists"]["dashboards"]["AllMaintainerMerge"], [1])
        self.assertTrue(nodes_by_id[1]["on_queue"])
        self.assertTrue(nodes_by_id[1]["awaiting_maintainer_merge"])
        self.assertFalse(nodes_by_id[2]["awaiting_maintainer_merge"])
        self.assertFalse(nodes_by_id[3]["awaiting_maintainer_merge"])
        self.assertEqual(nodes_by_id[3]["pr_status_ignoring_fork"], "AwaitingBors")

    def test_build_tolerates_payload_without_lists_or_label_dicts(self):
        # Snapshots persisted before these fields existed (and hand-written fixtures) must
        # still build: string labels degrade to a colourless chip, absent lists to off-queue.
        snapshot = {
            "prs": {
                "1": {"state": "open", "labels": ["t-alpha"], "direct_dependencies": ["2"]},
                "2": {"state": "open", "labels": [], "direct_dependencies": []},
            }
        }
        graph = DependencyGraphBuilder().build(repository=self.repo, snapshot=snapshot)
        nodes_by_id = {node["id"]: node for node in graph["nodes"]}

        self.assertEqual(nodes_by_id[1]["labels"], [{"name": "t-alpha", "color": None, "url": None}])
        self.assertFalse(nodes_by_id[1]["on_queue"])
        self.assertIsNone(nodes_by_id[1]["pr_status"])
        self.assertFalse(nodes_by_id[1]["awaiting_maintainer_merge"])
        # Recomputed from the labels/CI in the payload, so it is present even when pr_status is not.
        self.assertEqual(nodes_by_id[1]["pr_status_ignoring_fork"], "NotReady")
        self.assertEqual(nodes_by_id[2]["downstream_count"], 1)
        self.assertEqual(graph["metadata"]["prs_on_queue"], 0)
