#!/usr/bin/env python3

"""
Unit tests for the analytics beacon wiring in `dashboard.py`.

These guard a regression that silently dropped every pageview: the beacon URL was built by
appending "/api/v1/analytics/collect" to a base URL that already ended in "/api" (producing a
404 on "/api/api/..."), and the CSP source expression pinned the exact path "/api" rather than
the subtree below it, so the browser blocked the request before it was even sent.
"""

from queueboard.dashboard import _analytics_connect_src, _analytics_endpoint, _make_analytics_snippet, _make_html_header

API_BASE = "https://queueboard-backend.example.com/api"
COLLECT_URL = "https://queueboard-backend.example.com/api/v1/analytics/collect"


def test_analytics_endpoint() -> None:
    # The base URL already carries the server's /api prefix, so it must not be repeated.
    assert _analytics_endpoint(API_BASE) == COLLECT_URL
    assert _analytics_endpoint(f"{API_BASE}/") == COLLECT_URL


def test_analytics_connect_src_covers_the_endpoint() -> None:
    source = _analytics_connect_src(API_BASE)
    # CSP matches a path without a trailing slash exactly, which would block the POST.
    assert source.endswith("/")
    assert _analytics_endpoint(API_BASE).startswith(source)


def test_header_csp_allows_the_endpoint() -> None:
    header = _make_html_header(_analytics_connect_src(API_BASE))
    assert f"connect-src 'self' {API_BASE}/;" in header
    # Without analytics configured, the directive is omitted entirely.
    assert "connect-src" not in _make_html_header()


def test_snippet_embeds_endpoint_and_site() -> None:
    snippet = _make_analytics_snippet(API_BASE, "queueboard")
    assert f'var endpoint = "{COLLECT_URL}"' in snippet
    assert "/api/api/" not in snippet
    assert 'site: "queueboard"' in snippet


def test_snippet_stays_a_cors_simple_request() -> None:
    # application/json is not CORS-safelisted, so it would force a preflight. text/plain is.
    snippet = _make_analytics_snippet(API_BASE, "queueboard")
    assert "application/json" not in snippet
    assert "'Content-Type': 'text/plain'" in snippet


def test_snippet_does_not_use_sendbeacon() -> None:
    # Beacons are discarded by blockers and privacy settings that filter on request type,
    # silently: sendBeacon returns true once the request is queued, not once it is sent.
    # fetch(keepalive) survives page unload just as well and is actually delivered.
    snippet = _make_analytics_snippet(API_BASE, "queueboard")
    assert "sendBeacon" not in snippet
    assert "keepalive: true" in snippet


if __name__ == "__main__":
    test_analytics_endpoint()
    test_analytics_connect_src_covers_the_endpoint()
    test_header_csp_allows_the_endpoint()
    test_snippet_embeds_endpoint_and_site()
    test_snippet_stays_a_cors_simple_request()
    test_snippet_does_not_use_sendbeacon()
