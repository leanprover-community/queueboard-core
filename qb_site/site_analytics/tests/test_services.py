"""Unit tests for site_analytics hashing and bot-filter services."""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from site_analytics.services.bot_filter import is_bot
from site_analytics.services.hashing import compute_visitor_month_hash, get_client_ip


class GetClientIpTests(TestCase):
    def _make_request(self, remote_addr: str = "", xff: str = "") -> MagicMock:
        req = MagicMock()
        meta: dict[str, str] = {}
        if remote_addr:
            meta["REMOTE_ADDR"] = remote_addr
        if xff:
            meta["HTTP_X_FORWARDED_FOR"] = xff
        req.META = meta
        return req

    def test_remote_addr_used_when_no_xff(self):
        req = self._make_request(remote_addr="1.2.3.4")
        self.assertEqual(get_client_ip(req), "1.2.3.4")

    def test_xff_takes_precedence_over_remote_addr(self):
        req = self._make_request(remote_addr="10.0.0.1", xff="5.6.7.8, 10.0.0.1")
        self.assertEqual(get_client_ip(req), "5.6.7.8")

    def test_xff_single_address(self):
        req = self._make_request(remote_addr="10.0.0.1", xff="9.9.9.9")
        self.assertEqual(get_client_ip(req), "9.9.9.9")

    def test_xff_strips_whitespace(self):
        req = self._make_request(xff="  203.0.113.5 , 10.0.0.1")
        self.assertEqual(get_client_ip(req), "203.0.113.5")

    def test_empty_xff_falls_back_to_remote_addr(self):
        req = self._make_request(remote_addr="1.2.3.4", xff="")
        self.assertEqual(get_client_ip(req), "1.2.3.4")

    def test_missing_both_returns_empty_string(self):
        req = self._make_request()
        self.assertEqual(get_client_ip(req), "")


@override_settings(SITE_ANALYTICS_HASH_SALT="test-salt")
class ComputeVisitorMonthHashTests(TestCase):
    def test_returns_64_char_hex(self):
        result = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        self.assertEqual(len(result), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_deterministic(self):
        a = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        b = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        self.assertEqual(a, b)

    def test_different_month_keys_produce_different_hashes(self):
        a = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        b = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-04")
        self.assertNotEqual(a, b)

    def test_different_ips_produce_different_hashes(self):
        a = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        b = compute_visitor_month_hash("1.2.3.5", "Mozilla/5.0", "2026-03")
        self.assertNotEqual(a, b)

    def test_ua_normalized_case_insensitive(self):
        a = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        b = compute_visitor_month_hash("1.2.3.4", "MOZILLA/5.0", "2026-03")
        self.assertEqual(a, b)

    @override_settings(SITE_ANALYTICS_HASH_SALT="other-salt")
    def test_different_salts_produce_different_hashes(self):
        a = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        with self.settings(SITE_ANALYTICS_HASH_SALT="test-salt"):
            b = compute_visitor_month_hash("1.2.3.4", "Mozilla/5.0", "2026-03")
        self.assertNotEqual(a, b)


class IsBotTests(TestCase):
    def test_known_bot_substrings(self):
        bot_uas = [
            "Googlebot/2.1",
            "Mozilla/5.0 (compatible; bingbot/2.0)",
            "curl/7.68.0",
            "wget/1.20",
            "python-requests/2.28.0",
            "Go-http-client/1.1",
            "Java/11.0",
            "okhttp/4.9.0",
            "axios/1.3.0",
        ]
        for ua in bot_uas:
            with self.subTest(ua=ua):
                self.assertTrue(is_bot(ua), f"Expected {ua!r} to be detected as bot")

    def test_legitimate_browser_uas(self):
        browser_uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
        ]
        for ua in browser_uas:
            with self.subTest(ua=ua):
                self.assertFalse(is_bot(ua), f"Expected {ua!r} not to be detected as bot")

    def test_empty_ua_is_not_bot(self):
        self.assertFalse(is_bot(""))
