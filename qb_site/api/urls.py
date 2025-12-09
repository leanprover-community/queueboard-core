"""URL configuration for the API app."""

from django.urls import path
from api.views import index
from api.views.queueboard_dependency_graph import QueueboardDependencyGraphView
from api.views.queueboard_snapshot import QueueboardSnapshotView

urlpatterns: list = [
    path("", index, name="index"),
    path("v1/queueboard/snapshot", QueueboardSnapshotView.as_view(), name="queueboard-snapshot"),
    path(
        "v1/queueboard/dependency-graph",
        QueueboardDependencyGraphView.as_view(),
        name="queueboard-dependency-graph",
    ),
]
