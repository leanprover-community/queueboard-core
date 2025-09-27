# Beat Scheduler Choice

## Context
- The backend requires periodic tasks for sync and analytics.
- We run locally and in CI via Docker Compose with services for `web`, `db`, `redis`, `worker`, and `beat`.
- Two viable scheduling options:
  - Default Celery beat (`PersistentScheduler`) with code-defined schedules (`CELERY_BEAT_SCHEDULE`).
  - `django-celery-beat` storing schedules in Postgres, managed via Django Admin.

## Decision
- Use the default Celery beat for now.
- Define periodic tasks in code and ship changes via deploys.
- Do not add `django-celery-beat` yet; revisit after initial pipelines are stable and we understand the frequency of schedule changes.

## Consequences
- Pros:
  - Fewer dependencies and migrations; simpler Compose stack.
  - Schedules co-located with code, versioned in Git, easy to review.
  - Works out-of-the-box with our Redis broker/result backend.
- Cons:
  - No runtime editing via Admin; changes require a code deploy.
  - The default scheduler persists state to a local file; if not persisted across container restarts, last-run metadata is lost.

## Operational Notes
- To persist the schedule file, run beat with an explicit path and a named volume, e.g.:
  - Command: `celery -A qb_site beat -l info --schedule /data/celerybeat-schedule`
  - Compose: mount a volume to `/data` on the `beat` service.
- If requirements evolve (e.g., runtime toggles, frequent schedule changes, audit trail), migrate to `django-celery-beat` by:
  - Adding the dependency, enabling the app in `INSTALLED_APPS`, and migrating.
  - Switching the beat command to `--scheduler django_celery_beat.schedulers:DatabaseScheduler`.
  - Managing schedules via Django Admin.
