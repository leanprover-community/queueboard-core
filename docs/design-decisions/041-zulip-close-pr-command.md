# Zulip Close-PR Command

## Context
- Certain Queueboard users have GitHub write/maintain/admin access and sometimes need to close PRs without
  their personal GitHub identity being visible (e.g. to keep moderation actions neutral).
- Existing Zulip bot commands use `ZULIP_COMMAND_POLICY` for access control, but that only gates which
  Zulip users can invoke a command — it does not verify that the invoker actually has the corresponding
  GitHub permission at runtime.
- Closing a PR is a destructive, hard-to-reverse action; it requires a confirmation step beyond a single
  command invocation.
- The existing secure-link + web form pattern (used by `prefs` and registration) is a natural fit for a
  confirmation flow.
- The `queueboard-assignment` GitHub App currently has `Issues: Read and write` and `Pull requests:
  Read-only`. Closing a PR via `PATCH /repos/{owner}/{repo}/pulls/{number}` requires `Pull requests:
  Read and write`. Checking a user's collaborator permission via
  `GET /repos/{owner}/{repo}/collaborators/{username}/permission` requires `Members: Read` (org-level).
  Both permissions must be added to the `queueboard-assignment` app.

## Goals / Non-Goals
- Goals:
  - Let authorized users close a PR from Zulip with their personal GitHub identity hidden (action
    attributed to the GitHub App).
  - Verify at command time that the invoker has GitHub write/maintain/admin on the target repo, or is the
    PR author.
  - Require explicit confirmation before any mutation.
  - Log closes to a configurable per-repo Zulip stream/topic.
- Non-goals:
  - Re-opening PRs.
  - Closing PRs on repos not tracked by Queueboard.
  - Storing or exposing the user's personal GitHub OAuth token.

## Proposed Design

### Command: `close-pr`
- Syntax: `close-pr <pr-url>`
- Registered in `qb_site/zulip_bot/commands/close_pr.py`.
- Response mode: `PRIVATE` (link is sent only to the invoker).
- Flow:
  1. Parse exactly one PR URL from args / `rendered_content` (reuse `_parse_single_pr_ref` logic).
  2. Resolve invoker's `core.User` by `context.sender_id` (`zulip_user_id`); require `github_login`.
  3. Check permission (see below).
  4. Issue an encrypted `close-pr` token and send private link.
- Feature flag: `ZULIP_CLOSE_PR_MUTATIONS_ENABLED`. When disabled, command still validates and issues
  the link but the form view blocks the actual close and returns a preflight-only message.

### Permission Check (command time)
Performed using a GitHub App token for operation `close_pr` (mapped to `queueboard-assignment`):

1. Fetch PR via `GET /repos/{owner}/{repo}/pulls/{number}`:
   - Check PR is open; if not, return private "PR is not open" message (no link issued).
   - Extract `user.login` to identify the PR author.
2. If `github_login == pr_author_login` (case-insensitive): **permitted**.
3. Otherwise call `GET /repos/{owner}/{repo}/collaborators/{github_login}/permission`:
   - If `permission` in `{"write", "admin"}`: **permitted** (`maintain` role maps to `write` here).
   - Otherwise: return private "you don't have permission to close this PR" message.

### Token Service: `close_pr_links.py`
- Pattern mirrors `prefs_links.py` (Fernet encryption, `iat`/`exp` claims).
- Claims: `zulip_user_id`, `github_login`, `pr_owner`, `pr_repo`, `pr_number`.
- Settings:
  - `ZULIP_CLOSE_PR_TOKEN_SECRET` (falls back to `SECRET_KEY`)
  - `ZULIP_CLOSE_PR_TOKEN_SALT` (default: `"zulip_bot.close_pr"`)
  - `ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS` (default: `1800`)
  - `ZULIP_CLOSE_PR_URL_BASE` (optional; falls back to relative path)

### Execution Service: `close_pr_execution.py`
- `check_close_pr_permission(github_login, owner, repo, number)` → `PermissionCheckResult`
  - Fetches PR + optionally checks collaborator permission.
  - Returns structured result: `permitted`, `not_permitted`, `pr_not_open`, `token_unavailable`.
- `close_pull_request(owner, repo, number)` → success/error
  - `PATCH /repos/{owner}/{repo}/pulls/{number}` with `{"state": "closed"}`.
  - Uses `close_pr` operation token (same `queueboard-assignment` app).

### Web Layer
- URL: `close-pr/<str:token>/` → `views.close_pr_form`
- View behavior:
  - Validate token; show `close_pr_invalid.html` (HTTP 403) on expiry/invalid.
  - Fetch live PR details (title, state) from GitHub to display in form.
  - **GET**: render confirmation form. If PR is already closed/merged, show informational message
    without a submit button (no redirect, no error page — same template, conditional rendering).
  - **POST**: if `ZULIP_CLOSE_PR_MUTATIONS_ENABLED` is off, show preflight-only message. Otherwise:
    1. Close PR via execution service.
    2. Enqueue `sync_pr_task.delay(repository_id, pr_number)` (best-effort).
    3. Send DM to invoker confirming the close (best-effort; failure logged, not surfaced).
    4. Post to repo log thread if `ZULIP_REPO_LOG` has an entry for `owner/repo` (best-effort).
    5. Render success state in same template.
  - `Cache-Control: no-store` on all responses.

### Settings
- `ZULIP_CLOSE_PR_MUTATIONS_ENABLED` — feature flag (same pattern as assignment mutations flag).
- `ZULIP_REPO_LOG` — JSON object mapping `"owner/repo"` to `{"stream": "...", "topic": "..."}`.
  If a repo has no entry, the log post step is skipped and a `WARNING` is emitted.
  Designed to be reusable for future per-repo Zulip logging beyond close-PR.

  Example:
  ```json
  {
    "leanprover-community/mathlib4": {
      "stream": "mathlib4",
      "topic": "bot actions"
    }
  }
  ```

### GitHub App Changes (manual)
- Add to `queueboard-assignment`:
  - `Pull requests: Read and write` (upgrade from read-only)
  - `Members: Read` (org-level permission, new)
- Update `docs/github_app_setup.md` and `GITHUB_APP_TOKEN_CONFIG` operation map to include `close_pr`.

## Subtleties / Invariants
- Permission check uses the GitHub App token, not the user's personal token. The close itself is also
  performed by the GitHub App. The invoker's personal GitHub identity is never exposed.
- The collaborator permission endpoint returns `permission: "none"` for users with no access (not 404).
  `maintain` role maps to `permission: "write"` in this endpoint's response.
- If `check_close_pr_permission` can't obtain a GitHub App token (app not installed on repo, config
  missing), the command fails privately with a clear message — same behavior as assignment mutations.
- The form token embeds `github_login` at issuance time. If the user's GitHub login changes between
  issuance and form submission, the close still proceeds using the embedded login for attribution
  purposes (acceptable; the login change is rare and the token is short-lived).
- Sync enqueue, DM, and log post are all best-effort: failures are logged but do not cause the HTTP
  response to indicate failure. The PR close itself is the authoritative outcome.
- The `ZULIP_REPO_LOG` setting is intentionally generic (not named `ZULIP_CLOSE_PR_LOG`) to allow
  other future bot operations to reuse the same per-repo destination config.

## Implementation Plan

### Commit 1 (this doc)
- Add `docs/design-decisions/041-zulip-close-pr-command.md`.

### Commit 2 (implementation)
- New files:
  - `qb_site/zulip_bot/commands/close_pr.py`
  - `qb_site/zulip_bot/services/close_pr_links.py`
  - `qb_site/zulip_bot/services/close_pr_execution.py`
  - `qb_site/templates/zulip_bot/close_pr_form.html`
  - `qb_site/templates/zulip_bot/close_pr_invalid.html`
- Modified files:
  - `qb_site/zulip_bot/views.py` — add `close_pr_form` view, import command module
  - `qb_site/zulip_bot/urls.py` — add `close-pr/<str:token>/`
  - `qb_site/zulip_bot/AGENTS.md` / `CLAUDE.md` — document new command and settings
  - `docs/github_app_setup.md` — add new app permissions and `close_pr` operation

### Commit 3 (follow-up: custom close message)
- Add textarea to confirmation form for optional close message.
- Add pre-written close message templates (rendered as selectable buttons/options).
- On submit, post the message as a PR comment (`POST /repos/{owner}/{repo}/issues/{number}/comments`)
  before closing. No additional GitHub App permissions required (`Issues: Read and write` already
  granted).

## Validation Plan
- Tests:
  - Token service: issue → validate round-trip; expiry; invalid/tampered tokens.
  - Permission check: permitted as author; permitted as write collaborator; denied; PR not open;
    token unavailable.
  - Command handler: no linked user; linked but no GitHub login; not permitted; permitted (link issued);
    feature-flag preflight path.
  - View (GET): valid open PR; valid already-closed PR; expired token; invalid token.
  - View (POST): successful close with sync enqueue + DM + log post; mutations disabled preflight;
    GitHub error on close.
  - `ZULIP_REPO_LOG` parsing: valid config; missing repo entry (skip + warn); malformed config.
- Manual checks:
  - Verify `GET /repos/{owner}/{repo}/collaborators/{username}/permission` response shape and `"none"`
    behavior for non-collaborators with the updated `queueboard-assignment` app credentials.
  - Verify `PATCH /repos/{owner}/{repo}/pulls/{number}` with `{"state": "closed"}` succeeds with the
    updated app token.
  - End-to-end: issue command → receive link → open form → submit → PR closed → DM received →
    log thread updated.

## Progress Notes
- 2026-04-12: Design doc written; implementation not yet started.
