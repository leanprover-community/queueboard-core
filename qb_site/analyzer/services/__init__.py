"""Service layer for analytics computations."""

from .ci_backfill import PlanItem, enqueue_ci_by_shas, plan_missing_ci_shas
from .dependency_graph import DependencyGraphBuilder
from .dependencies import DependencyRebuildResult, body_hash, parse_dependency_numbers, rebuild_pr_dependencies
from .reviewer_assignment import (
    AssignmentStatistics,
    AreaStatsBuilder,
    PRAssignmentPriority,
    ReviewerAssignmentBuilder,
    ReviewerProfile,
    ReviewerSuggestionResult,
    build_reviewer_catalog,
    collect_assignment_statistics,
    compute_area_stats,
    rank_prs_for_assignment,
    suggest_reviewer_for_pr,
    suggest_reviewers_many,
)
from .reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport, build_reviewer_attention_reports
from .queue_windows import (
    QueueSummary,
    QueueWindow,
    is_on_queue_at,
    queue_windows_for_pr,
    rebuild_queue_windows_for_pr,
    total_queue_time_for_pr,
    who_was_on_queue_at,
)
from .queue_window_build_state import (
    QueueWindowBuildStateBackfillResult,
    backfill_queue_window_build_states_for_repo,
    record_queue_window_build_states,
)

__all__ = [
    "PlanItem",
    "enqueue_ci_by_shas",
    "plan_missing_ci_shas",
    "DependencyGraphBuilder",
    "DependencyRebuildResult",
    "body_hash",
    "parse_dependency_numbers",
    "rebuild_pr_dependencies",
    "AreaStatsBuilder",
    "ReviewerAssignmentBuilder",
    "ReviewerProfile",
    "AssignmentStatistics",
    "PRAssignmentPriority",
    "ReviewerSuggestionResult",
    "build_reviewer_catalog",
    "collect_assignment_statistics",
    "compute_area_stats",
    "rank_prs_for_assignment",
    "suggest_reviewer_for_pr",
    "suggest_reviewers_many",
    "ReviewerAttentionItem",
    "ReviewerAttentionReport",
    "build_reviewer_attention_reports",
    "QueueSummary",
    "QueueWindow",
    "queue_windows_for_pr",
    "rebuild_queue_windows_for_pr",
    "QueueWindowBuildStateBackfillResult",
    "backfill_queue_window_build_states_for_repo",
    "record_queue_window_build_states",
    "total_queue_time_for_pr",
    "is_on_queue_at",
    "who_was_on_queue_at",
]
