# GitHub App Setup for Assignment Commands and Syncer

This guide covers how to create and configure GitHub Apps for Queueboard's operation-token flow.

## Scope and Current Token Policy
- Assignment commands (`assign_pr`, `unassign_pr`) require GitHub App installation tokens.
- Syncer operations (`syncer_repo_discovery`, `syncer_pr_read`, `syncer_ci_read`) try GitHub App tokens first and fall back to `GH_TOKEN`/`GITHUB_TOKEN` when no app token is available.
- Runtime config is loaded from `GITHUB_APP_TOKEN_CONFIG` (JSON object).

## Recommended App Layout
- Use two apps (recommended):
  - `queueboard-assignment`: minimal write permissions for assignee mutations.
  - `queueboard-syncer-read`: read-only permissions for sync/discovery.
- One app for all operations also works, but increases blast radius and over-permission risk.

## 1) Create GitHub Apps Under the Organization
1. In GitHub, open your org settings:
   - `https://github.com/organizations/<org>/settings/apps`
2. Choose `GitHub Apps` then `New GitHub App`.
3. Suggested names:
   - `Queueboard Assignment (<env>)`
   - `Queueboard Syncer Read (<env>)`
4. Set a minimal `Homepage URL` (for example the repo URL).
5. Disable webhooks unless you explicitly plan to consume GitHub App webhooks.
6. Choose `Only on this account` for installation scope (org-local app), then create the app.
7. Generate a private key and store the `.pem` in your secret store.
8. Record the app id shown on the app settings page.

Notes:
- If you cannot create org-owned apps, ask an org owner to create them and share app id + private key securely.
- Use separate apps per environment (dev/staging/prod) unless you intentionally share credentials.

## 2) Set Repository Permissions

Set only the permissions needed by each operation set.

### App A: `queueboard-assignment` (assign/unassign)
- Repository permissions:
  - `Issues: Read and write` (required for `POST/DELETE /repos/{owner}/{repo}/issues/{number}/assignees`)
  - `Pull requests: Read-only` (required for live precondition checks on PR state/assignees)
  - `Metadata: Read-only` (baseline repository visibility/lookup behavior)
- Organization permissions:
  - none required

### App B: `queueboard-syncer-read` (syncer read operations)
- Repository permissions:
  - If Queueboard only targets public repositories, you can leave repository permissions at `No access`.
  - Recommended default (safer when repo visibility or API usage changes):
    - `Metadata: Read-only`
    - `Pull requests: Read-only`
    - `Issues: Read-only`
    - `Checks: Read-only`
    - `Commit statuses: Read-only`
    - `Contents: Read-only` (recommended for commit/head query coverage)
- Organization permissions:
  - none required

## 3) Install Each App on Target Repositories
1. In each app page, open `Install App`.
2. Select the organization.
3. Prefer `Only select repositories` and choose the repos Queueboard manages.
4. Confirm installation.

Assignment commands will fail for repos where the assignment app is not installed.

## 4) Configure Environment Variables

Set these values in `.env` or your deployment secret manager:
- `GITHUB_API_URL=https://api.github.com`
- `GITHUB_APP_TOKEN_CONFIG=<json object>`

`GITHUB_APP_TOKEN_CONFIG` schema used by the code:
- top-level:
  - `api_base_url` (optional; defaults to `GITHUB_API_URL`)
  - `cache_skew_seconds` (optional; default `60`)
  - `operation_app_map` (optional; operation -> app name)
  - `apps` (required for app-token use)
- each app item:
  - `name` (string)
  - `app_id` (integer)
  - `operations` (list of operation names)
  - installation lookup behavior (optional):
    - `installation_lookup`: `repo` (default) or `owner`
    - `installation_owner_type`: `org` (default) or `user` (used when `installation_lookup=owner`)
    - `installation_owner`: fixed org/user name for owner lookup (optional; defaults to the repo owner from operation context)
  - one of:
    - `private_key` (PEM string; use `\\n` escapes in env JSON; preferred for current Heroku deployment), or
    - `private_key_path` (path to PEM file visible to the process)

Example (two-app config):

```json
{
  "api_base_url": "https://api.github.com",
  "cache_skew_seconds": 60,
  "operation_app_map": {
    "assign_pr": "queueboard-assignment",
    "unassign_pr": "queueboard-assignment",
    "syncer_repo_discovery": "queueboard-syncer-read",
    "syncer_pr_read": "queueboard-syncer-read",
    "syncer_ci_read": "queueboard-syncer-read"
  },
  "apps": [
    {
      "name": "queueboard-assignment",
      "app_id": 123456,
      "private_key_path": "/run/secrets/queueboard-assignment.pem",
      "installation_lookup": "repo",
      "operations": ["assign_pr", "unassign_pr"]
    },
    {
      "name": "queueboard-syncer-read",
      "app_id": 234567,
      "private_key_path": "/run/secrets/queueboard-syncer-read.pem",
      "installation_lookup": "owner",
      "installation_owner_type": "org",
      "installation_owner": "leanprover-community",
      "operations": ["syncer_repo_discovery", "syncer_pr_read", "syncer_ci_read"]
    }
  ]
}
```

Helper command for generating/maintaining this JSON:

```bash
uv run python qb_site/manage.py github_app_config init .github-app-config.local.json
uv run python qb_site/manage.py github_app_config validate .github-app-config.local.json --check-key-paths
uv run python qb_site/manage.py github_app_config inline-keys .github-app-config.local.json --in-place
uv run python qb_site/manage.py github_app_config to-env .github-app-config.local.json --export
```

Notes:
- `inline-keys` reads each app `private_key_path` PEM and writes it into `private_key` with JSON-safe newline escaping.
- Relative `private_key_path` values are resolved relative to the JSON config file location.

## 5) Quick Verification
1. Restart services after updating env secrets.
2. Run an `assign` command in Zulip against a repo where the assignment app is installed.
3. Confirm behavior:
   - success path: assignee mutation succeeds and reaction is added
   - missing installation/permission path: private failure explains app token is unavailable
4. For syncer, run a repo sync and confirm it still succeeds (app token first, PAT fallback if needed).

## References
- `docs/design-decisions/026-zulip-assign-unassign-and-github-app-tokens.md`
- `qb_site/core/services/github_app_tokens.py`
- `qb_site/core/services/github_operation_tokens.py`
- `qb_site/zulip_bot/services/assignment_execution.py`
- `qb_site/syncer/services/github_client.py`
