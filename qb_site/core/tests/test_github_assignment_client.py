from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient


class TestGitHubAssignmentClient(SimpleTestCase):
    def _response(self, *, status_code: int, payload: dict | None = None, text: str = "") -> Mock:
        response = Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = payload or {}
        return response

    def test_assign_calls_issues_assignees_endpoint(self) -> None:
        with patch(
            "core.services.github_assignment.requests.request", return_value=self._response(status_code=200)
        ) as mock_request:
            client = GitHubAssignmentClient(token="tok")
            client.assign(owner="o", repo="r", number=3, github_login="alice")

        self.assertEqual(mock_request.call_args.args[0], "POST")
        self.assertTrue(mock_request.call_args.args[1].endswith("/repos/o/r/issues/3/assignees"))
        self.assertEqual(mock_request.call_args.kwargs["json"], {"assignees": ["alice"]})

    def test_unassign_maps_422_to_validation_failed(self) -> None:
        with patch(
            "core.services.github_assignment.requests.request",
            return_value=self._response(status_code=422, payload={"message": "Validation Failed"}),
        ):
            client = GitHubAssignmentClient(token="tok")
            with self.assertRaises(AssignmentMutationError) as exc:
                client.unassign(owner="o", repo="r", number=3, github_login="alice")

        self.assertEqual(exc.exception.code, "validation_failed")

    def test_assign_maps_403_to_permission_denied(self) -> None:
        with patch("core.services.github_assignment.requests.request", return_value=self._response(status_code=403)):
            client = GitHubAssignmentClient(token="tok")
            with self.assertRaises(AssignmentMutationError) as exc:
                client.assign(owner="o", repo="r", number=3, github_login="alice")

        self.assertEqual(exc.exception.code, "permission_denied")
