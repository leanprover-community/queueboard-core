from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevisionBuildState
from analyzer.tasks.process_pr import process_pr
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType, CheckRun


class TestProcessPRTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
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

        class _StubTask:
            def __init__(self):
                self.calls: list[dict] = []

            def delay(self, **kwargs):
                self.calls.append(kwargs)
                return type("Res", (), {"id": "task123"})

        stub_task = _StubTask()

        res = process_pr(pr, client=self._StubClient(), harvest_task=stub_task)
        self.assertEqual(res["status"], "ok")
        self.assertIn(res["revisions"], {"full", "append", "noop"})
        # Queue windows should be built for the ruleset
        self.assertGreaterEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).count(), 1)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.windows_built_revision_version, state.revision_version)
        self.assertIsNotNone(state.windows_built_at)

    def test_harvest_tasks_include_cutoffs_and_missing_ci(self) -> None:
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

        class _StubTask:
            def __init__(self):
                self.calls: list[dict] = []

            def delay(self, **kwargs):
                self.calls.append(kwargs)
                # Simulate harvested SHAs in result; real task returns via Celery result.
                return type("Res", (), {"id": "task123"})

        stub_task = _StubTask()
        with patch("analyzer.tasks.process_pr.enqueue_ci_by_shas", return_value="task123") as mock_enqueue:
            res = process_pr(pr, client=self._StubClient(), harvest_task=stub_task)
        self.assertEqual(res["status"], "ok")
        # Two tasks: before_sha with cutoff = created_at, after_sha with cutoff = occurred_at.
        self.assertEqual(len(stub_task.calls), 2)
        self.assertEqual(stub_task.calls[0]["start_sha"], "h1")
        self.assertEqual(stub_task.calls[0]["since_iso"], pr.gh_created_at.isoformat())
        self.assertEqual(stub_task.calls[1]["start_sha"], "h2")
        self.assertEqual(stub_task.calls[1]["since_iso"], t_fp.isoformat())
        # Queue windows rebuilt. CI backfill planning is handled in process_pr_task, so this call should mark it skipped.
        qwin = res["queue_windows"][self.rule_set.id]
        self.assertEqual(qwin.get("created"), 1)
        self.assertEqual(qwin.get("updated"), 0)
        self.assertEqual(qwin.get("deleted"), 0)
        self.assertEqual(qwin.get("status"), "rebuilt")
        self.assertEqual(res["ci_backfill"].get("status"), "skipped")
        self.assertEqual(res["ci_backfill"].get("planned"), 0)

    def test_rebuilds_when_rollup_fields_missing(self) -> None:
        pr = self._mk_pr(4)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=0,
            first_on_queue_ts=None,
        )

        class _StubRes:
            strategy = "noop"
            created = 0
            deleted = 0

        with patch("analyzer.tasks.process_pr.rebuild_pr_revisions", return_value=_StubRes()):
            res = process_pr(pr, client=self._StubClient())

        self.assertEqual(res["status"], "ok")
        qwin = PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).order_by("-from_ts").first()
        self.assertIsNotNone(qwin)
        self.assertGreaterEqual(qwin.window_count, 1)
        self.assertIsNotNone(qwin.first_on_queue_ts)
