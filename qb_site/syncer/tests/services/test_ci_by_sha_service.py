from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.tests.factories import make_repo, make_pr
from syncer.services.ci_by_sha_service import sync_ci_for_sha


class TestCIBySHAService(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = make_pr(
            self.repo,
            10,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-02T00:00:00Z",
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="forko",
            head_repo_name="forkr",
        )

    @mock.patch("syncer.services.ci_by_sha_service.GitHubClient")
    def test_upserts_contexts_from_head_repo(self, MockClient) -> None:
        gh = MockClient.return_value
        page = {
            "data": {
                "repository": {
                    "object": {
                        "__typename": "Commit",
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CRx",
                                        "name": "ci/test",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "startedAt": "2024-01-02T00:01:00Z",
                                        "completedAt": "2024-01-02T00:02:00Z",
                                        "detailsUrl": None,
                                        "externalId": None,
                                    },
                                    {
                                        "__typename": "StatusContext",
                                        "id": "SCx",
                                        "context": "lint",
                                        "state": "SUCCESS",
                                        "targetUrl": None,
                                        "description": None,
                                        "createdAt": "2024-01-02T00:01:30Z",
                                    },
                                ],
                            }
                        },
                    }
                }
            }
        }
        gh.get_ci_by_commit.return_value = page
        res = sync_ci_for_sha(self.pr, "abc123", client=gh, max_pages=1)
        self.assertGreaterEqual(res.get("checkruns_created", 0) + res.get("status_created", 0), 2)

    @mock.patch("syncer.services.ci_by_sha_service.GitHubClient")
    def test_fallback_to_base_repo_when_object_missing(self, MockClient) -> None:
        gh = MockClient.return_value

        def _get(owner, name, sha, first, after, query_path=None):  # type: ignore[no-redef]
            if owner == self.pr.head_repo_owner_login and name == self.pr.head_repo_name:
                return {"data": {"repository": {"object": None}}}
            return {
                "data": {
                    "repository": {
                        "object": {
                            "__typename": "Commit",
                            "statusCheckRollup": {
                                "contexts": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "__typename": "StatusContext",
                                            "id": "SCy",
                                            "context": "build",
                                            "state": "SUCCESS",
                                            "targetUrl": None,
                                            "description": None,
                                            "createdAt": "2024-01-02T00:01:30Z",
                                        }
                                    ],
                                }
                            },
                        }
                    }
                }
            }

        gh.get_ci_by_commit.side_effect = _get
        res = sync_ci_for_sha(self.pr, "def456", client=gh, max_pages=1)
        self.assertGreaterEqual(res.get("status_created", 0), 1)

    @mock.patch("syncer.services.ci_by_sha_service.GitHubClient")
    def test_aggregates_contexts_from_head_and_base(self, MockClient) -> None:
        gh = MockClient.return_value
        head_page = {
            "data": {
                "repository": {
                    "object": {
                        "__typename": "Commit",
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR_head",
                                        "name": "ci/head",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "startedAt": "2024-01-03T00:01:00Z",
                                        "completedAt": "2024-01-03T00:02:00Z",
                                        "detailsUrl": None,
                                        "externalId": None,
                                    }
                                ],
                            }
                        },
                    }
                }
            }
        }
        base_page = {
            "data": {
                "repository": {
                    "object": {
                        "__typename": "Commit",
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR_base",
                                        "name": "ci/base",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "startedAt": "2024-01-03T00:03:00Z",
                                        "completedAt": "2024-01-03T00:04:00Z",
                                        "detailsUrl": None,
                                        "externalId": None,
                                    },
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR_skip",
                                        "name": "ci/skip",
                                        "status": "COMPLETED",
                                        "conclusion": "SKIPPED",
                                        "startedAt": "2024-01-03T00:05:00Z",
                                        "completedAt": "2024-01-03T00:06:00Z",
                                        "detailsUrl": None,
                                        "externalId": None,
                                    },
                                ],
                            }
                        },
                    }
                }
            }
        }

        def _get(owner, name, sha, first, after, query_path=None):  # type: ignore[no-redef]
            if owner == self.pr.head_repo_owner_login and name == self.pr.head_repo_name:
                return head_page
            if owner == self.pr.repository.owner and name == self.pr.repository.name:
                return base_page
            return {"data": {"repository": {"object": None}}}

        gh.get_ci_by_commit.side_effect = _get
        res = sync_ci_for_sha(self.pr, "ghi789", client=gh, max_pages=1)
        self.assertGreaterEqual(res.get("checkruns_created", 0), 2)
