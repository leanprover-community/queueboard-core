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
  To limit blast radius, a separate `queueboard-org-read` app carries `Members: Read` and handles
  the `check_collaborator_permission` operation; `queueboard-assignment` only needs `Pull requests:
  Read and write` (no org-level permissions). The close-PR code falls back to the `close_pr` token
  if `check_collaborator_permission` is not mapped, for single-app setups.

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

### Token Service: `pr_action_links.py` (was `close_pr_links.py`)
- Pattern mirrored the prefs link module of the time (`prefs_links.py`, retired in design doc 022):
  Fernet encryption, `iat`/`exp` claims.
- **Since consolidated.** `close_pr_links.py` and `label_pr_links.py` were byte-identical apart from
  one URL path, and both hand-rolled the Fernet primitive that `core.services.signed_payloads` already
  provided. They are now one `pr_action_links.py` keyed by a `PRAction` constant (`CLOSE_PR` /
  `LABEL_PR`). Secret, salt, TTL and URL path stay per-action, so this is wire-compatible and the
  settings below are unchanged; distinct salts also keep a `label-pr` token from validating as a
  `close-pr` one. The claims and settings named here are otherwise as-shipped.
- Claims: `zulip_user_id`, `github_login`, `pr_owner`, `pr_repo`, `pr_number`.
- Settings:
  - `ZULIP_CLOSE_PR_TOKEN_SECRET` (falls back to `SECRET_KEY`)
  - `ZULIP_CLOSE_PR_TOKEN_SALT` (default: `"zulip_bot.close_pr"`)
  - `ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS` (default: `1800`)
  - `ZULIP_PREFS_URL_BASE` (shared with prefs/registration links; falls back to relative path)

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
- Create new app `queueboard-org-read`:
  - `Metadata: Read-only` (baseline)
  - `Members: Read` (org-level permission)
  - Install on the org with owner-level installation lookup
- Update `docs/github_app_setup.md` and `GITHUB_APP_TOKEN_CONFIG` operation map to include
  `close_pr` (→ `queueboard-assignment`) and `check_collaborator_permission`
  (→ `queueboard-org-read`).

## Subtleties / Invariants
- Permission check uses the GitHub App token, not the user's personal token. The close itself is also
  performed by the GitHub App. The invoker's personal GitHub identity is never exposed.
- The collaborator permission endpoint returns `permission: "none"` for users with no access (not 404).
  `maintain` role maps to `permission: "write"` in this endpoint's response.
- **Known limitation (triage role):** In org-owned repos, users with the GitHub `triage` role can
  close PRs but the collaborator permission endpoint's `permission` field returns `"read"` for them
  (it does not distinguish `triage` from plain read). As a result, triage users are currently denied
  by `close-pr`. A future change could also inspect the `role_name` field in the API response to
  grant access. This is a known limitation, not a correctness bug; the current check errs on the
  side of caution.
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

### Commit 1 (this doc) ✓
- Add `docs/design-decisions/041-zulip-close-pr-command.md`.

### Commit 2 (implementation) ✓
- New files:
  - `qb_site/zulip_bot/commands/close_pr.py`
  - `qb_site/zulip_bot/services/close_pr_links.py` (later merged into `pr_action_links.py`)
  - `qb_site/zulip_bot/services/close_pr_execution.py`
  - `qb_site/templates/zulip_bot/close_pr_form.html`
  - `qb_site/templates/zulip_bot/close_pr_invalid.html`
- Modified files:
  - `qb_site/zulip_bot/views.py` — add `close_pr_form` view, import command module
  - `qb_site/zulip_bot/urls.py` — add `close-pr/<str:token>/`
  - `qb_site/zulip_bot/AGENTS.md` / `CLAUDE.md` — document new command and settings
  - `docs/github_app_setup.md` — add new app permissions and `close_pr` operation
- Also included two follow-up fixes:
  - `fix(zulip_bot): normalise underscores to hyphens in command name parser` — so
    `close_pr`, `assigned_prs`, `pr_info` variants resolve to the canonical hyphenated names
    without per-command aliases.
  - `fix(zulip_bot): fix two test_close_pr_form assertions` — missing
    `ZULIP_CLOSE_PR_MUTATIONS_ENABLED` override in one test and a wrong error-substring check
    in another.

### Commit 3 (follow-up: custom close message) ✓
- Textarea on confirmation form for an optional close message.
- Preset buttons load pre-written messages into the textarea. Presets are stored as `.md` files in
  `qb_site/zulip_bot/close_pr_presets/`; the first `# Heading` line becomes the button label, the
  remainder becomes the body. Files are sorted by filename so numbering controls display order. No
  code change required to add or edit presets.
- On submit, if a message is provided it is posted as a PR comment
  (`POST /repos/{owner}/{repo}/issues/{number}/comments`) before the PR is closed. If the comment
  post fails, an inline error is shown and the PR is not closed (user can retry or clear the
  message). No additional GitHub App permissions required (`Issues: Read and write` already granted).
- New service: `qb_site/zulip_bot/services/close_pr_presets.py` (`load_close_pr_presets`).
- New execution helper: `post_pr_comment` in `close_pr_execution.py`.
- New CSS: `qb_site/zulip_bot/static/zulip_bot/close_pr_form.css`.
- The error state now renders inline in the form (rather than a dead-end page) so the user's
  typed message is preserved for retry.

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

## Operational Deployment Notes

### Before enabling in production
1. **Update the `queueboard-assignment` GitHub App** (manual step in the GitHub UI):
   - Upgrade `Pull requests` from Read-only to **Read and write**.
   - After saving, GitHub will ask any installation admins to approve the expanded permissions.
     Existing `assign`/`unassign` operations continue with the old token scope until approved,
     so coordinate approval with minimal downtime.
2. **Create the `queueboard-org-read` GitHub App** (manual step):
   - Set `Metadata: Read-only` and org-level `Members: Read`.
   - Install on the org (owner-level installation).
   - Generate a private key and store it in your secret store.
3. **Update `GITHUB_APP_TOKEN_CONFIG`** to include `close_pr` (→ `queueboard-assignment`) and
   `check_collaborator_permission` (→ `queueboard-org-read`) in the `operation_app_map`, and add
   both apps to the `apps` list. See `docs/github_app_setup.md` for the full example config.
4. **Add `close-pr` to `ZULIP_COMMAND_POLICY`**. Without a policy entry the command is silently
   ignored for all users. Restrict `allowed_groups` to the Zulip group(s) whose members should be
   able to invoke the command (GitHub permission is still checked at runtime). Allow both DM and
   stream contexts so the command can be used from either:
   ```json
   {
     "close-pr": {
       "allowed_groups": [<maintainers_group_id>],
       "allowed_contexts": ["dm", "stream:*"]
     }
   }
   ```
   Use `manage.py zulip_policy` to build and validate the JSON before setting the env var.
5. **Add `ZULIP_REPO_LOG`** to settings/env for each repo you want audit-log posts in Zulip:
   ```json
   {
     "leanprover-community/mathlib4": {
       "stream": "mathlib4",
       "topic": "bot actions"
     }
   }
   ```
   If a repo has no entry, closes still work but a WARNING is logged and no Zulip log post is
   sent. The setting is intentionally generic for reuse by future operations.
6. **Add `ZULIP_CLOSE_PR_MUTATIONS_ENABLED = True`** to enable actual PR closes. Without this
   flag the form renders and the POST runs all post-actions (sync enqueue, DM, log post) but does
   not call GitHub — useful for end-to-end smoke testing without closing the PR.
7. **Optional token settings** (all have reasonable defaults):
   - `ZULIP_CLOSE_PR_TOKEN_SECRET` (falls back to `SECRET_KEY`)
   - `ZULIP_CLOSE_PR_TOKEN_SALT` (default: `"zulip_bot.close_pr"`)
   - `ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS` (default: `1800`)
   - `ZULIP_PREFS_URL_BASE` (shared with prefs/registration links; falls back to relative path)

### Manual verification checklist (after app permissions are updated)
- [ ] `GET /repos/{owner}/{repo}/collaborators/{username}/permission` returns `{"permission":
  "none"}` for a non-collaborator (not 404).
- [ ] `GET /repos/{owner}/{repo}/collaborators/{username}/permission` returns `"write"` for a
  user with the `maintain` role.
- [ ] `PATCH /repos/{owner}/{repo}/pulls/{number}` with `{"state": "closed"}` succeeds using
  the updated `queueboard-assignment` installation token.
- [ ] End-to-end: issue `close-pr <pr-url>` in Zulip → receive private link → open form →
  confirm → PR closed on GitHub → DM received → log thread updated in Zulip.
- [ ] Issue command as a non-collaborator who is not the PR author → receive "you don't have
  permission" private message, no link issued.
- [ ] Issue command for an already-closed PR → receive "PR is not open" private message.
- [ ] Open confirmation link after TTL expires → see 403 invalid-token page.

## Progress Notes
- 2026-04-12: Design doc written.
- 2026-04-12: Implementation complete (commits 1 and 2 landed on `close-pr-command` branch).
  All unit tests pass under Compose. GitHub App permission upgrade and production env vars are
  the remaining manual steps before the feature can be enabled.
- 2026-04-17: Commit 3 complete — optional close message with markdown presets added.
