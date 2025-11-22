from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

from syncer.services.github_client import GitHubClient


class TestGitHubClient(SimpleTestCase):
    def test_execute_success_and_headers(self) -> None:
        client = GitHubClient(token="test-token")

        with mock.patch("requests.post") as mpost:
            mresp = mock.Mock()
            mresp.raise_for_status.return_value = None
            mresp.json.return_value = {
                "data": {"ok": True, "rateLimit": {"remaining": 4999, "resetAt": "2025-10-21T00:00:00Z", "cost": 1, "used": 1}}
            }
            mpost.return_value = mresp

            out = client.execute("query X", {"a": 1})
            self.assertEqual(out["data"]["ok"], True)
            rl = client.get_last_rate_limit()
            self.assertIsNotNone(rl)
            assert rl is not None
            self.assertIn("remaining", rl)

            # Verify request was formed correctly
            self.assertTrue(mpost.called)
            args, kwargs = mpost.call_args
            self.assertEqual(args[0], client.endpoint)
            self.assertIn("Authorization", kwargs["headers"])  # Bearer header present
            self.assertIn("application/vnd.github+json", kwargs["headers"]["Accept"])
            self.assertEqual(kwargs["json"]["query"], "query X")
            self.assertEqual(kwargs["json"]["variables"]["a"], 1)

    def test_execute_raises_on_graphql_errors(self) -> None:
        client = GitHubClient(token="test-token")
        with mock.patch("requests.post") as mpost:
            mresp = mock.Mock()
            mresp.raise_for_status.return_value = None
            mresp.json.return_value = {"errors": [{"message": "nope"}]}
            mpost.return_value = mresp

            with self.assertRaises(RuntimeError) as cm:
                client.execute("q", {})
            self.assertIn("GraphQL", str(cm.exception))

    def test_init_chooses_token_from_comma_separated_env(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "a, b , c", "GITHUB_TOKEN": ""}):
            with mock.patch("syncer.services.github_client.choose_token", return_value="b") as mchoose:
                client = GitHubClient()

        self.assertEqual(client.token, "b")
        mchoose.assert_called_once_with(["a", "b", "c"])

    def test_get_pr_bundle_calls_execute_with_vars(self) -> None:
        client = GitHubClient(token="t")

        # Avoid reading from disk
        with mock.patch.object(GitHubClient, "_read_file", return_value="query { bundle }"):
            captured = {}

            def fake_execute(_self, q, variables):  # type: ignore[no-redef]
                captured.update(variables)
                return {"data": {}}

            with mock.patch.object(GitHubClient, "execute", new=fake_execute):
                out = client.get_pr_bundle(owner="o", name="r", number=123, timelineK=10, commitsM=2)
                self.assertEqual(out, {"data": {}})
                self.assertEqual(captured["owner"], "o")
                self.assertEqual(captured["name"], "r")
                self.assertEqual(captured["number"], 123)
                self.assertEqual(captured["timelineK"], 10)
                self.assertEqual(captured["commitsM"], 2)

    def test_get_changed_pr_numbers_pagination_and_cutoff(self) -> None:
        client = GitHubClient(token="t")

        # Two pages; second page has items older than cutoff
        pages = [
            {
                "data": {
                    "rateLimit": {"remaining": 4999, "resetAt": "2025-10-21T00:00:00Z", "cost": 1, "used": 1},
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                            "nodes": [
                                {"number": 5, "updatedAt": "2025-10-20T10:00:00Z", "state": "OPEN"},
                                {"number": 4, "updatedAt": "2025-10-20T09:00:00Z", "state": "OPEN"},
                            ],
                        }
                    },
                }
            },
            {
                "data": {
                    "rateLimit": {"remaining": 4998, "resetAt": "2025-10-21T00:00:00Z", "cost": 1, "used": 2},
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"number": 3, "updatedAt": "2025-10-19T23:00:00Z", "state": "OPEN"},
                                {"number": 2, "updatedAt": "2025-10-19T12:00:00Z", "state": "OPEN"},
                            ],
                        }
                    },
                }
            },
        ]

        calls = {"i": 0}

        def fake_execute(_self, q, variables):  # type: ignore[no-redef]
            i = calls["i"]
            calls["i"] = i + 1
            # basic sanity checks on variables
            if i == 0:
                assert variables["after"] is None
            else:
                assert variables["after"] == "c1"
            return pages[i]

        with mock.patch.object(GitHubClient, "execute", new=fake_execute):
            # Cutoff excludes 2025-10-19T23:00:00Z
            nums = client.get_changed_pr_numbers(owner="o", name="r", since_iso="2025-10-20T00:00:00Z", limit=10)
            self.assertEqual(nums, [5, 4])

    def test_get_changed_pr_numbers_respects_limit(self) -> None:
        client = GitHubClient(token="t")
        page = {
            "data": {
                "rateLimit": {"remaining": 4999, "resetAt": "2025-10-21T00:00:00Z", "cost": 1, "used": 1},
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "x"},
                        "nodes": [
                            {"number": 10, "updatedAt": "2025-10-21T01:00:00Z", "state": "OPEN"},
                            {"number": 9, "updatedAt": "2025-10-21T00:30:00Z", "state": "OPEN"},
                            {"number": 8, "updatedAt": "2025-10-21T00:00:00Z", "state": "OPEN"},
                        ],
                    }
                },
            }
        }

        def fake_execute(self, q, variables):  # type: ignore[no-redef]
            # Simulate execute() capturing rateLimit like the real implementation
            self._last_rate_limit = {"remaining": 4999, "resetAt": "2025-10-21T00:00:00Z", "cost": 1, "used": 1}
            return page

        with mock.patch.object(GitHubClient, "execute", new=fake_execute):
            nums = client.get_changed_pr_numbers(owner="o", name="r", since_iso="2025-10-20T00:00:00Z", limit=2)
            self.assertEqual(nums, [10, 9])
            rl = client.get_last_rate_limit()
            self.assertIsNotNone(rl)
