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
    - name: Checkout repository
      uses: actions/checkout@1af3b93b6815bc44a9784bd300feb67ff0d1eeb3 # v6.0.0
      with:
        ref: master

    - name: "Checkout queueboard-core"
      uses: actions/checkout@1af3b93b6815bc44a9784bd300feb67ff0d1eeb3 # v6.0.0
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
      uses: actions/setup-python@83679a892e2d95755f2dac6acb0bfd1e9ac5d548 # v6.1.0
      with:
        python-version: "3.12"

    - name: "Setup uv"
      uses: astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244 # v7.1.4

    - name: Install queueboard-core (editable)
      run: |
        uv venv
        cd queueboard-core
        uv pip install -e .

    - name: "Run scripts/gather_stats.sh"
      shell: bash -euo pipefail {0}
      env:
        GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      run: |
        scripts/gather_stats.sh 12 2>&1 | tee gather_stats.log  # Log output to gather_stats.log

    - name: "Update aggregate data file"
      if: ${{ !cancelled() }}
      run: |
        # Write files with aggregate PR data, to "processed_data/{all_pr,open_pr,assignment}_data.json".
        uv run python -m queueboard.process

    - name: "Download .json files for all open PRs"
      id: "download-json"
      if: ${{ !cancelled() }}
      env:
        GH_TOKEN: ${{ github.token }}
      run: |
        bash scripts/dashboard.sh

    - name: "Check data integrity"
      if: ${{ !cancelled() && (steps.download-json.outcome == 'success') }}
      run: |
        uv run python -m queueboard.check_data_integrity

    - name: "(Re-)download missing or outdated PR data"
      if: ${{ !cancelled() && (steps.download-json.outcome == 'success') }}
      env:
        GH_TOKEN: ${{ github.token }}
      run: |
        scripts/download_missing_outdated_PRs.sh

    - name: "Update aggregate data file (again)"
      id: update-aggregate-again
      if: ${{ !cancelled() }}
      run: |
        # Write files with aggregate PR data, to "processed_data/{all_pr,open_pr,assignment}_data.json".
        uv run python -m queueboard.process

    - name: Commit changes
      if: ${{ !cancelled() }}
      id: "commit"
      run: |
        git config --global user.email 'github-actions[bot]@users.noreply.github.com'
        git config --global user.name 'github-actions[bot]'
        git add data
        # Split the large file before committing and remove original
        # Can be reconstructed with `cat processed_data/all_pr_data.json.* > processed_data/all_pr_data.json`
        split -b 10M processed_data/all_pr_data.json processed_data/all_pr_data.json.
        rm -f processed_data/all_pr_data.json
        git add processed_data
        # These files may not exist, if there was no broken download to begin with resp. all metadata is up to date.
        rm -f broken_pr_data.txt
        rm -f outdated_prs.txt
        git add *.txt
        git commit -m 'Update data; redownload missing and outdated data; regenerate aggregate files'

    - name: Push changes
      if: ${{ !cancelled() && steps.commit.outcome == 'success' }}
      run: |
        # FIXME: make this more robust about incoming changes
        # The other workflow does not push to this branch, so this should be fine.
        git push

    - name: Upload artifact containing files used to generate API
      id: upload-pre-api-artifact
      if: ${{ !cancelled() && (steps.download-json.outcome == 'success') && (steps.update-aggregate-again.outcome == 'success') }}
      uses: actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4 # v5.0.0
      with:
        name: pre-api-artifact
        path: |
          queue.json
          all-open-PRs-1.json
          all-open-PRs-2a.json
          all-open-PRs-2b.json
          all-open-PRs-3.json
          processed_data/open_pr_data.json
          processed_data/assignment_data.json

    - name: "Generate the data used in dashboard generation"
      id: generate-dashboard-data
      if: ${{ !cancelled() && (steps.download-json.outcome == 'success') }}
      run: |
        uv run python -m queueboard.dashboard_data "all-open-PRs-1.json" "all-open-PRs-2a.json" "all-open-PRs-2b.json" "all-open-PRs-3.json"
        rm all-open-PRs-*.json queue.json

    - name: Upload artifact containing API files
      id: upload-api-artifact
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      uses: actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4 # v5.0.0
      with:
        name: api-artifact
        path: api/

    - name: "Regenerate the dashboard webpages"
      id: generate-dashboard
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      run: |
        uv run python -m queueboard.dashboard

    - name: "Generate dashboard from API (rule set 1)"
      id: generate-dashboard-api-rs1
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      env:
        QUEUEBOARD_API_BASE_URL: ${{ secrets.QUEUEBOARD_API_BASE_URL }}
      run: |
        uv run python -m queueboard.dashboard \
          --api \
          --rule-set-id 1 \
          --gh-pages-dir gh-pages/test-rule-set-1 \
          --api-dir api-rule-set-1

    - name: "Generate dashboard from API (rule set 2)"
      id: generate-dashboard-api-rs2
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      env:
        QUEUEBOARD_API_BASE_URL: ${{ secrets.QUEUEBOARD_API_BASE_URL }}
      run: |
        uv run python -m queueboard.dashboard \
          --api \
          --rule-set-id 2 \
          --gh-pages-dir gh-pages/test-rule-set-2 \
          --api-dir api-rule-set-2

    - name: Upload artifact
      id: pages-artifact
      if: ${{ !cancelled() && (steps.generate-dashboard.outcome == 'success') && (steps.generate-dashboard-api-rs1.outcome == 'success') && (steps.generate-dashboard-api-rs2.outcome == 'success') }}
      uses: actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b # v4.0.0
      with:
        path: gh-pages

    - name: Deploy to GitHub Pages
      if: ${{ github.ref == 'refs/heads/master' && !cancelled() && (steps.pages-artifact.outcome == 'success') }}
      id: deployment
      uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e # v4.0.5
```
