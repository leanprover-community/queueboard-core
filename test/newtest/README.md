Synthetic fixtures for running queueboard.dashboard_data locally/CI from this directory.

Included files:
- `all-open-PRs-test.json`: minimal GraphQL-like search output with two PRs (101, 102).
- `queue.json`: minimal queue (#queue) result containing the same two PRs.
- `processed_data/open_pr_data.json`: aggregate per-PR data for PRs 101 and 102.
- `processed_data/assignment_data.json`: minimal reviewer assignment stats.
- `reviewer-topics.json`: two synthetic reviewers with areas matching the PR labels.

Run from this directory:
- `uv run python -m queueboard.dashboard_data "all-open-PRs-test.json"`
- Outputs are written to `api/` in this directory.
- `uv run python -m queueboard.dashboard"`
- HTML pages are written to `gh-pages/` in this directory.
