"""URL configuration for the API app."""

from django.urls import path
from api.views import index
from api.views.analytics_collect import AnalyticsCollectView
from api.views.queueboard_dependency_graph import QueueboardDependencyGraphView
from api.views.queueboard_snapshot import QueueboardSnapshotView
from api.views.reviewer_assignment import AreaStatsView, ReviewerAssignmentsView
from api.views.reviewer_interests import ReviewerInterestsView

urlpatterns: list = [
    path("", index, name="index"),
    path("v1/analytics/collect", AnalyticsCollectView.as_view(), name="analytics-collect"),
    path("v1/queueboard/snapshot", QueueboardSnapshotView.as_view(), name="queueboard-snapshot"),
    path(
        "v1/queueboard/dependency-graph",
        QueueboardDependencyGraphView.as_view(),
        name="queueboard-dependency-graph",
    ),
    path(
        "v1/queueboard/automatic-assignments",
        ReviewerAssignmentsView.as_view(),
        name="queueboard-automatic-assignments",
    ),
    path(
        "v1/queueboard/area-stats",
        AreaStatsView.as_view(),
        name="queueboard-area-stats",
    ),
    path(
        "v1/reviewer-interests",
        ReviewerInterestsView.as_view(),
        name="reviewer-interests",
    ),
]
