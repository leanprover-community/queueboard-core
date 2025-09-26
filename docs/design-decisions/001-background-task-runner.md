# Background Task Runner Decision

## Context
- The migration plan calls for periodic GitHub syncs and analytics recomputation.
- We need retryable task execution plus a scheduler that fits Django workflows and Docker Compose.
- Options considered: Celery, RQ (+ rq-scheduler), Django-Q, Dramatiq, and RabbitMQ-backed brokers.
- Desired capabilities include built-in retries with exponential backoff, chained workflows for multi-step analytics, and minimal additional infrastructure.

## Decision
- Adopt Celery as the task runner with Redis as both broker and result backend.
- Extend docker-compose with worker and beat services alongside a Redis container.

## Consequences
- Celery provides built-in retries, task chaining, rate limiting, and periodic scheduling (`celery beat`) without extra components.
- Redis keeps the local stack lightweight while offering sufficient reliability for at-least-once delivery; RabbitMQ or other brokers remain options if routing/durability requirements grow.
- Community maturity and Django integration around Celery/Redis shorten onboarding and give us access to ready-made monitoring/ops tooling.
- Documentation and onboarding must cover running the worker/beat processes and the additional services in Compose.
