from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from syncer.models import SyncerMetricsSnapshot


class TestSyncerMetricsAdminColumnGroups(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client = Client()
        self.client.force_login(self.admin_user)
        SyncerMetricsSnapshot.objects.create(window_start=timezone.now(), window_seconds=900)

    def _list_display(self, url: str) -> tuple[str, ...]:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        changelist = response.context["cl"]
        return tuple(changelist.list_display)

    def test_cols_tasks_changes_list_display(self) -> None:
        url = reverse("admin:syncer_syncermetricssnapshot_changelist") + "?cols=tasks"
        list_display = self._list_display(url)
        self.assertIn("pr_failures", list_display)
        self.assertIn("repo_discovery_cost", list_display)
        self.assertNotIn("webhook_route_check", list_display)
        self.assertNotIn("rows_check_run", list_display)
        # Selection persists for subsequent navigations without the UI param.
        list_display_followup = self._list_display(reverse("admin:syncer_syncermetricssnapshot_changelist"))
        self.assertIn("pr_failures", list_display_followup)

    def test_cols_webhook_changes_list_display(self) -> None:
        url = reverse("admin:syncer_syncermetricssnapshot_changelist") + "?cols=webhook"
        list_display = self._list_display(url)
        self.assertIn("webhook_route_check", list_display)
        self.assertIn("webhook_sha_first_tasks_enqueued", list_display)
        self.assertIn("sha_task_impacted_pr_fanout_total", list_display)
        self.assertNotIn("pr_failures", list_display)
        self.assertNotIn("rows_check_run", list_display)

    def test_group_links_drop_invalid_lookup_param(self) -> None:
        url = reverse("admin:syncer_syncermetricssnapshot_changelist") + "?e=1&cols=overview"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        links = dict(response.context["metrics_column_group_links"])
        self.assertIn("tasks", links)
        self.assertIn("cols=tasks", links["tasks"])
        self.assertNotIn("e=1", links["tasks"])
