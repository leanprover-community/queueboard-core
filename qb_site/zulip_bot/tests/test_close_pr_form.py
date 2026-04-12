from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from zulip_bot.services.close_pr_execution import ClosePRError, LivePRDetails
from zulip_bot.services.close_pr_links import ClosePRLinkClaims, issue_close_pr_token


def _token(
    *,
    zulip_user_id: int = 101,
    github_login: str = "reviewer",
    pr_owner: str = "leanprover-community",
    pr_repo: str = "mathlib4",
    pr_number: int = 999,
) -> str:
    return issue_close_pr_token(
        claims=ClosePRLinkClaims(
            zulip_user_id=zulip_user_id,
            github_login=github_login,
            pr_owner=pr_owner,
            pr_repo=pr_repo,
            pr_number=pr_number,
        )
    )


def _url(token: str) -> str:
    return reverse("zulip-close-pr-form", kwargs={"token": token})


def _open_pr(title: str = "My PR") -> LivePRDetails:
    return LivePRDetails(title=title, is_open=True, author_login="author")


def _closed_pr(title: str = "My PR") -> LivePRDetails:
    return LivePRDetails(title=title, is_open=False, author_login="author")


def _patch_pr_details(pr: LivePRDetails | None):
    return patch("zulip_bot.views.fetch_pr_details_for_form", return_value=pr)


def _patch_close(*, raises: ClosePRError | None = None):
    if raises:
        return patch("zulip_bot.views.close_pull_request", side_effect=raises)
    return patch("zulip_bot.views.close_pull_request")


def _patch_post_actions():
    return patch("zulip_bot.views._enqueue_close_pr_post_actions")


class TestClosePRFormGet(TestCase):
    def test_expired_token_returns_403(self) -> None:
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000):
            tok = _token()
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000 + 1_900):
            with _patch_pr_details(_open_pr()):
                response = self.client.get(_url(tok))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "zulip_bot/close_pr_invalid.html")
        self.assertIn("expired", response.context["reason"])

    def test_invalid_token_returns_403(self) -> None:
        response = self.client.get(_url("not-a-valid-token"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "zulip_bot/close_pr_invalid.html")
        self.assertIn("invalid", response.context["reason"])

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_valid_open_pr_shows_confirmation_form(self) -> None:
        with _patch_pr_details(_open_pr("Fix the thing")):
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "zulip_bot/close_pr_form.html")
        self.assertContains(response, "Fix the thing")
        self.assertContains(response, "Close this pull request")
        self.assertFalse(response.context["success"])
        self.assertTrue(response.context["pr_is_open"])

    def test_already_closed_pr_shows_informational_message_no_button(self) -> None:
        with _patch_pr_details(_closed_pr()):
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["pr_is_open"])
        self.assertNotContains(response, "Close this pull request")

    def test_github_fetch_failure_assumes_open(self) -> None:
        # If we can't fetch PR details, we still show the form (POST will validate).
        with _patch_pr_details(None):
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["pr_is_open"])

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_mutations_enabled_flag_in_context(self) -> None:
        with _patch_pr_details(_open_pr()):
            response = self.client.get(_url(_token()))
        self.assertFalse(response.context["mutations_disabled"])

    def test_mutations_disabled_flag_in_context(self) -> None:
        with _patch_pr_details(_open_pr()):
            response = self.client.get(_url(_token()))
        self.assertTrue(response.context["mutations_disabled"])


class TestClosePRFormPost(TestCase):
    def test_mutations_disabled_shows_preflight_message(self) -> None:
        with _patch_pr_details(_open_pr()), _patch_close() as mock_close:
            response = self.client.post(_url(_token()))
        mock_close.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["mutations_disabled"])

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_successful_close(self) -> None:
        with _patch_pr_details(_open_pr()), _patch_close(), _patch_post_actions() as mock_post:
            response = self.client.post(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["success"])
        mock_post.assert_called_once()

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_github_close_error_shown_in_template(self) -> None:
        error = ClosePRError(code="permission_denied", message="GitHub permission denied when closing pull request.")
        with _patch_pr_details(_open_pr()), _patch_close(raises=error), _patch_post_actions():
            response = self.client.post(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["success"])
        self.assertIn("permission denied", response.context["close_error"] or "")

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_post_actions_called_with_claims_and_title(self) -> None:
        tok = _token(pr_number=42)
        with _patch_pr_details(_open_pr("The Title")), _patch_close(), _patch_post_actions() as mock_post:
            self.client.post(_url(tok))
        mock_post.assert_called_once()
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs["claims"].pr_number, 42)
        self.assertEqual(kwargs["pr_title"], "The Title")

    @override_settings(ZULIP_CLOSE_PR_MUTATIONS_ENABLED="true")
    def test_expired_token_on_post_returns_403(self) -> None:
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000):
            tok = _token()
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000 + 1_900):
            response = self.client.post(_url(tok))
        self.assertEqual(response.status_code, 403)


class TestRepoLogResolution(TestCase):
    def test_missing_setting_skips_log(self) -> None:
        from zulip_bot.views import _resolve_repo_log_target

        result = _resolve_repo_log_target(owner="leanprover-community", repo="mathlib4")
        self.assertIsNone(result)

    @override_settings(ZULIP_REPO_LOG={"leanprover-community/mathlib4": {"stream": "mathlib4", "topic": "bot log"}})
    def test_configured_repo_returns_target(self) -> None:
        from zulip_bot.views import _resolve_repo_log_target

        result = _resolve_repo_log_target(owner="leanprover-community", repo="mathlib4")
        self.assertIsNotNone(result)
        self.assertEqual(result["stream"], "mathlib4")
        self.assertEqual(result["topic"], "bot log")

    @override_settings(ZULIP_REPO_LOG={"other/repo": {"stream": "s", "topic": "t"}})
    def test_unconfigured_repo_returns_none(self) -> None:
        from zulip_bot.views import _resolve_repo_log_target

        result = _resolve_repo_log_target(owner="leanprover-community", repo="mathlib4")
        self.assertIsNone(result)

    @override_settings(ZULIP_REPO_LOG={"leanprover-community/mathlib4": {"stream": "", "topic": "bot log"}})
    def test_missing_stream_returns_none(self) -> None:
        from zulip_bot.views import _resolve_repo_log_target

        result = _resolve_repo_log_target(owner="leanprover-community", repo="mathlib4")
        self.assertIsNone(result)

    @override_settings(ZULIP_REPO_LOG="not-a-dict")
    def test_malformed_setting_returns_none(self) -> None:
        from zulip_bot.views import _resolve_repo_log_target

        result = _resolve_repo_log_target(owner="leanprover-community", repo="mathlib4")
        self.assertIsNone(result)
