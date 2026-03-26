"""Tests for daily metric aggregation service and task."""

from __future__ import annotations

import datetime
from unittest import mock

from django.test import TestCase, override_settings

from site_analytics.models import AnalyticsDailyMetric, AnalyticsPageView
from site_analytics.services.aggregation import aggregate_daily_metrics
from site_analytics.tasks.aggregate_daily import aggregate_daily_metrics_task

_SALT = override_settings(SITE_ANALYTICS_HASH_SALT="test-salt")

TODAY = datetime.date(2026, 3, 25)
YESTERDAY = TODAY - datetime.timedelta(days=1)


def _pv(site: str, path: str, date: datetime.date, visitor_hash: str = "abc") -> AnalyticsPageView:
    return AnalyticsPageView.objects.create(
        site=site,
        path=path,
        occurred_at=datetime.datetime(date.year, date.month, date.day, 12, 0, 0, tzinfo=datetime.timezone.utc),
        visitor_month_hash=visitor_hash,
    )


@_SALT
class AggregateDailyMetricsServiceTests(TestCase):
    def test_basic_count(self):
        _pv("s1", "/a", TODAY, "h1")
        _pv("s1", "/b", TODAY, "h2")
        _pv("s1", "/c", TODAY, "h1")  # same hash — same visitor

        result = aggregate_daily_metrics(date=TODAY, days_back=1)

        metric = AnalyticsDailyMetric.objects.get(site="s1", date=TODAY)
        self.assertEqual(metric.pageviews, 3)
        self.assertEqual(metric.unique_visitors, 2)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["dates_processed"], [str(TODAY)])

    def test_idempotent_rerun(self):
        _pv("s1", "/a", TODAY, "h1")
        aggregate_daily_metrics(date=TODAY, days_back=1)
        aggregate_daily_metrics(date=TODAY, days_back=1)

        self.assertEqual(AnalyticsDailyMetric.objects.filter(site="s1", date=TODAY).count(), 1)
        self.assertEqual(AnalyticsDailyMetric.objects.get(site="s1", date=TODAY).pageviews, 1)

    def test_rerun_after_new_events_updates_counts(self):
        _pv("s1", "/a", TODAY, "h1")
        aggregate_daily_metrics(date=TODAY, days_back=1)
        _pv("s1", "/b", TODAY, "h2")
        aggregate_daily_metrics(date=TODAY, days_back=1)

        metric = AnalyticsDailyMetric.objects.get(site="s1", date=TODAY)
        self.assertEqual(metric.pageviews, 2)
        self.assertEqual(metric.unique_visitors, 2)

    def test_days_back_covers_yesterday(self):
        _pv("s1", "/a", TODAY, "h1")
        _pv("s1", "/b", YESTERDAY, "h2")

        aggregate_daily_metrics(date=TODAY, days_back=2)

        self.assertEqual(AnalyticsDailyMetric.objects.get(site="s1", date=TODAY).pageviews, 1)
        self.assertEqual(AnalyticsDailyMetric.objects.get(site="s1", date=YESTERDAY).pageviews, 1)

    def test_multiple_sites_aggregated_separately(self):
        _pv("site-a", "/", TODAY, "h1")
        _pv("site-a", "/", TODAY, "h2")
        _pv("site-b", "/", TODAY, "h3")

        aggregate_daily_metrics(date=TODAY, days_back=1)

        self.assertEqual(AnalyticsDailyMetric.objects.get(site="site-a", date=TODAY).pageviews, 2)
        self.assertEqual(AnalyticsDailyMetric.objects.get(site="site-b", date=TODAY).pageviews, 1)

    def test_no_raw_rows_does_not_create_metric(self):
        aggregate_daily_metrics(date=TODAY, days_back=1)
        self.assertEqual(AnalyticsDailyMetric.objects.count(), 0)

    def test_no_raw_rows_preserves_existing_metric(self):
        # Existing aggregate from a previous run; no raw rows remain (pruned).
        AnalyticsDailyMetric.objects.create(site="s1", date=TODAY, pageviews=5, unique_visitors=3)
        aggregate_daily_metrics(date=TODAY, days_back=1)

        metric = AnalyticsDailyMetric.objects.get(site="s1", date=TODAY)
        self.assertEqual(metric.pageviews, 5)  # preserved, not zeroed

    def test_unique_visitors_uses_distinct_hash(self):
        for _ in range(10):
            _pv("s1", "/", TODAY, "same-hash")

        aggregate_daily_metrics(date=TODAY, days_back=1)

        metric = AnalyticsDailyMetric.objects.get(site="s1", date=TODAY)
        self.assertEqual(metric.pageviews, 10)
        self.assertEqual(metric.unique_visitors, 1)

    def test_date_boundary_is_utc(self):
        # Event at 23:59 UTC on YESTERDAY must count for YESTERDAY, not TODAY.
        AnalyticsPageView.objects.create(
            site="s1",
            path="/",
            occurred_at=datetime.datetime(
                YESTERDAY.year, YESTERDAY.month, YESTERDAY.day, 23, 59, 0, tzinfo=datetime.timezone.utc
            ),
            visitor_month_hash="h1",
        )

        aggregate_daily_metrics(date=TODAY, days_back=2)

        self.assertEqual(AnalyticsDailyMetric.objects.get(site="s1", date=YESTERDAY).pageviews, 1)
        self.assertFalse(AnalyticsDailyMetric.objects.filter(site="s1", date=TODAY).exists())


@_SALT
class AggregateDailyMetricsTaskTests(TestCase):
    @mock.patch("django.utils.timezone.now")
    def test_task_returns_summary_dict(self, mock_now):
        # Pin the clock so the task's default date=timezone.now().date() matches
        # the pageview date, avoiding midnight-boundary flakiness.
        mock_now.return_value = datetime.datetime(
            TODAY.year, TODAY.month, TODAY.day, 12, 0, 0, tzinfo=datetime.timezone.utc
        )
        _pv("s1", "/", TODAY, "h1")
        result = aggregate_daily_metrics_task(days_back=1)
        self.assertIn("upserted", result)
        self.assertIn("dates_processed", result)
        self.assertEqual(result["upserted"], 1)
