from __future__ import annotations

import json
from unittest.mock import patch

from django.urls import reverse


class WebhookTestMixin:
    def setUp(self) -> None:
        super().setUp()
        self._sender_classifier_patcher = patch("zulip_bot.views.SenderClassifier.is_bot_sender", return_value=False)
        self.mock_is_bot_sender = self._sender_classifier_patcher.start()

    def tearDown(self) -> None:
        self._sender_classifier_patcher.stop()
        super().tearDown()

    def _payload(
        self,
        *,
        content: str,
        token: str = "test-token",
        message_type: str = "stream",
        **message_overrides: object,
    ) -> dict:
        message: dict[str, object] = {
            "id": 999,
            "type": message_type,
            "content": content,
            "sender_id": 101,
            "sender_email": "reviewer@example.com",
            "sender_full_name": "Reviewer User",
        }
        if message_type == "stream":
            message["stream_id"] = 5
            message["subject"] = "topic"
        message.update(message_overrides)
        return {"token": token, "message": message}

    def _post_payload(self, payload: dict) -> dict:
        response = self.client.post(
            reverse("zulip-webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        data: dict | None = None
        if response.content:
            data = response.json()
        return {"status": response.status_code, "json": data}
