from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from zulip_bot.services.close_pr_execution import PermissionOutcome
from zulip_bot.services.label_pr_execution import (
    LabelPRError,
    check_label_pr_permission,
    set_pr_labels,
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


class TestCheckLabelPRPermission(SimpleTestCase):
    def _patch_token(self, token: str | None = "test-token"):
        return patch(
            "zulip_bot.services.label_pr_execution.resolve_github_app_operation_token",
            return_value=token,
        )

    def _patch_get(self, responses: list):
        return patch("zulip_bot.services.label_pr_execution.requests.get", side_effect=responses)

    def _open_issue(self, *, author: str = "author") -> MagicMock:
        return _make_response(
            status_code=200,
            json_data={"title": "Some PR", "state": "open", "user": {"login": author}},
        )

    def test_token_unavailable(self) -> None:
        with self._patch_token(None):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.TOKEN_UNAVAILABLE)

    def test_github_error_on_issue_fetch(self) -> None:
        with self._patch_token(), self._patch_get([_make_response(status_code=500)]):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.GITHUB_ERROR)

    def test_issue_not_open(self) -> None:
        closed = _make_response(
            status_code=200,
            json_data={"title": "Fix bug", "state": "closed", "user": {"login": "author"}},
        )
        with self._patch_token(), self._patch_get([closed]):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PR_NOT_OPEN)
        self.assertEqual(result.pr_title, "Fix bug")

    def test_author_is_not_auto_permitted(self) -> None:
        # Unlike close-pr (where authorship alone grants permission), label-pr
        # requires write/admin even from the PR author.
        issue = self._open_issue(author="reviewer")
        collab = _make_response(status_code=200, json_data={"permission": "read"})
        with self._patch_token(), self._patch_get([issue, collab]):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.NOT_PERMITTED)

    def test_permitted_write_collaborator(self) -> None:
        issue = self._open_issue()
        collab = _make_response(status_code=200, json_data={"permission": "write"})
        with self._patch_token(), self._patch_get([issue, collab]):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.PERMITTED)
        self.assertEqual(result.pr_title, "Some PR")

    def test_not_permitted_read_only(self) -> None:
        issue = self._open_issue()
        collab = _make_response(status_code=200, json_data={"permission": "read"})
        with self._patch_token(), self._patch_get([issue, collab]):
            result = check_label_pr_permission(
                github_login="reviewer",
                owner="leanprover-community",
                repo="mathlib4",
                number=1,
            )
        self.assertEqual(result.outcome, PermissionOutcome.NOT_PERMITTED)


class TestSetPRLabels(SimpleTestCase):
    def _patch_token(self, token: str | None = "test-token"):
        return patch(
            "zulip_bot.services.label_pr_execution.resolve_github_app_operation_token",
            return_value=token,
        )

    def _patch_put(self, response):
        return patch("zulip_bot.services.label_pr_execution.requests.put", return_value=response)

    def test_success(self) -> None:
        response = _make_response(status_code=200, json_data=[])
        with self._patch_token(), self._patch_put(response):
            set_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])

    def test_token_unavailable(self) -> None:
        with self._patch_token(None):
            with self.assertRaises(LabelPRError) as cm:
                set_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=[])
        self.assertEqual(cm.exception.code, "token_unavailable")

    def test_permission_denied(self) -> None:
        response = _make_response(status_code=403)
        with self._patch_token(), self._patch_put(response):
            with self.assertRaises(LabelPRError) as cm:
                set_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "permission_denied")

    def test_not_found(self) -> None:
        response = _make_response(status_code=404)
        with self._patch_token(), self._patch_put(response):
            with self.assertRaises(LabelPRError) as cm:
                set_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "not_found")

    def test_validation_failed(self) -> None:
        # 422 is unique to label mutations — e.g. a label name that doesn't exist in the repo.
        response = _make_response(status_code=422, json_data={"message": "Validation Failed"})
        with self._patch_token(), self._patch_put(response):
            with self.assertRaises(LabelPRError) as cm:
                set_pr_labels(
                    owner="leanprover-community",
                    repo="mathlib4",
                    number=1,
                    label_names=["no-such-label"],
                )
        self.assertEqual(cm.exception.code, "validation_failed")

    def test_github_transient_error(self) -> None:
        response = _make_response(status_code=503)
        with self._patch_token(), self._patch_put(response):
            with self.assertRaises(LabelPRError) as cm:
                set_pr_labels(owner="leanprover-community", repo="mathlib4", number=1, label_names=["bug"])
        self.assertEqual(cm.exception.code, "github_transient")
