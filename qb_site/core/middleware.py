"""Redirect GET and HEAD requests for the site's former host names to its canonical one.

When the site moves to a new name while an old one still reaches it, the old name's links and
bookmarks should land on the new one. A GET or HEAD request whose Host is listed in
``QUEUEBOARD_LEGACY_HOSTS`` gets a redirect (302, or 301 with
``QUEUEBOARD_LEGACY_HOST_REDIRECT_PERMANENT``) to the same path and query on
``QUEUEBOARD_BASE_URL``'s scheme and host. Every other method is served as before: webhook senders
don't follow redirects, and a redirected POST would lose its body.

The target comes only from ``QUEUEBOARD_BASE_URL``, never from the request, so this cannot be an
open redirect. The middleware removes itself (``MiddlewareNotUsed``) when no legacy host is set, or
when the base URL is unset or names a legacy host, which would redirect to itself.

It comes first in ``MIDDLEWARE``, so a redirected request reaches nothing that could redirect it
elsewhere, set a cookie, or touch the database.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.http.request import split_domain_port

from core.services.site_urls import resolve_site_base_url

log = logging.getLogger(__name__)

REDIRECTED_METHODS = frozenset({"GET", "HEAD"})


def _host_name(host: str) -> str:
    """The lowercased host name of a ``Host`` value, without its port or a trailing dot; ``""`` if invalid."""
    domain, _port = split_domain_port(host.strip())
    return domain


def _redirect_origin(base_url: str, legacy_hosts: frozenset[str]) -> str | None:
    """``scheme://host[:port]`` of the base URL, or None if it is unusable or names a legacy host."""
    parts = urlsplit(base_url)
    host = _host_name(parts.netloc)
    if parts.scheme not in ("http", "https") or not host or host in legacy_hosts:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _legacy_host_names(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(name for name in (_host_name(host) for host in hosts) if name)


class LegacyHostRedirectMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        legacy_hosts = _legacy_host_names(settings.QUEUEBOARD_LEGACY_HOSTS)
        if not legacy_hosts:
            raise MiddlewareNotUsed
        origin = _redirect_origin(resolve_site_base_url(), legacy_hosts)
        if origin is None:
            log.warning(
                "QUEUEBOARD_LEGACY_HOSTS is set, but QUEUEBOARD_BASE_URL is unset, not an http(s) URL, "
                "or names one of those hosts: not redirecting"
            )
            raise MiddlewareNotUsed
        self.get_response = get_response
        self.legacy_hosts = legacy_hosts
        self.origin = origin
        self.redirect_class = (
            HttpResponsePermanentRedirect if settings.QUEUEBOARD_LEGACY_HOST_REDIRECT_PERMANENT else HttpResponseRedirect
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The raw Host header, not request.get_host(): the host only decides whether to redirect, and
        # get_host() would reject one missing from ALLOWED_HOSTS before that.
        if request.method in REDIRECTED_METHODS and _host_name(request.META.get("HTTP_HOST", "")) in self.legacy_hosts:
            return self.redirect_class(self.origin + request.get_full_path())
        return self.get_response(request)
