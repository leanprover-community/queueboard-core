from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow
from analyzer.tasks.process_pr import process_pr
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType, CheckRun


class TestProcessPRTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version="v1",
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
        )

    def _mk_pr(self, number: int) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    class _StubClient:
        def __init__(self, pages=None):
            self.pages = pages or {}
            self.calls = []

        def get_commit_history_from_sha(self, *, owner, name, sha, first, after=None, since=None, query_path=None):
            self.calls.append({"sha": sha, "first": first, "after": after})
            key = after or "page1"
            return self.pages.get(
                key,
                {
                    "data": {
                        "repository": {
                            "object": {
                                "__typename": "Commit",
                                "history": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
                            }
                        }
                    }
                },
            )

    def test_skips_when_not_backfilled(self) -> None:
        pr = self._mk_pr(1)
        pr.timeline_backfill_done = False
        pr.save(update_fields=["timeline_backfill_done"])
        res = process_pr(pr, client=self._StubClient())
        self.assertEqual(res["status"], "skipped")

    def test_runs_and_builds_queue_windows(self) -> None:
        pr = self._mk_pr(2)
        t_fp = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="aaa111",
            after_sha="bbb222",
        )
        res = process_pr(pr, client=self._StubClient())
        self.assertEqual(res["status"], "ok")
        self.assertIn(res["revisions"], {"full", "append", "noop"})
        # Queue windows should be built for the ruleset
        self.assertGreaterEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).count(), 1)

    def test_enqueues_ci_for_harvested_missing_heads(self) -> None:
        pr = self._mk_pr(3)
        t_fp = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="h1",
            after_sha="h2",
        )
        # Seed CI for h1 so only h2 is missing.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_h1",
            head_sha="h1",
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
        )

        pages = {
            "page1": {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "history": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"oid": "h1"}, {"oid": "h2"}],
                            },
                        }
                    }
                }
            }
        }
        client = self._StubClient(pages=pages)
        with patch("analyzer.tasks.process_pr.enqueue_ci_by_shas", return_value="task123") as mock_enqueue:
            res = process_pr(pr, client=client)
        self.assertEqual(res["status"], "ok")
        # Only h2 should be enqueued because h1 already has CI.
        self.assertEqual(res.get("ci_backfill"), [{"task_id": "task123", "shas": ["h2"]}])
        self.assertEqual(res.get("harvest", {}).get("harvested_shas"), ["h1", "h2"])
