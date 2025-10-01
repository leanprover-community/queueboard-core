Here is the main workflow in the `queueboard` repo, which queries data and generates the dashboard using the code in this repo (`queueboard-core`).
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
      uses: actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8 # v5.0.0
      with:
        ref: master

    - name: "Checkout queueboard-core"
      uses: actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8 # v5.0.0
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
      uses: actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c # v6.0.0
      with:
        python-version: "3.12"

    - name: "Setup uv"
      uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0

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

    - name: "Generate the data used in dashboard generation"
      id: generate-dashboard-data
      if: ${{ !cancelled() && (steps.download-json.outcome == 'success') }}
      run: |
        uv run python -m queueboard.dashboard_data "all-open-PRs-1.json" "all-open-PRs-2.json" "all-open-PRs-3.json"
        rm all-open-PRs-*.json queue.json

    - name: Upload artifact containing API files
      id: upload-api-artifact
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
      with:
        name: api-artifact
        path: api/

    - name: "Regenerate the dashboard webpages"
      id: generate-dashboard
      if: ${{ !cancelled() && (steps.generate-dashboard-data.outcome == 'success') }}
      run: |
        uv run python -m queueboard.dashboard

    - name: Upload artifact
      id: pages-artifact
      if: ${{ !cancelled() && (steps.generate-dashboard.outcome == 'success') }}
      uses: actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b # v4.0.0
      with:
        path: gh-pages

    - name: Deploy to GitHub Pages
      if: ${{ github.ref == 'refs/heads/master' && !cancelled() && (steps.pages-artifact.outcome == 'success') }}
      id: deployment
      uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e # v4.0.5
```
