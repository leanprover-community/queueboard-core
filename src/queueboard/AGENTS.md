# Repository Guidelines

## Module Focus & Layout
- `dashboard.py` renders the review and triage HTML; helpers such as `_compute_pr_entries` and `_write_table_row` drive most layout tweaks.
- `process.py` and `compute_dashboard_prs.py` extract metadata from downloaded JSON, with shared data classes in `dashboard_data.py` and utilities in `util.py`.
- `queries/` contains GraphQL payload templates used by the sync scripts in the sibling `queueboard` repo; adjust these alongside processing logic.
- `static/` bundles CSS/JS referenced by the generated HTML; keep asset naming stable because workflows copy these directly to GitHub Pages.

## Daily Commands
```bash
uv run python -m queueboard.dashboard test/all-open-PRs-1.json test/all-open-PRs-2.json  # regenerate all dashboards from fixtures
uv run python src/queueboard/process.py                                               # rebuild processed_data artifacts
uv run python src/queueboard/test_state_evolution.py                                   # run state evolution regression tests
bash scripts/repo_check_compose.sh                                                     # canonical full-repo checks (Compose + Postgres)
```
- Use `scripts/dashboard.sh` from repo root to mirror CI behavior when validating cross-repo integrations.

## Coding Style
- Follow four-space indentation, `snake_case` functions, `PascalCase` classes, and descriptive module names (match the behavior you expose).
- Treat `ruff` as authoritative (`uv run ruff check .` before committing); keep lines ≤130 columns unless data literals read better wrapped.
- Prefer explicit imports from siblings (e.g., `from queueboard.util import now_utc`) rather than relative `from .util` when code is intended for CLI use.

## Testing & Verification
- `scripts/repo_check_compose.sh` is the primary full validation path for repo changes that touch Django/syncer/analyzer data flows.
- That script depends on Docker Compose and may be unavailable in sandboxed environments.
- In restricted environments, run what is still valid locally:
  - `uv run ruff check .`
  - fixture-based legacy tests under `src/queueboard/`
  - other non-DB checks.
- If Compose checks cannot run, clearly report that gap and request user-run results when needed.
- Snapshot dashboard outputs by copying `*.html` into `before/` and `after/` folders, then diff to spot regressions.
- Extend `test_state_evolution.py` when changing classification timelines; add new fixture JSON to `test/` and reference it in tests.
- For ad-hoc checks, run `uv run python src/queueboard/check_data_integrity.py` to leverage existing validation hooks described in the module docstring.

## Integration Notes
- Coordinate schema tweaks with the Django migration docs (`docs/django_backend_plan.md`) so new analytics tables map cleanly to legacy fields.
- When updating GraphQL payloads, mirror the changes in the `queueboard` repo workflows to keep data downloads in sync.
- Document manual validation or data backfills in your PR and link to relevant Zulip threads for reviewer context.
