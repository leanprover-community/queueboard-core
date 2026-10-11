#!/usr/bin/env python3
"""Download a database dump from an S3-compatible bucket: the one named, or the newest holding table data.

The bucket holds pg_dump custom-format archives keyed ``<prefix>-YYYYMMDDTHHMMSSZ.dump``, so their
keys sort by time. Without ``--key``, the newest dump whose table of contents (``pg_restore --list``)
has TABLE DATA entries is taken: a dump of a database that has no tables yet, as on a new, empty
volume, is skipped for an older one. Keys that don't match the pattern are ignored.

The connection comes from the environment, so no credential appears on a command line or in the
metadata file (which may be published):

  BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET,
  BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY   required; read-only credentials suffice
  BACKUP_S3_REGION       optional, default us-east-1
  BACKUP_S3_KEY_PREFIX   optional, default queueboard

``pg_restore`` must be on PATH and at least as new as the pg_dump that made the dumps. It reads each
dump from stdin.

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
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_REGION = "us-east-1"
DEFAULT_KEY_PREFIX = "queueboard"
# One dump an hour: two days of them, beyond which an empty run of dumps means something is wrong.
DEFAULT_MAX_CANDIDATES = 48


class DumpError(Exception):
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DumpError(f"{name} must be set")
    return value


def dump_key_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(prefix)}-\d{{8}}T\d{{6}}Z\.dump")


def list_dump_keys(client, bucket: str, prefix: str) -> list[str]:
    """Keys of the prefix's dumps, newest first."""
    pattern = dump_key_pattern(prefix)
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}-"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) if pattern.fullmatch(obj["Key"]))
    return sorted(keys, reverse=True)


def table_of_contents(path: Path) -> str:
    """``pg_restore --list`` of a dump; raises DumpError if pg_restore can't read it."""
    with path.open("rb") as dump:
        result = subprocess.run(["pg_restore", "--list"], stdin=dump, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DumpError(f"pg_restore --list failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def table_data_entries(toc: str) -> int:
    return sum(1 for line in toc.splitlines() if not line.startswith(";") and " TABLE DATA " in line)


def _toc_header(toc: str) -> list[str]:
    """The archive's description at the top of the listing: when it was made, by which versions."""
    header = []
    for line in toc.splitlines():
        if line.startswith("; Selected TOC Entries"):
            break
        if line.startswith(";") and line.strip(";").strip():
            header.append(line)
    return header


def _fetch(client, bucket: str, key: str, output: Path) -> tuple[dict, str, int]:
    print(f"Downloading {key}", flush=True)
    client.download_file(bucket, key, str(output))
    head = client.head_object(Bucket=bucket, Key=key)
    toc = table_of_contents(output)
    return head, toc, table_data_entries(toc)


def download(
    client, bucket: str, prefix: str, output: Path, key: str | None, max_candidates: int
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
    lines += ["", "# pg_restore --list (header)", *_toc_header(toc)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def make_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("BACKUP_S3_ENDPOINT"),
        region_name=os.environ.get("BACKUP_S3_REGION", "").strip() or DEFAULT_REGION,
        aws_access_key_id=_required_env("BACKUP_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("BACKUP_S3_SECRET_ACCESS_KEY"),
        config=Config(s3={"addressing_style": "path"}, retries={"mode": "standard", "max_attempts": 5}),
    )


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
        key, head, toc, skipped = download(
            client, bucket, prefix, args.output, (args.key or "").strip() or None, args.max_candidates
        )
        write_info(args.info, key, head, toc, skipped)
    except DumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError) as exc:
        print(f"error: S3 request failed: {exc}", file=sys.stderr)
        return 1
    print(f"Took {key}: {table_data_entries(toc)} table data entries", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
