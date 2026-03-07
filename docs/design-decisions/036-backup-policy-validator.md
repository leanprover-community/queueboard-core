# Backup Policy Validator for Sanitized Public Backups

## Context
- The public-backup workflow and helper scripts use hand-maintained table lists.
- Django schema has evolved, creating drift risk between:
  - sanitization (`scripts/sanitize_backup.py`),
  - dataset export (`scripts/export_for_analysis.py`),
  - intended privacy policy in design docs.
- We want strict, immediate failure when table policy and schema diverge.
- We do not want a deprecation grace period for removed tables.

## Decision
- Introduce a single shared policy module: `scripts/backup_policy.py`.
- Centralize backup table classification into:
  - `BACKUP_TABLES`: all tables expected to be present in backup scope,
  - `TRUNCATE_TABLES`: tables removed from sanitized output,
  - `RETAIN_TABLES`: tables intentionally preserved in sanitized output,
  - `SCRUB_SQL_BY_TABLE`: per-table scrubbing statements,
  - `EXPORT_TABLE_QUERIES`: curated offline dataset exports.
- Add strict validator: `scripts/validate_backup_policy.py`.
- Validation is a hard gate with immediate failure for:
  - new discovered tables not in `BACKUP_TABLES`,
  - stale policy tables no longer discovered,
  - overlap between retain/truncate sets,
  - incomplete classification (`TRUNCATE_TABLES ∪ RETAIN_TABLES != BACKUP_TABLES`),
  - scrub/export tables not retained,
  - invalid export projections (including unknown columns for non-`SELECT *` queries).
- Treat `django_migrations` as a required non-model backup table in scope.

## Consequences
- Schema-policy drift is caught early instead of silently skipping or shipping unintended tables.
- Any model/table change that affects backup scope now requires explicit policy updates in the same change.
- Removing tables requires immediate policy cleanup; lingering references fail CI.
- Backup behavior becomes easier to reason about because sanitization/export logic is policy-driven.

## Operational Notes
- `scripts/sanitize_backup.py` and `scripts/export_for_analysis.py` import shared policy definitions.
- `scripts/repo_check_compose.sh` runs `scripts/validate_backup_policy.py` as a host-side validation step.
- Reviewer operational preference/attention tables remain excluded from public sanitized backups:
  - `core_reviewerpreference`
  - `analyzer_revieweroptout`
  - `analyzer_reviewerattentiondailyrun`
  - `analyzer_reviewerattentionnotificationrecord`
  - `analyzer_reviewerattentionautounassignrecord`
- `django_celery_results_chordcounter` is explicitly truncated alongside other celery result tables.
