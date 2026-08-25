"""Console suggestions + claim view tests (design doc 053).

GitHub is mocked (`assign_reviewer_and_record`, operation tokens); sessions are seeded directly.
The suggestion *service* itself runs for real against a seeded queue snapshot, so the claim
re-verification (Invariant 6) is exercised end to end rather than mocked.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analyzer.models import QueueSnapshot, ReviewerAssignmentApplication
from console.session import SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
from syncer.models import LabelDef


def _pr_entry(*, author: str = "zed", labels: list[str], assignees: list[str] | None = None, title: str = "a change") -> dict:
    return {
        "author": author,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "assignees": assignees or [],
        "pr_status": "AwaitingReview",
        "total_queue_time": {"status": "valid", "value_td": 1000.0},
    }


@override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED=True, ANALYZER_ASSIGNMENT_SUGGESTIONS_CONSOLE_CLAIM_ENABLED=True)
class SuggestionViewTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.now = timezone.now()
        self.reviewer = User.objects.create(github_login="bob", github_node_id="node-bob", zulip_user_id=7001)
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.reviewer,
            preferred_labels=["t-analysis"],
            maximum_capacity=5,
        )
        for name in ("t-analysis", "t-algebra"):
            LabelDef.objects.create(repository=self.repo, name=name, color="ededed")

    def _login_session(self, user: User | None = None) -> None:
        session = self.client.session
        session[SESSION_USER_KEY] = int((user or self.reviewer).id)
        session.save()

    def _seed_snapshot(self, prs: dict[str, dict]) -> None:
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key="default",  # no QueueRuleSet in these tests
            generated_at=self.now,
            payload={
                "meta": {"generated_at": self.now.isoformat()},
                "prs": prs,
                "lists": {"dashboards": {"Queue": [int(n) for n in prs]}},
            },
            etag="etag",
            pr_count=len(prs),
            queue_count=len(prs),
        )

    def _seed_default_snapshot(self) -> None:
        self._seed_snapshot(
            {
                "101": _pr_entry(labels=["t-analysis"], title="analysis PR"),
                "102": _pr_entry(labels=["t-algebra"], title="algebra PR"),
            }
        )

    # ---- suggestions page ------------------------------------------------

    def test_requires_session(self) -> None:
        response = self.client.get(reverse("console:suggestions"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("console:login"), response["Location"])

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED=False)
    def test_read_path_is_flag_gated(self) -> None:
        self._login_session()
        response = self.client.get(reverse("console:suggestions"))
        self.assertContains(response, "aren’t enabled yet")

    def test_renders_suggestions_for_the_session_reviewer(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        response = self.client.get(reverse("console:suggestions"))
        self.assertContains(response, "analysis PR")
        self.assertNotContains(response, "algebra PR")  # outside bob's areas
        self.assertContains(response, "Load:")

    def test_labels_param_prefills_and_overrides(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        response = self.client.get(reverse("console:suggestions"), {"labels": "t-algebra"})
        self.assertContains(response, "algebra PR")
        self.assertNotContains(response, "analysis PR")  # override replaces stored labels
        self.assertContains(response, 'value="t-algebra"')

    def test_unknown_labels_are_reported_not_trusted(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        response = self.client.get(reverse("console:suggestions"), {"labels": "t-typo,t-algebra"})
        self.assertContains(response, "Not topic labels in this repository")
        self.assertContains(response, "t-typo")
        self.assertContains(response, "algebra PR")

    def test_bogus_repo_param_is_ignored(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        response = self.client.get(reverse("console:suggestions"), {"repo": "999999"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "leanprover-community/mathlib4")

    def test_no_snapshot_renders_explanation(self) -> None:
        self._login_session()
        response = self.client.get(reverse("console:suggestions"))
        self.assertContains(response, "No queue snapshot is available")

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_CONSOLE_CLAIM_ENABLED=False)
    def test_claim_form_hidden_when_claim_disabled(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        response = self.client.get(reverse("console:suggestions"))
        self.assertContains(response, "analysis PR")
        self.assertNotContains(response, 'name="pr_numbers"')

    def test_home_links_to_suggestions_including_empty_state(self) -> None:
        self._login_session()
        response = self.client.get(reverse("console:home"))
        self.assertContains(response, "Find PRs to review")
        self.assertContains(response, reverse("console:suggestions"))

    # ---- claim -----------------------------------------------------------

    def _claim(self, pr_numbers: list[int], **extra):
        data = {"repo_id": str(self.repo.id), "pr_numbers": [str(n) for n in pr_numbers]}
        data.update(extra)
        return self.client.post(reverse("console:claim"), data)

    def test_claim_assigns_via_the_046_path(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("applied", None, None)) as mock_assign,
        ):
            response = self._claim([101])
        self.assertContains(response, "Assigned you to")
        self.assertContains(response, "#101")
        mock_assign.assert_called_once()
        kwargs = mock_assign.call_args.kwargs
        self.assertEqual(kwargs["pr_number"], 101)
        self.assertEqual(kwargs["login"], "bob")
        self.assertIsNone(kwargs["snapshot"])

    def test_claim_login_always_comes_from_the_session(self) -> None:
        # A posted login/reviewer field is ignored: the assigned login is the session reviewer's.
        self._seed_default_snapshot()
        self._login_session()
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("applied", None, None)) as mock_assign,
        ):
            self._claim([101], login="eve", reviewer_login="eve")
        self.assertEqual(mock_assign.call_args.kwargs["login"], "bob")

    def test_claim_rejects_a_number_not_in_the_fresh_eligible_set(self) -> None:
        # 102 is outside bob's areas, 999 is not in the pool at all: neither may reach GitHub.
        self._seed_default_snapshot()
        self._login_session()
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record") as mock_assign,
        ):
            response = self._claim([102, 999])
        mock_assign.assert_not_called()
        self.assertContains(response, "Could not assign")
        self.assertContains(response, "no longer eligible")

    def test_claim_partial_failure_renders_the_split(self) -> None:
        self._seed_snapshot(
            {
                "101": _pr_entry(labels=["t-analysis"], title="analysis PR"),
                "103": _pr_entry(labels=["t-analysis"], title="second analysis PR"),
            }
        )
        self._login_session()
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch(
                "console.views.assign_reviewer_and_record",
                side_effect=[("applied", None, None), ("failed", None, None)],
            ),
        ):
            response = self._claim([101, 103])
        self.assertContains(response, "Assigned you to")
        self.assertContains(response, "#101")
        self.assertContains(response, "Could not assign")
        self.assertContains(response, "#103")
        self.assertContains(response, "didn’t confirm")

    def test_claim_already_recorded_counts_only_when_applied(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        failed_record = ReviewerAssignmentApplication(status=ReviewerAssignmentApplication.STATUS_FAILED)
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("already_recorded", None, failed_record)),
        ):
            response = self._claim([101])
        self.assertContains(response, "Could not assign")

    def test_claim_respects_the_label_override_it_was_offered_under(self) -> None:
        # The offer came from ?labels=t-algebra; the re-check must run the same question,
        # or PR 102 (outside bob's stored areas) would wrongly fail eligibility.
        self._seed_default_snapshot()
        self._login_session()
        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("applied", None, None)) as mock_assign,
        ):
            response = self._claim([102], labels="t-algebra")
        self.assertContains(response, "Assigned you to")
        self.assertEqual(mock_assign.call_args.kwargs["pr_number"], 102)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_CONSOLE_CLAIM_ENABLED=False)
    def test_claim_is_flag_gated(self) -> None:
        self._seed_default_snapshot()
        self._login_session()
        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            response = self._claim([101])
        mock_assign.assert_not_called()
        self.assertContains(response, "isn’t enabled yet")

    def test_claim_requires_session(self) -> None:
        response = self._claim([101])
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("console:login"), response["Location"])
