from __future__ import annotations

from django.urls import path

from syncer import views

urlpatterns = [
    path("webhooks/github/", views.github_webhook, name="github-webhook"),
]
