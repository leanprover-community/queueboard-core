#!/usr/bin/env python3
"""Sanitize a restored Postgres database per the public-backup policy.

This script truncates private/operational tables and scrubs non-public fields.
It writes a manifest describing the actions taken.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

import psycopg
from psycopg import sql


# Tables to truncate (RESTART IDENTITY CASCADE). Missing tables are skipped.
TRUNCATE_TABLES: tuple[str, ...] = (
    # Django auth/admin/session
    "auth_group",
    "auth_group_permissions",
    "auth_permission",
    "auth_user",
    "auth_user_groups",
    "auth_user_user_permissions",
    "django_admin_log",
    "django_session",
    "django_content_type",
    "django_migrations",
    # Celery task results
    "django_celery_results_taskresult",
    "django_celery_results_groupresult",
    "core_taskresultlink",
    # Reviewer preferences (private config)
    "core_reviewerpreference",
    # Snapshots/metrics caches
    "analyzer_queuesnapshot",
    "analyzer_reviewerassignmentsnapshot",
    "analyzer_areastatssnapshot",
    "analyzer_analyzerconvergencesnapshot",
    "syncer_syncermetricssnapshot",
    "syncer_syncerconvergencesnapshot",
)

# In-place scrubs (set private fields to NULL).
SCRUB_QUERIES: tuple[tuple[str, sql.SQL], ...] = (
    (
        "core_user",
        sql.SQL(
            """
            UPDATE core_user
            SET
              zulip_user_id = NULL,
              zulip_full_name = NULL,
              timezone = NULL
            """
        ),
    ),
)


def mask_url(url: str) -> str:
    parsed = urlparse(url)
    netloc_parts = []
    if parsed.username:
        user = parsed.username
        password = ":***" if parsed.password else ""
        netloc_parts.append(f"{user}{password}@")
    host = parsed.hostname or ""
    if host:
        netloc_parts.append(host)
    if parsed.port:
        netloc_parts.append(f":{parsed.port}")
    netloc = "".join(netloc_parts)
    return parsed._replace(netloc=netloc).geturl()


def table_exists(conn: psycopg.Connection[Any], table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table,),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def count_rows(conn: psycopg.Connection[Any], table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def truncate_tables(
    conn: psycopg.Connection[Any],
    tables: Iterable[str],
    dry_run: bool,
    manifest: list[dict[str, Any]],
) -> None:
    for table in tables:
        if not table_exists(conn, table):
            manifest.append({"table": table, "action": "truncate", "status": "missing"})
            continue

        before = count_rows(conn, table)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table))
                )
        after = 0 if not dry_run else before
        manifest.append(
            {"table": table, "action": "truncate", "status": "ok", "rows_before": before, "rows_after": after}
        )


def scrub_tables(
    conn: psycopg.Connection[Any],
    scrubs: Iterable[tuple[str, sql.SQL]],
    dry_run: bool,
    manifest: list[dict[str, Any]],
) -> None:
    for table, statement in scrubs:
        if not table_exists(conn, table):
            manifest.append({"table": table, "action": "scrub", "status": "missing"})
            continue

        updated = 0
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(statement)
                updated = cur.rowcount or 0
        manifest.append({"table": table, "action": "scrub", "status": "ok", "rows_updated": updated})


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitize a restored Postgres database.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="Postgres connection URL")
    parser.add_argument("--manifest", default="artifacts/sanitize-manifest.json", help="Path to write manifest JSON")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without mutating data")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL must be set (or passed via --database-url)")

    os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)

    manifest: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "database": mask_url(args.database_url),
        "dry_run": bool(args.dry_run),
        "truncate": [],
        "scrub": [],
    }

    with psycopg.connect(args.database_url) as conn:
        truncate_tables(conn, TRUNCATE_TABLES, args.dry_run, manifest["truncate"])
        scrub_tables(conn, SCRUB_QUERIES, args.dry_run, manifest["scrub"])
        if not args.dry_run:
            conn.commit()

    with open(args.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print(f"Wrote manifest to {args.manifest}")


if __name__ == "__main__":
    main()
