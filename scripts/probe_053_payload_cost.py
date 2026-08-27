#!/usr/bin/env python
"""Measure the queue-snapshot payload load cost on the suggestion request path (design doc 053).

The 053 baseline left one number unmeasured: what it costs to get the ~12.8 MB ``QueueSnapshot``
payload out of Postgres and into a Python dict. The original probe timed attribute access on an
already-materialized ``JSONField`` and therefore measured nothing. This script times *queryset
evaluation* and decomposes it:

- **fetch**   Postgres read + TOAST decompress + wire transfer, measured as ``payload::text``
- **parse**   ``json.loads`` on that text, in Python
- **orm**     the real ``QueueSnapshot.objects...first()`` path both surfaces would use
- **request** the whole suggestion request: payload + assignment inputs + load line + rank + walk

It also measures **memory**, which on a small dyno may matter more than latency: a 12.8 MB JSON
document becomes a far larger graph of Python dicts, strings and lists, and every concurrent
console request holds its own copy.

Strictly read-only: SELECTs only, no writes, no GitHub calls, no snapshot builds.

Emits one JSON object on stdout between BEGIN/END markers.

Usage on the dyno:
    PYTHONPATH=$PWD/qb_site:$PWD python probe_053_payload_cost.py [--repeats 5] [--repo owner/name]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import time
import tracemalloc
from datetime import datetime, timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qb_site.settings.production")

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from analyzer.models import QueueSnapshot  # noqa: E402
from analyzer.services.queue_rules import default_rule_set_for_repo  # noqa: E402

# The pool assembler was private (`_prepare_assignment_inputs`) until design doc 053 promoted it.
# Accept either name: this probe measures the *pre-deploy* baseline, so it has to run against a
# revision that predates the rename — a baseline probe that only runs post-deploy is useless.
try:
    from analyzer.services.reviewer_assignment import prepare_assignment_inputs  # noqa: E402
except ImportError:  # deployed revision predates the 053 rename
    from analyzer.services.reviewer_assignment import (  # noqa: E402
        _prepare_assignment_inputs as prepare_assignment_inputs,
    )
from analyzer.services.reviewer_assignment_engine import rank_prs_for_assignment  # noqa: E402
from analyzer.services.reviewer_assignment_engine import suggest_reviewer_for_pr_with_trace  # noqa: E402
from analyzer.services.reviewer_load import reviewer_load_for  # noqa: E402
from core.models import Repository, ReviewerPreference  # noqa: E402
from core.services.topic_labels import topic_label_matcher_for_repo  # noqa: E402

MARK_BEGIN = "===QB-PAYLOAD-COST-JSON-BEGIN==="
MARK_END = "===QB-PAYLOAD-COST-JSON-END==="


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def stats(samples: list[float]) -> dict:
    """Timing summary in milliseconds. `first` is reported separately: it carries cold-cache cost."""
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "first_ms": round(samples[0] * 1000, 1),
        "min_ms": round(ordered[0] * 1000, 1),
        "median_ms": round(ordered[len(ordered) // 2] * 1000, 1),
        "max_ms": round(ordered[-1] * 1000, 1),
    }


def rss_bytes() -> int | None:
    """Current resident set size, from /proc (Linux dynos). None where unavailable."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def peak_rss_bytes() -> int:
    """Peak RSS for the process. ru_maxrss is KiB on Linux, bytes on macOS."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw * 1024 if sys.platform.startswith("linux") else raw


# Big objects are released by rebinding the name to None at the call site and then collecting.
# This has to be inline: `locals()` is a snapshot for function scopes, so a helper that assigns
# into it frees nothing. It matters because a 12.8 MB payload expands to a much larger Python
# graph, and holding the previous iteration's copy while the next query materialises would double
# peak memory on a small dyno.


def measure_repo(repository: Repository, *, repeats: int) -> dict:
    out: dict = {"repository": f"{repository.owner}/{repository.name}"}

    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    out["cache_key"] = cache_key

    row = (
        QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key)
        .order_by("-generated_at")
        .only("id", "generated_at", "pr_count", "queue_count")
        .first()
    )
    if row is None:
        out["status"] = "no_snapshot"
        return out
    snapshot_id = row.id
    out["snapshot"] = {
        "id": snapshot_id,
        "generated_at": row.generated_at.isoformat(),
        "pr_count": row.pr_count,
        "queue_count": row.queue_count,
    }

    # --- sizes, straight from Postgres ---------------------------------------------------------
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_column_size(payload), octet_length(payload::text) FROM analyzer_queuesnapshot WHERE id = %s",
            [snapshot_id],
        )
        on_disk, as_text = cur.fetchone()
    out["size"] = {
        "toast_compressed_bytes": int(on_disk),
        "json_text_bytes": int(as_text),
        "toast_compressed_mb": round(int(on_disk) / 1_000_000, 2),
        "json_text_mb": round(int(as_text) / 1_000_000, 2),
        "compression_ratio": round(int(as_text) / int(on_disk), 2),
    }

    # --- A. fetch: Postgres read + decompress + wire, no Python JSON parsing -------------------
    fetch_samples: list[float] = []
    for _ in range(repeats):
        with connection.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute("SELECT payload::text FROM analyzer_queuesnapshot WHERE id = %s", [snapshot_id])
            text = cur.fetchone()[0]
            fetch_samples.append(time.perf_counter() - t0)
        last_text = text
    out["fetch_as_text"] = stats(fetch_samples)

    # --- B. parse: json.loads on the text we already have --------------------------------------
    parse_samples: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        parsed = json.loads(last_text)
        parse_samples.append(time.perf_counter() - t0)
        del parsed
        gc.collect()
    out["json_parse"] = stats(parse_samples)
    last_text = text = None
    gc.collect()

    # --- C. the real ORM path both surfaces would use ------------------------------------------
    orm_samples: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        obj = (
            QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key)
            .order_by("-generated_at")
            .only("payload")
            .first()
        )
        payload = obj.payload
        n_prs = len(payload.get("prs", {}))
        orm_samples.append(time.perf_counter() - t0)
        obj = payload = None
        gc.collect()
    out["orm_only_payload"] = stats(orm_samples)
    out["orm_prs_seen"] = n_prs

    # --- D. same, without .only() — does fetching the other columns matter? --------------------
    full_samples: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        obj = QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()
        _ = obj.payload.get("prs", {})
        full_samples.append(time.perf_counter() - t0)
        obj = None
        gc.collect()
    out["orm_full_row"] = stats(full_samples)

    # --- E. memory: what one held payload costs ------------------------------------------------
    gc.collect()
    rss_before = rss_bytes()
    tracemalloc.start()
    obj = (
        QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").only("payload").first()
    )
    payload = obj.payload
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = rss_bytes()
    out["memory"] = {
        "traced_current_bytes": traced_current,
        "traced_current_mb": round(traced_current / 1_000_000, 1),
        "traced_peak_mb": round(traced_peak / 1_000_000, 1),
        "rss_before_mb": round(rss_before / 1_000_000, 1) if rss_before else None,
        "rss_after_mb": round(rss_after / 1_000_000, 1) if rss_after else None,
        "rss_delta_mb": round((rss_after - rss_before) / 1_000_000, 1) if rss_before and rss_after else None,
        "expansion_vs_json_text": round(traced_current / int(as_text), 2),
        "note": "traced_current is the live Python object graph for one held payload",
    }
    obj = payload = None
    gc.collect()

    # --- F. end-to-end: the whole suggestion request -------------------------------------------
    reviewer = (
        ReviewerPreference.objects.filter(repository=repository)
        .select_related("user")
        .exclude(user__github_login=None)
        .order_by("user__github_login")
        .first()
    )
    if reviewer is not None:
        matcher = topic_label_matcher_for_repo(repository)
        login = reviewer.user.github_login
        request_samples: list[float] = []
        phase_totals = {"payload": 0.0, "inputs": 0.0, "load": 0.0, "rank": 0.0, "walk": 0.0}
        for _ in range(repeats):
            now = datetime.now(timezone.utc)
            t_start = time.perf_counter()

            t0 = time.perf_counter()
            obj = (
                QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key)
                .order_by("-generated_at")
                .only("payload")
                .first()
            )
            payload_e2e = obj.payload
            phase_totals["payload"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            inputs = prepare_assignment_inputs(repository, payload=payload_e2e, now=now, rule_set=rule_set)
            phase_totals["inputs"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            reviewer_load_for(repository, login, snapshot_payload=payload_e2e, now=now)
            phase_totals["load"] += time.perf_counter() - t0

            all_prs = payload_e2e.get("prs", {})
            t0 = time.perf_counter()
            ranked, _trace = rank_prs_for_assignment(
                prs_to_assign=inputs.assignable_queue_prs,
                all_prs=all_prs,
                reviewers=inputs.reviewers,
                assignment_stats=inputs.assignments,
                excluded_by_pr=inputs.excluded_by_pr,
                topic_label_matcher=matcher,
            )
            phase_totals["rank"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            for pr_number in ranked:
                suggest_reviewer_for_pr_with_trace(
                    pr_entry=all_prs.get(pr_number) or all_prs.get(str(pr_number)) or {},
                    reviewers=inputs.reviewers,
                    assignment_stats=inputs.assignments,
                    excluded_logins=inputs.excluded_by_pr.get(pr_number, set()),
                    topic_label_matcher=matcher,
                )
            phase_totals["walk"] += time.perf_counter() - t0

            request_samples.append(time.perf_counter() - t_start)
            obj = payload_e2e = inputs = ranked = all_prs = None
            gc.collect()

        out["end_to_end_request"] = stats(request_samples)
        out["end_to_end_phases_mean_ms"] = {k: round(v / repeats * 1000, 1) for k, v in phase_totals.items()}
        out["end_to_end_note"] = (
            "one representative reviewer; walk is the full pool with no early stop at `limit`, i.e. the worst case"
        )

    out["peak_rss_mb"] = round(peak_rss_bytes() / 1_000_000, 1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--repo", default=None, help="restrict to owner/name")
    args = parser.parse_args()

    # Warm the connection so setup cost does not land in the first sample.
    with connection.cursor() as cur:
        cur.execute("SELECT 1")

    repos = Repository.objects.all().order_by("id")
    if args.repo:
        owner, _, name = args.repo.partition("/")
        repos = repos.filter(owner=owner, name=name)

    result = {
        "probe": "design-doc-053-payload-cost",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repeats": args.repeats,
        "python": sys.version.split()[0],
        "dyno": os.getenv("DYNO", ""),
        "repositories": [],
    }
    for repo in repos:
        if not QueueSnapshot.objects.filter(repository=repo).exists():
            continue
        log(f"measuring {repo.owner}/{repo.name} ...")
        try:
            result["repositories"].append(measure_repo(repo, repeats=args.repeats))
        except Exception as exc:  # noqa: BLE001 - a probe should report, not crash the run
            result["repositories"].append({"repository": f"{repo.owner}/{repo.name}", "error": f"{type(exc).__name__}: {exc}"})
            log(f"  failed: {type(exc).__name__}: {exc}")

    print(MARK_BEGIN)
    print(json.dumps(result, indent=2, default=str))
    print(MARK_END)


if __name__ == "__main__":
    main()
