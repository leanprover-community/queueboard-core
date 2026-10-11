#!/usr/bin/env python3
"""Download a database dump from an S3-compatible bucket: the one named, or the newest holding table data.

The bucket holds pg_dump custom-format archives keyed ``<prefix>-YYYYMMDDTHHMMSSZ.dump``, so their
keys sort by time. Without ``--key``, the newest dump whose table of contents (``pg_restore --list``)
has TABLE DATA entries is taken: a dump of a database that has no tables yet, as a new deployment
may take before its data is restored, is skipped for an older one. Keys that don't match the pattern
are ignored.

The connection comes from the environment, so no credential appears on a command line or in the
metadata file (which may be published):

  BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET,
  BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY   required; read-only credentials suffice
  BACKUP_S3_REGION       optional, default us-east-1
  BACKUP_S3_KEY_PREFIX   optional, default queueboard

``pg_restore`` must be on PATH and at least as new as the pg_dump that made the dumps; the script
prints its version first. It reads each dump from stdin. Where Debian's pg_wrapper provides
``pg_restore``, it may pick an older installed version: put the wanted version's ``bin`` directory
first on PATH (``scripts/install_postgres_client.sh`` does).

Usage:
  python scripts/download_s3_dump.py -o backup.dump -i backup-info.txt [--key KEY]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3
from boto3.exceptions import Boto3Error
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_REGION = "us-east-1"
DEFAULT_KEY_PREFIX = "queueboard"
# How many dumps without table data to get past before giving up: more than a fresh deployment should
# take before its data is restored.
DEFAULT_MAX_CANDIDATES = 48
# A TOC line for a table's data: "<dump id>; <catalog oid> <object oid> TABLE DATA <schema> <name> <owner>".
# Anchored, so an object whose name contains " TABLE DATA " can't pass for one.
TABLE_DATA_ENTRY = re.compile(r"\d+; \d+ \d+ TABLE DATA ")
# The lines of the archive's header worth publishing: which formats and versions. Not when or where it
# was made: "Archive created at" is in the dumping host's local time.
ARCHIVE_HEADER_FIELDS = ("Dump Version:", "Format:", "Dumped from database version:", "Dumped by pg_dump version:")


class DumpError(Exception):
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DumpError(f"{name} must be set")
    return value


def dump_key_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(prefix)}-\d{{8}}T\d{{6}}Z\.dump")


def list_dump_keys(client: BaseClient, bucket: str, prefix: str) -> list[str]:
    """Keys of the prefix's dumps, newest first."""
    pattern = dump_key_pattern(prefix)
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}-"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) if pattern.fullmatch(obj["Key"]))
    return sorted(keys, reverse=True)


def pg_restore_version() -> str:
    try:
        result = subprocess.run(["pg_restore", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise DumpError("pg_restore is not on PATH") from None
    if result.returncode != 0:
        raise DumpError(f"pg_restore --version failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def table_of_contents(path: Path) -> str:
    """``pg_restore --list`` of a dump; raises DumpError if pg_restore can't read it."""
    with path.open("rb") as dump:
        result = subprocess.run(["pg_restore", "--list"], stdin=dump, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DumpError(f"pg_restore --list failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def table_data_entries(toc: str) -> int:
    return sum(1 for line in toc.splitlines() if TABLE_DATA_ENTRY.match(line))


def _archive_header(toc: str) -> list[str]:
    """The archive's formats and versions, from the comments at the top of the listing."""
    return [line for line in toc.splitlines() if line.startswith(";") and line.lstrip("; ").startswith(ARCHIVE_HEADER_FIELDS)]


def _fetch(client: BaseClient, bucket: str, key: str, output: Path) -> tuple[dict, str, int]:
    print(f"Downloading {key}", flush=True)
    client.download_file(bucket, key, str(output))
    head = client.head_object(Bucket=bucket, Key=key)
    toc = table_of_contents(output)
    return head, toc, table_data_entries(toc)


def download(
    client: BaseClient, bucket: str, prefix: str, output: Path, key: str | None, max_candidates: int
) -> tuple[str, dict, str, list[str]]:
    """Download the named dump, or the newest with table data. Returns (key, head, toc, skipped keys)."""
    if key:
        head, toc, entries = _fetch(client, bucket, key, output)
        if not entries:
            raise DumpError(f"{key} holds no table data")
        return key, head, toc, []

    keys = list_dump_keys(client, bucket, prefix)
    if not keys:
        raise DumpError(f"the bucket holds no dumps named {prefix}-YYYYMMDDTHHMMSSZ.dump")
    skipped = []
    for candidate in keys[:max_candidates]:
        head, toc, entries = _fetch(client, bucket, candidate, output)
        if entries:
            return candidate, head, toc, skipped
        print(f"Skipping {candidate}: it holds no table data, like a dump of a database with no tables", flush=True)
        skipped.append(candidate)
    output.unlink(missing_ok=True)
    raise DumpError(f"none of the newest {len(skipped)} dumps holds table data: name an older one with --key")


def write_info(path: Path, key: str, head: dict, toc: str, skipped: list[str]) -> None:
    lines = [
        f"# dump: {key}",
        f"size: {head.get('ContentLength')} bytes",
        f"last modified: {head.get('LastModified')}",
        f"table data entries: {table_data_entries(toc)}",
    ]
    if skipped:
        lines.append(f"skipped, holding no table data: {', '.join(skipped)}")
    lines += ["", "# archive (from pg_restore --list)", *_archive_header(toc)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def make_client() -> BaseClient:
    endpoint = _required_env("BACKUP_S3_ENDPOINT")
    access_key_id = _required_env("BACKUP_S3_ACCESS_KEY_ID")
    secret_access_key = _required_env("BACKUP_S3_SECRET_ACCESS_KEY")
    try:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=os.environ.get("BACKUP_S3_REGION", "").strip() or DEFAULT_REGION,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(s3={"addressing_style": "path"}, retries={"mode": "standard", "max_attempts": 5}),
        )
    except ValueError:
        # botocore's message repeats the endpoint, a secret: say which setting instead.
        raise DumpError("BACKUP_S3_ENDPOINT is not a valid URL (it needs a scheme, e.g. https://)") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("-o", "--output", type=Path, required=True, help="where to write the dump")
    parser.add_argument("-i", "--info", type=Path, required=True, help="where to write the dump's metadata")
    parser.add_argument("--key", help="the object key of a dump to take, instead of the newest with table data")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help=f"how many of the newest dumps to try before giving up (default {DEFAULT_MAX_CANDIDATES})",
    )
    args = parser.parse_args(argv)

    try:
        bucket = _required_env("BACKUP_S3_BUCKET")
        prefix = os.environ.get("BACKUP_S3_KEY_PREFIX", "").strip() or DEFAULT_KEY_PREFIX
        client = make_client()
        print(f"Using {pg_restore_version()}", flush=True)
        key, head, toc, skipped = download(
            client, bucket, prefix, args.output, (args.key or "").strip() or None, args.max_candidates
        )
        write_info(args.info, key, head, toc, skipped)
    except DumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError, Boto3Error) as exc:
        print(f"error: S3 request failed: {exc}", file=sys.stderr)
        return 1
    print(f"Took {key}: {table_data_entries(toc)} table data entries", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
