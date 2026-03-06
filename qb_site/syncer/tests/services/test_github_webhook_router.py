from __future__ import annotations

from django.test import SimpleTestCase

from syncer.services.github_webhook_router import route_github_webhook


class TestGitHubWebhookRouter(SimpleTestCase):
    def test_pull_request_event_routes_with_pr_number(self) -> None:
        payload = {
            "action": "synchronize",
            "repository": {"owner": {"login": "leanprover-community"}, "name": "mathlib4"},
            "pull_request": {"number": 12345},
        }
        res = route_github_webhook(event="pull_request", payload=payload)
        self.assertEqual(res["route"], "pull_request")
        self.assertTrue(res["supported_event"])
        self.assertEqual(res["pr_numbers"], [12345])
        self.assertEqual(res["repository"], {"owner": "leanprover-community", "name": "mathlib4"})

    def test_check_run_event_routes_with_head_sha_and_pr_numbers(self) -> None:
        payload = {
            "action": "completed",
            "repository": {"owner": {"login": "leanprover-community"}, "name": "mathlib4"},
            "check_run": {
                "head_sha": "abc123",
                "pull_requests": [{"number": 11}, {"number": 11}, {"number": 12}],
            },
        }
        res = route_github_webhook(event="check_run", payload=payload)
        self.assertEqual(res["route"], "check")
        self.assertTrue(res["supported_event"])
        self.assertEqual(res["head_sha"], "abc123")
        self.assertEqual(res["pr_numbers"], [11, 12])

    def test_unsupported_event_routes_to_noop(self) -> None:
        payload = {"action": "created", "repository": {"owner": {"login": "o"}, "name": "r"}}
        res = route_github_webhook(event="fork", payload=payload)
        self.assertEqual(res["route"], "noop")
        self.assertFalse(res["supported_event"])
        self.assertEqual(res["reason"], "unsupported_event")
