# Production image

`Dockerfile.production` builds an image for running the app in containers. Local development
still uses `Dockerfile` and `docker-compose.yml`, which this image does not touch.

The `Production image` workflow builds it on every pull request. Each push to `master` also
publishes it as `ghcr.io/leanprover-community/queueboard-core`, tagged `sha-<short commit hash>`
and `latest`; the run's summary gives the digest, so a deployment can pin the exact build
(`ghcr.io/leanprover-community/queueboard-core@sha256:…`) rather than a tag that moves.

To build it locally:

```bash
docker build -f Dockerfile.production -t queueboard-core .
```

## What is in it

- Python 3.12 (as `.python-version`) on a slim Debian base, pinned by digest.
- Exactly the packages `uv.lock` pins, without the dev group. The build fails if `uv.lock` is out of
  date with `pyproject.toml`. The repo's own code is not installed as a package: it is copied to
  `/app` and found through `PYTHONPATH=/app/qb_site:/app:/app/src`.
- `DJANGO_SETTINGS_MODULE=qb_site.settings.production`.
- Static files, collected at build time into `/app/staticfiles` (`DJANGO_STATIC_ROOT`) and served
  by WhiteNoise. Leave `DJANGO_STATIC_ROOT` unset at runtime, or set it to that path.
- An unprivileged user, `queueboard`, UID and GID 10001. Everything under `/app` belongs to root
  and is read-only to it. The only directory it can write is `/data`, meant for a volume.

## Configuration

Settings come from environment variables, as listed in `.env.example`. Production settings refuse
to start without `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS`; the database is `DJANGO_DB_*`
and the broker `CELERY_BROKER_URL`. No secret is built into the image.

`.env.example` is written for local development: it sets `DJANGO_SETTINGS_MODULE=qb_site.settings.local`
and `DJANGO_DEBUG=1`. Leave both out of a production env file. A `DJANGO_SETTINGS_MODULE` there
overrides the image's, and the local settings show Django's debug pages.

The web server is gunicorn, with defaults in `qb_site/gunicorn.conf.py`: port 8000, 3 sync
workers, a 120 s worker timeout and the same graceful timeout on shutdown (Zulip commands call
GitHub and Zulip inside the request), and access and error logs on stdout/stderr. Set the worker
count with `WEB_CONCURRENCY` and anything else with `GUNICORN_CMD_ARGS`, e.g.
`GUNICORN_CMD_ARGS="--timeout 60"`. The settings trust `X-Forwarded-Proto`, so run it behind a
reverse proxy that sets that header. A sync worker serves one request at a time, so gunicorn
recommends a proxy that buffers slow clients.

## Processes

One image runs every process; only the command differs.

| Process | Command |
| --- | --- |
| migrations (one-off) | `python qb_site/manage.py migrate --noinput` |
| web | the default command (gunicorn) |
| Celery worker | `celery -A qb_site worker -l info -Q default --concurrency=2 --prefetch-multiplier=1 --max-tasks-per-child=25 --max-memory-per-child=200000 --without-gossip --without-mingle` |
| Celery beat | `celery -A qb_site beat -l info --schedule /data/celerybeat-schedule` |

- Run migrations to completion before starting web and workers, including after every image update.
- Run exactly one beat at a time. A second one sends every periodic task twice.
- Mount a volume at `/data` for beat, so its schedule survives a restart. A new named volume takes
  its owner from the image; a host directory needs `chown 10001:10001` first.
- `/api/health` checks the database and the broker and answers 200 or 503, for a health check or
  an uptime monitor (`/api/` touches neither). A probe from inside the container needs `localhost`
  in `DJANGO_ALLOWED_HOSTS`; the image has no `curl`, so use Python, with a timeout:
  `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=10)"`.
- Docker gives a stopping container 10 s before killing it. Allow web more than gunicorn's 120 s
  graceful timeout, and the worker long enough for running tasks (`stop_grace_period` in Compose).
- For a one-off Django command, use `manage.py` (e.g. `python qb_site/manage.py shell`). A bare
  `python -c` started in `/app` finds the project directory `/app/qb_site` before the settings
  package, and cannot import `qb_site.settings`.

A minimal Compose file, for illustration:

```yaml
x-app: &app
  image: ghcr.io/leanprover-community/queueboard-core@sha256:<digest>
  env_file: queueboard.env

services:
  migrate:
    <<: *app
    command: python qb_site/manage.py migrate --noinput
  web:
    <<: *app
    ports: ["127.0.0.1:8000:8000"]
    stop_grace_period: 130s
    depends_on:
      migrate:
        condition: service_completed_successfully
  worker:
    <<: *app
    command: celery -A qb_site worker -l info -Q default --concurrency=2 --prefetch-multiplier=1 --max-tasks-per-child=25 --max-memory-per-child=200000 --without-gossip --without-mingle
    stop_grace_period: 2m
    depends_on:
      migrate:
        condition: service_completed_successfully
  beat:
    <<: *app
    command: celery -A qb_site beat -l info --schedule /data/celerybeat-schedule
    volumes: ["beat-data:/data"]
    depends_on:
      migrate:
        condition: service_completed_successfully

volumes:
  beat-data:
```

Postgres and Redis are left out; `queueboard.env` points at them.
