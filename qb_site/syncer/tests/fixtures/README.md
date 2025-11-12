# Syncer test fixtures (real GraphQL snapshots)

This folder holds optional real GitHub GraphQL snapshots used by smoke tests. The unit/integration tests work without them; tests that rely on these files will skip gracefully if a file is missing.

Prerequisites
- Install/authorize GitHub CLI: `gh auth status`
- Ensure a token is available via `GH_TOKEN` or `GITHUB_TOKEN` (or be logged in via `gh`)

Recommended environment vars
```
export OWNER="leanprover-community"
export NAME="mathlib4"
export PR=12345   # replace with a target PR number
```

1) Force‑push bundle (used by `test_real_bundle_smoke_forcepush`)
- Save full bundle (no minimization) so we can inspect later:
```
gh api graphql \
  -F query=@qb_site/syncer/queries/pr_bundle.graphql \
  -F owner="$OWNER" -F name="$NAME" -F number=$PR \
  -F timelineK=250 -F commitsM=20 \
  > qb_site/syncer/tests/fixtures/pr_bundle_real_forcepush.json
```
- Pick a PR that has `HeadRefForcePushedEvent` in its timeline. The smoke test asserts at least one persisted force‑push event.

2) Paging fixtures (timeline/commits)
- To exercise paging against real data, capture a bundle with tiny windows (K=1, M=1), then fetch the next pages.

Capture initial bundle with small K/M:
```
gh api graphql \
  -F query=@qb_site/syncer/queries/pr_bundle.graphql \
  -F owner="$OWNER" -F name="$NAME" -F number=$PR \
  -F timelineK=1 -F commitsM=1 \
  > qb_site/syncer/tests/fixtures/pr_bundle_smallK.json
```

Extract cursors:
```
TL_AFTER=$(jq -r '.data.repository.pullRequest.timelineItems.pageInfo.endCursor' qb_site/syncer/tests/fixtures/pr_bundle_smallK.json)
CM_BEFORE=$(jq -r '.data.repository.pullRequest.commits.pageInfo.startCursor' qb_site/syncer/tests/fixtures/pr_bundle_smallK.json)
```

Fetch next timeline page (older/newer depending on API; we only need the next page):
```
gh api graphql \
  -F query=@qb_site/syncer/queries/timeline_page.graphql \
  -F owner="$OWNER" -F name="$NAME" -F number=$PR \
  -F first=100 -F after="$TL_AFTER" \
  > qb_site/syncer/tests/fixtures/timeline_page_after.json
```

Fetch next commits page (older commits):
```
gh api graphql \
  -F query=@qb_site/syncer/queries/commits_page.graphql \
  -F owner="$OWNER" -F name="$NAME" -F number=$PR \
  -F last=100 -F before="$CM_BEFORE" \
  > qb_site/syncer/tests/fixtures/commits_page_before.json
```

Notes
- Keep filenames exactly as shown if you want tests to pick them up without changes.
- After adding fixtures, run tests inside Compose:
```
docker compose exec -T web python qb_site/manage.py test syncer
```
- Or use `scripts/repo_check_compose.sh`
