-- Design doc 054 measure-first probe: what does reviewer *intake* actually look like?
--
-- Answers, against live production state and read-only:
--   1. Is `analyzer_reviewerassignmentapplication` a usable count source at all (volume, history)?
--   2. What is the trailing 7-day distinct-PR intake per reviewer *right now*?
--   3. What did the worst rolling 7-day week ever look like, per reviewer?  <- picks the limit
--   4. How many assignments would candidate limits have blocked historically?
--   5. Do the sharp edges the doc flags actually bite (login case, distinct-vs-rows, provenance)?
--
-- Read-only: SELECTs only, no dyno needed.
--
--   heroku pg:psql -a queueboard-backend -f scripts/probe_054_rate_limit.sql
--
-- Reviewer logins are pseudonymised (first 8 hex of md5) so the output can be pasted into the
-- design doc. To see real logins, change `\set show_logins 0` below to 1 -- `heroku pg:psql`
-- does not forward psql flags like `-v`, so the toggle has to be edited in the file.

\pset pager off
\set show_logins 0
\set window_days 7

-- Every section reads this one relation: applied rows only, normalised login, repo label.
-- `status='applied' AND applied_at IS NOT NULL` is exactly the population the proposed
-- `recent_assignment_counts()` service would count.
CREATE TEMP VIEW p054_applied AS
SELECT a.repository_id,
       r.owner || '/' || r.name                                   AS repo,
       lower(btrim(a.reviewer_login))                             AS login,
       CASE WHEN :show_logins = 1 THEN lower(btrim(a.reviewer_login))
            ELSE substr(md5(lower(btrim(a.reviewer_login))), 1, 8) END AS who,
       a.pr_number,
       a.applied_at,
       a.run_date,
       a.snapshot_id
FROM analyzer_reviewerassignmentapplication a
JOIN core_repository r ON r.id = a.repository_id
WHERE a.status = 'applied' AND a.applied_at IS NOT NULL;

\echo ''
\echo '=== 1. is the count source alive? (per repo, all statuses) ==='
SELECT r.owner || '/' || r.name AS repo,
       count(*)                                                        AS rows_all,
       count(*) FILTER (WHERE a.status = 'applied')                    AS applied,
       count(DISTINCT a.pr_number) FILTER (WHERE a.status = 'applied') AS distinct_prs,
       count(DISTINCT lower(btrim(a.reviewer_login)))
         FILTER (WHERE a.status = 'applied')                           AS distinct_reviewers,
       min(a.run_date)                                                 AS first_run_date,
       max(a.run_date)                                                 AS last_run_date,
       count(DISTINCT a.run_date) FILTER (WHERE a.status = 'applied')  AS days_with_intake
FROM analyzer_reviewerassignmentapplication a
JOIN core_repository r ON r.id = a.repository_id
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '--- 1b. status mix + recency (a thin/short history means the numbers below are weak) ---'
SELECT status,
       count(*)                                                          AS all_time,
       count(*) FILTER (WHERE created_at > now() - interval '30 days')   AS last_30d,
       count(*) FILTER (WHERE created_at > now() - interval '7 days')    AS last_7d
FROM analyzer_reviewerassignmentapplication
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '--- 1c. CORRECTNESS: applied rows with a NULL applied_at would be invisible to the gate ---'
SELECT count(*) AS applied_but_no_applied_at
FROM analyzer_reviewerassignmentapplication
WHERE status = 'applied' AND applied_at IS NULL;

\echo ''
\echo '=== 2. trailing 7-day intake per reviewer -- THE headline number ==='
\echo '--- distinct_prs is what the proposed gate counts; rows shows the re-assign inflation ---'
SELECT repo,
       who,
       count(DISTINCT pr_number) AS distinct_prs_7d,
       count(*)                  AS rows_7d,
       min(applied_at)::date     AS first_in_window,
       max(applied_at)::date     AS last_in_window
FROM p054_applied
WHERE applied_at > now() - (:window_days * interval '1 day')
GROUP BY 1, 2
ORDER BY 3 DESC, 1, 2;

\echo ''
\echo '--- 2b. same, 30-day view (context: is this week typical?) ---'
SELECT repo, who,
       count(DISTINCT pr_number)                                   AS distinct_prs_30d,
       count(DISTINCT run_date)                                    AS active_days_30d,
       round(count(DISTINCT pr_number) / 30.0 * 7, 1)              AS implied_per_week
FROM p054_applied
WHERE applied_at > now() - interval '30 days'
GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2;

\echo ''
\echo '=== 3. peak rolling 7-day window per reviewer, over all history ==='
\echo '--- max distinct PRs in ANY 7-day window: a limit below this would have bound ---'
WITH anchored AS (
    SELECT a.repo, a.who, a.login, a.repository_id, a.applied_at,
           (SELECT count(DISTINCT b.pr_number)
              FROM p054_applied b
             WHERE b.repository_id = a.repository_id
               AND b.login = a.login
               AND b.applied_at <= a.applied_at
               AND b.applied_at > a.applied_at - (:window_days * interval '1 day')) AS window_count
    FROM p054_applied a
)
SELECT repo, who,
       max(window_count)                       AS peak_7d,
       round(avg(window_count), 1)             AS avg_7d_when_active,
       count(DISTINCT applied_at::date)        AS active_days_all_time,
       max(applied_at)::date                   AS last_intake
FROM anchored
GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2;

\echo ''
\echo '--- 3b. distribution of that peak across reviewers (where a cap would start to bite) ---'
\echo '--- the gate allows exactly N per window, so a reviewer is blocked only when peak > N ---'
WITH anchored AS (
    SELECT a.login, a.repository_id,
           (SELECT count(DISTINCT b.pr_number)
              FROM p054_applied b
             WHERE b.repository_id = a.repository_id
               AND b.login = a.login
               AND b.applied_at <= a.applied_at
               AND b.applied_at > a.applied_at - (:window_days * interval '1 day')) AS window_count
    FROM p054_applied a
), peaks AS (
    SELECT repository_id, login, max(window_count) AS peak FROM anchored GROUP BY 1, 2
)
SELECT count(*)                                      AS reviewers_with_intake,
       min(peak)                                     AS min_peak,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY peak)::numeric, 1)  AS p50_peak,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY peak)::numeric, 1)  AS p90_peak,
       max(peak)                                     AS max_peak,
       count(*) FILTER (WHERE peak > 3)             AS would_block_at_3,
       count(*) FILTER (WHERE peak > 5)             AS would_block_at_5,
       count(*) FILTER (WHERE peak > 8)             AS would_block_at_8,
       count(*) FILTER (WHERE peak > 10)            AS would_block_at_10
FROM peaks;

\echo ''
\echo '--- 3c. same peak, but only reviewers active in the last 30 days ---'
\echo '--- anchors are recent; the window still counts across the full history, so no truncation ---'
WITH anchored AS (
    SELECT a.repository_id, a.login,
           (SELECT count(DISTINCT b.pr_number)
              FROM p054_applied b
             WHERE b.repository_id = a.repository_id
               AND b.login = a.login
               AND b.applied_at <= a.applied_at
               AND b.applied_at > a.applied_at - (:window_days * interval '1 day')) AS window_count
    FROM p054_applied a
    WHERE a.applied_at > now() - interval '30 days'
), peaks AS (
    SELECT repository_id, login, max(window_count) AS peak FROM anchored GROUP BY 1, 2
)
SELECT count(*)                                     AS reviewers_active_30d,
       min(peak)                                    AS min_peak,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY peak)::numeric, 1) AS p50_peak,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY peak)::numeric, 1) AS p90_peak,
       max(peak)                                    AS max_peak,
       count(*) FILTER (WHERE peak > 3)            AS would_block_at_3,
       count(*) FILTER (WHERE peak > 5)            AS would_block_at_5,
       count(*) FILTER (WHERE peak > 8)            AS would_block_at_8,
       count(*) FILTER (WHERE peak > 10)           AS would_block_at_10
FROM peaks;

\echo ''
\echo '=== 4. what-if: how much would candidate limits have withheld? (last 90 days) ==='
\echo '--- UPPER BOUND: blocking an assignment would also lower later window counts ---'
WITH prior AS (
    SELECT a.repo, a.login, a.applied_at,
           (SELECT count(DISTINCT b.pr_number)
              FROM p054_applied b
             WHERE b.repository_id = a.repository_id
               AND b.login = a.login
               AND b.applied_at < a.applied_at
               AND b.applied_at > a.applied_at - (:window_days * interval '1 day')
               AND b.pr_number <> a.pr_number) AS prior_distinct
    FROM p054_applied a
    WHERE a.applied_at > now() - interval '90 days'
)
SELECT l.lim                                                              AS limit_per_week,
       count(*)                                                           AS assignments_90d,
       count(*) FILTER (WHERE prior_distinct >= l.lim)                    AS would_be_blocked,
       round(100.0 * count(*) FILTER (WHERE prior_distinct >= l.lim) / nullif(count(*), 0), 1)
                                                                          AS pct_blocked,
       count(DISTINCT login) FILTER (WHERE prior_distinct >= l.lim)       AS reviewers_affected,
       count(DISTINCT login)                                              AS reviewers_total
FROM prior CROSS JOIN (VALUES (2), (3), (5), (8), (10), (15)) AS l(lim)
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '--- 4b. same replay, last 30 days only (the 90d view spans the apply rollout) ---'
\echo '--- anchors are recent; each window still counts across the full history ---'
WITH prior AS (
    SELECT a.login,
           (SELECT count(DISTINCT b.pr_number)
              FROM p054_applied b
             WHERE b.repository_id = a.repository_id
               AND b.login = a.login
               AND b.applied_at < a.applied_at
               AND b.applied_at > a.applied_at - (:window_days * interval '1 day')
               AND b.pr_number <> a.pr_number) AS prior_distinct
    FROM p054_applied a
    WHERE a.applied_at > now() - interval '30 days'
)
SELECT l.lim                                                              AS limit_per_week,
       count(*)                                                           AS assignments_30d,
       count(*) FILTER (WHERE prior_distinct >= l.lim)                    AS would_be_blocked,
       round(100.0 * count(*) FILTER (WHERE prior_distinct >= l.lim) / nullif(count(*), 0), 1)
                                                                          AS pct_blocked,
       count(DISTINCT login) FILTER (WHERE prior_distinct >= l.lim)       AS reviewers_affected,
       count(DISTINCT login)                                              AS reviewers_total
FROM prior CROSS JOIN (VALUES (2), (3), (5), (8), (10), (15)) AS l(lim)
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 5. supply side: reviewers with preferences vs reviewers who get any intake ==='
\echo '--- a limit only matters for reviewers the push actually reaches ---'
WITH intake AS (
    SELECT repository_id, login,
           count(DISTINCT pr_number) FILTER (WHERE applied_at > now() - (:window_days * interval '1 day')) AS d7,
           count(DISTINCT pr_number) FILTER (WHERE applied_at > now() - interval '30 days')                AS d30
    FROM p054_applied GROUP BY 1, 2
)
SELECT r.owner || '/' || r.name AS repo,
       count(*)                                              AS prefs,
       count(*) FILTER (WHERE p.auto_assign)                 AS auto_assign_on,
       count(*) FILTER (WHERE i.d30 > 0)                     AS got_intake_30d,
       count(*) FILTER (WHERE i.d7 > 0)                      AS got_intake_7d,
       round(avg(p.maximum_capacity), 1)                     AS avg_max_capacity,
       count(*) FILTER (WHERE p.assignment_acceptance = 'confirm') AS confirm_mode
FROM core_reviewerpreference p
JOIN core_repository r ON r.id = p.repository_id
LEFT JOIN core_user u ON u.id = p.user_id
LEFT JOIN intake i ON i.repository_id = p.repository_id
                  AND i.login = lower(btrim(coalesce(u.github_login, '')))
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '--- 5b. per-reviewer: concurrent cap vs actual weekly intake (which gate binds first) ---'
WITH intake AS (
    SELECT repository_id, login,
           count(DISTINCT pr_number) FILTER (WHERE applied_at > now() - (:window_days * interval '1 day')) AS d7,
           count(DISTINCT pr_number) FILTER (WHERE applied_at > now() - interval '30 days')                AS d30
    FROM p054_applied GROUP BY 1, 2
)
SELECT r.owner || '/' || r.name AS repo,
       CASE WHEN :show_logins = 1 THEN lower(btrim(u.github_login))
            ELSE substr(md5(lower(btrim(coalesce(u.github_login, '')))), 1, 8) END AS who,
       p.maximum_capacity,
       p.auto_assign,
       p.assignment_acceptance,
       (p.away_until IS NOT NULL AND p.away_until > now())  AS away_now,
       coalesce(i.d7, 0)                                    AS intake_7d,
       coalesce(i.d30, 0)                                   AS intake_30d
FROM core_reviewerpreference p
JOIN core_repository r ON r.id = p.repository_id
LEFT JOIN core_user u ON u.id = p.user_id
LEFT JOIN intake i ON i.repository_id = p.repository_id
                  AND i.login = lower(btrim(coalesce(u.github_login, '')))
WHERE coalesce(i.d30, 0) > 0
ORDER BY coalesce(i.d30, 0) DESC, 1
LIMIT 60;

\echo ''
\echo '=== 6. SHARP EDGE -- login case: is reviewer_login stored normalised? ==='
\echo '--- assign_reviewer_and_record stores the login verbatim; a case mismatch UNDERCOUNTS ---'
SELECT count(*)                                                             AS applied_rows,
       count(*) FILTER (WHERE reviewer_login <> lower(reviewer_login))      AS not_lowercase,
       count(*) FILTER (WHERE reviewer_login <> btrim(reviewer_login))      AS has_whitespace,
       count(DISTINCT reviewer_login)                                       AS distinct_raw,
       count(DISTINCT lower(btrim(reviewer_login)))                         AS distinct_normalised
FROM analyzer_reviewerassignmentapplication
WHERE status = 'applied';

\echo ''
\echo '--- 6b. logins stored under more than one spelling (each row = a split count) ---'
SELECT lower(btrim(reviewer_login))            AS normalised,
       count(DISTINCT reviewer_login)          AS spellings,
       string_agg(DISTINCT reviewer_login, ' | ') AS variants
FROM analyzer_reviewerassignmentapplication
WHERE status = 'applied'
GROUP BY 1 HAVING count(DISTINCT reviewer_login) > 1
ORDER BY 2 DESC;

\echo ''
\echo '--- 6c. applied logins that do not match any core_user.github_login (case-insensitive) ---'
SELECT a.who, count(*) AS applied_rows, max(a.applied_at)::date AS last_seen
FROM p054_applied a
LEFT JOIN core_user u ON lower(btrim(coalesce(u.github_login, ''))) = a.login
WHERE u.id IS NULL
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '--- 6d. how many REVIEWERS (not rows) carry a capitalized spelling? ---'
\echo '--- a case-sensitive query filter would silently exempt exactly these from the gate ---'
\echo '--- (spellings == reviewers only while 6b is empty; check that first) ---'
SELECT count(*)                                                AS spellings_total,
       count(*) FILTER (WHERE spelling <> lower(spelling))     AS spellings_capitalized,
       coalesce(sum(n) FILTER (WHERE spelling <> lower(spelling)), 0) AS their_applied_rows
FROM (
    SELECT reviewer_login AS spelling, count(*) AS n
    FROM analyzer_reviewerassignmentapplication
    WHERE status = 'applied'
    GROUP BY 1
) t;

\echo ''
\echo '=== 7. SHARP EDGE -- distinct PRs vs rows: how much re-assignment churn is there? ==='
\echo '--- PRs with >1 applied row for the same reviewer; row-counting would double-count these ---'
SELECT count(*)                                              AS pr_reviewer_pairs,
       count(*) FILTER (WHERE n > 1)                         AS pairs_reassigned,
       coalesce(sum(n - 1), 0)                               AS extra_rows_distinct_avoids,
       max(n)                                                AS max_rows_for_one_pair
FROM (
    SELECT repository_id, login, pr_number, count(*) AS n
    FROM p054_applied GROUP BY 1, 2, 3
) t;

\echo ''
\echo '=== 8. provenance proxy: on-demand claims (053) vs nightly/confirm intake ==='
\echo '--- snapshot_id IS NULL == console pull-claim (053 passes snapshot=None). Open Question 4 ---'
SELECT repo,
       count(*)                                        AS applied_30d,
       count(*) FILTER (WHERE snapshot_id IS NULL)     AS claim_like,
       count(*) FILTER (WHERE snapshot_id IS NOT NULL) AS snapshot_anchored,
       count(DISTINCT login) FILTER (WHERE snapshot_id IS NULL) AS claiming_reviewers
FROM p054_applied
WHERE applied_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '--- 8b. per reviewer, last 30 days (would excluding claims change anyone materially?) ---'
SELECT repo, who,
       count(DISTINCT pr_number)                                                 AS distinct_prs_30d,
       count(DISTINCT pr_number) FILTER (WHERE snapshot_id IS NULL)              AS claim_like,
       count(DISTINCT pr_number) FILTER (WHERE snapshot_id IS NOT NULL)          AS snapshot_anchored
FROM p054_applied
WHERE applied_at > now() - interval '30 days'
GROUP BY 1, 2
HAVING count(*) FILTER (WHERE snapshot_id IS NULL) > 0
ORDER BY 4 DESC, 3 DESC;

\echo ''
\echo '=== 9. per-day intake, last 21 days (burstiness: is a single night already clustered?) ==='
SELECT run_date,
       count(DISTINCT pr_number)      AS distinct_prs,
       count(*)                       AS rows_total,
       count(DISTINCT login)          AS reviewers,
       round(count(DISTINCT pr_number)::numeric / nullif(count(DISTINCT login), 0), 1) AS prs_per_reviewer
FROM p054_applied
WHERE run_date > current_date - 21
GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '--- 9b. worst single day per reviewer (Subtlety 6: one run may fill the weekly budget) ---'
SELECT repo, who, run_date, count(DISTINCT pr_number) AS prs_that_day
FROM p054_applied
GROUP BY 1, 2, 3
ORDER BY 4 DESC, 3 DESC
LIMIT 15;

DROP VIEW p054_applied;
