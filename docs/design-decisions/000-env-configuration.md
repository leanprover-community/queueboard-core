# Environment Configuration Decision

## Context
- Django settings currently read values directly from `os.environ`.
- `.env` files are loaded by Docker Compose via `env_file`; local commands outside Compose are uncommon.
- Previous guidance suggested a SQLite fallback for quick tests.

## Decision
- Continue using the standard library (`os.environ`) without adding a third-party dotenv/settings library.
- Treat Docker Compose as the supported workflow for local development; `.env` is not auto-loaded by the Django settings module.
- Require PostgreSQL in all environments and drop the SQLite fallback messaging.

## Consequences
- Developers running management commands outside Compose must export the necessary environment variables manually.
- The repo docs and agent guides highlight PostgreSQL as the only supported database path.
- We retain flexibility to revisit a dedicated settings library later if configuration complexity grows.
