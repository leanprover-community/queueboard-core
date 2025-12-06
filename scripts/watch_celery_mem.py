#!/usr/bin/env python3
"""Stream Heroku logs and surface memory spikes with recent Celery tasks.

Usage:
  heroku logs --tail -a <app> | python scripts/watch_celery_mem.py --threshold 430

Requirements:
  - Enable Heroku runtime metrics: `heroku labs:enable log-runtime-metrics -a <app>`
  - Celery logs should include task lines (the default Celery logging format works).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, Optional


MEM_RE = re.compile(r"sample#memory_(?:total|rss)=([0-9.]+)MB", re.IGNORECASE)
TASK_RE = re.compile(
    r"(?:Received task: (?P<recv>[^\[]+)\[(?P<recv_id>[^\]]+)\])|"
    r"(?:Task (?P<task>[^\s\[]+)\[(?P<task_id>[^\]]+)\])",
    re.IGNORECASE,
)
R14_RE = re.compile(r"\bR1[45]\b", re.IGNORECASE)


@dataclass
class TaskSample:
    ts: str
    name: str
    task_id: Optional[str]
    line: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Correlate Heroku dyno memory samples with recent Celery tasks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=400.0,
        help="Emit an alert when memory (MB) at or above this value is seen.",
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=30,
        help="How many recent task log lines to keep for context.",
    )
    parser.add_argument(
        "--show-all-memory",
        action="store_true",
        help="Print every memory sample, not just threshold crossings.",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Optional prefix to add to alert lines (helps when teeing multiple streams).",
    )
    return parser.parse_args()


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def record_task(line: str) -> Optional[TaskSample]:
    m = TASK_RE.search(line)
    if not m:
        return None
    name = m.group("recv") or m.group("task") or "unknown"
    tid = m.group("recv_id") or m.group("task_id")
    return TaskSample(ts=now_str(), name=name.strip(), task_id=(tid or "").strip() or None, line=line.strip())


def parse_memory_mb(line: str) -> Optional[float]:
    m = MEM_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def print_alert(
    prefix: str,
    label: str,
    detail: str,
    context_tasks: Iterable[TaskSample],
) -> None:
    header = f"{prefix}{label}: {detail}"
    print("=" * len(header))
    print(header)
    print("- recent tasks -")
    for t in context_tasks:
        tid = f"[{t.task_id}]" if t.task_id else ""
        print(f"{t.ts} {t.name}{tid} :: {t.line}")
    print("=" * len(header))
    sys.stdout.flush()


def main() -> int:
    args = parse_args()
    recent: Deque[TaskSample] = deque(maxlen=max(args.buffer_size, 1))

    try:
        for raw_line in sys.stdin:
            line = raw_line.rstrip("\n")

            # Track task lines for later context
            tsample = record_task(line)
            if tsample:
                recent.append(tsample)

            # Memory samples
            mem_mb = parse_memory_mb(line)
            if mem_mb is not None:
                if args.show_all_memory:
                    print(f"{args.prefix}memory_sample={mem_mb:.1f}MB line={line}")
                if mem_mb >= args.threshold:
                    print_alert(
                        args.prefix,
                        "MEMORY_THRESHOLD",
                        f"{mem_mb:.1f} MB (>= {args.threshold:.1f} MB)",
                        list(recent),
                    )

            # Heroku R14/R15 OOM warnings
            if R14_RE.search(line) or "out of memory" in line.lower():
                print_alert(
                    args.prefix,
                    "DYNO_MEMORY_EVENT",
                    line.strip(),
                    list(recent),
                )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
