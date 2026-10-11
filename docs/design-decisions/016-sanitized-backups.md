# Sanitized backups for public artifacts

## Context
- We want a GitHub Action that downloads a Heroku PGBackups dump, sanitizes it, and publishes artifacts for analysis.
- Public data (cloneable repo content or GitHub API responses for the repo) can remain; private/operational tables must be removed.
- Django admin/session/task history and reviewer preferences can contain non-public or stale information and should not ship.
- Cached snapshots are likely stale and may embed reviewer info; they are not needed for downstream analysis.

## Decision
- Treat GitHub-derived data (PRs, labels, timeline, CI, dependencies) as public and keep it intact.
- Truncate/drop operational/secret-bearing tables:
  - Django auth/admin/session metadata: `auth_*`, `django_admin_log`, `django_session`.
  - Celery results/linkage: `django_celery_results_taskresult`, `django_celery_results_groupresult`, `core_taskresultlink`.
  - Reviewer preferences: `core_reviewerpreference`.
  - Cached snapshots/metrics: `analyzer_queuesnapshot`, `analyzer_reviewerassignmentsnapshot`, `analyzer_areastatssnapshot`, `analyzer_analyzerconvergencesnapshot`, `syncer_syncermetricssnapshot`, `syncer_syncerconvergencesnapshot`.
  - Django metadata not required by kept tables: `django_content_type`, `django_migrations`, and any other `django_*` tables without incoming FKs from core data. Verify dependencies before truncation; prefer `TRUNCATE ... CASCADE`.
- Scrub private fields on retained tables:
  - `core_user`: null `zulip_user_id`, `zulip_full_name`, `timezone`; keep GitHub fields (`github_login`, `github_node_id`, `name`, `avatar_url`).
- Preserve everything else (core repositories, syncer raw data, analyzer derived non-snapshot tables, Django content needed for app startup if dependencies exist).

## Consequences
- Published dumps remain useful for analytics parity while excluding sensitive/admin data.
- Snapshot tables will need to be rebuilt downstream if required; they are intentionally omitted to avoid stale or private content.
- Removing Django metadata means migration history/content types may need to be regenerated if the dump is later used with the Django app; acceptable for analysis-focused artifacts.

## Operational Notes
- Apply sanitization after restoring the dump into a temp Postgres instance.
- Use `TRUNCATE ... RESTART IDENTITY CASCADE` to clear tables while keeping schema intact; ensure no remaining FK dependencies on dropped metadata tables.
- Emit a manifest/log of truncate/scrub actions and row counts before/after for auditability.
- Export a sanitized pg dump plus optional CSV/Parquet datasets derived from the cleaned database.
- Where the source dump comes from is the repository variable `BACKUP_SOURCE`, read by
  `upload_backup.yaml`. Unset (or `heroku`) means Heroku PGBackups, as originally designed. `s3` means
  the newest dump in an S3-compatible bucket, keyed `<prefix>-YYYYMMDDTHHMMSSZ.dump`, whose
  `pg_restore --list` shows TABLE DATA entries: a dump of a database with no tables yet is skipped
  (`scripts/download_s3_dump.py`).
  - The bucket's connection comes from the secrets `BACKUP_S3_ENDPOINT`, `BACKUP_S3_BUCKET`,
    `BACKUP_S3_ACCESS_KEY_ID` and `BACKUP_S3_SECRET_ACCESS_KEY`, with read-only credentials. Optional
    variables: `BACKUP_S3_KEY_PREFIX` (default `queueboard`) and `BACKUP_S3_REGION` (default
    `us-east-1`).
  - A manual run takes `backup_id` for the Heroku source and `dump_key` for the S3 one, and refuses
    the other source's input.
  - Switching sources means setting the secrets, then the variable; unsetting the variable switches
    back. `scripts/test_download_s3_dump.sh` (CI job `backup-download`) tests the S3 path against a
    local S3 server.
- The workflow restores with Postgres 18 (service and client). pg_restore reads its own major version
  and older ones, so it takes both Postgres 17 and 18 dumps. The sanitized dump it publishes now comes
  from pg_dump 18 and needs pg_restore 18 or newer.
