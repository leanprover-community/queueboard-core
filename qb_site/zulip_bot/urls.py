from __future__ import annotations

from django.urls import path

from zulip_bot import views

urlpatterns = [
    path("webhook/", views.webhook, name="zulip-webhook"),
]
