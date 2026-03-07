#!/usr/bin/env python3
"""Validate sanitized backup policy coverage against Django-managed tables."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import django
from django.apps import apps

from backup_policy import BACKUP_TABLES, EXPORT_TABLE_QUERIES, RETAIN_TABLES, SCRUB_SQL_BY_TABLE, TRUNCATE_TABLES


NON_MODEL_BACKUP_TABLES: set[str] = {
    "django_migrations",
}


def _as_set(name: str, values: Iterable[str]) -> set[str]:
    values_list = list(values)
    out = set(values_list)
    if len(out) != len(values_list):
        raise ValueError(f"{name} contains duplicate entries")
    return out


def _extract_selected_columns(query: str, table: str) -> tuple[bool, list[str]]:
    match = re.match(r"^\s*SELECT\s+(.*?)\s+FROM\s+([a-zA-Z0-9_]+)\b", query, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError(f"Unparseable export query for {table}: {query!r}")
    select_expr = match.group(1).strip()
    from_table = match.group(2).strip().lower()
    if from_table != table:
        raise ValueError(f"Export query table mismatch for {table}: FROM {from_table}")
    if select_expr == "*":
        return True, []
    cols = [part.strip().strip('"') for part in select_expr.split(",")]
    if not cols or any(not c for c in cols):
        raise ValueError(f"Invalid column projection for {table}: {select_expr!r}")
    return False, cols


def _model_table_columns() -> dict[str, set[str]]:
    table_columns: dict[str, set[str]] = {}
    for model in apps.get_models(include_auto_created=True):
        opts = model._meta
        if not opts.managed:
            continue
        table = opts.db_table
        table_columns[table] = {f.column for f in opts.local_fields}
    return table_columns


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "qb_site"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qb_site.settings")
    django.setup()

    model_table_columns = _model_table_columns()
    discovered_tables = set(model_table_columns) | NON_MODEL_BACKUP_TABLES

    backup_tables = _as_set("BACKUP_TABLES", BACKUP_TABLES)
    truncate_tables = _as_set("TRUNCATE_TABLES", TRUNCATE_TABLES)
    retain_tables = _as_set("RETAIN_TABLES", RETAIN_TABLES)
    scrub_tables = _as_set("SCRUB_SQL_BY_TABLE", SCRUB_SQL_BY_TABLE.keys())
    export_tables = _as_set("EXPORT_TABLE_QUERIES", EXPORT_TABLE_QUERIES.keys())

    errors: list[str] = []

    missing_from_policy = discovered_tables - backup_tables
    extra_in_policy = backup_tables - discovered_tables
    if missing_from_policy:
        errors.append(f"Missing from BACKUP_TABLES: {sorted(missing_from_policy)}")
    if extra_in_policy:
        errors.append(f"Unknown/stale entries in BACKUP_TABLES: {sorted(extra_in_policy)}")

    overlap = truncate_tables & retain_tables
    if overlap:
        errors.append(f"Tables cannot be both truncate and retain: {sorted(overlap)}")

    classified_tables = truncate_tables | retain_tables
    if classified_tables != backup_tables:
        errors.append(
            "TRUNCATE_TABLES ∪ RETAIN_TABLES must exactly equal BACKUP_TABLES; "
            f"missing={sorted(backup_tables - classified_tables)}, extra={sorted(classified_tables - backup_tables)}"
        )

    if not scrub_tables.issubset(retain_tables):
        errors.append(f"SCRUB_SQL_BY_TABLE keys must be retained (not truncated): {sorted(scrub_tables - retain_tables)}")

    if not export_tables.issubset(retain_tables):
        errors.append(f"EXPORT_TABLE_QUERIES keys must be retained (not truncated): {sorted(export_tables - retain_tables)}")

    for table, query in EXPORT_TABLE_QUERIES.items():
        if table not in model_table_columns:
            # Non-model table exports are not currently supported.
            errors.append(f"Export table has no managed model columns: {table}")
            continue
        select_all, selected_columns = _extract_selected_columns(query, table)
        if select_all:
            continue
        unknown_cols = sorted(set(selected_columns) - model_table_columns[table])
        if unknown_cols:
            errors.append(f"Export query for {table} references unknown columns: {unknown_cols}")

    if errors:
        print("Backup policy validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1

    print("Backup policy validation passed.")
    print(f"- Discovered tables: {len(discovered_tables)}")
    print(f"- Truncate tables: {len(truncate_tables)}")
    print(f"- Retain tables: {len(retain_tables)}")
    print(f"- Export tables: {len(export_tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
