from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from zulip_bot.services.close_pr_execution import (
    ClosePRError,
    PermissionOutcome,
    add_pr_labels,
    check_close_pr_permission,
    close_pull_request,
)


def _make_response(*, status_code: int, json_data: object = None) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    if json_data is not None:
        mock.json.return_value = json_data
    else:
        mock.json.side_effect = ValueError("no body")
        mock.text = ""
    return mock


class TestCheckClosePRPermission(SimpleTestCase):
    def _patch_token(self, token: str | None = "test-token"):
        return patch(
            "zulip_bot.services.close_pr_execution.resolve_github_app_operation_token",
            return_value=token,
        )

    def _patch_get(self, responses: list):
        """Patch requests.get to return successive responses."""
        return patch("zulip_bot.services.close_pr_execution.requests.get", side_effect=responses)

    def test_token_unavailable(self) -> None:
        with self._patch_token(None):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.TOKEN_UNAVAILABLE)

    def test_github_error_on_pr_fetch(self) -> None:
        pr_response = _make_response(status_code=500)
        with self._patch_token(), self._patch_get([pr_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.GITHUB_ERROR)

    def test_pr_not_open(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Fix bug", "state": "closed", "merged_at": None, "user": {"login": "author"}},
        )
        with self._patch_token(), self._patch_get([pr_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PR_NOT_OPEN)
        self.assertEqual(result.pr_title, "Fix bug")

    def test_permitted_as_pr_author(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "My PR", "state": "open", "merged_at": None, "user": {"login": "reviewer"}},
        )
        with self._patch_token(), self._patch_get([pr_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PERMITTED)
        self.assertEqual(result.pr_title, "My PR")

    def test_permitted_as_pr_author_case_insensitive(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "My PR", "state": "open", "merged_at": None, "user": {"login": "Reviewer"}},
        )
        with self._patch_token(), self._patch_get([pr_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PERMITTED)

    def test_permitted_as_write_collaborator(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Someone's PR", "state": "open", "merged_at": None, "user": {"login": "author"}},
        )
        collab_response = _make_response(status_code=200, json_data={"permission": "write"})
        with self._patch_token(), self._patch_get([pr_response, collab_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PERMITTED)

    def test_permitted_as_admin_collaborator(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Someone's PR", "state": "open", "merged_at": None, "user": {"login": "author"}},
        )
        collab_response = _make_response(status_code=200, json_data={"permission": "admin"})
        with self._patch_token(), self._patch_get([pr_response, collab_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PERMITTED)

    def test_not_permitted_read_only(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Someone's PR", "state": "open", "merged_at": None, "user": {"login": "author"}},
        )
        collab_response = _make_response(status_code=200, json_data={"permission": "read"})
        with self._patch_token(), self._patch_get([pr_response, collab_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.NOT_PERMITTED)

    def test_not_permitted_no_access(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Someone's PR", "state": "open", "merged_at": None, "user": {"login": "author"}},
        )
        collab_response = _make_response(status_code=200, json_data={"permission": "none"})
        with self._patch_token(), self._patch_get([pr_response, collab_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.NOT_PERMITTED)

    def test_not_permitted_when_collaborator_endpoint_returns_404(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={"title": "Someone's PR", "state": "open", "merged_at": None, "user": {"login": "author"}},
        )
        collab_response = _make_response(status_code=404)
        with self._patch_token(), self._patch_get([pr_response, collab_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.NOT_PERMITTED)

    def test_merged_pr_treated_as_not_open(self) -> None:
        pr_response = _make_response(
            status_code=200,
            json_data={
                "title": "Merged PR",
                "state": "closed",
                "merged_at": "2024-01-01T00:00:00Z",
                "user": {"login": "author"},
            },
        )
        with self._patch_token(), self._patch_get([pr_response]):
            result = check_close_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PR_NOT_OPEN)


class TestClosePullRequest(SimpleTestCase):
    def _patch_token(self, token: str | None = "test-token"):
        return patch(
            "zulip_bot.services.close_pr_execution.resolve_github_app_operation_token",
            return_value=token,
        )

    def _patch_patch(self, response):
        return patch("zulip_bot.services.close_pr_execution.requests.patch", return_value=response)

    def test_success(self) -> None:
        response = _make_response(status_code=200, json_data={"state": "closed"})
        with self._patch_token(), self._patch_patch(response):
            close_pull_request(owner="leanprover-community", repo="mathlib4", number=1)
        # No exception raised.

    def test_token_unavailable(self) -> None:
        with self._patch_token(None):
            with self.assertRaises(ClosePRError) as cm:
                close_pull_request(owner="leanprover-community", repo="mathlib4", number=1)
        self.assertEqual(cm.exception.code, "token_unavailable")

    def test_permission_denied(self) -> None:
        response = _make_response(status_code=403)
        with self._patch_token(), self._patch_patch(response):
            with self.assertRaises(ClosePRError) as cm:
                close_pull_request(owner="leanprover-community", repo="mathlib4", number=1)
        self.assertEqual(cm.exception.code, "permission_denied")

    def test_pr_not_found(self) -> None:
        response = _make_response(status_code=404)
        with self._patch_token(), self._patch_patch(response):
            with self.assertRaises(ClosePRError) as cm:
                close_pull_request(owner="leanprover-community", repo="mathlib4", number=1)
        self.assertEqual(cm.exception.code, "pr_not_found")

    def test_github_transient_error(self) -> None:
        response = _make_response(status_code=503)
        with self._patch_token(), self._patch_patch(response):
            with self.assertRaises(ClosePRError) as cm:
                close_pull_request(owner="leanprover-community", repo="mathlib4", number=1)
        self.assertEqual(cm.exception.code, "github_transient")


class TestAddPRLabels(SimpleTestCase):
    def _patch_token(self, token: str | None = "test-token"):
        return patch(
            "zulip_bot.services.close_pr_execution.resolve_github_app_operation_token",
            return_value=token,
        )

    def _patch_post(self, response):
        return patch("zulip_bot.services.close_pr_execution.requests.post", return_value=response)

    def test_empty_selection_no_ops(self) -> None:
        with self._patch_token(), patch("zulip_bot.services.close_pr_execution.requests.post") as mock_post:
            add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=[])
        mock_post.assert_not_called()

    def test_success(self) -> None:
        response = _make_response(status_code=200, json_data=[])
        with self._patch_token(), self._patch_post(response):
            add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])

    def test_token_unavailable(self) -> None:
        with self._patch_token(None):
            with self.assertRaises(ClosePRError) as cm:
                add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "token_unavailable")

    def test_permission_denied(self) -> None:
        response = _make_response(status_code=403)
        with self._patch_token(), self._patch_post(response):
            with self.assertRaises(ClosePRError) as cm:
                add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "permission_denied")

    def test_validation_failed(self) -> None:
        # 422: label name not found in the repo.
        response = _make_response(status_code=422, json_data={"message": "Validation Failed"})
        with self._patch_token(), self._patch_post(response):
            with self.assertRaises(ClosePRError) as cm:
                add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["no-such-label"])
        self.assertEqual(cm.exception.code, "validation_failed")

    def test_github_transient_error(self) -> None:
        response = _make_response(status_code=503)
        with self._patch_token(), self._patch_post(response):
            with self.assertRaises(ClosePRError) as cm:
                add_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "github_transient")
