from __future__ import annotations

from django.test import TestCase

from core.models import Repository
from syncer.services.commit_history import harvest_commit_history_shas, harvest_commit_history_with_cursor
from syncer.models import PullRequest, CommitHistoryHarvest


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_commit_history_from_sha(self, *, owner, name, sha, first, after=None, since=None, query_path=None):
        self.calls.append({"owner": owner, "name": name, "sha": sha, "first": first, "after": after, "since": since})
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


class TestCommitHistoryHarvest(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self.repo.created_at,
            gh_updated_at=self.repo.created_at,
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

    def test_harvest_history_paging(self) -> None:
        pages = {
            "page1": {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "history": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [{"oid": "sha1"}, {"oid": "sha2"}],
                            },
                        }
                    }
                }
            },
            "c1": {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "history": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"oid": "sha3"}, {"oid": "sha2"}],
                            },
                        }
                    }
                }
            },
        }
        client = FakeClient(pages)
        shas = harvest_commit_history_shas(
            client=client, repo=self.repo, start_sha="sha1", max_pages=2, page_size=2, since_iso=None
        )
        self.assertEqual(shas, ["sha1", "sha2", "sha3"])
        self.assertEqual(len(client.calls), 2)

    def test_harvest_with_cursor_persists_state(self) -> None:
        pages = {
            "page1": {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "history": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [{"oid": "sha1", "committedDate": "2025-10-20T00:00:00Z"}],
                            },
                        }
                    }
                }
            },
            "c1": {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "history": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"oid": "sha0", "committedDate": "2025-10-19T00:00:00Z"}],
                            },
                        }
                    }
                }
            },
        }
        client = FakeClient(pages)
        shas, state = harvest_commit_history_with_cursor(
            client=client,
            pr=self.pr,
            start_sha="sha1",
            max_pages=1,
            page_size=1,
            since_iso="2025-10-20T00:00:00Z",
        )
        # First page only, has_more stays True.
        self.assertEqual(shas, ["sha1"])
        state.refresh_from_db()
        self.assertTrue(state.has_more)
        self.assertEqual(state.cursor, "c1")
        self.assertEqual(state.attempts, 1)

        # Second call resumes from cursor; cutoff stops before sha0
        shas2, state2 = harvest_commit_history_with_cursor(
            client=client,
            pr=self.pr,
            start_sha="sha1",
            max_pages=1,
            page_size=1,
            since_iso="2025-10-20T00:00:00Z",
        )
        self.assertEqual(shas2, [])
        state2.refresh_from_db()
        self.assertFalse(state2.has_more)
        self.assertEqual(state2.attempts, 2)
