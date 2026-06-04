from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase

from analyzer.services.reviewer_assignment import (
    AreaStatsBuilder,
    PRAssignmentPriority,
    ReviewerAssignmentBuilder,
    ReviewerProfile,
    _filter_assignment_forbidden_prs,
    collect_assignment_statistics,
    compute_area_stats,
    rank_prs_for_assignment,
    suggest_reviewer_for_pr,
    suggest_reviewers_many,
)
from core.models import Repository, ReviewerPreference, User
from analyzer.models import QueueRuleSet, ReviewerOptOut
from syncer.models.ci_enums import CheckRunConclusion, CheckRunStatus
from syncer.services.pr_sync_service import PRSyncService
from syncer.models import CommitCheckRun, LabelDef, PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState


class ReviewerAssignmentServiceTests(SimpleTestCase):
    def setUp(self):
        self.snapshot = {
            "meta": {"generated_at": "2025-01-01T00:00:00Z"},
            "prs": {
                1: {
                    "assignees": ["alice"],
                    "author": "bob",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
                2: {
                    "assignees": ["alice"],
                    "author": "alice",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 150},
                },
                3: {
                    "assignees": ["carol"],
                    "author": "dan",
                    "pr_status": "AwaitingAuthor",
                    "last_status_change": None,
                    "labels": [{"name": "t-algebra", "color": "abcdef"}],
                    "total_queue_time": {"status": "missing", "value_td": None},
                },
                4: {
                    "assignees": [],
                    "author": "dave",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 1}},
                    "labels": [{"name": "t-analysis", "color": "654321"}],
                    "total_queue_time": {"status": "valid", "value_td": 150},
                },
            },
            "lists": {"dashboards": {"Queue": [1, 4], "QueueStaleUnassigned": [4]}},
        }
        self.reviewers = [
            ReviewerProfile(
                github_login="alice",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
            ReviewerProfile(
                github_login="bob",
                maximum_capacity=5,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
        ]

    def test_collects_assignment_stats_and_suggests_available_reviewer(self):
        stats = collect_assignment_statistics(self.snapshot)

        self.assertEqual(stats.assignments["alice"][0], [1, 2])
        self.assertAlmostEqual(stats.assignments["alice"][1], 1.0)  # self-assign ignored
        self.assertEqual(stats.assignments["alice"][2], 2)

        result = suggest_reviewer_for_pr(
            pr_number=4,
            pr_entry=self.snapshot["prs"][4],
            reviewers=self.reviewers,
            assignment_stats=stats.assignments,
            rng=random.Random(0),
        )

        self.assertEqual(result.all_potential_reviewers, ["bob", "alice"])
        self.assertEqual(result.all_available_reviewers, ["bob"])
        self.assertEqual(result.suggested, "bob")

    def test_computes_area_stats_with_missing_queue_time(self):
        stats = collect_assignment_statistics(self.snapshot)
        area_stats = compute_area_stats(
            existing_assignments=stats.assignments,
            reviewers=self.reviewers,
            queue_pr_numbers=[1, 4],
            all_prs=self.snapshot["prs"],
            rng=random.Random(0),
        )

        analysis = area_stats["t-analysis"]
        self.assertEqual(analysis["assigned"], 1)
        self.assertEqual(analysis["unassigned"], 1)
        self.assertEqual(analysis["on_queue"], 2)
        self.assertEqual(analysis["total_queue_time"], 200)
        self.assertAlmostEqual(analysis["avg_queue_time"], 100.0)
        self.assertEqual(analysis["assigned_queue_time"], 50)
        self.assertAlmostEqual(analysis["avg_assigned_queue_time"], 50.0)
        self.assertEqual(analysis["num_reviewers"], 2)
        self.assertEqual(analysis["num_reviewers_on_rotation"], 2)
        self.assertFalse(analysis["at_max_capacity"])

    def test_opt_out_excludes_from_available_pool(self):
        stats = collect_assignment_statistics(self.snapshot)

        result = suggest_reviewer_for_pr(
            pr_number=4,
            pr_entry=self.snapshot["prs"][4],
            reviewers=self.reviewers,
            assignment_stats=stats.assignments,
            rng=random.Random(0),
            excluded_logins={"bob"},
        )

        self.assertEqual(result.all_potential_reviewers, ["bob", "alice"])
        self.assertEqual(result.all_available_reviewers, [])
        self.assertIsNone(result.suggested)

    def test_filter_assignment_forbidden_prs_drops_matching_labels(self):
        all_prs = {
            1: {"labels": [{"name": "t-analysis"}]},
            2: {"labels": [{"name": "t-analysis"}, {"name": "maintainer-merge"}]},
            3: {"labels": [{"name": "Maintainer-Merge"}]},  # matched case-insensitively
        }
        kept = _filter_assignment_forbidden_prs([1, 2, 3], all_prs=all_prs, forbidden_labels={"maintainer-merge"})
        self.assertEqual(kept, [1])

    def test_filter_assignment_forbidden_prs_is_noop_without_forbidden_labels(self):
        all_prs = {1: {"labels": [{"name": "maintainer-merge"}]}}
        self.assertEqual(_filter_assignment_forbidden_prs([1], all_prs=all_prs, forbidden_labels=set()), [1])

    def test_pr_without_topic_label_is_not_auto_assigned(self):
        result = suggest_reviewer_for_pr(
            pr_number=5,
            pr_entry={
                "assignees": [],
                "author": "dave",
                "title": "fix: unlabeled",
                "labels": [],
                "total_queue_time": {"status": "valid", "value_td": 100},
            },
            reviewers=self.reviewers,
            assignment_stats={},
            rng=random.Random(0),
        )

        self.assertEqual(result.reason, "missing-topic-label")
        self.assertEqual(result.all_potential_reviewers, [])
        self.assertEqual(result.all_available_reviewers, [])

    def test_rank_prs_for_assignment_defaults_to_input_order(self):
        stats = collect_assignment_statistics(self.snapshot)

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[4, 1],
            all_prs=self.snapshot["prs"],
            reviewers=self.reviewers,
            assignment_stats=stats.assignments,
        )

        self.assertEqual(ordered, [4, 1])
        self.assertEqual(trace["4"]["input_index"], 0)
        self.assertEqual(trace["4"]["output_index"], 0)
        self.assertEqual(trace["4"]["details"]["available_reviewer_count"], 1)

    def test_default_priority_prefers_assignable_prs_over_unassignable_prs(self):
        snapshot = {
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "title": "fix: alpha",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 100},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "title": "fix: beta",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 200},
                },
            }
        }
        reviewers = [
            ReviewerProfile(
                github_login="alice",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            )
        ]

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[1, 2],
            all_prs=snapshot["prs"],
            reviewers=reviewers,
            assignment_stats={},
            excluded_by_pr={1: {"alice"}},
        )

        self.assertEqual(ordered, [2, 1])
        self.assertFalse(trace["1"]["details"]["assignable_now"])
        self.assertTrue(trace["2"]["details"]["assignable_now"])

    def test_default_priority_treats_missing_topic_label_as_unassignable(self):
        snapshot = {
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "title": "fix: unlabeled",
                    "labels": [],
                    "total_queue_time": {"status": "valid", "value_td": 1000},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "title": "fix: labeled",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 10},
                },
            }
        }

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[1, 2],
            all_prs=snapshot["prs"],
            reviewers=self.reviewers,
            assignment_stats={},
        )

        self.assertEqual(ordered, [2, 1])
        self.assertFalse(trace["1"]["details"]["assignable_now"])
        self.assertFalse(trace["1"]["details"]["has_topic_label"])

    def test_default_priority_prefers_scarcer_prs_before_older_prs(self):
        snapshot = {
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "title": "fix: scarce",
                    "labels": [{"name": "t-zeta", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 10},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "title": "fix: older",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 1000},
                },
            }
        }
        reviewers = [
            ReviewerProfile(
                github_login="alice",
                maximum_capacity=2,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-zeta", "t-analysis"],
                preferred_labels_lower={"t-zeta", "t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
            ReviewerProfile(
                github_login="bob",
                maximum_capacity=2,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
        ]

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[2, 1],
            all_prs=snapshot["prs"],
            reviewers=reviewers,
            assignment_stats={},
        )

        self.assertEqual(ordered, [1, 2])
        self.assertEqual(trace["1"]["details"]["available_reviewer_count"], 1)
        self.assertEqual(trace["2"]["details"]["available_reviewer_count"], 2)

    def test_default_priority_prefers_older_prs_before_feat_bonus(self):
        snapshot = {
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "title": "fix: older",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 1000},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "title": "feat: newer",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 100},
                },
            }
        }

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[2, 1],
            all_prs=snapshot["prs"],
            reviewers=self.reviewers,
            assignment_stats={},
        )

        self.assertEqual(ordered, [1, 2])
        self.assertEqual(trace["2"]["details"]["title_priority"], 0)

    def test_default_priority_uses_feat_bonus_as_tiebreak(self):
        snapshot = {
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "title": "fix: alpha",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 100},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "title": "feat: beta",
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 100},
                },
            }
        }

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[1, 2],
            all_prs=snapshot["prs"],
            reviewers=self.reviewers,
            assignment_stats={},
        )

        self.assertEqual(ordered, [2, 1])
        self.assertEqual(trace["2"]["details"]["title_priority"], 0)
        self.assertEqual(trace["1"]["details"]["title_priority"], 1)

    def test_rank_prs_for_assignment_accepts_custom_priority_scorer(self):
        stats = collect_assignment_statistics(self.snapshot)

        def scorer(pr_number, pr_entry, reviewers, assignment_stats, excluded_logins):
            del pr_entry, reviewers, assignment_stats, excluded_logins
            return PRAssignmentPriority(sort_key=(-pr_number,), details={"priority_score": pr_number})

        ordered, trace = rank_prs_for_assignment(
            prs_to_assign=[1, 4],
            all_prs=self.snapshot["prs"],
            reviewers=self.reviewers,
            assignment_stats=stats.assignments,
            priority_scorer=scorer,
        )

        self.assertEqual(ordered, [4, 1])
        self.assertEqual(trace["4"]["details"], {"priority_score": 4})
        self.assertEqual(trace["4"]["output_index"], 0)

    def test_suggest_reviewers_many_uses_ranked_pr_order(self):
        snapshot = {
            "meta": {"generated_at": "2025-01-01T00:00:00Z"},
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
            },
        }
        reviewers = [
            ReviewerProfile(
                github_login="alice",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            )
        ]

        def scorer(pr_number, pr_entry, reviewers, assignment_stats, excluded_logins):
            del pr_entry, reviewers, assignment_stats, excluded_logins
            return PRAssignmentPriority(sort_key=(0 if pr_number == 2 else 1,))

        suggestions = suggest_reviewers_many(
            reviewers=reviewers,
            assignments={},
            prs_to_assign=[1, 2],
            all_prs=snapshot["prs"],
            rng=random.Random(0),
            priority_scorer=scorer,
        )

        self.assertEqual(suggestions, {2: "alice"})

    def test_suggest_reviewers_many_recomputes_priority_after_each_assignment(self):
        snapshot = {
            "meta": {"generated_at": "2025-01-01T00:00:00Z"},
            "prs": {
                1: {
                    "assignees": [],
                    "author": "dave",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-zeta", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
                2: {
                    "assignees": [],
                    "author": "erin",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-analysis", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
                3: {
                    "assignees": [],
                    "author": "frank",
                    "pr_status": "AwaitingReview",
                    "last_status_change": {"status": "valid", "delta": {"days": 0}},
                    "labels": [{"name": "t-algebra", "color": "123456"}],
                    "total_queue_time": {"status": "valid", "value_td": 50},
                },
            },
        }
        reviewers = [
            ReviewerProfile(
                github_login="alice",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-zeta", "t-analysis"],
                preferred_labels_lower={"t-zeta", "t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
            ReviewerProfile(
                github_login="bob",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis", "t-algebra"],
                preferred_labels_lower={"t-analysis", "t-algebra"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
            ReviewerProfile(
                github_login="carol",
                maximum_capacity=1,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=["t-analysis"],
                preferred_labels_lower={"t-analysis"},
                free_form="",
                conflict_of_interest=[],
                conflict_of_interest_lower=set(),
            ),
        ]
        existing_assignments = {
            "carol": ([], 0.99, 0),
        }

        def scorer(pr_number, pr_entry, reviewers, assignment_stats, excluded_logins):
            del pr_entry, excluded_logins

            def remaining_capacity(login):
                current = assignment_stats.get(login, ([], 0.0, 0))
                reviewer = next(r for r in reviewers if r.github_login == login)
                return reviewer.maximum_capacity - float(current[1])

            alice_remaining = remaining_capacity("alice")
            if pr_number == 1:
                return PRAssignmentPriority(sort_key=(-10 if alice_remaining > 0 else 10, pr_number))

            candidate_capacity = 0.0
            for reviewer in reviewers:
                if pr_number == 2 and reviewer.github_login in {"alice", "bob", "carol"}:
                    candidate_capacity += max(0.0, remaining_capacity(reviewer.github_login))
                if pr_number == 3 and reviewer.github_login == "bob":
                    candidate_capacity += max(0.0, remaining_capacity(reviewer.github_login))

            if alice_remaining > 0:
                return PRAssignmentPriority(sort_key=(-candidate_capacity, pr_number))
            return PRAssignmentPriority(sort_key=(candidate_capacity, pr_number))

        suggestions = suggest_reviewers_many(
            reviewers=reviewers,
            assignments=existing_assignments,
            prs_to_assign=[1, 2, 3],
            all_prs=snapshot["prs"],
            rng=random.Random(0),
            priority_scorer=scorer,
        )

        self.assertEqual(suggestions, {1: "alice", 2: "carol", 3: "bob"})


class ReviewerAssignmentBuilderTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.alice = User.objects.create(github_login="alice")
        self.bob = User.objects.create(github_login="bob")
        self.now = datetime.now(timezone.utc) - timedelta(days=10)

    def _make_pr(self, number: int, *, labels: tuple[str, ...] = ()) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=self.alice,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            closed_at=None,
            merged_at=None,
            base_ref_name="master",
            head_ref_name=f"feature/{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body="description",
            additions=1,
            deletions=1,
            changed_files_count=1,
            files=["src/file.py"],
            assignees=[],
            approvals=[],
            commenters=[],
            number_total_comments=0,
            last_synced_at=self.now,
            files_incomplete=False,
            assignees_incomplete=False,
            reviews_incomplete=False,
            comments_incomplete=False,
            timeline_backfill_done=True,
        )
        for label_name in labels:
            label_def, _ = LabelDef.objects.get_or_create(repository=self.repo, name=label_name, defaults={"color": "123456"})
            PRLabel.objects.create(pull_request=pr, label_def=label_def)
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"cr-{number}",
            head_sha="a" * 40,
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )
        return pr

    def _bundle(
        self,
        number: int,
        *,
        author_login: str,
        updated_at: datetime,
        timeline_nodes: list[dict],
        assignees: list[str] | None = None,
    ) -> dict:
        assignees = assignees or []
        iso = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "number": number,
            "state": "OPEN",
            "isDraft": False,
            "title": f"PR {number}",
            "body": "description",
            "createdAt": iso,
            "updatedAt": iso,
            "closedAt": None,
            "mergedAt": None,
            "baseRefName": "master",
            "headRefName": f"feature/{number}",
            "headRefOid": "a" * 40,
            "headRepositoryOwner": {"login": self.repo.owner},
            "headRepository": {"name": self.repo.name},
            "additions": 1,
            "deletions": 1,
            "changedFiles": 1,
            "author": {"login": author_login},
            "labels": {"nodes": [{"name": "t-analysis", "color": "123456"}]},
            "timelineItems": {"nodes": timeline_nodes},
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "committedDate": iso,
                            "oid": "a" * 40,
                            "statusCheckRollup": {
                                "state": "SUCCESS",
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "id": f"cr-{number}",
                                            "name": "lint",
                                            "status": "COMPLETED",
                                            "conclusion": "SUCCESS",
                                            "startedAt": iso,
                                            "completedAt": iso,
                                            "detailsUrl": "https://example.com",
                                            "externalId": "ext-1",
                                        }
                                    ]
                                },
                            },
                        }
                    }
                ]
            },
            "files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"path": "src/file.py"}]},
            "assignees": {
                "totalCount": len(assignees),
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"login": login} for login in assignees],
            },
            "reviews": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
            "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
            "reviewThreads": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
        }

    def test_build_and_store_links_queue_snapshot(self):
        pr = self._make_pr(10, labels=("t-analysis",))
        # Make the PR stale and unassigned
        pr.gh_updated_at = self.now - timedelta(days=5)
        pr.save(update_fields=["gh_updated_at"])

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        builder = ReviewerAssignmentBuilder(rng=random.Random(0))
        obj = builder.build_and_store(self.repo, queue_snapshot=queue_snapshot)

        self.assertEqual(obj.repository, self.repo)
        self.assertEqual(obj.queue_snapshot, queue_snapshot)
        self.assertEqual(obj.cache_key, queue_snapshot.cache_key)
        self.assertGreaterEqual(obj.assignment_count, 1)
        self.assertIn(10, obj.payload["automatic_assignments"])
        self.assertEqual(
            obj.payload["meta"]["queue_snapshot_cache_key"],
            queue_snapshot.cache_key,
        )

    def test_build_assigns_non_stale_queue_prs(self):
        self._make_pr(14, labels=("t-analysis",))

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        self.assertIn(14, payload["automatic_assignments"])

    def test_build_does_not_assign_queue_pr_without_topic_label(self):
        self._make_pr(15, labels=())

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        self.assertNotIn(15, payload["automatic_assignments"])

    def test_build_excludes_assignment_forbidden_label_but_keeps_pr_on_queue(self):
        # PR 20 is a normal queue PR; PR 21 also carries the maintainer-merge label.
        self._make_pr(20, labels=("t-analysis",))
        self._make_pr(21, labels=("t-analysis", "maintainer-merge"))

        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_default=True,
            assignment_forbidden_label_names=["maintainer-merge"],
        )

        # bob is a non-author reviewer (PRs are authored by alice).
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        # The maintainer-merge PR stays on the review queue ...
        self.assertIn(21, queue_snapshot.payload["lists"]["dashboards"]["Queue"])
        # ... but is withheld from reviewer auto-assignment, while the normal PR is assigned.
        self.assertIn(20, payload["automatic_assignments"])
        self.assertNotIn(21, payload["automatic_assignments"])

    def test_build_skips_pr_already_assigned_to_active_reviewer(self):
        pr = self._make_pr(16, labels=("t-analysis",))
        pr.assignees = ["alice"]
        pr.save(update_fields=["assignees"])

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        self.assertNotIn(16, payload["automatic_assignments"])

    def test_build_keeps_pr_assigned_only_to_inactive_reviewer_as_candidate(self):
        other = User.objects.create(github_login="carol")
        pr = self._make_pr(17, labels=("t-analysis",))
        pr.assignees = ["carol"]
        pr.save(update_fields=["assignees"])

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=other,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=False,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        self.assertIn(17, payload["automatic_assignments"])

    def test_build_keeps_pr_assigned_only_to_reviewer_on_break_as_candidate(self):
        pr = self._make_pr(18, labels=("t-analysis",))
        pr.assignees = ["alice"]
        pr.save(update_fields=["assignees"])

        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
            away_until=datetime.now(timezone.utc) + timedelta(days=1),
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)

        self.assertIn(18, payload["automatic_assignments"])

    def test_unassignment_event_excludes_reviewer_from_auto_assignments(self):
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        now = datetime.now(timezone.utc)
        timeline_nodes = [
            {
                "__typename": "AssignedEvent",
                "id": "A1",
                "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "actor": {"login": "bot"},
                "assignee": {"login": "bob"},
            },
            {
                "__typename": "UnassignedEvent",
                "id": "U1",
                "createdAt": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "actor": {"login": "bob"},
                "assignee": {"login": "bob"},
            },
        ]
        bundle = self._bundle(
            10,
            author_login="carol",
            updated_at=now,
            timeline_nodes=timeline_nodes,
            assignees=[],
        )
        PRSyncService().sync_pull_request_bundle(self.repo, bundle)

        pr = PullRequest.objects.get(repository=self.repo, number=10)
        pr.gh_updated_at = now - timedelta(days=5)
        pr.save(update_fields=["gh_updated_at"])

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        builder = ReviewerAssignmentBuilder(rng=random.Random(0))
        payload = builder.build(self.repo, queue_snapshot=queue_snapshot)

        self.assertEqual(payload["automatic_assignments"].get(10), "alice")

    def test_assigned_event_clears_opt_out_for_auto_assignments(self):
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=0,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        now = datetime.now(timezone.utc)
        unassign_bundle = self._bundle(
            11,
            author_login="carol",
            updated_at=now,
            timeline_nodes=[
                {
                    "__typename": "UnassignedEvent",
                    "id": "U2",
                    "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bob"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )
        assign_bundle = self._bundle(
            11,
            author_login="carol",
            updated_at=now + timedelta(minutes=2),
            timeline_nodes=[
                {
                    "__typename": "AssignedEvent",
                    "id": "A2",
                    "createdAt": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bot"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )

        service = PRSyncService()
        service.sync_pull_request_bundle(self.repo, unassign_bundle)
        service.sync_pull_request_bundle(self.repo, assign_bundle)

        pr = PullRequest.objects.get(repository=self.repo, number=11)
        pr.gh_updated_at = now - timedelta(days=5)
        pr.save(update_fields=["gh_updated_at"])

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        builder = ReviewerAssignmentBuilder(rng=random.Random(0))
        payload = builder.build(self.repo, queue_snapshot=queue_snapshot)

        self.assertEqual(payload["automatic_assignments"].get(11), "bob")

    def test_ignores_older_assignment_events_after_unassign(self):
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        now = datetime.now(timezone.utc)
        unassign_bundle = self._bundle(
            12,
            author_login="carol",
            updated_at=now,
            timeline_nodes=[
                {
                    "__typename": "UnassignedEvent",
                    "id": "U12",
                    "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bob"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )
        older_assign_bundle = self._bundle(
            12,
            author_login="carol",
            updated_at=now + timedelta(minutes=5),
            timeline_nodes=[
                {
                    "__typename": "AssignedEvent",
                    "id": "A12",
                    "createdAt": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bot"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )

        service = PRSyncService()
        service.sync_pull_request_bundle(self.repo, unassign_bundle)
        service.sync_pull_request_bundle(self.repo, older_assign_bundle)

        pr = PullRequest.objects.get(repository=self.repo, number=12)
        pr.gh_updated_at = now - timedelta(days=5)
        pr.save(update_fields=["gh_updated_at"])

        opt_out = ReviewerOptOut.objects.get(repository=self.repo, pr_number=12, reviewer_login="bob")
        self.assertTrue(opt_out.active)

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)
        self.assertEqual(payload["automatic_assignments"].get(12), "alice")

    def test_newer_assignment_event_overrides_opt_out(self):
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=3,
            auto_assign=True,
        )

        now = datetime.now(timezone.utc)
        unassign_bundle = self._bundle(
            13,
            author_login="carol",
            updated_at=now,
            timeline_nodes=[
                {
                    "__typename": "UnassignedEvent",
                    "id": "U13",
                    "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bob"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )
        assign_bundle = self._bundle(
            13,
            author_login="carol",
            updated_at=now + timedelta(minutes=5),
            timeline_nodes=[
                {
                    "__typename": "AssignedEvent",
                    "id": "A13",
                    "createdAt": (now + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "actor": {"login": "bot"},
                    "assignee": {"login": "bob"},
                }
            ],
            assignees=[],
        )

        service = PRSyncService()
        service.sync_pull_request_bundle(self.repo, unassign_bundle)
        service.sync_pull_request_bundle(self.repo, assign_bundle)

        pr = PullRequest.objects.get(repository=self.repo, number=13)
        pr.gh_updated_at = now - timedelta(days=5)
        pr.save(update_fields=["gh_updated_at"])

        opt_out = ReviewerOptOut.objects.get(repository=self.repo, pr_number=13, reviewer_login="bob")
        self.assertFalse(opt_out.active)

        queue_snapshot = ReviewerAssignmentBuilder().queue_snapshot_builder.build_and_store(self.repo, cache_key="default")
        payload = ReviewerAssignmentBuilder(rng=random.Random(0)).build(self.repo, queue_snapshot=queue_snapshot)
        self.assertEqual(payload["automatic_assignments"].get(13), "bob")


class AreaStatsBuilderTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.alice = User.objects.create(github_login="alice")
        self.bob = User.objects.create(github_login="bob")
        self.now = datetime.now(timezone.utc)

    def _make_pr(self, number: int, *, labels: tuple[str, ...] = ()) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=self.alice,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            closed_at=None,
            merged_at=None,
            base_ref_name="master",
            head_ref_name=f"feature/{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body="description",
            additions=1,
            deletions=1,
            changed_files_count=1,
            files=["src/file.py"],
            assignees=[],
            approvals=[],
            commenters=[],
            number_total_comments=0,
            last_synced_at=self.now,
            files_incomplete=False,
            assignees_incomplete=False,
            reviews_incomplete=False,
            comments_incomplete=False,
            timeline_backfill_done=True,
        )
        for label_name in labels:
            label_def, _ = LabelDef.objects.get_or_create(repository=self.repo, name=label_name, defaults={"color": "123456"})
            PRLabel.objects.create(pull_request=pr, label_def=label_def)
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"cr-area-{number}",
            head_sha="a" * 40,
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )
        return pr

    def test_build_and_store_area_stats_snapshot(self):
        self._make_pr(50, labels=("t-analysis",))
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            maximum_capacity=2,
            auto_assign=True,
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-analysis"],
            maximum_capacity=2,
            auto_assign=True,
        )

        builder = AreaStatsBuilder(rng=random.Random(0))
        obj = builder.build_and_store(self.repo)

        self.assertEqual(obj.repository, self.repo)
        self.assertIsNotNone(obj.queue_snapshot)
        self.assertGreaterEqual(obj.area_count, 1)
        self.assertIn("t-analysis", obj.payload["area_stats"])
