from __future__ import annotations

from django.test import TestCase

from core.models import Repository
from syncer.services.commit_history import harvest_commit_history_shas


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
