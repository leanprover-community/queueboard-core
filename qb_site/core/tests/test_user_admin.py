from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import User


class UserAdminZulipImportTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client = Client()
        self.client.force_login(self.admin_user)

    def _upload(self, payload: list[dict[str, object]], **extra_fields: str) -> None:
        url = reverse("admin:core_user_import_zulip_users")
        file_obj = SimpleUploadedFile(
            "reviewer_zulip_ids.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        data = {"file": file_obj, **extra_fields}
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)

    def test_changelist_shows_import_button(self) -> None:
        url = reverse("admin:core_user_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import reviewer_zulip_ids.json")
        self.assertContains(response, reverse("admin:core_user_import_zulip_users"))

    def test_import_updates_existing_user_case_insensitively(self) -> None:
        user = User.objects.create(github_login="Alice")

        self._upload(
            [
                {
                    "github_login": "alice",
                    "zulip_full_name": "Alice A",
                    "zulip_user_id": 123,
                }
            ]
        )

        user.refresh_from_db()
        self.assertEqual(user.github_login, "alice")
        self.assertEqual(user.zulip_full_name, "Alice A")
        self.assertEqual(user.zulip_user_id, 123)

    def test_import_dry_run_does_not_persist_changes(self) -> None:
        user = User.objects.create(github_login="bob")

        self._upload(
            [
                {
                    "github_login": "bob",
                    "zulip_full_name": "Bob B",
                    "zulip_user_id": 456,
                }
            ],
            dry_run="on",
        )

        user.refresh_from_db()
        self.assertIsNone(user.zulip_full_name)
        self.assertIsNone(user.zulip_user_id)

    def test_import_can_create_missing_user_when_enabled(self) -> None:
        self._upload(
            [
                {
                    "github_login": "new-user",
                    "zulip_full_name": "New User",
                    "zulip_user_id": 999,
                }
            ],
            create_missing_users="on",
        )

        user = User.objects.get(github_login="new-user")
        self.assertEqual(user.zulip_full_name, "New User")
        self.assertEqual(user.zulip_user_id, 999)
