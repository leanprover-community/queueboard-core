from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from zulip_bot.services.label_pr_execution import LabelPRError, LiveIssueDetails
from zulip_bot.services.pr_action_links import LABEL_PR, PRActionLinkClaims, issue_pr_action_token


def _token(
    *,
    zulip_user_id: int = 101,
    github_login: str = "reviewer",
    pr_owner: str = "leanprover-community",
    pr_repo: str = "mathlib4",
    pr_number: int = 999,
    now: int | None = None,
) -> str:
    return issue_pr_action_token(
        action=LABEL_PR,
        claims=PRActionLinkClaims(
            zulip_user_id=zulip_user_id,
            github_login=github_login,
            pr_owner=pr_owner,
            pr_repo=pr_repo,
            pr_number=pr_number,
        ),
        now=now,
    )


def _url(token: str) -> str:
    return reverse("zulip-label-pr-form", kwargs={"token": token})


def _open_issue(title: str = "My PR", labels: tuple[tuple[str, str], ...] = ()) -> LiveIssueDetails:
    return LiveIssueDetails(title=title, is_open=True, author_login="author", labels=labels)


def _closed_issue(title: str = "My PR") -> LiveIssueDetails:
    return LiveIssueDetails(title=title, is_open=False, author_login="author")


@dataclass
class _FakeLabel:
    name: str
    color: str = ""


def _patch_issue_details(details: LiveIssueDetails | None):
    return patch("zulip_bot.views.fetch_issue_details_for_form", return_value=details)


def _patch_repo_labels(labels=None):
    return patch("zulip_bot.views.fetch_repo_labels_from_db", return_value=labels or [])


def _patch_set_labels(*, raises: LabelPRError | None = None):
    if raises:
        return patch("zulip_bot.views.set_pr_labels", side_effect=raises)
    return patch("zulip_bot.views.set_pr_labels")


def _patch_post_actions():
    return patch("zulip_bot.views._enqueue_label_pr_post_actions")


class TestLabelPRFormGet(TestCase):
    def test_expired_token_returns_403(self) -> None:
        tok = _token(now=int(time.time()) - 2000)
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
        with _patch_issue_details(_open_issue("Resolve the thing")), _patch_repo_labels():
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "zulip_bot/label_pr_form.html")
        self.assertContains(response, "Resolve the thing")
        self.assertFalse(response.context["success"])
        self.assertTrue(response.context["pr_is_open"])
        self.assertTrue(response.context["pr_details_available"])

    def test_mutations_disabled_flag_in_context(self) -> None:
        with _patch_issue_details(_open_issue()), _patch_repo_labels():
            response = self.client.get(_url(_token()))
        self.assertTrue(response.context["mutations_disabled"])

    def test_fetch_failure_shows_error_and_hides_form(self) -> None:
        # When the live fetch fails, we cannot safely render a label picker,
        # because PUT /labels would replace an unknown current set.
        with _patch_issue_details(None), _patch_repo_labels():
            response = self.client.get(_url(_token()))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["pr_details_available"])
        self.assertNotContains(response, "Save labels")
        self.assertContains(response, "Could not fetch")


class TestLabelPRFormPost(TestCase):
    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_selected_labels_passed_to_set_pr_labels(self) -> None:
        tok = _token(pr_number=42)
        with (
            _patch_issue_details(_open_issue()),
            _patch_repo_labels(),
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
            _patch_set_labels() as mock_set,
            _patch_post_actions() as mock_post,
        ):
            response = self.client.post(_url(_token()), data={"selected_labels": ["bug"]})
        mock_set.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["pr_is_open"])

    @override_settings(ZULIP_LABEL_PR_MUTATIONS_ENABLED="true")
    def test_fetch_failure_on_post_skips_mutation(self) -> None:
        # If the live state is unknown at POST time, the safest action is to
        # do nothing — replacing an unknown label set could silently drop labels.
        with (
            _patch_issue_details(None),
            _patch_repo_labels(),
            _patch_set_labels() as mock_set,
            _patch_post_actions() as mock_post,
        ):
            response = self.client.post(_url(_token()), data={"selected_labels": ["bug"]})
        mock_set.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["pr_details_available"])


class TestPreSelectionFromLiveLabels(TestCase):
    def test_live_labels_pre_check_matching_catalog_entries(self) -> None:
        issue = _open_issue(labels=(("bug", "ee0701"), ("awaiting-review", "00ff00")))
        catalog = [_FakeLabel(name="bug", color="ee0701"), _FakeLabel(name="other", color="cccccc")]
        with _patch_issue_details(issue), _patch_repo_labels(catalog):
            response = self.client.get(_url(_token()))
        labels = {item["name"]: item for item in response.context["available_labels"]}
        self.assertTrue(labels["bug"]["checked"])
        self.assertFalse(labels["other"]["checked"])

    def test_live_labels_outside_catalog_are_rendered_and_checked(self) -> None:
        # Defends against silent data loss: a label present on GitHub but not in
        # LabelDef must still appear in the picker so the user sees it and PUT
        # replacement does not drop it.
        issue = _open_issue(labels=(("custom-tag", "ff00ff"),))
        catalog = [_FakeLabel(name="bug", color="ee0701")]
        with _patch_issue_details(issue), _patch_repo_labels(catalog):
            response = self.client.get(_url(_token()))
        labels = {item["name"]: item for item in response.context["available_labels"]}
        self.assertIn("custom-tag", labels)
        self.assertTrue(labels["custom-tag"]["checked"])
        self.assertFalse(labels["bug"]["checked"])

    def test_label_name_match_is_case_insensitive(self) -> None:
        issue = _open_issue(labels=(("Bug", "ee0701"),))
        catalog = [_FakeLabel(name="bug", color="ee0701")]
        with _patch_issue_details(issue), _patch_repo_labels(catalog):
            response = self.client.get(_url(_token()))
        labels = {item["name"]: item for item in response.context["available_labels"]}
        self.assertTrue(labels["bug"]["checked"])
        self.assertNotIn("Bug", labels)

    def test_no_live_labels_results_in_no_pre_selection(self) -> None:
        issue = _open_issue(labels=())
        catalog = [_FakeLabel(name="bug", color="ee0701")]
        with _patch_issue_details(issue), _patch_repo_labels(catalog):
            response = self.client.get(_url(_token()))
        labels = {item["name"]: item for item in response.context["available_labels"]}
        self.assertFalse(labels["bug"]["checked"])
