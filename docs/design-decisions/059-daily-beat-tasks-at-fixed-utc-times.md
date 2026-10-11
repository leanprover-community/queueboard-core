# 059 — Daily beat tasks run at fixed UTC times

## Context
- Beat uses Celery's default file-based `PersistentScheduler` (doc 002), which keeps each entry's
  last-run time in its schedule file. A beat that starts without that file treats every entry as
  never run.
- An interval entry first fires one full interval after beat starts. Where the schedule file is
  not kept across restarts, every restart resets that clock, so an entry whose interval is longer
  than the time between restarts never fires. A daily task on a 24-hour interval may therefore
  never run where beat restarts more often than daily. Doc 028 already noted that clock-time
  schedules are more reliable than long intervals where the filesystem does not persist.
- A crontab entry fires at its clock time whenever beat is up at that time, however recently it
  started.
- Five entries ran on a 24-hour interval (`86400`): `expire_old_webhook_deliveries`,
  `expire_stale_ci_for_active_repos`, `reviewer_attention_daily` (unless its clock time was set),
  `site_analytics_aggregate_monthly` and `site_analytics_prune_pageviews`. No other default
  interval is 12 hours or longer; the next longest is 3600 s.
- `.env.example` sets `PERIOD_SECONDS=86400` (uncommented) for four of the five, so a deployment
  configured from it has the variable set without having chosen anything.

## Decision
- **One helper**, `daily_beat_schedule()` in `qb_site/qb_site/settings/base.py`, builds all five
  entries from `<PREFIX>_PERIOD_SECONDS`, `<PREFIX>_UTC_HOUR` and `<PREFIX>_UTC_MINUTE`:
  1. a set hour or minute means a crontab at that time, with the unset half taken from the
     default. This was already reviewer attention's contract ("these override PERIOD_SECONDS");
  2. otherwise `PERIOD_SECONDS <= 0` disables the entry, as before;
  3. otherwise `PERIOD_SECONDS == 86400` (the default) means a crontab at the default time;
  4. any other period keeps running on that interval, as before.
- **Why 86400, and not "set or unset", decides the mode.** A deployment configured from
  `.env.example` has `86400` set, so "an explicit period keeps interval mode" would leave exactly
  those deployments on the schedule that fails. `86400` says "daily", which a clock time still
  honours. Any other value is a cadence someone chose, and it is kept.
- **Default times** keep clear of each other and of the existing fixed-time jobs: salt rotation
  (00:00 on the 1st), reviewer-assignment refresh (00:30), apply/propose (00:45), the
  reviewer-attention cleanup (Sunday 03:00) and Celery's own `celery.backend_cleanup` (04:00,
  added by Celery when the result backend cannot expire results itself, e.g. the database
  backend). Minutes :35–:55 stay off the top of the hour.

  | entry | prefix | default (UTC) | why |
  | --- | --- | --- | --- |
  | `reviewer_attention_daily` | `ANALYZER_REVIEWER_ATTENTION_` | 01:35 | after the 00:45 propose run, so its proposals make the same day's DM |
  | `site_analytics_aggregate_monthly` | `SITE_ANALYTICS_MONTHLY_AGGREGATE_` | 01:50 | light: reads at most two months of pageviews |
  | `site_analytics_prune_pageviews` | `SITE_ANALYTICS_PRUNE_` | 01:55 | light: about a day's rows per run |
  | `expire_stale_ci_for_active_repos` | `SYNCER_CI_EXPIRY_` | 02:35 | the heaviest; an hour to itself |
  | `expire_old_webhook_deliveries` | `SYNCER_WEBHOOK_DELIVERY_CLEANUP_` | 03:35 | after the Sunday 03:00 cleanup; capped (below) so it ends well before 04:00 |

- **Reviewer-attention window.** The "newly assigned" window (doc 028) is 24 h in clock-time mode
  and the period in interval mode. A one-day period now counts as clock-time mode, so the window
  is still 86400 s but its reported source is `fixed_utc_clock`, not `period_seconds`.
- **Webhook delivery cleanup is batched and capped.** It used to be one
  `DELETE … WHERE received_at < cutoff`: one statement and one transaction for the whole backlog,
  with no statement timeout and no index on `received_at` alone. Where the task seldom ran, the
  table can hold far more than the 7-day window, and the first fixed-time run would have deleted
  all of it at once. It now deletes oldest-first in id-cursor batches of 5000, each its own
  statement (as `_expire_ci_rows_in_batches` does for CI rows), and stops after
  `SYNCER_WEBHOOK_DELIVERY_CLEANUP_MAX_ROWS` rows (default 1,000,000; `<= 0` removes the cap). The
  result reports `capped` when a run stopped at the cap; the next run carries on. Retention is
  unchanged.

## First-Run Cost of the Other Tasks
- **CI expiry** is already batched (5000-row id cursor) under a per-statement timeout
  (`SYNCER_CI_EXPIRY_STATEMENT_TIMEOUT_SECONDS`, 300 s). A backlog makes the run longer, not any
  one statement. Each batch enqueues `analyzer.process_pr` for PRs whose queue windows referenced
  a deleted row (doc 040), so a large first run can enqueue a burst of rebuilds. Unchanged.
- **Pageview prune** is one `DELETE` over the `occurred_at` index. Nothing reaches the 540-day
  retention until 540 days after the first pageview; from then on a run deletes about a day's
  rows. Unchanged.
- **Monthly aggregate** deletes nothing; it recomputes the current and previous month. A month
  that ended more than a month before its first run is not recomputed by the schedule;
  `site_analytics.aggregate_monthly_metrics` with a larger `months_back` fills it from raw rows
  while they are retained.
- **Reviewer attention** does nothing unless `ANALYZER_REVIEWER_ATTENTION_ENABLED` is on; when on,
  a run's work does not depend on how long ago the last one was.

## Consequences
- The five tasks run once a day at known times, whatever beat's restart pattern. A day's run is
  missed only when beat is down at that minute; the cleanups and aggregates catch up on their next
  run.
- An operator who set only one of `ANALYZER_REVIEWER_ATTENTION_UTC_HOUR` / `_UTC_MINUTE` now gets
  the default for the other half (hour 1 or minute 35) instead of 0.
- A non-daily period at least as long as the time between beat restarts still never fires. That
  is the operator's explicit choice; set a clock time instead.
- A new daily entry should be built with `daily_beat_schedule`.

## Operational Notes
- New, all optional: `<PREFIX>_UTC_HOUR` / `<PREFIX>_UTC_MINUTE` for the four prefixes above other
  than reviewer attention (which had them), and `SYNCER_WEBHOOK_DELIVERY_CLEANUP_MAX_ROWS`.
  `SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS` and `_CLEANUP_PERIOD_SECONDS` were missing from
  `.env.example` and are now listed. An hour outside 0–23 or a minute outside 0–59 fails at
  startup, as the existing `_UTC_` settings do.
- To move a task, set its hour and minute. To disable it, set `PERIOD_SECONDS=0` and leave the
  clock time unset.
- Clock times are in `CELERY_TIMEZONE`, which follows `DJANGO_TIME_ZONE` (default `UTC`); the
  `_UTC_` names assume the default, as the existing ones do.
- No schema changes.

## Alternatives
- **Keep beat's schedule file across restarts** (doc 002's named volume). Worth doing where it is
  possible, and the interval entries still benefit, but whether it is possible depends on where
  beat runs; the clock times work either way.
- **`django-celery-beat`'s database scheduler** keeps last-run times in Postgres. Doc 002 left it
  as the upgrade path; it brings a dependency, its own tables and admin-edited schedules, which is
  more than this needs.
- **An explicitly set period keeps interval mode.** Rejected: see "Why 86400" above.
