# Zulip Label-PR Command and Close-PR Label Integration

## Context
- The `close-pr` command (`docs/design-decisions/041-zulip-close-pr-command.md`) lets authorized users
  close PRs via the GitHub App without exposing their personal GitHub identity. The same motivation
  applies to label management: maintainers sometimes need to add or change labels on PRs as a moderation
  action, and should be able to do so without their personal GitHub identity being visible.
- Label data is already synced into the database: `LabelDef` (per-repo label catalog) and `PRLabel`
  (label-to-PR attachment). This makes it straightforward to populate the label *catalog* in the
  picker from DB state. The *current* label set is read live from the GitHub API at form-render
  time, since `set_pr_labels` uses `PUT` (full replacement) and a stale snapshot would risk
  silently dropping labels that exist on GitHub.
- Label mutations (`POST` or `PUT /repos/{owner}/{repo}/issues/{number}/labels`) require `Issues:
  Read and write`. This permission is already granted to the `queueboard-assignment` app (used by
  `close-pr` for posting PR comments). No new GitHub App or permission upgrade is needed.
- A natural second use case is adding labels *as part of closing a PR* — tagging a closed PR with
  something like "won't fix" or "stale" in the same action. This calls for a label-picker UI embedded
  in the existing `close-pr` confirmation form.

## Goals / Non-Goals
- Goals:
  - New `label-pr` command: let write/admin collaborators apply or update labels on a PR from Zulip,
    with their personal GitHub identity hidden (action attributed to the GitHub App).
  - Require explicit confirmation before any mutation (same secure-link + form pattern as `close-pr`).
  - Embed a label-picker in the `close-pr` form so users can optionally add labels when closing a PR
    (add-only, not full replacement).
  - Read the available-label catalog from the database (`LabelDef`); read the *current* label set
    live from GitHub at form-render time so that PUT-based replacement is safe.
  - Log label mutations to the per-repo Zulip log (`ZULIP_REPO_LOG`) alongside close-PR events.
- Non-goals:
  - Removing labels via `close-pr` (the `label-pr` form supports full replacement, so removing by
    unchecking is covered, but `close-pr` label integration is add-only).
  - Creating or deleting label definitions (only applying existing ones).
  - `label-pr` when the live GitHub fetch fails: the form refuses to render the picker, since
    PUT replacement against unknown current state could drop labels (see Subtleties).

## Proposed Design

### Part 1: `label-pr` Command

#### Command
- Syntax: `label-pr <pr-or-issue-url>`
- File: `qb_site/zulip_bot/commands/label_pr.py`
- Response mode: `PRIVATE`
- Supports both pull request URLs (`/pull/NNN`) and issue URLs (`/issues/NNN`). The GitHub API for
  label mutations, permission checks, and the live `GET /issues/{number}` fetch (used for the
  current label set) all work identically for PRs and plain issues, so the form behaves the same
  for both.
- Flow:
  1. Parse exactly one PR or issue URL (extend `_parse_single_pr_ref` logic from `close_pr.py` to
     also match `/issues/NNN` paths).
  2. Resolve invoker's `core.User` by `context.sender_id`; require `github_login`.
  3. Check permission (write/admin collaborator only — see below).
  4. Issue an encrypted `label-pr` token and send private confirmation link.
- Feature flag: `ZULIP_LABEL_PR_MUTATIONS_ENABLED`. When disabled, the form view blocks the actual
  mutation and shows a preflight-only message.

#### Permission Check
- Fetch the issue/PR via `GET /repos/{owner}/{repo}/issues/{number}` (using `label_pr` operation
  token, which maps to `queueboard-assignment`). This endpoint covers both plain issues and PRs:
  - If not found or GitHub error: return private error message.
  - If `state` is closed: return private "issue/PR is not open" message; no link issued.
- Then call collaborator permission check (using `check_collaborator_permission` operation, which
  maps to `queueboard-org-read`):
  - `permission` in `{"write", "admin"}`: **permitted**.
  - Otherwise: return private "you don't have permission to label this PR/issue" message.
- Unlike `close-pr`, authorship does not confer labeling permission. Label management is a
  moderation action; GitHub's own model restricts label application to users with write access.
  The token falls back to the `label_pr` token if `check_collaborator_permission` is not mapped.
- **Known limitation (triage role):** In org-owned repos, users with the `triage` role can close
  issues/PRs and apply labels on GitHub, but the collaborator permission endpoint returns
  `permission: "read"` for them (the `permission` field does not distinguish `triage` from plain
  `read`). As a result, triage users are currently denied by this permission check. A future change
  could also inspect the `role_name` field in the API response to grant access to triage users.
  This limitation also applies to `close-pr` (noted in doc 041).

#### Token Service: `pr_action_links.py` (was `label_pr_links.py`)
- Mirrored `close_pr_links.py` (Fernet encryption, `iat`/`exp` claims) — literally, byte for byte
  apart from one URL path line, which is why the two were later merged into a single
  `pr_action_links.py` keyed by a `PRAction` constant. See design doc 041 for the consolidation note;
  it is wire-compatible and the settings below are unchanged.
- Claims: `zulip_user_id`, `github_login`, `pr_owner`, `pr_repo`, `pr_number`.
- Settings:
  - `ZULIP_LABEL_PR_TOKEN_SECRET` (falls back to `SECRET_KEY`)
  - `ZULIP_LABEL_PR_TOKEN_SALT` (default: `"zulip_bot.label_pr"`)
  - `ZULIP_LABEL_PR_TOKEN_TTL_SECONDS` (default: `1800`)
  - `ZULIP_PREFS_URL_BASE` (shared with prefs/registration/close-pr links)

#### Execution Service: `label_pr_execution.py`
- `check_label_pr_permission(github_login, owner, repo, number)` → `PermissionCheckResult`
  - Same return type as `close_pr_execution.py` (`PermissionOutcome` enum + `PermissionCheckResult`).
  - Reuses or mirrors the check pattern; does not grant permission to authors.
- `fetch_issue_details_for_form(owner, repo, number)` → `LiveIssueDetails | None`
  - `GET /repos/{owner}/{repo}/issues/{number}` — works for both PRs and plain issues.
  - Returns title, state, author, opened/updated timestamps, and live labels.
- `fetch_repo_labels_from_db(owner, repo)` → `list[LabelDef]`
  - Reads `LabelDef.objects.filter(repository__owner=owner, repository__name=repo).order_by("name")`.
  - Returns DB-side label catalog for the repo; used to populate the form's checkbox list.
- The *current* label set on the issue/PR is read live from `LiveIssueDetails.labels` (returned
  by `fetch_issue_details_for_form` above) and used to drive checkbox pre-selection. The DB
  (`PRLabel`) is **not** consulted for current state, because it lags GitHub and is empty for
  plain issues; using it as the source of truth for a `PUT` replacement risks silent label loss.
- `set_pr_labels(owner, repo, number, label_names)` → None, raises `LabelPRError`
  - `PUT /repos/{owner}/{repo}/issues/{number}/labels` with the full desired set.
  - Uses `label_pr` operation token (`queueboard-assignment`).
- `LabelPRError(code, message)` — mirrors `ClosePRError`.

#### Web Layer
- URL: `label-pr/<str:token>/` → `views.label_pr_form`
- View behavior:
  - Validate token; show `label_pr_invalid.html` (HTTP 403) on expiry/invalid.
  - Fetch live issue/PR details from GitHub (`fetch_issue_details_for_form`) for both the
    metadata card and current label state. If the fetch fails, render an error card and refuse
    to show the picker — a `PUT` replacement against unknown current state could drop labels.
  - Fetch the available-label catalog from DB (`fetch_repo_labels_from_db`).
  - Pre-check boxes whose name matches a label on `LiveIssueDetails.labels` (case-insensitive).
    Any live label that is not in the `LabelDef` catalog is appended to the picker as an extra
    pre-checked row, so the user can see it and PUT replacement does not silently drop it.
  - **GET**: render label-picker form. If issue/PR is already closed, show informational
    message without submit button. If the live fetch failed, show error card with no form.
  - **POST**: refuse to mutate when the live fetch failed or the issue/PR is closed. Otherwise,
    if `ZULIP_LABEL_PR_MUTATIONS_ENABLED` is off, show preflight-only message; else:
    1. Set labels via execution service (`PUT /repos/{owner}/{repo}/issues/{number}/labels`).
    2. Enqueue `sync_pr_task.delay(repository_id, pr_number)` best-effort (skipped for plain issues
       not tracked in the DB; logged at DEBUG).
    3. Send DM to invoker confirming the change (best-effort).
    4. Post to repo log thread if `ZULIP_REPO_LOG` has an entry for `owner/repo` (best-effort).
    5. Render success state in same template (PRG pattern).
  - `Cache-Control: no-store` on all responses.

#### Template: `label_pr_form.html`
- Extends a shared base template (see below) for common page structure and PR/issue metadata.
- Label picker: a scrollable checkbox list showing all `LabelDef` entries for the repo, with label
  color badges. Boxes are pre-checked when the label name appears in the live label set returned
  by `fetch_issue_details_for_form` (case-insensitive). Any live label not present in the catalog
  is rendered as an additional pre-checked row at the bottom of the list. User can check/uncheck
  freely; submit sends the full desired set.
- If the live GitHub fetch failed, the form is replaced with an error card explaining that
  labels cannot be edited safely without a confirmed current set; no submit button is shown.
- "Select all" / "Clear all" convenience buttons (JavaScript, no server round-trip).
- JavaScript warning before submitting with zero labels checked: "This will remove all labels."
- Success and error states rendered inline (same template, conditional blocks).

#### Shared template and CSS
- Extract the PR/issue metadata card (title, author, dates, label badges, body) from
  `close_pr_form.html` into a Django template include:
  `qb_site/templates/zulip_bot/partials/_pr_card.html`.
  Both `close_pr_form.html` and `label_pr_form.html` use `{% include %}` to render it.
- Move the PR card CSS (`.pr-card`, `.pr-title`, `.pr-meta-row`, `.pr-labels`, `.pr-label`,
  `.pr-body`, `.countdown`, `.attribution-notice`) from `close_pr_form.css` into `shared_pages.css`
  so it is available to both forms without duplication.
- `label_pr_form.css` adds only label-picker-specific styles (checkbox list, label chip layout).
- `close_pr_form.css` retains only styles specific to the message/preset area.

### Part 2: Label Picker in `close-pr` Form

#### Motivation
When closing a PR, a maintainer often wants to simultaneously tag it (e.g. "stale",
"wont-fix", "duplicate"). Adding a label picker to the close-pr form avoids the two-step
`label-pr` → `close-pr` flow for this common case.

#### Behavior
- **Add-only**: The close-pr label picker lets users *add* labels alongside closing. It does not
  replace existing labels. This is intentional — the user's intent is to close the PR and optionally
  tag it, not to perform full label management. Uses
  `POST /repos/{owner}/{repo}/issues/{number}/labels` rather than `PUT`.
- **Optional**: No label selection is required; the close action proceeds with or without labels.
- **Source**: Available labels come from the DB (`LabelDef` for the repo); current labels are shown
  (already fetched for display) and cannot be unchecked (to keep the UI simple and avoid accidental
  removal during an already-complex form action).
- **Ordering**: Mutually, label addition happens after the optional comment and before the PR close
  call (so a label-add failure can be surfaced inline without having closed the PR yet).
- **Error handling**: If label addition fails, show inline error and do not proceed with close. User
  can retry with a different selection or clear the label selection to close without adding labels.
  This matches the existing comment-error behavior.

#### Changes to Existing Files
- `close_pr_execution.py`: add `add_pr_labels(owner, repo, number, label_names)` helper using
  `POST /repos/{owner}/{repo}/issues/{number}/labels` and the existing `_get_token()` call.
- `label_pr_execution.py`: `fetch_repo_labels_from_db(owner, repo)` — shared utility used by
  both `label_pr_form` and `close_pr_form` views.
- `close_pr_form.html`: add label picker section (checkboxes, none pre-checked by default, since the
  goal is to *add* labels rather than manage the full set). Current labels are shown as read-only
  badges above the picker. Uses the shared `_pr_card.html` partial (see template factoring above).
- `views.py` (`close_pr_form`): load label context from DB; handle `selected_labels` POST field;
  call `add_pr_labels` before `close_pull_request` if any labels selected.
- `views.py` (`_enqueue_close_pr_post_actions`): include selected labels in the DM and repo log
  messages, e.g. "PR X was closed by Y [labels added: `stale`, `wont-fix`]." No labels selected →
  no label mention in the log.

### Settings Summary
| Setting | Default | Description |
|---|---|---|
| `ZULIP_LABEL_PR_MUTATIONS_ENABLED` | `False` | Feature flag for label-pr mutations |
| `ZULIP_LABEL_PR_TOKEN_SECRET` | `SECRET_KEY` | Fernet key for label-pr tokens |
| `ZULIP_LABEL_PR_TOKEN_SALT` | `"zulip_bot.label_pr"` | HKDF salt for label-pr tokens |
| `ZULIP_LABEL_PR_TOKEN_TTL_SECONDS` | `1800` | Token validity window |

`ZULIP_REPO_LOG` (already defined) is reused for label-pr log posts.

### GitHub App Changes
No new apps or permission changes are required. The `queueboard-assignment` app already has
`Issues: Read and write`, which covers both `POST` and `PUT` to
`/repos/{owner}/{repo}/issues/{number}/labels`. Add `label_pr` to `GITHUB_APP_TOKEN_CONFIG`
`operation_app_map` pointing to the same app as `close_pr`.

Example addition to `operation_app_map`:
```json
"label_pr": { "app": "queueboard-assignment" }
```

## Subtleties / Invariants
- **Catalog from DB, current state from live GitHub**: The picker's *catalog* (which labels exist
  in the repo) is read from `LabelDef` to avoid an extra API call per form load — DB lag here is
  benign, since a newly-created label simply won't appear in the picker until syncer runs. The
  *current* label set on the issue/PR is read live from `GET /issues/{number}`, because
  `set_pr_labels` uses `PUT` (full replacement) and a stale DB snapshot would silently drop labels
  that exist on GitHub but haven't synced (or aren't tracked, e.g. plain issues).
- **Live labels not in the catalog**: A label can exist on GitHub but be absent from `LabelDef`
  (e.g. created on GitHub since the last sync, or never seen because the issue isn't a tracked PR).
  Such labels are appended to the picker as extra pre-checked rows so the user can see them and
  PUT replacement does not silently remove them. They render without a color badge since the
  catalog row that would carry the color is missing.
- **Live fetch failure refuses the form**: If `fetch_issue_details_for_form` returns `None`, the
  view does not render the picker (and POST refuses to mutate). Without a confirmed current label
  set, a `PUT` could replace unknown state — there is no safe default action.
- **Full replacement vs. add-only**: `label-pr` uses `PUT` (full replacement), which is why the
  form pre-checks current labels. `close-pr` integration uses `POST` (add-only), since users aren't
  expected to manage the full label set while closing. Both approaches are safe with the GitHub API.
- **Race conditions on label state**: Between the form-render fetch and form submission, another
  user may change labels. For `label-pr` (full replacement), the submitted set wins — expected for
  a label management tool, and the TTL is short enough to bound the window. For `close-pr`
  (add-only), new labels added by others are preserved.
- **Empty selection in `label-pr`**: Submitting with zero boxes checked sends `PUT []`, removing all
  labels. The form warns via JavaScript before proceeding. This is intentional — `label-pr` is a
  full label editor.
- **Plain issues vs. PRs**: `label-pr` accepts issue URLs in addition to PR URLs. The GitHub
  `/issues/{number}` endpoint and label/permission APIs all work identically for both, so the form
  behaves the same way. Sync enqueue after mutation is skipped for plain issues (not tracked in
  the `PullRequest` table).
- **Triage role limitation**: In org-owned repos, users with the GitHub `triage` role can apply
  labels and close issues/PRs, but the collaborator permission endpoint's `permission` field returns
  `"read"` for them (it does not distinguish `triage` from plain read). As a result, triage users
  are currently denied by both `label-pr` and `close-pr`. A future change could inspect the
  `role_name` field in the API response to grant access. This is a known limitation, not a
  correctness bug; the current check errs on the side of caution.
- **Sync enqueue after label mutation**: `sync_pr_task` is enqueued best-effort after the mutation
  to pull updated label state back into the DB promptly. Skipped for plain issues (no DB record).
- **`ZULIP_REPO_LOG` for label-pr**: If the repo has no log entry, a WARNING is emitted and no
  Zulip post is sent — same behavior as `close-pr`.
- **No author exception for `label-pr`**: Authors without write access cannot use `label-pr`
  (unlike `close-pr` which grants an author exception). This matches GitHub's own access model.
- **Token `github_login` embedding**: Same as `close-pr` — if the user's GitHub login changes
  between token issuance and submission, the embedded login is used for attribution. Acceptable
  given the short TTL.
- **No `kind` field in token**: The token stores `(owner, repo, number)` but not whether the item
  is a PR or plain issue. The form view does not need to distinguish — the live
  `/issues/{number}` fetch and the `PUT /labels` endpoint work identically for both. The parser
  accepts `/pull/NNN` and `/issues/NNN` interchangeably and extracts the same tuple.
- **`label_pr_form.js` imports from `close_pr_form.js`**: The `formatTimestamp` helper is re-exported
  from `close_pr_form.js` and imported by `label_pr_form.js` to avoid duplication. Both files also
  import `getExpiryState`/`formatRemaining` from `prefs_form.js`.

## Implementation Plan

### Commit 1 (this doc)
- Add `docs/design-decisions/042-zulip-label-pr-command.md`.

### Commit 2: `label-pr` command, form, and shared template/CSS refactor ✓
- New files:
  - `qb_site/zulip_bot/commands/label_pr.py`
  - `qb_site/zulip_bot/services/label_pr_links.py` (later merged into `pr_action_links.py`)
  - `qb_site/zulip_bot/services/label_pr_execution.py`
  - `qb_site/templates/zulip_bot/label_pr_form.html`
  - `qb_site/templates/zulip_bot/label_pr_invalid.html`
  - `qb_site/templates/zulip_bot/partials/_pr_card.html` — extracted PR/issue metadata card partial
  - `qb_site/zulip_bot/static/zulip_bot/label_pr_form.css`
- Modified files:
  - `qb_site/zulip_bot/views.py` — add `label_pr_form` view
  - `qb_site/zulip_bot/urls.py` — add `label-pr/<str:token>/`
  - `qb_site/zulip_bot/static/zulip_bot/shared_pages.css` — absorb PR card styles from
    `close_pr_form.css`
  - `qb_site/zulip_bot/static/zulip_bot/close_pr_form.css` — remove PR card styles now in shared
  - `qb_site/templates/zulip_bot/close_pr_form.html` — switch PR card to `{% include %}` partial
  - `qb_site/zulip_bot/AGENTS.md` / `CLAUDE.md` — document new command and settings
  - `docs/github_app_setup.md` — document `label_pr` operation mapping

### Commit 3: Label picker in `close-pr` form ✓
- Modified files:
  - `qb_site/zulip_bot/services/close_pr_execution.py` — add `add_pr_labels` helper
  - `qb_site/zulip_bot/views.py` — extend `close_pr_form` to load labels from DB and handle
    `selected_labels` on POST; update `_enqueue_close_pr_post_actions` to include label list in DM
    and repo log message
  - `qb_site/templates/zulip_bot/close_pr_form.html` — add label picker section

## Validation Plan
- Tests:
  - Token service (now `pr_action_links.py`, covered for both actions by `tests/test_pr_action_links.py`): issue → validate round-trip; expiry; invalid/tampered; cross-action rejection.
  - Permission check: permitted as write collaborator; permitted as admin; denied for read-only;
    denied for non-collaborator; issue/PR closed; token unavailable.
  - `fetch_repo_labels_from_db`: returns correct `LabelDef` list for known repo; empty list for
    unknown repo.
  - `set_pr_labels`: successful PUT; GitHub error; token unavailable.
  - `add_pr_labels` (close-pr integration): successful POST; GitHub error; empty selection no-ops.
  - Command handler (PR URL): no linked user; no GitHub login; not permitted; permitted; PR not open.
  - Command handler (issue URL): same cases; issue URL parsing works alongside PR URL parsing.
  - `label_pr_form` view (GET): valid open PR (live labels pre-checked); valid open issue (live
    labels pre-checked); live labels outside the `LabelDef` catalog rendered as extra rows;
    case-insensitive name matching; already-closed; live fetch failure (error card, no form);
    expired token; invalid token.
  - `label_pr_form` view (POST): successful label set with sync + DM + log; mutations disabled
    preflight; GitHub error on set; empty selection (proceeds after JS warning); live fetch
    failure refuses to mutate.
  - `close_pr_form` view (POST with labels): label add succeeds before close; label add fails
    (inline error, no close); no labels selected (close proceeds normally).
  - Log message content: close without labels includes no label mention; close with labels includes
    "labels added: …"; label-pr mutation log entry.
  - PR card partial `_pr_card.html`: renders correctly when included in both form templates.
  - `ZULIP_REPO_LOG` for label-pr: repo with entry; missing entry (skip + warn).
- Manual checks:
  - Verify `PUT /repos/{owner}/{repo}/issues/{number}/labels` with the `label_pr` token succeeds.
  - Verify `POST /repos/{owner}/{repo}/issues/{number}/labels` with the `close_pr` token succeeds.
  - End-to-end `label-pr` on a PR: issue command → receive link → open form → adjust labels →
    submit → labels updated on GitHub → DB sync enqueued → DM received → log thread updated.
  - End-to-end `label-pr` on a plain issue: live labels pre-checked from GitHub; submit updates labels.
  - End-to-end `close-pr` with labels: receive link → open form → select labels + optional message
    → submit → labels added, PR closed → DM and log include label mention.
  - Issue `label-pr` as read-only collaborator → receive private "permission denied" message.
  - Open confirmation link after TTL → see 403 invalid-token page.
  - Submit `label-pr` form with zero labels checked → confirm JS warning shown before submitting.

## Operational Deployment Notes

### Before enabling in production
1. **Add `label_pr` to `GITHUB_APP_TOKEN_CONFIG`** `operation_app_map` pointing to
   `queueboard-assignment`. No new app or permission change needed.
2. **Add `label-pr` to `ZULIP_COMMAND_POLICY`**. Same group restrictions as `close-pr`:
   ```json
   {
     "label-pr": {
       "allowed_groups": [<maintainers_group_id>],
       "allowed_contexts": ["dm", "stream:*"]
     }
   }
   ```
3. **Add `ZULIP_LABEL_PR_MUTATIONS_ENABLED = True`** once ready for production label mutations.
4. **`ZULIP_REPO_LOG`** is already configured for `close-pr`; the same config is reused for
   `label-pr` log posts with no additional setup.
5. **Optional token settings** (all have reasonable defaults):
   - `ZULIP_LABEL_PR_TOKEN_SECRET`, `ZULIP_LABEL_PR_TOKEN_SALT`, `ZULIP_LABEL_PR_TOKEN_TTL_SECONDS`

### Manual verification checklist (after deployment)
- [ ] `PUT /repos/{owner}/{repo}/issues/{number}/labels` succeeds with `queueboard-assignment` token.
- [ ] `POST /repos/{owner}/{repo}/issues/{number}/labels` succeeds with `queueboard-assignment` token.
- [ ] End-to-end `label-pr` on a PR (command → link → form → adjust labels → submit → labels updated).
- [ ] End-to-end `label-pr` on a plain issue (live labels pre-checked from GitHub; submit works).
- [ ] End-to-end `close-pr` with label selection (select labels → submit → labels added + PR closed;
      log and DM include label mention).
- [ ] Non-write-access user receives permission denied message for `label-pr`.
- [ ] Expired token shows 403 invalid-token page.
- [ ] Zero-label submission on `label-pr` form shows JavaScript warning before proceeding.

## Progress Notes
- 2026-04-24: Design doc written and revised: added issue URL support for `label-pr`, triage-role
  limitation note, shared template partial and CSS factoring plan, close-pr log label mention, and
  plain-issue DB caveat.
- 2026-04-24: Commit 2 complete. Post-implementation note: `label_pr_form.js` imports
  `formatTimestamp` from `close_pr_form.js`.
- 2026-04-24: Commit 3 complete. `close_pr_form.html` loads `label_pr_form.css` for shared picker
  styles. `ZULIP_LABEL_PR_MUTATIONS_ENABLED` env var wiring was found missing from `base.py` and
  fixed (settings must always be declared in both `base.py` and `.env.example`). Label picker is
  hidden when no `LabelDef` rows exist for the repo (syncer must have run).
- 2026-04-24: Switched current-label pre-selection from DB (`PRLabel`) to live GitHub state
  (`LiveIssueDetails.labels`). Original DB-based design caused silent data loss on plain issues
  (DB always empty) and on tracked PRs whenever syncer lagged: PUT replacement against a stale
  empty/partial set would drop labels that exist on GitHub. Side effects: removed
  `fetch_current_pr_label_names_from_db` and the `has_db_labels` `PullRequest`-existence check;
  live labels not in `LabelDef` are now appended to the picker as extra pre-checked rows; live
  fetch failure now refuses to render the form (no safe default for a PUT against unknown state).
