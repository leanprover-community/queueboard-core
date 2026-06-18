from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from core.utils.zulip_time import format_global_time


class FormatGlobalTimeTests(SimpleTestCase):
    def test_utc_datetime_renders_iso_8601(self) -> None:
        dt = datetime(2025, 12, 2, 20, 40, 0, tzinfo=timezone.utc)
        self.assertEqual(format_global_time(dt), "<time:2025-12-02T20:40:00+00:00>")

    def test_naive_datetime_assumed_utc(self) -> None:
        dt = datetime(2025, 12, 2, 20, 40, 0)
        self.assertEqual(format_global_time(dt), "<time:2025-12-02T20:40:00+00:00>")

    def test_non_utc_datetime_normalized_to_utc(self) -> None:
        from datetime import timedelta

        eastern = timezone(timedelta(hours=-5))
        dt = datetime(2025, 12, 2, 15, 40, 0, tzinfo=eastern)
        self.assertEqual(format_global_time(dt), "<time:2025-12-02T20:40:00+00:00>")

    def test_unix_timestamp_int(self) -> None:
        ts = int(datetime(2025, 12, 2, 20, 40, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(format_global_time(ts), "<time:2025-12-02T20:40:00+00:00>")

    def test_sub_second_precision_truncated(self) -> None:
        dt = datetime(2025, 12, 2, 20, 40, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(format_global_time(dt), "<time:2025-12-02T20:40:00+00:00>")

    def test_output_parses_with_fromisoformat(self) -> None:
        # Mirrors how Zulip's markdown parser resolves <time:...> values since
        # Zulip 12.0 (feature level 451): datetime.fromisoformat on the inner
        # string. A bare Unix timestamp would raise ValueError here.
        dt = datetime(2025, 12, 2, 20, 40, 0, tzinfo=timezone.utc)
        rendered = format_global_time(dt)
        inner = rendered.removeprefix("<time:").removesuffix(">")
        self.assertEqual(datetime.fromisoformat(inner), dt)
