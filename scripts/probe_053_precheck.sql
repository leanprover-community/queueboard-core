-- Design doc 053 pre-check: is there anything to suggest, and is push-based assignment landing?
-- Read-only. Run with:  heroku pg:psql -a <APP> -f scripts/probe_053_precheck.sql
\pset pager off
\echo '=== 1. queue size / snapshot freshness ==='
SELECT r.owner || '/' || r.name AS repo,
       q.cache_key,
       q.pr_count,
       q.queue_count,
       q.generated_at,
       round(extract(epoch FROM (now() - q.generated_at)) / 3600.0, 1) AS age_hours,
       pg_size_pretty(pg_column_size(q.payload)::bigint) AS payload_size
FROM analyzer_queuesnapshot q
JOIN core_repository r ON r.id = q.repository_id
ORDER BY q.generated_at DESC;

\echo ''
\echo '=== 2. what the nightly engine last placed (assignment_count) vs the queue ==='
SELECT r.owner || '/' || r.name AS repo,
       s.cache_key,
       s.assignment_count,
       s.generated_at,
       round(extract(epoch FROM (now() - s.generated_at)) / 3600.0, 1) AS age_hours
FROM analyzer_reviewerassignmentsnapshot s
JOIN core_repository r ON r.id = s.repository_id
ORDER BY s.generated_at DESC;

\echo ''
\echo '=== 3. reviewer population (the supply side) ==='
SELECT r.owner || '/' || r.name AS repo,
       count(*) AS prefs,
       count(*) FILTER (WHERE u.github_login IS NOT NULL AND u.github_login <> '') AS with_login,
       count(*) FILTER (WHERE NOT p.auto_assign) AS auto_assign_off,
       count(*) FILTER (WHERE p.away_until IS NOT NULL AND p.away_until > now()) AS away_now,
       count(*) FILTER (WHERE jsonb_array_length(p.preferred_labels) = 0) AS no_preferred_labels,
       count(*) FILTER (WHERE jsonb_array_length(p.conflict_of_interest) > 0) AS with_conflicts,
       count(*) FILTER (WHERE u.zulip_user_id IS NOT NULL) AS zulip_linked,
       round(avg(p.maximum_capacity), 1) AS avg_capacity,
       round(avg(jsonb_array_length(p.preferred_labels)), 1) AS avg_labels,
       min(jsonb_array_length(p.preferred_labels)) AS min_labels,
       max(jsonb_array_length(p.preferred_labels)) AS max_labels
FROM core_reviewerpreference p
JOIN core_repository r ON r.id = p.repository_id
LEFT JOIN core_user u ON u.id = p.user_id
GROUP BY 1
ORDER BY 1;

\echo ''
\echo '=== 4. acceptance mode mix ==='
SELECT r.owner || '/' || r.name AS repo, p.assignment_acceptance, count(*)
FROM core_reviewerpreference p
JOIN core_repository r ON r.id = p.repository_id
GROUP BY 1, 2 ORDER BY 1, 3 DESC;

\echo ''
\echo '=== 5. label demand: how many reviewers claim each preferred label ==='
SELECT lower(lab.value #>> '{}') AS label, count(*) AS reviewers
FROM core_reviewerpreference p, jsonb_array_elements(p.preferred_labels) AS lab
GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 60;

\echo ''
\echo '=== 6. proposal lifecycle: does push-based assignment get accepted? ==='
SELECT state,
       count(*) AS all_time,
       count(*) FILTER (WHERE created_at > now() - interval '30 days') AS last_30d,
       count(*) FILTER (WHERE created_at > now() - interval '7 days') AS last_7d
FROM analyzer_assignmentproposal
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 6b. time-to-decision for decided proposals ==='
SELECT state,
       count(*) AS n,
       round(avg(extract(epoch FROM (decided_at - created_at)) / 3600.0)::numeric, 1) AS avg_hours,
       round(max(extract(epoch FROM (decided_at - created_at)) / 3600.0)::numeric, 1) AS max_hours
FROM analyzer_assignmentproposal
WHERE decided_at IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 7. assignment applications actually written ==='
SELECT status,
       count(*) AS all_time,
       count(*) FILTER (WHERE run_date > current_date - 30) AS last_30d,
       count(DISTINCT run_date) FILTER (WHERE run_date > current_date - 30) AS run_days_30d
FROM analyzer_reviewerassignmentapplication
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 7b. per-day application volume, last 21 days ==='
SELECT run_date, count(*) AS applications, count(DISTINCT reviewer_login) AS reviewers
FROM analyzer_reviewerassignmentapplication
WHERE run_date > current_date - 21
GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '=== 8. opt-outs (a proxy for "the push picked wrong") ==='
SELECT count(*) FILTER (WHERE active) AS active_opt_outs,
       count(*) AS all_time,
       count(*) FILTER (WHERE opted_out_at > now() - interval '30 days') AS last_30d
FROM analyzer_revieweroptout;
