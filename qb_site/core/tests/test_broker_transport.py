"""The broker's transport loads, as the Celery worker and beat load it when they start.

kombu imports a transport's module only when a connection first needs it, so nothing else in the
suite does. A dependency that the transport's module imports and the lock lacks then passes every
other test, and web keeps answering, while the worker and beat exit at start. kombu 5.7.0 did this:
it moved `packaging`, which its Redis transport imports, into its `redis` extra, which is why the
dependency is `celery[redis]`. Loading the transport doesn't connect, so this needs no Redis.
"""

from __future__ import annotations

import redis
from django.test import SimpleTestCase

from qb_site.celery import app


class BrokerTransportTests(SimpleTestCase):
    def test_broker_transport_loads(self) -> None:
        with app.connection_for_write() as connection:
            transport = connection.transport
        self.assertEqual(transport.driver_type, "redis")
        self.assertEqual(transport.driver_version(), redis.__version__)
