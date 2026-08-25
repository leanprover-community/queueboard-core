#!/usr/bin/env python
"""Read-only probe for design doc 053 (on-demand assignment suggestions).

Answers, against live production state: *how many suggestions would this feature actually
have to make?* — plus the funnel and the skip-reason mix that explain the number.

Strictly read-only: no DB writes, no GitHub calls, no snapshot builds. It reads the latest
cached QueueSnapshot exactly the way `analyzer.services.reviewer_load` does, reuses
`_prepare_assignment_inputs` for the candidate pool, and drives the real engine
(`suggest_reviewer_for_pr_with_trace`) for every (reviewer, PR) pair.

Reviewer logins are pseudonymized by default (r01, r02, ... ordered by a salt-free sha256 of
the login) so the output can be shared; pass --no-anon to keep real logins. PR numbers and
label names are public and are kept as-is. Author logins are never emitted.

Emits one JSON object on stdout between BEGIN/END markers.

Usage on the dyno:
    PYTHONPATH=$PWD/qb_site:$PWD python probe_053_suggestions.py [--no-anon] [--repo owner/name]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qb_site.settings.production")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from analyzer.models import (  # noqa: E402
    AssignmentProposal,
    QueueSnapshot,
    ReviewerAssignmentApplication,
    ReviewerAssignmentSnapshot,
    ReviewerOptOut,
)
from analyzer.services.queue_rules import default_rule_set_for_repo  # noqa: E402
from analyzer.services.reviewer_assignment import (  # noqa: E402
    _active_proposal_rows,
    _assignment_forbidden_labels,
    _filter_assignment_forbidden_prs,
    _filter_prs_without_active_assignee,
    _filter_prs_without_active_proposal,
    _prepare_assignment_inputs,
    build_reviewer_catalog,
)
from analyzer.services.reviewer_assignment_engine import (  # noqa: E402
    _normalize_login,
    _topic_labels,
    rank_prs_for_assignment,
    suggest_reviewer_for_pr_with_trace,
)
from analyzer.services.reviewer_load import build_reviewer_loads  # noqa: E402
from core.models import Repository, ReviewerPreference  # noqa: E402
from core.services.topic_labels import topic_label_matcher_for_repo  # noqa: E402

MARK_BEGIN = "===QB-PROBE-053-JSON-BEGIN==="
MARK_END = "===QB-PROBE-053-JSON-END==="


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def hist(values) -> dict:
    """Counter as a plain dict with string keys, sorted numerically where possible."""
    counter = Counter(values)
    try:
        keys = sorted(counter, key=lambda k: (k is None, k))
    except TypeError:
        keys = sorted(counter, key=str)
    return {str(k): counter[k] for k in keys}


def buckets(values) -> dict:
    """0 / 1 / 2 / 3 / 4-5 / 6-10 / 11-25 / 26+ bucketing for suggestion counts."""
    out = Counter()
    for v in values:
        if v <= 3:
            out[str(v)] += 1
        elif v <= 5:
            out["4-5"] += 1
        elif v <= 10:
            out["6-10"] += 1
        elif v <= 25:
            out["11-25"] += 1
        else:
            out["26+"] += 1
    order = ["0", "1", "2", "3", "4-5", "6-10", "11-25", "26+"]
    return {k: out[k] for k in order if out[k]}


def quantiles(values) -> dict:
    vals = sorted(values)
    if not vals:
        return {}

    def q(p: float):
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[idx]

    return {
        "n": len(vals),
        "min": vals[0],
        "p25": q(0.25),
        "median": q(0.5),
        "p75": q(0.75),
        "p90": q(0.9),
        "max": vals[-1],
        "mean": round(sum(vals) / len(vals), 2),
    }


class Anonymizer:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._map: dict[str, str] = {}

    def seed(self, logins) -> None:
        ordered = sorted({_normalize_login(x) for x in logins if x}, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
        for i, login in enumerate(ordered, start=1):
            self._map[login] = f"r{i:02d}"

    def __call__(self, login: str | None) -> str:
        norm = _normalize_login(login)
        if not self.enabled:
            return norm
        return self._map.get(norm, "r??")


def classify(
    *,
    pr_entry: dict,
    me,
    others,
    excluded_for_pr: set[str],
    assignments,
    matcher,
    respect_availability: bool,
) -> str:
    """Re-derive the design-doc-053 skip tally for one (reviewer, PR) pair.

    Mirrors `_reviewer_candidate_state` step for step and in its order, so the reason names map
    onto the table in the design doc. Returns "eligible" when the reviewer would be offered the PR.
    """
    labels_lower = [lab.lower() for lab in _topic_labels(pr_entry, matcher)]
    if not labels_lower:
        return "no_topic_label"

    author_norm = _normalize_login(pr_entry.get("author") or "")
    my_login = _normalize_login(me.github_login)
    if author_norm == my_login:
        return "authored"
    if author_norm in me.conflict_of_interest_lower:
        return "conflict_of_interest"

    my_match = [lab for lab in labels_lower if lab in me.preferred_labels_lower]
    if not my_match:
        return "no_area_match"

    # max_score contest, over every reviewer the engine would consider "matching" for this PR
    # (conflict-of-interest filtered, availability NOT filtered — same as the engine).
    max_score = len(my_match)
    for rev in others:
        if author_norm in {rev.github_login.lower(), *rev.conflict_of_interest_lower}:
            continue
        score = sum(1 for lab in labels_lower if lab in rev.preferred_labels_lower)
        if score > max_score:
            max_score = score
    if max_score > 1 and len(my_match) < max_score:
        return "outranked"

    if my_login in excluded_for_pr:
        return "excluded"

    current_weight = float(assignments.get(me.github_login, ([], 0.0, 0))[1])
    if me.maximum_capacity - current_weight <= 0:
        return "at_capacity"

    if respect_availability:
        if not me.auto_assign:
            return "auto_assign_off"
        if me.temporary_break:
            return "away"

    return "eligible"


def run_pass(
    *,
    label: str,
    reviewers,
    profile_for,
    ranked_prs,
    all_prs,
    excluded_by_pr,
    assignments,
    matcher,
    respect_availability: bool,
    limit_probe: int,
    verify_against_engine: bool,
    anon: Anonymizer,
) -> dict:
    """One what-if pass over every reviewer.

    `profile_for(profile)` returns the (possibly overridden) profile the engine should see for
    that reviewer as the *requester*; every other reviewer keeps their real profile, exactly as
    design doc 053 specifies ("replace it, in place, with a dataclasses.replace copy").
    """
    per_reviewer = {}
    reason_totals = Counter()
    mismatches = 0
    rank_of_first_hit = []

    for me_real in reviewers:
        me = profile_for(me_real)
        my_login = _normalize_login(me.github_login)
        catalog = [me if _normalize_login(r.github_login) == my_login else r for r in reviewers]
        others = [r for r in catalog if _normalize_login(r.github_login) != my_login]

        reasons = Counter()
        eligible_prs = []
        first_hit_rank = None

        for rank, pr_number in enumerate(ranked_prs):
            pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number)) or {}
            excluded_for_pr = {_normalize_login(x) for x in excluded_by_pr.get(pr_number, set())}
            reason = classify(
                pr_entry=pr_entry,
                me=me,
                others=others,
                excluded_for_pr=excluded_for_pr,
                assignments=assignments,
                matcher=matcher,
                respect_availability=respect_availability,
            )
            reasons[reason] += 1
            if reason == "eligible":
                eligible_prs.append(pr_number)
                if first_hit_rank is None:
                    first_hit_rank = rank

            if verify_against_engine:
                _result, trace = suggest_reviewer_for_pr_with_trace(
                    pr_entry=pr_entry,
                    reviewers=catalog,
                    assignment_stats=assignments,
                    excluded_logins=excluded_by_pr.get(pr_number, set()),
                    topic_label_matcher=matcher,
                )
                in_available = my_login in {_normalize_login(x) for x in trace.get("available", [])}
                if in_available != (reason == "eligible"):
                    mismatches += 1

        if first_hit_rank is not None:
            rank_of_first_hit.append(first_hit_rank)
        reason_totals.update(reasons)
        per_reviewer[anon(me.github_login)] = {
            "eligible": len(eligible_prs),
            "would_show": min(len(eligible_prs), limit_probe),
            "top_prs": eligible_prs[:limit_probe],
            "reasons": dict(reasons),
        }

    counts = [v["eligible"] for v in per_reviewer.values()]
    return {
        "pass": label,
        "reviewers": len(per_reviewer),
        "eligible_counts": quantiles(counts),
        "eligible_buckets": buckets(counts),
        "reviewers_with_zero": sum(1 for c in counts if c == 0),
        "reviewers_with_at_least_limit": sum(1 for c in counts if c >= limit_probe),
        "reason_totals": dict(reason_totals.most_common()),
        "rank_of_first_eligible_pr": quantiles(rank_of_first_hit),
        "engine_mismatches": mismatches if verify_against_engine else None,
        "per_reviewer": per_reviewer,
    }


def probe_repo(repository: Repository, *, anon: Anonymizer, limit_probe: int, now: datetime) -> dict:
    out: dict = {"repository": f"{repository.owner}/{repository.name}", "repository_id": repository.id}

    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snap = QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()
    if snap is None or not snap.payload:
        out["status"] = "no_snapshot"
        return out

    t0 = time.perf_counter()
    payload = snap.payload
    payload_read_seconds = round(time.perf_counter() - t0, 3)
    payload_bytes = len(json.dumps(payload, separators=(",", ":")))

    out["snapshot"] = {
        "cache_key": cache_key,
        "generated_at": snap.generated_at.isoformat(),
        "age_hours": round((now - snap.generated_at).total_seconds() / 3600, 2),
        "pr_count": snap.pr_count,
        "queue_count": snap.queue_count,
        "payload_bytes": payload_bytes,
        "payload_mb": round(payload_bytes / 1_000_000, 2),
        "payload_read_seconds": payload_read_seconds,
    }

    matcher = topic_label_matcher_for_repo(repository)
    all_prs = payload.get("prs", {})
    queue_prs = list(payload.get("lists", {}).get("dashboards", {}).get("Queue", []))

    # --- funnel: reproduce _prepare_assignment_inputs step by step for the counts -------------
    reviewers = build_reviewer_catalog(repository, now=now)
    after_assignee = _filter_prs_without_active_assignee(queue_prs, all_prs=all_prs, reviewers=reviewers)
    forbidden = _assignment_forbidden_labels(repository, rule_set=rule_set)
    after_forbidden = _filter_assignment_forbidden_prs(after_assignee, all_prs=all_prs, forbidden_labels=forbidden)
    active_rows = _active_proposal_rows(repository)
    after_proposal = _filter_prs_without_active_proposal(after_forbidden, prs_with_active_proposal={n for n, _ in active_rows})

    inputs = _prepare_assignment_inputs(repository, payload=payload, now=now, rule_set=rule_set)
    pool = inputs.assignable_queue_prs

    no_topic_label = sum(1 for n in pool if not _topic_labels(all_prs.get(n) or all_prs.get(str(n)) or {}, matcher))
    out["funnel"] = {
        "queue_prs": len(queue_prs),
        "after_active_assignee_filter": len(after_assignee),
        "dropped_has_active_assignee": len(queue_prs) - len(after_assignee),
        "after_forbidden_label_filter": len(after_forbidden),
        "dropped_forbidden_label": len(after_assignee) - len(after_forbidden),
        "forbidden_labels": sorted(forbidden),
        "after_active_proposal_filter": len(after_proposal),
        "dropped_active_proposal": len(after_forbidden) - len(after_proposal),
        "assignable_pool": len(pool),
        "pool_without_topic_label": no_topic_label,
        "pool_with_topic_label": len(pool) - no_topic_label,
        "prs_with_per_pr_exclusions": sum(1 for n in pool if inputs.excluded_by_pr.get(n)),
        "total_per_pr_exclusions": sum(len(v) for v in inputs.excluded_by_pr.values()),
    }

    # --- reviewer population ------------------------------------------------------------------
    loads = build_reviewer_loads(repository, snapshot_payload=payload, now=now)
    prefs = list(ReviewerPreference.objects.filter(repository=repository).select_related("user"))
    anon.seed([r.github_login for r in reviewers])

    out["reviewers"] = {
        "preference_rows": len(prefs),
        "with_github_login": len(reviewers),
        "auto_assign_off": sum(1 for r in reviewers if not r.auto_assign),
        "away_now": sum(1 for r in reviewers if r.temporary_break),
        "unavailable_either_way": sum(1 for r in reviewers if not r.auto_assign or r.temporary_break),
        "no_preferred_labels": sum(1 for r in reviewers if not r.preferred_labels_lower),
        "with_conflicts": sum(1 for r in reviewers if r.conflict_of_interest_lower),
        "acceptance_mode": hist(p.assignment_acceptance for p in prefs),
        "zulip_linked": sum(1 for p in prefs if getattr(p.user, "zulip_user_id", None) is not None),
        "preferred_label_count": quantiles([len(r.preferred_labels_lower) for r in reviewers]),
        "maximum_capacity": quantiles([r.maximum_capacity for r in reviewers]),
        "current_load": quantiles([round(v.current_load, 2) for v in loads.values()]),
        "at_capacity": sum(1 for v in loads.values() if v.at_capacity),
        "remaining_capacity": quantiles([round(v.remaining, 2) for v in loads.values()]),
    }

    # --- label supply/demand ------------------------------------------------------------------
    pool_label_counts = Counter()
    for n in pool:
        for lab in _topic_labels(all_prs.get(n) or all_prs.get(str(n)) or {}, matcher):
            pool_label_counts[lab.lower()] += 1
    reviewers_per_label = Counter()
    for r in reviewers:
        for lab in r.preferred_labels_lower:
            reviewers_per_label[lab] += 1
    out["labels"] = {
        "distinct_topic_labels_in_pool": len(pool_label_counts),
        "topic_labels_per_pool_pr": quantiles(
            [len(_topic_labels(all_prs.get(n) or all_prs.get(str(n)) or {}, matcher)) for n in pool]
        ),
        "pool_prs_by_label": dict(pool_label_counts.most_common(40)),
        "reviewers_by_label": {lab: reviewers_per_label.get(lab, 0) for lab, _ in pool_label_counts.most_common(40)},
        "pool_labels_with_no_interested_reviewer": sorted(lab for lab in pool_label_counts if not reviewers_per_label.get(lab)),
    }

    # --- rank the pool once, exactly as the design says the service would ---------------------
    t0 = time.perf_counter()
    ranked_prs, _ranking_trace = rank_prs_for_assignment(
        prs_to_assign=pool,
        all_prs=all_prs,
        reviewers=inputs.reviewers,
        assignment_stats=inputs.assignments,
        excluded_by_pr=inputs.excluded_by_pr,
        topic_label_matcher=matcher,
    )
    rank_seconds = round(time.perf_counter() - t0, 3)

    all_pool_labels = set(pool_label_counts)

    passes = []
    common = dict(
        reviewers=inputs.reviewers,
        ranked_prs=ranked_prs,
        all_prs=all_prs,
        excluded_by_pr=inputs.excluded_by_pr,
        assignments=inputs.assignments,
        matcher=matcher,
        limit_probe=limit_probe,
        anon=anon,
    )

    # A. baseline: the reviewer exactly as they are (what the nightly engine sees)
    passes.append(
        run_pass(
            label="A_as_is",
            profile_for=lambda p: p,
            respect_availability=True,
            verify_against_engine=True,
            **common,
        )
    )
    # B. design 053 default: availability overridden, own labels, own capacity
    passes.append(
        run_pass(
            label="B_availability_override",
            profile_for=lambda p: replace(p, auto_assign=True, temporary_break=False),
            respect_availability=False,
            verify_against_engine=False,
            **common,
        )
    )
    # C. + allow_over_capacity
    passes.append(
        run_pass(
            label="C_over_capacity",
            profile_for=lambda p: replace(p, auto_assign=True, temporary_break=False, maximum_capacity=10**6),
            respect_availability=False,
            verify_against_engine=False,
            **common,
        )
    )
    # D. + broad label override ("give me anything"): every topic label present in the pool
    passes.append(
        run_pass(
            label="D_all_labels_override",
            profile_for=lambda p: replace(
                p,
                auto_assign=True,
                temporary_break=False,
                preferred_labels=sorted(all_pool_labels),
                preferred_labels_lower=set(all_pool_labels),
            ),
            respect_availability=False,
            verify_against_engine=False,
            **common,
        )
    )
    out["passes"] = passes

    # --- what the nightly run would place, for comparison -------------------------------------
    ras = ReviewerAssignmentSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()
    if ras:
        auto = (ras.payload or {}).get("automatic_assignments", {}) or {}
        per_login = Counter(_normalize_login(v) for v in auto.values())
        out["nightly"] = {
            "generated_at": ras.generated_at.isoformat(),
            "age_hours": round((now - ras.generated_at).total_seconds() / 3600, 2),
            "assignment_count": ras.assignment_count,
            "pool_size_now": len(pool),
            "unplaceable_now": len(pool) - len(auto),
            "apply_cap_per_repo": int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", 25)),
            "over_cap_backlog": max(0, len(auto) - int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", 25))),
            "prs_per_reviewer": quantiles(list(per_login.values())),
            "reviewers_receiving_any": len(per_login),
        }
    else:
        out["nightly"] = {"status": "no_snapshot"}

    # --- proposal / application history: is push-based assignment landing? --------------------
    props = AssignmentProposal.objects.filter(repository=repository)
    since_30 = now - timedelta(days=30)
    out["history"] = {
        "proposals_all_time": hist(props.values_list("state", flat=True)),
        "proposals_last_30d": hist(props.filter(created_at__gte=since_30).values_list("state", flat=True)),
        "proposals_active_now": props.filter(state=AssignmentProposal.STATE_PROPOSED).count(),
        "applications_all_time": hist(
            ReviewerAssignmentApplication.objects.filter(repository=repository).values_list("status", flat=True)
        ),
        "applications_last_30d": hist(
            ReviewerAssignmentApplication.objects.filter(repository=repository, created_at__gte=since_30).values_list(
                "status", flat=True
            )
        ),
        "opt_outs_active": ReviewerOptOut.objects.filter(repository=repository, active=True).count(),
        "opt_outs_all_time": ReviewerOptOut.objects.filter(repository=repository).count(),
    }

    # --- cost of one interactive request -------------------------------------------------------
    if reviewers and pool:
        probe_reviewer = reviewers[0]
        me = replace(probe_reviewer, auto_assign=True, temporary_break=False)
        catalog = [me if _normalize_login(r.github_login) == _normalize_login(me.github_login) else r for r in inputs.reviewers]
        t0 = time.perf_counter()
        for n in ranked_prs:
            suggest_reviewer_for_pr_with_trace(
                pr_entry=all_prs.get(n) or all_prs.get(str(n)) or {},
                reviewers=catalog,
                assignment_stats=inputs.assignments,
                excluded_logins=inputs.excluded_by_pr.get(n, set()),
                topic_label_matcher=matcher,
            )
        walk_seconds = round(time.perf_counter() - t0, 3)
        out["cost"] = {
            "payload_read_seconds": payload_read_seconds,
            "rank_seconds": rank_seconds,
            "full_walk_seconds": walk_seconds,
            "estimated_request_seconds": round(payload_read_seconds + rank_seconds + walk_seconds, 3),
            "note": "walk is the worst case (no early stop at limit); the service stops at `limit` hits",
        }

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-anon", action="store_true", help="emit real reviewer logins")
    parser.add_argument("--repo", default=None, help="restrict to owner/name")
    parser.add_argument("--limit", type=int, default=10, help="the ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT to probe")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    anon = Anonymizer(enabled=not args.no_anon)

    repos = Repository.objects.all().order_by("id")
    if args.repo:
        owner, _, name = args.repo.partition("/")
        repos = repos.filter(owner=owner, name=name)

    result = {
        "probe": "design-doc-053-on-demand-assignment-suggestions",
        "generated_at": now.isoformat(),
        "anonymized": anon.enabled,
        "limit_probed": args.limit,
        "settings": {
            "ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED": getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED", None),
            "ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN": getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN", None),
            "ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED": getattr(
                settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED", None
            ),
            "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED": getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED", None),
            "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN": getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN", None),
            "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO": getattr(
                settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", None
            ),
            "ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS": getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS", None),
            "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT": getattr(
                settings, "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", None
            ),
            "ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS": getattr(
                settings, "ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS", None
            ),
        },
        "repositories": [],
    }

    for repo in repos:
        if not ReviewerPreference.objects.filter(repository=repo).exists():
            continue
        log(f"probing {repo.owner}/{repo.name} ...")
        try:
            result["repositories"].append(probe_repo(repo, anon=anon, limit_probe=args.limit, now=now))
        except Exception as exc:  # noqa: BLE001 - a probe should report, not crash the run
            result["repositories"].append({"repository": f"{repo.owner}/{repo.name}", "error": f"{type(exc).__name__}: {exc}"})
            log(f"  failed: {type(exc).__name__}: {exc}")

    print(MARK_BEGIN)
    print(json.dumps(result, indent=2, default=str))
    print(MARK_END)


if __name__ == "__main__":
    main()
