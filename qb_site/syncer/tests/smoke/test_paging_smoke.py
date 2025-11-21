from __future__ import annotations

import json
from pathlib import Path

from django.test import TestCase

from core.models.repository import Repository
from syncer.models import PullRequest, PRTimelineEvent, CheckRun, StatusContext
from syncer.services.pr_sync_service import PRSyncService
from syncer.tests.helpers import supported_timeline, fixtures_dir


class TestPagingSmoke(TestCase):
    fixtures_dir = fixtures_dir()

    def test_real_timeline_paging_files(self) -> None:
        """Smoke test: replay timeline paging from real files if present.

        Requires fixtures:
          - pr_bundle_smallK.json (timelineK=1, commitsM=1)
          - timeline_page_after.json (page after the bundle's endCursor)
        """
        bundle_path = self.fixtures_dir / "pr_bundle_smallK.json"
        page_path = self.fixtures_dir / "timeline_page_after.json"
        if not bundle_path.exists() or not page_path.exists():
            self.skipTest("optional paging fixtures not present")

        bundle_data = json.loads(bundle_path.read_text())
        page_data = json.loads(page_path.read_text())

        repo_node = (bundle_data.get("data") or {}).get("repository") or {}
        pr_node_full = repo_node.get("pullRequest") or {}
        if not pr_node_full:
            self.skipTest("bundle fixture missing pullRequest node")

        # Count supported events across both files
        b_nodes = (pr_node_full.get("timelineItems") or {}).get("nodes") or []
        p_nodes = (
            (((page_data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
            .get("timelineItems", {})
            .get("nodes", [])
        )
        supported_total = len(supported_timeline(b_nodes) + supported_timeline(p_nodes))
        if supported_total < 2:
            self.skipTest("not enough supported timeline events to exercise paging")

        class FakeClient:
            def __init__(self) -> None:
                self.timeline_page_calls: list[dict] = []

            def get_pr_bundle(self, *, owner, name, number, timelineK, commitsM, timeline_since_iso=None, query_path=None):  # type: ignore[no-redef]
                return {
                    "data": {
                        "repository": {
                            "id": repo_node.get("id"),
                            "name": repo_node.get("name") or name,
                            "owner": repo_node.get("owner") or {"login": owner},
                            "defaultBranchRef": repo_node.get("defaultBranchRef") or {"name": "master"},
                            "pullRequest": pr_node_full,
                        }
                    }
                }

            def get_timeline_page(self, *, owner, name, number, first, after, since_iso=None, query_path=None):  # type: ignore[no-redef]
                # Return the real page content
                self.timeline_page_calls.append({"after": after, "first": first})
                return page_data

            def get_last_rate_limit(self):
                return None

        owner = (repo_node.get("owner") or {}).get("login") or "owner"
        name = repo_node.get("name") or "name"
        number = pr_node_full.get("number") or 0
        repo = Repository.objects.create(owner=owner, name=name, default_branch="master", is_active=True)

        svc = PRSyncService()
        fc = FakeClient()
        svc.sync_pull_request(
            repo,
            number=int(number),
            client=fc,  # type: ignore[arg-type]
            timelineK=1,
            commitsM=1,
            max_timeline_pages=2,
            dry_run=False,
        )

        pr = PullRequest.objects.get(repository=repo, number=int(number))
        db_count = PRTimelineEvent.objects.filter(pull_request=pr).count()
        self.assertEqual(db_count, supported_total)
        self.assertTrue(fc.timeline_page_calls)

    def test_real_commits_paging_files(self) -> None:
        """Smoke test: replay commits paging from real files if present.

        Requires fixtures:
          - pr_bundle_smallK.json (timelineK=1, commitsM=1)
          - commits_page_before.json (older commits before the bundle's startCursor)
        """
        bundle_path = self.fixtures_dir / "pr_bundle_smallK.json"
        page_path = self.fixtures_dir / "commits_page_before.json"
        if not bundle_path.exists() or not page_path.exists():
            self.skipTest("optional commits paging fixtures not present")

        bundle_data = json.loads(bundle_path.read_text())
        page_data = json.loads(page_path.read_text())

        repo_node = (bundle_data.get("data") or {}).get("repository") or {}
        pr_node_full = repo_node.get("pullRequest") or {}
        if not pr_node_full:
            self.skipTest("bundle fixture missing pullRequest node")

        # Count contexts across bundle + page
        def count_ctx(nodes: list[dict]) -> tuple[int, int]:
            cr = sc = 0
            for n in nodes:
                commit = (n or {}).get("commit") or {}
                ctx_nodes = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
                for c in ctx_nodes:
                    if not isinstance(c, dict):
                        continue
                    t = c.get("__typename")
                    if t == "CheckRun":
                        if (c.get("conclusion") or "").upper() == "SKIPPED":
                            continue
                        cr += 1
                    elif t == "StatusContext":
                        sc += 1
            return cr, sc

        b_nodes = (pr_node_full.get("commits") or {}).get("nodes") or []
        p_nodes = (
            (((page_data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("commits", {}).get("nodes", [])
        )
        exp_cr_b, exp_sc_b = count_ctx(b_nodes)
        exp_cr_p, exp_sc_p = count_ctx(p_nodes)
        exp_cr = exp_cr_b + exp_cr_p
        exp_sc = exp_sc_b + exp_sc_p
        if exp_cr + exp_sc < 1:
            self.skipTest("not enough contexts to exercise commits paging")

        class FakeClient:
            def __init__(self) -> None:
                self.commits_page_calls: list[dict] = []

            def get_pr_bundle(self, *, owner, name, number, timelineK, commitsM, timeline_since_iso=None, query_path=None):  # type: ignore[no-redef]
                return {
                    "data": {
                        "repository": {
                            "id": repo_node.get("id"),
                            "name": repo_node.get("name") or name,
                            "owner": repo_node.get("owner") or {"login": owner},
                            "defaultBranchRef": repo_node.get("defaultBranchRef") or {"name": "master"},
                            "pullRequest": pr_node_full,
                        }
                    }
                }

            def get_timeline_page(self, **kwargs):  # not used in this test
                return {"data": {"repository": {"pullRequest": {"timelineItems": {"pageInfo": {"hasNextPage": False}}}}}}

            def get_commits_page(self, *, owner, name, number, last, before, query_path=None):  # type: ignore[no-redef]
                self.commits_page_calls.append({"before": before, "last": last})
                return page_data

            def get_last_rate_limit(self):
                return None

        owner = (repo_node.get("owner") or {}).get("login") or "owner"
        name = repo_node.get("name") or "name"
        number = pr_node_full.get("number") or 0
        repo = Repository.objects.create(owner=owner, name=name, default_branch="master", is_active=True)

        svc = PRSyncService()
        fc = FakeClient()
        svc.sync_pull_request(
            repo,
            number=int(number),
            client=fc,  # type: ignore[arg-type]
            timelineK=1,
            commitsM=1,
            max_commit_pages=2,
            dry_run=False,
        )

        pr = PullRequest.objects.get(repository=repo, number=int(number))
        db_cr = CheckRun.objects.filter(pull_request=pr).count()
        db_sc = StatusContext.objects.filter(pull_request=pr).count()
        self.assertEqual(db_cr, exp_cr)
        self.assertEqual(db_sc, exp_sc)
        self.assertTrue(fc.commits_page_calls)

    def test_real_forcepush_bundle_smoke(self) -> None:
        """Smoke test: ingest a real force-push bundle and assert a force-push event.

        Requires fixture:
          - pr_bundle_real_forcepush.json (full bundle)
        """
        p = self.fixtures_dir / "pr_bundle_real_forcepush.json"
        if not p.exists():
            self.skipTest("optional fixture pr_bundle_real_forcepush.json not present")
        data = json.loads(p.read_text())
        repo = Repository.objects.create(owner="owner", name="name", default_branch="master", is_active=True)
        svc = PRSyncService()
        pr_node = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
        assert pr_node is not None
        res = svc.sync_pull_request_bundle(repo, pr_node)
        pr = PullRequest.objects.get(repository=repo, number=int(pr_node.get("number")))
        self.assertGreaterEqual(
            PRTimelineEvent.objects.filter(pull_request=pr, type="HEAD_FORCE_PUSHED").count(),
            1,
        )
        # Sanity: some data was ingested
        self.assertGreaterEqual(res["checkruns_upserted"] + res["statusctx_upserted"] + res["events_created"], 1)
