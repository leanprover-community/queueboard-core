"""URL configuration for the reviewer console (design doc 050)."""

from __future__ import annotations

from django.urls import path

from console import views

app_name = "console"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login, name="login"),
    path("oauth/callback/", views.oauth_callback, name="oauth-callback"),
    path("logout/", views.logout, name="logout"),
    path("proposals/<int:proposal_id>/accept/", views.accept, name="accept"),
    path("proposals/<int:proposal_id>/assign-anyway/", views.assign_anyway, name="assign-anyway"),
    path("proposals/<int:proposal_id>/decline/", views.decline, name="decline"),
    path("unassign/", views.unassign, name="unassign"),
]
