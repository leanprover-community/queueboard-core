"""Tests for the session-authenticated reviewer preferences page (design doc 022 amendment).

Covers the invariants that make GitHub-OAuth auth safe for a writable surface: the page never
creates preference rows, console admission is reviewer-ness rather than "known GitHub account", and
ownership scoping holds on POST as well as GET.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analyzer.models import AssignmentProposal
from console.session import SESSION_NONCE_KEY, SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
from core.services.github_oauth import GitHubUserIdentity
from core.services.oauth_state import ConsoleOAuthStateClaims, issue_console_oauth_state
from core.services.reviewer_notification_settings import DEFAULT_AUTO_UNASSIGN_DAYS, DEFAULT_STALE_NUDGE_DAYS
from syncer.models import LabelDef


@override_settings(CONSOLE_PREFS_ENABLED=True)
class ConsolePrefsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo1 = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.repo2 = Repository.objects.create(owner="leanprover", name="stdlib", default_branch="master")
        self.reviewer = User.objects.create(github_login="bob", github_node_id="node-bob", zulip_user_id=7001)
        self.pref1 = ReviewerPreference.objects.create(
            user=self.reviewer,
            repository=self.repo1,
            maximum_capacity=10,
            auto_assign=True,
            notifications_enabled=False,
            notification_settings={"stale_nudge_days": 2, "auto_unassign_days": 5},
            preferred_labels=["t-algebra"],
            free_form="note one",
            conflict_of_interest=["alice"],
        )
        self.pref2 = ReviewerPreference.objects.create(
            user=self.reviewer,
            repository=self.repo2,
            maximum_capacity=4,
            auto_assign=False,
            notifications_enabled=False,
            preferred_labels=[],
            free_form="note two",
            conflict_of_interest=[],
        )
        LabelDef.objects.bulk_create(
            [
                LabelDef(repository=self.repo1, name="t-algebra", color="111111"),
                LabelDef(repository=self.repo1, name="t-number-theory", color="222222"),
                LabelDef(repository=self.repo1, name="maintainer-merge", color="333333"),
                LabelDef(repository=self.repo2, name="t-analysis", color="444444"),
            ]
        )
        self.url = reverse("console:prefs")

    # ---- helpers -------------------------------------------------------

    def _login_session(self, user: User | None = None) -> None:
        session = self.client.session
        session[SESSION_USER_KEY] = int((user or self.reviewer).id)
        session.save()

    def _post_data(self) -> tuple[dict[str, object], dict[int, int]]:
        """A round-trip payload for the reviewer's own rows, in the page's own order."""
        prefs = list(
            ReviewerPreference.objects.filter(user=self.reviewer).order_by("repository__owner", "repository__name", "id")
        )
        data: dict[str, object] = {
            "form-TOTAL_FORMS": str(len(prefs)),
            "form-INITIAL_FORMS": str(len(prefs)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        index_by_pref_id: dict[int, int] = {}
        for idx, pref in enumerate(prefs):
            index_by_pref_id[pref.id] = idx
            policy = pref.notification_settings or {}
            data[f"form-{idx}-id"] = str(pref.id)
            data[f"form-{idx}-maximum_capacity"] = str(pref.maximum_capacity)
            data[f"form-{idx}-auto_assign"] = "on" if pref.auto_assign else ""
            data[f"form-{idx}-notifications_enabled"] = "on" if pref.notifications_enabled else ""
            data[f"form-{idx}-stale_nudge_days"] = str(policy.get("stale_nudge_days", DEFAULT_STALE_NUDGE_DAYS))
            data[f"form-{idx}-auto_unassign_days"] = str(policy.get("auto_unassign_days", DEFAULT_AUTO_UNASSIGN_DAYS))
            data[f"form-{idx}-away_until"] = ""
            data[f"form-{idx}-preferred_labels"] = list(pref.preferred_labels or [])
            data[f"form-{idx}-conflict_of_interest"] = "\n".join(pref.conflict_of_interest or [])
            data[f"form-{idx}-free_form"] = pref.free_form or ""
        return data, index_by_pref_id

    # ---- auth ----------------------------------------------------------

    def test_anonymous_get_redirects_to_login_carrying_next(self) -> None:
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{reverse('console:login')}?next={self.url}")

    def test_known_non_reviewer_session_is_refused_and_creates_no_preferences(self) -> None:
        # The syncer upserts a core.User for every PR author; such an account must not be able to
        # reach the prefs page, and above all must not gain preference rows by visiting it.
        contributor = User.objects.create(github_login="drive-by", github_node_id="node-drive-by")
        self._login_session(contributor)

        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "only for registered reviewers", status_code=403)
        self.assertFalse(ReviewerPreference.objects.filter(user=contributor).exists())

    def test_known_non_reviewer_is_refused_at_sign_in(self) -> None:
        contributor = User.objects.create(github_login="drive-by", github_node_id="node-drive-by")
        session = self.client.session
        session[SESSION_NONCE_KEY] = "nonce-123"
        session.save()
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="nonce-123", next="/console/"))

        fake = MagicMock()
        fake.exchange_code_for_access_token.return_value = "gho_token"
        fake.fetch_user_identity.return_value = GitHubUserIdentity(
            github_user_id=42,
            github_node_id=contributor.github_node_id,
            github_login=contributor.github_login,
            github_name="Drive By",
            github_avatar_url=None,
        )
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})

        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_USER_KEY))
        self.assertFalse(ReviewerPreference.objects.filter(user=contributor).exists())

    def test_reviewer_with_pending_proposal_but_no_rows_keeps_access(self) -> None:
        # A maintainer removing the last preference row must not lock the reviewer out of the
        # proposal already made to them; the prefs page then has nothing to edit.
        ReviewerPreference.objects.filter(user=self.reviewer).delete()
        AssignmentProposal.objects.create(
            repository=self.repo1,
            pr_number=123,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self._login_session()

        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "no reviewer preferences")
        self.assertFalse(ReviewerPreference.objects.filter(user=self.reviewer).exists())

    @override_settings(CONSOLE_PREFS_ENABLED=False)
    def test_page_is_unavailable_while_the_flag_is_off(self) -> None:
        self._login_session()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn’t enabled yet")

    # ---- render / save -------------------------------------------------

    def test_get_renders_a_card_per_owned_row(self) -> None:
        self._login_session()

        resp = self.client.get(self.url)
        body = resp.content.decode("utf-8")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertContains(resp, "Reviewer preferences")
        self.assertContains(resp, "leanprover-community/mathlib4")
        self.assertContains(resp, "leanprover/stdlib")
        # Same fields as the token page, from the shared partial.
        self.assertIn('name="form-0-maximum_capacity"', body)
        self.assertIn('name="form-1-maximum_capacity"', body)
        self.assertIn('name="form-0-preferred_labels"', body)
        # Topic labels only; non-topic labels stay out of the selector.
        self.assertContains(resp, "t-number-theory")
        self.assertNotContains(resp, "maintainer-merge")
        # No countdown on this page — the session bounds it, not a token.
        self.assertNotContains(resp, "countdown-text")
        # Django only strips `{#  #}` comments on a single line, so a multi-line one leaks as page
        # text. Nothing that looks like template syntax may reach the reader.
        self.assertNotContains(resp, "{#")
        self.assertNotContains(resp, "{%")
        self.assertNotIn("prefs_form.js mounts", body)

    def test_post_saves_and_redirects_with_saved_flag(self) -> None:
        self._login_session()
        data, index_by_id = self._post_data()
        i1 = index_by_id[self.pref1.id]
        i2 = index_by_id[self.pref2.id]
        data[f"form-{i1}-maximum_capacity"] = "7"
        data[f"form-{i1}-notifications_enabled"] = "on"
        data[f"form-{i1}-stale_nudge_days"] = "4"
        data[f"form-{i1}-auto_unassign_days"] = "9"
        data[f"form-{i1}-preferred_labels"] = ["t-algebra", "t-number-theory"]
        data[f"form-{i2}-free_form"] = "updated note"
        data[f"form-{i2}-auto_assign"] = "on"

        resp = self.client.post(self.url, data=data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{self.url}?saved=1")

        self.pref1.refresh_from_db()
        self.pref2.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 7)
        self.assertTrue(self.pref1.notifications_enabled)
        self.assertEqual(self.pref1.notification_settings, {"stale_nudge_days": 4, "auto_unassign_days": 9})
        self.assertEqual(self.pref1.preferred_labels, ["t-algebra", "t-number-theory"])
        self.assertEqual(self.pref2.free_form, "updated note")
        self.assertTrue(self.pref2.auto_assign)

        followed = self.client.get(f"{self.url}?saved=1")
        self.assertContains(followed, "Preferences saved")

    def test_cannot_post_another_reviewers_row(self) -> None:
        other = User.objects.create(github_login="carol", github_node_id="node-carol")
        other_pref = ReviewerPreference.objects.create(
            user=other, repository=self.repo1, maximum_capacity=3, free_form="carol's note"
        )
        self._login_session()
        data, index_by_id = self._post_data()
        # Point the first form at Carol's row and try to rewrite it.
        i1 = index_by_id[self.pref1.id]
        data[f"form-{i1}-id"] = str(other_pref.id)
        data[f"form-{i1}-maximum_capacity"] = "99"
        data[f"form-{i1}-free_form"] = "hijacked"

        resp = self.client.post(self.url, data=data)

        self.assertEqual(resp.status_code, 200)  # re-rendered with errors, not saved
        other_pref.refresh_from_db()
        self.assertEqual(other_pref.maximum_capacity, 3)
        self.assertEqual(other_pref.free_form, "carol's note")

    def test_away_until_is_interpreted_in_the_resolved_timezone(self) -> None:
        self._login_session()
        data, index_by_id = self._post_data()
        i1 = index_by_id[self.pref1.id]
        data[f"form-{i1}-away_until"] = "2026-03-01T09:00"

        with patch("console.views.resolve_user_timezone_name", return_value="Europe/Berlin"):
            resp = self.client.post(self.url, data=data)

        self.assertEqual(resp.status_code, 302)
        self.pref1.refresh_from_db()
        self.assertEqual(
            self.pref1.away_until.astimezone(dt_timezone.utc),
            datetime(2026, 3, 1, 8, 0, tzinfo=dt_timezone.utc),
        )

    def test_timezone_resolution_uses_the_reviewers_zulip_link(self) -> None:
        self._login_session()
        with patch("console.views.resolve_user_timezone_name", return_value="UTC") as resolver:
            self.client.get(self.url)
        resolver.assert_called_once_with(user=self.reviewer)

    def test_home_links_to_the_prefs_page(self) -> None:
        self._login_session()
        resp = self.client.get(reverse("console:home"))
        self.assertContains(resp, self.url)

    @override_settings(CONSOLE_PREFS_ENABLED=False)
    def test_home_hides_the_prefs_link_while_the_flag_is_off(self) -> None:
        self._login_session()
        resp = self.client.get(reverse("console:home"))
        self.assertNotContains(resp, self.url)
