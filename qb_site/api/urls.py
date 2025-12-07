"""URL configuration for the API app."""

from django.urls import path
from api.views import index
from api.views.queueboard_snapshot import QueueboardSnapshotView

urlpatterns: list = [
    path("", index, name="index"),
    path("v1/queueboard/snapshot", QueueboardSnapshotView.as_view(), name="queueboard-snapshot"),
]
