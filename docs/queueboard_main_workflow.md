Here is the main workflow in the `queueboard` repo, which queries data and generates the dashboard using the code in this repo (`queueboard-core`). For the planned v2 ingestion that replaces these ad‑hoc scripts with a database‑backed syncer, see docs/syncer_ingestion_plan.md.
Note that in the `queueboard` repo, the JSON files in `data/` and `processed-data/` are persisted from run to run by git pushes in this workflow.
This is also true of a few auxiliary text files: `closed_prs_to_backfill.txt`, `missing_prs.txt`, `redownload.txt`, `stubborn_prs.txt`.

```yaml
name: Update PR metadata

on:
  schedule:
    - cron: '*/8 * * * *' # Runs every 8 minutes
  workflow_dispatch:      # Allows manual execution

permissions:
  contents: write
  id-token: write
  pages: write

concurrency:
  # label each workflow run; only the latest with each label will run
  group: ${{ github.workflow }}-${{ github.ref }}
  # if there is a run in progress with the same label, the next new run will be queued
  # and new runs after that will cancel pending runs
  cancel-in-progress: false

jobs:
  gather-stats:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}

    steps:
    - name: "Checkout queueboard-core"
      uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      with:
        repository: leanprover-community/queueboard-core
        ref: master
        path: queueboard-core

    - name: "Copy files from queueboard-core into place"
      shell: bash -euo pipefail {0}
      run: |
        cp -r queueboard-core/scripts .
        cp -r queueboard-core/src/queueboard/queries .
        cp queueboard-core/reviewer-topics.json .

    - name: "Setup Python"
      uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
      with:
        python-version: "3.12"

    - name: "Setup uv"
      uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0

    - name: Install queueboard-core (editable)
      run: |
        uv venv
        cd queueboard-core
        uv pip install -e .

    - name: "Generate dashboard from API (rule set 1)"
      id: generate-dashboard-api-rs1
      env:
        QUEUEBOARD_API_BASE_URL: ${{ secrets.QUEUEBOARD_API_BASE_URL }}
      run: |
        uv run python -m queueboard.dashboard \
          --api \
          --rule-set-id 1 \
          --api-dir api-rule-set-1 \
          --gh-pages-dir gh-pages/test-rule-set-1

    - name: "Generate dashboard from API (rule set 2)"
      id: generate-dashboard-api-rs2
      env:
        QUEUEBOARD_API_BASE_URL: ${{ secrets.QUEUEBOARD_API_BASE_URL }}
      run: |
        uv run python -m queueboard.dashboard \
          --api \
          --rule-set-id 2 \
          --api-dir api-rule-set-2 \
          --gh-pages-dir gh-pages/test-rule-set-2

    - name: "Generate dashboard from API (rule set 3)"
      id: generate-dashboard-api-rs3
      env:
        QUEUEBOARD_API_BASE_URL: ${{ secrets.QUEUEBOARD_API_BASE_URL }}
      run: |
        uv run python -m queueboard.dashboard \
          --api \
          --rule-set-id 3 \
          --api-dir api-rule-set-3

    - name: Upload artifact
      id: pages-artifact
      if: ${{ (steps.generate-dashboard-api-rs1.outcome == 'success') && (steps.generate-dashboard-api-rs2.outcome == 'success') && (steps.generate-dashboard-api-rs3.outcome == 'success') }}
      uses: actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0
      with:
        path: gh-pages

    - name: Deploy to GitHub Pages
      if: ${{ github.ref == 'refs/heads/master' && (steps.pages-artifact.outcome == 'success') }}
      id: deployment
      uses: actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128 # v5.0.0
```
