"""Tests for monthly metric aggregation service and prune service/task."""

from __future__ import annotations

import datetime

from django.test import TestCase, override_settings

from site_analytics.models import AnalyticsMonthlyMetric, AnalyticsPageView
from site_analytics.services.aggregation import aggregate_monthly_metrics, prune_old_pageviews
from site_analytics.tasks.aggregate_monthly import aggregate_monthly_metrics_task, prune_old_pageviews_task

_SALT = override_settings(SITE_ANALYTICS_HASH_SALT="test-salt")

# Fixed reference dates
MAR_2026 = datetime.date(2026, 3, 25)
MAR_START = datetime.date(2026, 3, 1)
FEB_START = datetime.date(2026, 2, 1)
JAN_START = datetime.date(2026, 1, 1)


def _pv(
    site: str,
    path: str,
    date: datetime.date,
    visitor_hash: str = "h1",
) -> AnalyticsPageView:
    return AnalyticsPageView.objects.create(
        site=site,
        path=path,
        occurred_at=datetime.datetime(date.year, date.month, date.day, 12, 0, 0, tzinfo=datetime.timezone.utc),
        visitor_month_hash=visitor_hash,
    )


@_SALT
class AggregateMonthlyMetricsServiceTests(TestCase):
    def test_basic_monthly_count(self):
        _pv("s1", "/a", MAR_2026, "h1")
        _pv("s1", "/b", MAR_2026, "h2")
        _pv("s1", "/c", MAR_2026, "h1")  # same visitor

        result = aggregate_monthly_metrics(date=MAR_2026, months_back=1)

        metric = AnalyticsMonthlyMetric.objects.get(site="s1", month=MAR_START)
        self.assertEqual(metric.pageviews, 3)
        self.assertEqual(metric.unique_visitors, 2)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["months_processed"], [str(MAR_START)])

    def test_idempotent_rerun(self):
        _pv("s1", "/a", MAR_2026, "h1")
        aggregate_monthly_metrics(date=MAR_2026, months_back=1)
        aggregate_monthly_metrics(date=MAR_2026, months_back=1)

        self.assertEqual(AnalyticsMonthlyMetric.objects.filter(site="s1", month=MAR_START).count(), 1)
        self.assertEqual(AnalyticsMonthlyMetric.objects.get(site="s1", month=MAR_START).pageviews, 1)

    def test_months_back_covers_previous_month(self):
        _pv("s1", "/a", MAR_2026, "h1")
        _pv("s1", "/b", datetime.date(2026, 2, 15), "h2")

        aggregate_monthly_metrics(date=MAR_2026, months_back=2)

        self.assertEqual(AnalyticsMonthlyMetric.objects.get(site="s1", month=MAR_START).pageviews, 1)
        self.assertEqual(AnalyticsMonthlyMetric.objects.get(site="s1", month=FEB_START).pageviews, 1)

    def test_month_stored_as_first_of_month(self):
        _pv("s1", "/", datetime.date(2026, 3, 31), "h1")
        aggregate_monthly_metrics(date=MAR_2026, months_back=1)
        self.assertTrue(AnalyticsMonthlyMetric.objects.filter(month=MAR_START).exists())

    def test_events_spanning_month_boundary_separated(self):
        _pv("s1", "/", datetime.date(2026, 2, 28), "h1")
        _pv("s1", "/", datetime.date(2026, 3, 1), "h2")

        aggregate_monthly_metrics(date=MAR_2026, months_back=2)

        self.assertEqual(AnalyticsMonthlyMetric.objects.get(site="s1", month=FEB_START).pageviews, 1)
        self.assertEqual(AnalyticsMonthlyMetric.objects.get(site="s1", month=MAR_START).pageviews, 1)

    def test_no_raw_rows_preserves_existing_metric(self):
        AnalyticsMonthlyMetric.objects.create(site="s1", month=MAR_START, pageviews=99, unique_visitors=50)
        aggregate_monthly_metrics(date=MAR_2026, months_back=1)

        metric = AnalyticsMonthlyMetric.objects.get(site="s1", month=MAR_START)
        self.assertEqual(metric.pageviews, 99)  # preserved, not zeroed

    def test_task_returns_summary_dict(self):
        _pv("s1", "/", MAR_2026, "h1")
        result = aggregate_monthly_metrics_task(months_back=1)
        self.assertIn("upserted", result)
        self.assertIn("months_processed", result)
        self.assertEqual(result["upserted"], 1)


@_SALT
class PruneOldPageviewsTests(TestCase):
    def test_prunes_rows_older_than_retention(self):
        old = _pv("s1", "/old", datetime.date(2023, 1, 1))
        recent = _pv("s1", "/new", datetime.date(2026, 3, 1))

        result = prune_old_pageviews(retention_days=30)

        self.assertFalse(AnalyticsPageView.objects.filter(pk=old.pk).exists())
        self.assertTrue(AnalyticsPageView.objects.filter(pk=recent.pk).exists())
        self.assertEqual(result["deleted"], 1)

    def test_no_rows_deleted_when_all_recent(self):
        _pv("s1", "/", datetime.date(2026, 3, 24))
        result = prune_old_pageviews(retention_days=30)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(AnalyticsPageView.objects.count(), 1)

    def test_returns_cutoff_and_retention_days(self):
        result = prune_old_pageviews(retention_days=90)
        self.assertIn("cutoff", result)
        self.assertEqual(result["retention_days"], 90)

    @override_settings(SITE_ANALYTICS_RETENTION_DAYS=7)
    def test_uses_settings_default_when_not_specified(self):
        _pv("s1", "/", datetime.date(2026, 3, 1))
        result = prune_old_pageviews()
        self.assertEqual(result["retention_days"], 7)
        self.assertEqual(result["deleted"], 1)

    def test_prune_task_returns_summary(self):
        result = prune_old_pageviews_task(retention_days=365)
        self.assertIn("deleted", result)
        self.assertIn("cutoff", result)
