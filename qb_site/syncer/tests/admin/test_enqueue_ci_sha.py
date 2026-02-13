from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from core.models import Repository
from syncer.models import CIShaFetchState, PullRequest


class TestEnqueueCISHAAdmin(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client = Client()
        self.client.force_login(self.admin_user)
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="main", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self.repo.created_at,
            gh_updated_at=self.repo.created_at,
            base_ref_name="main",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    def test_require_association_checkbox_defaults_off_when_unchecked(self) -> None:
        url = reverse("admin:syncer_pullrequest_enqueue_ci_sha", args=[self.pr.pk])
        with self.settings(ALLOWED_HOSTS=["testserver"]):
            with self.modify_settings(MIDDLEWARE={"remove": ["django.middleware.csrf.CsrfViewMiddleware"]}):
                from unittest.mock import patch

                with patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task.delay") as mock_delay:
                    resp = self.client.post(
                        url,
                        {
                            "shas": "abc123",
                            "pages": "1",
                            # intentionally omit require_assoc to simulate unchecked box
                        },
                        follow=True,
                    )
                self.assertEqual(resp.status_code, 200)
                mock_delay.assert_called_once()
                kwargs = mock_delay.call_args.kwargs
                self.assertFalse(kwargs.get("require_pr_association"))

    def test_override_backoff_enqueues_blocked_sha(self) -> None:
        url = reverse("admin:syncer_pullrequest_enqueue_ci_sha", args=[self.pr.pk])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="blockedsha",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        with self.settings(ALLOWED_HOSTS=["testserver"], SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60):
            with self.modify_settings(MIDDLEWARE={"remove": ["django.middleware.csrf.CsrfViewMiddleware"]}):
                from unittest.mock import patch

                with patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task.delay") as mock_delay:
                    resp = self.client.post(
                        url,
                        {
                            "shas": "blockedsha",
                            "pages": "1",
                            "override_backoff": "on",
                        },
                        follow=True,
                    )
                self.assertEqual(resp.status_code, 200)
                mock_delay.assert_called_once()
                kwargs = mock_delay.call_args.kwargs
                self.assertEqual(kwargs.get("shas"), ["blockedsha"])

    def test_reset_fetch_state_allows_enqueue_after_backoff(self) -> None:
        url = reverse("admin:syncer_pullrequest_enqueue_ci_sha", args=[self.pr.pk])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="resetsha",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        with self.settings(ALLOWED_HOSTS=["testserver"], SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60):
            with self.modify_settings(MIDDLEWARE={"remove": ["django.middleware.csrf.CsrfViewMiddleware"]}):
                from unittest.mock import patch

                with patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task.delay") as mock_delay:
                    resp = self.client.post(
                        url,
                        {
                            "shas": "resetsha",
                            "pages": "1",
                            "reset_fetch_state": "on",
                        },
                        follow=True,
                    )
                self.assertEqual(resp.status_code, 200)
                mock_delay.assert_called_once()
                kwargs = mock_delay.call_args.kwargs
                self.assertEqual(kwargs.get("shas"), ["resetsha"])
                self.assertFalse(CIShaFetchState.objects.filter(repository=self.repo, sha="resetsha").exists())

    def test_response_feedback_includes_task_id(self) -> None:
        url = reverse("admin:syncer_pullrequest_enqueue_ci_sha", args=[self.pr.pk])
        with self.settings(ALLOWED_HOSTS=["testserver"]):
            with self.modify_settings(MIDDLEWARE={"remove": ["django.middleware.csrf.CsrfViewMiddleware"]}):
                from unittest.mock import patch

                with patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task.delay") as mock_delay:
                    mock_delay.return_value.id = "task-feedback-123"
                    resp = self.client.post(
                        url,
                        {
                            "shas": "abc123",
                            "pages": "1",
                            "override_backoff": "on",
                        },
                        follow=True,
                    )
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "task-feedback-123")
                self.assertContains(resp, "CI-by-SHA task enqueued")
