# Repository Guidelines

## Module Focus & Layout
- `dashboard.py` renders the review and triage HTML; helpers such as `_compute_pr_entries` and `_write_table_row` drive most layout tweaks.
- `process.py` and `compute_dashboard_prs.py` extract metadata from downloaded JSON, with shared data classes in `dashboard_data.py` and utilities in `util.py`.
- `queries/` contains GraphQL payload templates used by the sync scripts in the sibling `queueboard` repo; adjust these alongside processing logic.
- `static/` bundles CSS/JS referenced by the generated HTML; keep asset naming stable because workflows copy these directly to GitHub Pages.

## static/dependency_dashboard.html
A self-contained page — markup, CSS and ~1300 lines of inline JS in one file — with d3 from an
SRI-pinned CDN and its data from `dependency_graph.json`, which `dashboard.py` copies out of
`api/`. `dashboard.py` copies the page itself verbatim into `gh-pages/`, so there is no build
step; editing the file is the whole change. Four conventions it is easy to break:
- **Colours come only from CSS custom properties**, resolved once per theme by `readPalette()`;
  `NODE_CATEGORIES` names the token for each bucket, so the nodes and the legend cannot drift
  apart. Never hardcode a colour in the JS.
- **Light is the default and the OS preference is not consulted**, because the rest of the
  frontend is light-only (see `qb_site/console/AGENTS.md`). Dark is opt-in through the toggle and
  stored per browser in `localStorage` under `queueboard-dependency-theme`, applied by a `<head>`
  script before the first paint. Rationale in `docs/design-decisions/055-dependency-graph-queue-status-colouring.md`.
- **Anything sized for the reader is constant in *screen* pixels**, which in user space means
  dividing by the zoom `k`. `applyZoomAwareStyling` is the single place node radii, ring widths,
  link widths and the PR-number labels are resized; put new marks there rather than giving them a
  fixed user-space size, which silently changes meaning with zoom. A mark that is inflated when
  zoomed out needs *every* part of it inflated — the "blocked" ring thinned away with distance
  because the node grew and the ring did not.
- **d3 writes styles inline, and an inline style beats a stylesheet.** A rule in the `<style>`
  block that targets a property some `.style(...)` call also sets will never apply — it simply
  does nothing, which is easy to miss in review. Where a property must be both zoom-compensated
  and switchable by class, write the computed value into a CSS custom property on the container
  and let the stylesheet substitute it; that is what `--link-width` / `--link-width-strong` do
  for `.link` and `.link.incident`. Do the arithmetic in JS rather than a CSS `calc()`, so the
  stylesheet only ever substitutes a plain value.
- **The tooltip is placed around the highlight, not just around the cursor**: `positionTooltip`
  scores candidate rectangles against the hovered PR's whole component plus the fixed overlays,
  preferring one that covers nothing and, among those, the nearest. A new pinned panel has to be
  registered in `overlayScreenBoxes()` or the tooltip will happily sit on it.
- **An outline on a node means "blocked by an open PR" and nothing else.** Other node-level
  states need a different channel, or they read as a variant of that one — which is why the
  hover emphasis marks the incident *edges* rather than the neighbouring nodes.

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
  - `uv run python src/queueboard/test_state_evolution.py`
  - `uv run python src/queueboard/test_snapshot.py`
  - `uv run python src/queueboard/test_process.py`
  - `uv run python src/queueboard/test_util.py`
  - `uv run python src/queueboard/test_reviewer_topics.py`
  - other non-DB checks.
- If Compose checks cannot run, clearly report that gap and request user-run results when needed.
- Snapshot dashboard outputs by copying `*.html` into `before/` and `after/` folders, then diff to spot regressions.
- Extend `test_state_evolution.py` when changing classification timelines; add new fixture JSON to `test/` and reference it in tests.
- For ad-hoc checks, run `uv run python src/queueboard/check_data_integrity.py` to leverage existing validation hooks described in the module docstring.

## Integration Notes
- Coordinate schema tweaks with the Django migration docs (`docs/django_backend_plan.md`) so new analytics tables map cleanly to legacy fields.
- When updating GraphQL payloads, mirror the changes in the `queueboard` repo workflows to keep data downloads in sync.
- Document manual validation or data backfills in your PR and link to relevant Zulip threads for reviewer context.
