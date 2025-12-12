#!/usr/bin/env python3
"""Export sanitized datasets from Postgres for offline analysis."""

from __future__ import annotations

import argparse
import io
import os
from typing import Any

import psycopg

try:
    import pandas as pd
except ImportError:  # pragma: no cover - optional dependency for parquet
    pd = None  # type: ignore

DEFAULT_TABLE_QUERIES: dict[str, str] = {
    "core_repository": "SELECT * FROM core_repository ORDER BY id",
    "core_user": "SELECT * FROM core_user ORDER BY id",
    "syncer_pullrequest": "SELECT * FROM syncer_pullrequest ORDER BY id",
    "syncer_labeldef": "SELECT * FROM syncer_labeldef ORDER BY id",
    "syncer_prlabel": "SELECT * FROM syncer_prlabel ORDER BY id",
    "syncer_prtimelineevent": "SELECT * FROM syncer_prtimelineevent ORDER BY id",
    "syncer_checkrun": "SELECT * FROM syncer_checkrun ORDER BY id",
    "syncer_statuscontext": "SELECT * FROM syncer_statuscontext ORDER BY id",
    "syncer_repobackfillcursor": "SELECT * FROM syncer_repobackfillcursor ORDER BY id",
    "syncer_commithistoryharvest": "SELECT * FROM syncer_commithistoryharvest ORDER BY id",
    "analyzer_queueruleset": "SELECT * FROM analyzer_queueruleset ORDER BY id",
    "analyzer_prqueuewindow": "SELECT * FROM analyzer_prqueuewindow ORDER BY id",
    "analyzer_prrevision": "SELECT * FROM analyzer_prrevision ORDER BY id",
    "analyzer_prrevisionbuildstate": "SELECT * FROM analyzer_prrevisionbuildstate ORDER BY id",
    "analyzer_prdependency": "SELECT * FROM analyzer_prdependency ORDER BY id",
    "analyzer_prdependencystate": "SELECT * FROM analyzer_prdependencystate ORDER BY id",
}


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


def export_table_csv(conn: psycopg.Connection[Any], name: str, query: str, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with conn.cursor() as cur, cur.copy(f"COPY ({query}) TO STDOUT WITH CSV HEADER") as copy, open(output_path, "wb") as out:
        for data in copy:
            out.write(data)
    print(f"Wrote {name} -> {output_path} (csv)")


def export_table_parquet(conn: psycopg.Connection[Any], name: str, query: str, output_path: str) -> None:
    if pd is None:
        raise RuntimeError("pandas/pyarrow are required for parquet export but are not installed")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    buf = io.BytesIO()
    with conn.cursor() as cur, cur.copy(f"COPY ({query}) TO STDOUT WITH CSV HEADER") as copy:
        for data in copy:
            buf.write(data)
    buf.seek(0)
    df = pd.read_csv(buf)
    df.to_parquet(output_path, index=False)
    print(f"Wrote {name} -> {output_path} (parquet)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized datasets for analysis.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="Postgres connection URL")
    parser.add_argument("--output-dir", default="artifacts/data", help="Directory to write table exports")
    parser.add_argument(
        "--format",
        choices=["csv", "parquet"],
        default="parquet",
        help="Export format (default: parquet)",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        help="Optional list of tables to export (defaults to curated set)",
    )
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL must be set (or passed via --database-url)")

    selected_tables = args.tables if args.tables else list(DEFAULT_TABLE_QUERIES.keys())

    with psycopg.connect(args.database_url) as conn:
        for table in selected_tables:
            query = DEFAULT_TABLE_QUERIES.get(table)
            if query is None:
                print(f"Skipping unknown table '{table}' (not in curated list)")
                continue
            if not table_exists(conn, table):
                print(f"Skipping missing table '{table}'")
                continue
            if args.format == "csv":
                export_table_csv(conn, table, query, os.path.join(args.output_dir, f"{table}.csv"))
            else:
                export_table_parquet(conn, table, query, os.path.join(args.output_dir, f"{table}.parquet"))


if __name__ == "__main__":
    main()
