from __future__ import annotations

import socket
import threading
import time
from unittest import mock

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from redis.exceptions import ConnectionError as RedisConnectionError


class HealthViewTests(TestCase):
    def setUp(self) -> None:
        self.redis_client = mock.Mock()
        self.redis_client.ping.return_value = True
        patcher = mock.patch("api.views.health._get_redis_client", return_value=self.redis_client)
        self.get_redis_client = patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_checks_pass(self):
        resp = self.client.get("/api/health")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(resp.json(), {"status": "ok", "checks": {"database": "ok", "broker": "ok"}})
        self.get_redis_client.assert_called_once_with(socket_connect_timeout=2, socket_timeout=2)
        self.redis_client.ping.assert_called_once_with()
        self.redis_client.close.assert_called_once_with()

    def test_database_query_runs_under_a_statement_timeout(self):
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/api/health")

        sql = [q["sql"] for q in ctx.captured_queries]
        self.assertIn("SET LOCAL statement_timeout = 2000", sql)
        self.assertIn("SELECT 1", sql)
        self.assertLess(sql.index("SET LOCAL statement_timeout = 2000"), sql.index("SELECT 1"))

    def test_database_failure_returns_503_naming_it(self):
        failing = mock.Mock()
        failing.cursor.side_effect = OperationalError('connection to server at "db.internal.example" failed')

        with mock.patch("api.views.health.connection", failing), self.assertLogs("api.views.health", "WARNING") as logs:
            resp = self.client.get("/api/health")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(resp.json(), {"status": "error", "checks": {"database": "error", "broker": "ok"}})
        # The details are logged, never served.
        self.assertNotIn(b"db.internal.example", resp.content)
        self.assertIn("database check failed", logs.output[0])
        self.assertIn("db.internal.example", logs.output[0])

    def test_broker_failure_returns_503_naming_it(self):
        self.redis_client.ping.side_effect = RedisConnectionError("Error 111 connecting to redis.internal.example:6379.")

        with self.assertLogs("api.views.health", "WARNING") as logs:
            resp = self.client.get("/api/health")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"status": "error", "checks": {"database": "ok", "broker": "error"}})
        self.assertNotIn(b"redis.internal.example", resp.content)
        self.assertIn("broker check failed", logs.output[0])
        self.redis_client.close.assert_called_once_with()

    def test_broker_without_a_redis_url_is_a_failure(self):
        self.get_redis_client.return_value = None

        with self.assertLogs("api.views.health", "WARNING"):
            resp = self.client.get("/api/health")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["broker"], "error")

    def test_head_runs_the_checks(self):
        resp = self.client.head("/api/health")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")
        self.redis_client.ping.assert_called_once_with()

    def test_other_methods_are_refused(self):
        self.assertEqual(self.client.post("/api/health").status_code, 405)

    def test_sets_no_session_or_csrf_cookie(self):
        resp = self.client.get("/api/health")

        self.assertNotIn(settings.SESSION_COOKIE_NAME, resp.cookies)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, resp.cookies)
        # No Vary: Cookie either: the session was never read, let alone saved.
        self.assertNotIn("Cookie", resp.get("Vary", ""))
        self.assertFalse(Session.objects.exists())


class HealthBrokerTimeoutTests(TestCase):
    """The broker check against real sockets, not a mocked client."""

    def test_refused_connection_fails_the_check(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        with self.settings(CELERY_BROKER_URL=f"redis://127.0.0.1:{port}/0"), self.assertLogs("api.views.health", "WARNING"):
            resp = self.client.get("/api/health")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["broker"], "error")

    def test_silent_server_fails_the_check_within_the_timeout(self):
        # The kernel completes the handshake for a listening socket nobody accepts from, so the
        # client connects and then never hears back: a hung Redis.
        listener = socket.create_server(("127.0.0.1", 0), backlog=8)
        self.addCleanup(listener.close)
        # If the timeout ever stops reaching the client, this turns a hang into a slow failure.
        safety = threading.Timer(10, listener.close)
        safety.start()
        self.addCleanup(safety.cancel)
        port = listener.getsockname()[1]

        with (
            self.settings(CELERY_BROKER_URL=f"redis://127.0.0.1:{port}/0"),
            mock.patch("api.views.health.CHECK_TIMEOUT_SECONDS", 0.5),
            self.assertLogs("api.views.health", "WARNING"),
        ):
            started = time.monotonic()
            resp = self.client.get("/api/health")
            elapsed = time.monotonic() - started

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["broker"], "error")
        self.assertLess(elapsed, 1.5)
