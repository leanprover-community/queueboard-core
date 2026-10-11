"""Daily beat entries run at fixed UTC clock times by default (design doc 059)."""

from __future__ import annotations

import os
import runpy
from pathlib import Path
from unittest import mock

from celery.schedules import crontab
from django.test import SimpleTestCase

from qb_site.settings import base as base_settings
from qb_site.settings.base import daily_beat_schedule

# beat entry name -> (prefix of its PERIOD_SECONDS / UTC_HOUR / UTC_MINUTE env vars, default UTC time)
_DAILY_ENTRIES = {
    "reviewer_attention_daily": ("ANALYZER_REVIEWER_ATTENTION_", 1, 35),
    "site_analytics_aggregate_monthly": ("SITE_ANALYTICS_MONTHLY_AGGREGATE_", 1, 50),
    "site_analytics_prune_pageviews": ("SITE_ANALYTICS_PRUNE_", 1, 55),
    "expire_stale_ci_for_active_repos": ("SYNCER_CI_EXPIRY_", 2, 35),
    "expire_old_webhook_deliveries": ("SYNCER_WEBHOOK_DELIVERY_CLEANUP_", 3, 35),
}
# Stripped before each load so the tests see the code defaults, not the .env the suite runs under.
_DAILY_ENV_PREFIXES = tuple(prefix for prefix, _, _ in _DAILY_ENTRIES.values())


def _load_settings(**env: str) -> dict:
    """Evaluate settings/base.py afresh under the given env (on top of the current one)."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith(_DAILY_ENV_PREFIXES)}
    environ.update(env)
    with mock.patch.dict(os.environ, environ, clear=True):
        return runpy.run_path(str(Path(base_settings.__file__)))


def _beat_schedule(**env: str) -> dict:
    return _load_settings(**env)["CELERY_BEAT_SCHEDULE"]


class DailyBeatScheduleHelperTests(SimpleTestCase):
    def _schedule(self, period_seconds: int, utc_hour: int | None = None, utc_minute: int | None = None):
        return daily_beat_schedule(period_seconds, utc_hour, utc_minute, default_utc_hour=2, default_utc_minute=35)

    def test_one_day_period_runs_at_default_time(self) -> None:
        self.assertEqual(self._schedule(86400), crontab(hour=2, minute=35))

    def test_other_period_keeps_interval(self) -> None:
        self.assertEqual(self._schedule(43200), 43200)
        self.assertEqual(self._schedule(3600), 3600)

    def test_non_positive_period_disables(self) -> None:
        self.assertIsNone(self._schedule(0))
        self.assertIsNone(self._schedule(-1))

    def test_clock_time_wins_over_any_period(self) -> None:
        for period in (86400, 43200, 0):
            with self.subTest(period=period):
                self.assertEqual(self._schedule(period, 5, 40), crontab(hour=5, minute=40))

    def test_unset_half_of_clock_time_comes_from_default(self) -> None:
        self.assertEqual(self._schedule(86400, utc_hour=5), crontab(hour=5, minute=35))
        self.assertEqual(self._schedule(86400, utc_minute=0), crontab(hour=2, minute=0))
        self.assertEqual(self._schedule(86400, utc_hour=0, utc_minute=0), crontab(hour=0, minute=0))


class DailyBeatEntriesTests(SimpleTestCase):
    def test_defaults_are_staggered_crontabs(self) -> None:
        schedule = _beat_schedule()
        for name, (_, hour, minute) in _DAILY_ENTRIES.items():
            with self.subTest(entry=name):
                self.assertEqual(schedule[name]["schedule"], crontab(hour=hour, minute=minute))
        times = [(hour, minute) for _, hour, minute in _DAILY_ENTRIES.values()]
        self.assertEqual(len(set(times)), len(times))

    def test_explicit_one_day_period_still_runs_at_default_time(self) -> None:
        # .env.example ships PERIOD_SECONDS=86400 uncommented, so "set" must not mean "interval".
        env = {f"{prefix}PERIOD_SECONDS": "86400" for prefix, _, _ in _DAILY_ENTRIES.values()}
        schedule = _beat_schedule(**env)
        for name, (_, hour, minute) in _DAILY_ENTRIES.items():
            with self.subTest(entry=name):
                self.assertEqual(schedule[name]["schedule"], crontab(hour=hour, minute=minute))

    def test_env_clock_time_overrides_default(self) -> None:
        for name, (prefix, _, default_minute) in _DAILY_ENTRIES.items():
            with self.subTest(entry=name):
                schedule = _beat_schedule(**{f"{prefix}UTC_HOUR": "5", f"{prefix}UTC_MINUTE": "40"})
                self.assertEqual(schedule[name]["schedule"], crontab(hour=5, minute=40))
                schedule = _beat_schedule(**{f"{prefix}UTC_HOUR": "0"})
                self.assertEqual(schedule[name]["schedule"], crontab(hour=0, minute=default_minute))

    def test_other_explicit_period_keeps_interval(self) -> None:
        for name, (prefix, _, _) in _DAILY_ENTRIES.items():
            with self.subTest(entry=name):
                schedule = _beat_schedule(**{f"{prefix}PERIOD_SECONDS": "43200"})
                self.assertEqual(schedule[name]["schedule"], 43200)

    def test_zero_period_disables_entry(self) -> None:
        for name, (prefix, _, _) in _DAILY_ENTRIES.items():
            with self.subTest(entry=name):
                self.assertNotIn(name, _beat_schedule(**{f"{prefix}PERIOD_SECONDS": "0"}))

    def test_clock_time_with_zero_period_still_schedules(self) -> None:
        # The reviewer-attention contract predates doc 059: a set clock time overrides PERIOD_SECONDS.
        schedule = _beat_schedule(
            ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS="0",
            ANALYZER_REVIEWER_ATTENTION_UTC_HOUR="16",
            ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE="30",
        )
        self.assertEqual(schedule["reviewer_attention_daily"]["schedule"], crontab(hour=16, minute=30))

    def test_invalid_clock_time_raises(self) -> None:
        cases = {"UTC_HOUR": ("24", "-1", "noon"), "UTC_MINUTE": ("60", "-1", "half")}
        for prefix, _, _ in _DAILY_ENTRIES.values():
            for suffix, values in cases.items():
                for value in values:
                    with self.subTest(var=f"{prefix}{suffix}", value=value):
                        with self.assertRaisesRegex(RuntimeError, f"{prefix}{suffix}"):
                            _load_settings(**{f"{prefix}{suffix}": value})

    def test_task_kwargs_unchanged(self) -> None:
        schedule = _beat_schedule(SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS="9", SYNCER_CI_STALE_PENDING_DAYS="31")
        self.assertEqual(schedule["expire_old_webhook_deliveries"]["kwargs"], {"retention_days": 9})
        self.assertEqual(schedule["expire_stale_ci_for_active_repos"]["kwargs"], {"stale_pending_days": 31})
