from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from django.db import IntegrityError

from django.test import SimpleTestCase, override_settings
from django.urls import reverse


def _signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestGitHubWebhookEndpoint(SimpleTestCase):
    def test_method_not_allowed(self) -> None:
        response = self.client.get(reverse("github-webhook"))
        self.assertEqual(response.status_code, 405)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=False, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_disabled_returns_404(self) -> None:
        response = self.client.post(reverse("github-webhook"), data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="")
    def test_missing_secret_returns_503(self) -> None:
        response = self.client.post(reverse("github-webhook"), data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_invalid_signature_returns_403(self) -> None:
        response = self.client.post(
            reverse("github-webhook"),
            data=b'{"zen":"hi"}',
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=bad",
            HTTP_X_GITHUB_EVENT="ping",
            HTTP_X_GITHUB_DELIVERY="delivery-1",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_missing_signature_returns_403(self) -> None:
        response = self.client.post(
            reverse("github-webhook"),
            data=b'{"zen":"hi"}',
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="ping",
            HTTP_X_GITHUB_DELIVERY="delivery-1b",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_valid_signature_returns_202(self) -> None:
        payload = b'{"zen":"hi"}'
        with patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create:
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="ping",
                HTTP_X_GITHUB_DELIVERY="delivery-2",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "accepted"})
        self.assertEqual(mock_create.call_count, 1)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "ping")
        self.assertEqual(kwargs["status"], "ACCEPTED")
        self.assertEqual(kwargs["summary_json"]["route"], "noop")

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_missing_delivery_id_returns_400(self) -> None:
        payload = b'{"zen":"hi"}'
        response = self.client.post(
            reverse("github-webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
            HTTP_X_GITHUB_EVENT="ping",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_duplicate_delivery_returns_202_duplicate(self) -> None:
        payload = b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"}}'
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create", side_effect=IntegrityError),
            patch("syncer.views.GitHubWebhookDelivery.objects.filter") as mock_filter,
        ):
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-3",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "duplicate"})
        self.assertEqual(mock_filter.call_count, 1)
        self.assertEqual(mock_filter.return_value.update.call_count, 1)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_form_encoded_payload_parses_action_and_repo(self) -> None:
        inner = '{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"}}'
        payload = urlencode({"payload": inner}).encode("utf-8")
        with patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create:
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/x-www-form-urlencoded",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-4",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_create.call_count, 1)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["action"], "completed")
        self.assertEqual(kwargs["repository_owner"], "leanprover-community")
        self.assertEqual(kwargs["repository_name"], "mathlib4")
        self.assertEqual(kwargs["summary_json"]["route"], "check")
        self.assertEqual(kwargs["summary_json"]["head_sha"], "")

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_pull_request_event_enqueues_sync_pr(self) -> None:
        payload = b'{"action":"synchronize","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},"pull_request":{"number":123}}'
        repo = SimpleNamespace(id=7)
        task = SimpleNamespace(id="task-1")
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_filter,
            patch("syncer.views.sync_pr_task.delay", return_value=task) as mock_delay,
        ):
            mock_filter.return_value.only.return_value.first.return_value = repo
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="pull_request",
                HTTP_X_GITHUB_DELIVERY="delivery-5",
            )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_called_once_with(7, 123)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["route"], "pull_request")
        self.assertEqual(kwargs["summary_json"]["reason"], "enqueued_sync_pr")
        self.assertEqual(kwargs["summary_json"]["enqueued_sync_prs"], 1)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_pull_request_event_skips_when_repo_missing_or_inactive(self) -> None:
        payload = b'{"action":"opened","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},"pull_request":{"number":124}}'
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_filter,
            patch("syncer.views.sync_pr_task.delay") as mock_delay,
        ):
            mock_filter.return_value.only.return_value.first.return_value = None
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="pull_request",
                HTTP_X_GITHUB_DELIVERY="delivery-6",
            )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "repository_not_active_or_missing")

    @override_settings(
        SYNCER_GITHUB_WEBHOOK_ENABLED=True,
        SYNCER_GITHUB_WEBHOOK_DRY_RUN=True,
        GITHUB_WEBHOOK_SECRET="test-secret",
    )
    def test_pull_request_event_dry_run_does_not_enqueue(self) -> None:
        payload = b'{"action":"opened","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},"pull_request":{"number":130}}'
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.sync_pr_task.delay") as mock_delay,
        ):
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="pull_request",
                HTTP_X_GITHUB_DELIVERY="delivery-6b",
            )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "dry_run")
        self.assertEqual(kwargs["summary_json"]["would_enqueue_sync_prs"], 1)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_pull_request_event_ignores_untracked_action(self) -> None:
        payload = b'{"action":"review_requested","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},"pull_request":{"number":125}}'
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.sync_pr_task.delay") as mock_delay,
        ):
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="pull_request",
                HTTP_X_GITHUB_DELIVERY="delivery-7",
            )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "ignored_action")

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_pull_request_event_dedupe_suppresses_enqueue(self) -> None:
        payload = b'{"action":"synchronize","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},"pull_request":{"number":126}}'
        repo = SimpleNamespace(id=7)
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_filter,
            patch("syncer.views.claim_enqueue_slot", return_value=False),
            patch("syncer.views.sync_pr_task.delay") as mock_delay,
        ):
            mock_filter.return_value.only.return_value.first.return_value = repo
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="pull_request",
                HTTP_X_GITHUB_DELIVERY="delivery-7b",
            )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "deduped_sync_pr")
        self.assertEqual(kwargs["summary_json"]["enqueued_sync_prs"], 0)
        self.assertEqual(kwargs["summary_json"]["deduped_sync_prs"], 1)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_check_run_event_enqueues_sync_ci_for_resolved_prs(self) -> None:
        payload = (
            b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},'
            b'"check_run":{"head_sha":"abc123","pull_requests":[{"number":201}]}}'
        )
        repo = SimpleNamespace(id=9)
        task = SimpleNamespace(id="ci-task-1")
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_repo_filter,
            patch("syncer.views.PullRequest.objects.filter") as mock_pr_filter,
            patch("syncer.views.sync_ci_for_shas_task.delay", return_value=task) as mock_ci_delay,
        ):
            mock_repo_filter.return_value.only.return_value.first.return_value = repo
            mock_pr_filter.return_value.values_list.return_value = [202]
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-8",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_ci_delay.call_count, 2)
        mock_ci_delay.assert_any_call(
            9,
            201,
            shas=["abc123"],
            require_pr_association=False,
            trigger_analyzer_after_sync=True,
        )
        mock_ci_delay.assert_any_call(
            9,
            202,
            shas=["abc123"],
            require_pr_association=False,
            trigger_analyzer_after_sync=True,
        )
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "enqueued_sync_ci")
        self.assertEqual(kwargs["summary_json"]["enqueued_sync_ci"], 2)
        self.assertEqual(kwargs["summary_json"]["resolved_pr_numbers"], [201, 202])
        self.assertEqual(kwargs["summary_json"]["check_sync_mode"], "pr_fanout")

    @override_settings(
        SYNCER_GITHUB_WEBHOOK_ENABLED=True,
        SYNCER_GITHUB_WEBHOOK_CHECK_SHA_FIRST=True,
        GITHUB_WEBHOOK_SECRET="test-secret",
    )
    def test_check_run_event_sha_first_mode_enqueues_single_repo_sha_task(self) -> None:
        payload = (
            b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},'
            b'"check_run":{"head_sha":"abc123","pull_requests":[{"number":201}]}}'
        )
        repo = SimpleNamespace(id=9)
        task = SimpleNamespace(id="repo-sha-task-1")
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_repo_filter,
            patch("syncer.views.PullRequest.objects.filter") as mock_pr_filter,
            patch("syncer.views.sync_ci_for_repo_shas_task.delay", return_value=task) as mock_repo_sha_delay,
            patch("syncer.views.sync_ci_for_shas_task.delay") as mock_legacy_ci_delay,
        ):
            mock_repo_filter.return_value.only.return_value.first.return_value = repo
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-8b",
            )
        self.assertEqual(response.status_code, 202)
        mock_repo_sha_delay.assert_called_once_with(
            9,
            shas=["abc123"],
            require_pr_association=False,
            trigger_analyzer_after_sync=True,
        )
        mock_legacy_ci_delay.assert_not_called()
        mock_pr_filter.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "enqueued_sync_ci")
        self.assertEqual(kwargs["summary_json"]["enqueued_sync_ci"], 1)
        self.assertEqual(kwargs["summary_json"]["check_sync_mode"], "sha_first")

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_check_run_event_skips_when_action_ignored(self) -> None:
        payload = (
            b'{"action":"foo","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},'
            b'"check_run":{"head_sha":"abc123","pull_requests":[{"number":203}]}}'
        )
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.sync_ci_for_shas_task.delay") as mock_ci_delay,
        ):
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-9",
            )
        self.assertEqual(response.status_code, 202)
        mock_ci_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "ignored_action")

    @override_settings(
        SYNCER_GITHUB_WEBHOOK_ENABLED=True,
        SYNCER_GITHUB_WEBHOOK_DRY_RUN=True,
        GITHUB_WEBHOOK_SECRET="test-secret",
    )
    def test_check_run_event_dry_run_does_not_enqueue(self) -> None:
        payload = (
            b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},'
            b'"check_run":{"head_sha":"abc123","pull_requests":[{"number":204}]}}'
        )
        repo = SimpleNamespace(id=10)
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_repo_filter,
            patch("syncer.views.PullRequest.objects.filter") as mock_pr_filter,
            patch("syncer.views.sync_ci_for_shas_task.delay") as mock_ci_delay,
        ):
            mock_repo_filter.return_value.only.return_value.first.return_value = repo
            mock_pr_filter.return_value.values_list.return_value = [205]
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-9b",
            )
        self.assertEqual(response.status_code, 202)
        mock_ci_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "dry_run")
        self.assertEqual(kwargs["summary_json"]["would_enqueue_sync_ci"], 2)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_check_run_event_dedupe_suppresses_enqueue(self) -> None:
        payload = (
            b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"},'
            b'"check_run":{"head_sha":"abc123","pull_requests":[{"number":201}]}}'
        )
        repo = SimpleNamespace(id=9)
        with (
            patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create,
            patch("syncer.views.Repository.objects.filter") as mock_repo_filter,
            patch("syncer.views.PullRequest.objects.filter") as mock_pr_filter,
            patch("syncer.views.claim_enqueue_slot", return_value=False),
            patch("syncer.views.sync_ci_for_shas_task.delay") as mock_ci_delay,
        ):
            mock_repo_filter.return_value.only.return_value.first.return_value = repo
            mock_pr_filter.return_value.values_list.return_value = []
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-10",
            )
        self.assertEqual(response.status_code, 202)
        mock_ci_delay.assert_not_called()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["summary_json"]["reason"], "deduped_sync_ci")
        self.assertEqual(kwargs["summary_json"]["enqueued_sync_ci"], 0)
        self.assertEqual(kwargs["summary_json"]["deduped_sync_ci"], 1)
