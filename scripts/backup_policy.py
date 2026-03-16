"""Shared policy for sanitized backup retention, scrubbing, and dataset exports."""

from __future__ import annotations

# Tables expected to exist in backups and managed by this policy.
# We include implicit Django auth M2M tables and django_migrations explicitly.
BACKUP_TABLES: tuple[str, ...] = (
    "analyzer_analyzerconvergencesnapshot",
    "analyzer_areastatssnapshot",
    "analyzer_prdependency",
    "analyzer_prdependencystate",
    "analyzer_prqueuewindow",
    "analyzer_prqueuewindowbuildstate",
    "analyzer_prrevision",
    "analyzer_prrevisionbuildstate",
    "analyzer_queueruleset",
    "analyzer_queuesnapshot",
    "analyzer_reviewerassignmentsnapshot",
    "analyzer_reviewerattentionautounassignrecord",
    "analyzer_reviewerattentiondailyrun",
    "analyzer_reviewerattentionnotificationrecord",
    "analyzer_revieweroptout",
    "auth_group",
    "auth_group_permissions",
    "auth_permission",
    "auth_user",
    "auth_user_groups",
    "auth_user_user_permissions",
    "core_repository",
    "core_reviewerpreference",
    "core_taskresultlink",
    "core_user",
    "django_admin_log",
    "django_celery_results_chordcounter",
    "django_celery_results_groupresult",
    "django_celery_results_taskresult",
    "django_content_type",
    "django_migrations",
    "django_session",
    "syncer_cishafetchstate",
    "syncer_commitcheckrun",
    "syncer_commithistoryharvest",
    "syncer_commitstatuscontext",
    "syncer_githubwebhookdelivery",
    "syncer_labeldef",
    "syncer_prlabel",
    "syncer_prtimelineevent",
    "syncer_pullrequest",
    "syncer_repobackfillcursor",
    "syncer_repodiscoverystate",
    "syncer_syncerconvergencesnapshot",
    "syncer_syncermetricssnapshot",
)

# Tables to truncate during sanitization (RESTART IDENTITY CASCADE).
TRUNCATE_TABLES: tuple[str, ...] = (
    # Django auth/admin/session
    "auth_group",
    "auth_group_permissions",
    "auth_permission",
    "auth_user",
    "auth_user_groups",
    "auth_user_user_permissions",
    "django_admin_log",
    "django_session",
    "django_content_type",
    "django_migrations",
    # Celery task results
    "django_celery_results_taskresult",
    "django_celery_results_groupresult",
    "django_celery_results_chordcounter",
    "core_taskresultlink",
    # Reviewer preferences/config
    "core_reviewerpreference",
    # Reviewer attention/opt-out operational state
    "analyzer_revieweroptout",
    "analyzer_reviewerattentiondailyrun",
    "analyzer_reviewerattentionnotificationrecord",
    "analyzer_reviewerattentionautounassignrecord",
    # Operational snapshots/metrics caches
    "analyzer_queuesnapshot",
    "analyzer_reviewerassignmentsnapshot",
    "analyzer_areastatssnapshot",
    "analyzer_analyzerconvergencesnapshot",
    "syncer_syncermetricssnapshot",
    "syncer_syncerconvergencesnapshot",
    # Sync operational watermarks / webhook dedupe state
    "syncer_cishafetchstate",
    "syncer_repodiscoverystate",
    "syncer_githubwebhookdelivery",
)

# Tables retained in sanitized dump.
RETAIN_TABLES: tuple[str, ...] = (
    "core_repository",
    "core_user",
    "syncer_pullrequest",
    "syncer_labeldef",
    "syncer_prlabel",
    "syncer_prtimelineevent",
    "syncer_commitcheckrun",
    "syncer_commitstatuscontext",
    "syncer_repobackfillcursor",
    "syncer_commithistoryharvest",
    "analyzer_queueruleset",
    "analyzer_prqueuewindow",
    "analyzer_prqueuewindowbuildstate",
    "analyzer_prrevision",
    "analyzer_prrevisionbuildstate",
    "analyzer_prdependency",
    "analyzer_prdependencystate",
)

# Export queries for curated offline analysis datasets.
EXPORT_TABLE_QUERIES: dict[str, str] = {
    "core_repository": "SELECT * FROM core_repository ORDER BY id",
    "core_user": "SELECT * FROM core_user ORDER BY id",
    "syncer_pullrequest": "SELECT * FROM syncer_pullrequest ORDER BY id",
    "syncer_labeldef": "SELECT * FROM syncer_labeldef ORDER BY id",
    "syncer_prlabel": "SELECT * FROM syncer_prlabel ORDER BY id",
    "syncer_prtimelineevent": "SELECT * FROM syncer_prtimelineevent ORDER BY id",
    "syncer_commitcheckrun": "SELECT * FROM syncer_commitcheckrun ORDER BY id",
    "syncer_commitstatuscontext": "SELECT * FROM syncer_commitstatuscontext ORDER BY id",
    "syncer_repobackfillcursor": "SELECT * FROM syncer_repobackfillcursor ORDER BY id",
    "syncer_commithistoryharvest": "SELECT * FROM syncer_commithistoryharvest ORDER BY id",
    "analyzer_queueruleset": "SELECT * FROM analyzer_queueruleset ORDER BY id",
    "analyzer_prqueuewindow": "SELECT * FROM analyzer_prqueuewindow ORDER BY id",
    "analyzer_prqueuewindowbuildstate": "SELECT * FROM analyzer_prqueuewindowbuildstate ORDER BY id",
    "analyzer_prrevision": "SELECT * FROM analyzer_prrevision ORDER BY id",
    "analyzer_prrevisionbuildstate": "SELECT * FROM analyzer_prrevisionbuildstate ORDER BY id",
    "analyzer_prdependency": "SELECT * FROM analyzer_prdependency ORDER BY id",
    "analyzer_prdependencystate": "SELECT * FROM analyzer_prdependencystate ORDER BY id",
}

# In-place field scrub SQL snippets.
SCRUB_SQL_BY_TABLE: dict[str, str] = {
    "core_user": """
        UPDATE core_user
        SET
          zulip_user_id = NULL,
          zulip_full_name = NULL,
          timezone = NULL
    """,
}
