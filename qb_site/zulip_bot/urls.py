from __future__ import annotations

from django.urls import path

from zulip_bot import views

urlpatterns = [
    path("webhook/", views.webhook, name="zulip-webhook"),
    path("register/<str:token>/", views.register_start, name="zulip-register-start"),
    path("register/<str:token>/github/", views.register_github_start, name="zulip-register-github-start"),
    path("register/github/callback/", views.register_github_callback, name="zulip-register-github-callback"),
    path("close-pr/<str:token>/", views.close_pr_form, name="zulip-close-pr-form"),
    path("label-pr/<str:token>/", views.label_pr_form, name="zulip-label-pr-form"),
]
