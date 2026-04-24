from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from zulip_bot.services.label_pr_execution import LabelPRError, LiveIssueDetails
from zulip_bot.services.label_pr_links import LabelPRLinkClaims, issue_label_pr_token


def _token(
    *,
    zulip_user_id: int = 101,
    github_login: str = "reviewer",
    pr_owner: str = "leanprover-community",
    pr_repo: str = "mathlib4",
    pr_number: int = 999,
) -> str:
    return issue_label_pr_token(
        claims=LabelPRLinkClaims(
            zulip_user_id=zulip_user_id,
            github_login=github_login,
            pr_owner=pr_owner,
            pr_repo=pr_repo,
            pr_number=pr_number,
        )
    )


def _url(token: str) -> str:
    return reverse("zulip-label-pr-form", kwargs={"token": token})


def _open_issue(title: str = "My PR") -> LiveIssueDetails:
    return LiveIssueDetails(title=title, is_open=True, author_login="author")


def _closed_issue(title: str = "My PR") -> LiveIssueDetails:
    return LiveIssueDetails(title=title, is_open=False, author_login="author")


def _patch_issue_details(details: LiveIssueDetails | None):
    return patch("zulip_bot.views.fetch_issue_details_for_form", return_value=details)


def _patch_repo_labels(labels=None):
    return patch("zulip_bot.views.fetch_repo_labels_from_db", return_value=labels or [])


def _patch_current_labels(names=None):
    return patch("zulip_bot.views.fetch_current_pr_label_names_from_db", return_value=names or set())


def _patch_pr_exists(exists: bool = False):
    mock_manager = MagicMock()
    mock_manager.filter.return_value.exists.return_value = exists
    return patch("syncer.models.PullRequest.objects", mock_manager)


def _patch_set_labels(*, raises: LabelPRError | None = None):
    if raises:
        return patch("zulip_bot.views.set_pr_labels", side_effect=raises)
    return patch("zulip_bot.views.set_pr_labels")


def _patch_post_actions():
    return patch("zulip_bot.views._enqueue_label_pr_post_actions")


class TestLabelPRFormGet(TestCase):
    def test_expired_token_returns_403(self) -> None:
        with patch("zulip_bot.services.label_pr_links.time.time", return_value=1_700_000_000):
            tok = _token()
        with patch("zulip_bot.services.label_pr_links.time.time", return_value=1_700_000_000 + 2_000):
            response = self.client.get(_url(tok))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "zulip_bot/label_pr_invalid.html")
        self.assertIn("expired", response.context["reason"])

    def test_invalid_token_returns_403(self) -> None:
        response = self.client.get(_url("not-a-valid-token"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "zulip_bot/label_pr_invalid.html")
        self.assertIn("invalid", response.context["reason"])

    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_valid_open_issue_shows_form(self) -> None:
        with (
            _patch_issue_details(_open_issue("Resolve the thing")),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
        ):
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "zulip_bot/label_pr_form.html")
        self.assertContains(response, "Resolve the thing")
        self.assertFalse(response.context["success"])
        self.assertTrue(response.context["pr_is_open"])

    def test_mutations_disabled_flag_in_context(self) -> None:
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
        ):
            response = self.client.get(_url(_token()))
        self.assertTrue(response.context["mutations_disabled"])


class TestLabelPRFormPost(TestCase):
    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_selected_labels_passed_to_set_pr_labels(self) -> None:
        tok = _token(pr_number=42)
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
            _patch_set_labels() as mock_set,
            _patch_post_actions(),
        ):
            response = self.client.post(_url(tok), data={"selected_labels": ["bug", "awaiting-review"]})
        self.assertEqual(response.status_code, 302)
        mock_set.assert_called_once_with(
            owner="leanprover-community",
            repo="mathlib4",
            number=42,
            label_names=["bug", "awaiting-review"],
        )

    def test_mutations_disabled_skips_set_labels(self) -> None:
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
            _patch_set_labels() as mock_set,
            _patch_post_actions(),
        ):
            response = self.client.post(_url(_token()), data={"selected_labels": ["bug"]})
        mock_set.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertIn("preflight=1", response["Location"])

    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_set_labels_error_shown_in_template(self) -> None:
        error = LabelPRError(code="permission_denied", message="GitHub permission denied when setting labels.")
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
            _patch_set_labels(raises=error),
            _patch_post_actions(),
        ):
            response = self.client.post(_url(_token()), data={"selected_labels": ["bug"]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["success"])
        self.assertIn("permission denied", response.context["label_error"] or "")

    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_closed_issue_on_post_skips_mutation(self) -> None:
        with (
            _patch_issue_details(_closed_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
            _patch_set_labels() as mock_set,
            _patch_post_actions() as mock_post,
        ):
            response = self.client.post(_url(_token()), data={"selected_labels": ["bug"]})
        mock_set.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["pr_is_open"])


class TestHasDbLabels(TestCase):
    def test_has_db_labels_false_when_pr_not_tracked(self) -> None:
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(),
            _patch_pr_exists(False),
        ):
            response = self.client.get(_url(_token()))
        self.assertFalse(response.context["has_db_labels"])

    def test_has_db_labels_true_when_pr_is_tracked(self) -> None:
        # A tracked PR with zero labels applied should still show has_db_labels=True,
        # so the UI doesn't incorrectly warn that current labels are unavailable.
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
            _patch_current_labels(set()),  # no labels, but PR is tracked
            _patch_pr_exists(True),
        ):
            response = self.client.get(_url(_token()))
        self.assertTrue(response.context["has_db_labels"])
