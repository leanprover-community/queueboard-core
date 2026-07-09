# Zulip Bot Guidelines

## Scope
- `qb_site/zulip_bot/` owns webhook intake, command routing, policy checks, preference/registration flows, and Zulip API interactions.
- Keep command surface in `commands/`, reusable logic in `services/`, and webhook request parsing/policy in `webhook/`.

## High-Value Commands and Tests
```bash
# App test suite
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test zulip_bot

# Policy inspection helper
docker compose exec -T web python qb_site/manage.py zulip_policy

# Frontend tests (prefs form)
cd qb_site/zulip_bot/frontend && npm test
```

## Command Architecture Notes
- Commands live in `commands/`: `assign`, `unassign`, `assigned-prs`, `pr-info`, `prefs`, `help`, `echo`, `register_test`, `close-pr`, `label-pr`.
- `pr-info`: parses GitHub PR links from Zulip `rendered_content`, reacts with 👀, then sends one message per PR (up to 10) with queue info sourced from `analyzer.services.pr_info`. Replies in the same conversation as the triggering message. Renders the acceptance-gate "Proposed to X (awaiting acceptance, expires …)" state (design doc 050) on its own line, distinct from Assignees.
- Assignment command flow (all under `services/`) is split for clarity:
  - parse: `assignment_command_parser.py`,
  - validate: `assignment_validation.py`,
  - preflight/mutation orchestration: `assignment_execution.py` + `assignment_preflight.py`.
- `close-pr` command: checks GitHub permission at command time, then issues a short-lived private link to a confirmation form. Services: `close_pr_links.py` (token), `close_pr_execution.py` (permission check + `close_pull_request` + `add_pr_labels` + `post_pr_comment`). Feature flag: `ZULIP_CLOSE_PR_MUTATIONS_ENABLED`. Uses operation `close_pr` via the GitHub App token system. The confirmation form includes an optional add-only label picker (checkboxes from `LabelDef` DB, none pre-checked); selected labels are POSTed to GitHub before closing and mentioned in the DM/log.
- `label-pr` command: same secure-link pattern as `close-pr`. Accepts both `/pull/NNN` and `/issues/NNN` URLs. Requires write/admin collaborator access (no author exception). Services: `label_pr_links.py` (token), `label_pr_execution.py` (permission check + `PUT /issues/{number}/labels`). Feature flag: `ZULIP_LABEL_PR_MUTATIONS_ENABLED`. Uses operation `label_pr` (mapped to `queueboard-assignment`). The picker catalog comes from `LabelDef` in DB; current-label pre-selection comes from the live `GET /issues/{number}` response (because `PUT /labels` replaces the full set, a stale source would silently drop labels). Live labels not in the catalog are appended as extra pre-checked rows; if the live fetch fails, the form refuses to render the picker. URL parsing for both PR and issue URLs is in `assignment_command_parser._parse_single_issue_or_pr_ref`.
- Keep user-facing command responses explicit and safe for partial failures.
- Prefer private failure responses for sensitive mutation/policy errors.

## How Command Replies Are Routed

**Zulip outgoing webhook responses always go back to the same conversation as the triggering message** — stream messages get stream replies, DM messages get DM replies. There is no supported mechanism to redirect a webhook response to a different destination via the response body.

This has a critical implication: **never return sensitive content (token links, private URLs) in `CommandResult.content` if the command can be invoked from a stream.** Doing so would expose that content to all stream subscribers.

### Two patterns for replies

**In-place reply** (most commands): return `CommandResult(content="...")`. Zulip delivers the reply wherever the command was invoked. Use this for non-sensitive content where in-stream visibility is acceptable (e.g., `assign`, `unassign`, `pr-info`, error messages).

**Proactive DM** (commands that send private links): call `ZulipClient().send_direct_message()` directly and return `CommandResult(response_not_required=True)`. Zulip does not deliver the webhook response at all; the DM goes to the user regardless of where the command was invoked. Use this whenever the reply contains a private token link or other content that must not appear in a stream.

Commands currently using the proactive DM pattern: `close-pr`, `label-pr`, `prefs`, `register_test`, `assigned-prs`.

### Adding new commands

- If the command replies with a private link (or any content that must stay private): use the proactive DM pattern. See `commands/close_pr.py` for the canonical example.
- If the command replies with non-sensitive status text: return `CommandResult(content=...)` directly.
- The `CommandResult.response_not_required` field is also used by commands that send multiple messages proactively (e.g., `assigned-prs` chunks large reports across several DMs).

## Policy and Safety Notes
- Command availability and context restrictions are controlled by `ZULIP_COMMAND_POLICY`.
- Mutation paths are feature-flagged (`ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`, `ZULIP_CLOSE_PR_MUTATIONS_ENABLED`, `ZULIP_LABEL_PR_MUTATIONS_ENABLED`) and depend on GitHub operation-token services.
- Do not log secrets/tokens or raw sensitive payload fragments.

## Per-Repo Zulip Log
- `ZULIP_REPO_LOG` is a JSON setting mapping `"owner/repo"` to `{"stream": "...", "topic": "..."}`.
- Used by both `close-pr` and `label-pr` to post an audit log entry after a mutation.
- If a repo has no entry, the log post is skipped and a WARNING is emitted.

## Registration and Preferences
- Registration-link/state behavior is in `services/`:
  - `registration_links.py`,
  - `registration_oauth_state.py`,
  - `registration_linking.py`,
  - `registration_bootstrap.py` (initial bootstrap helpers),
  - `prefs_links.py` (preference deep-link generation),
  - `close_pr_links.py` (close-PR confirmation link generation),
  - `label_pr_links.py` (label-PR confirmation link generation).
- Zulip prefs form/UI behavior spans Django forms/views and `frontend/` tests; keep behavior parity across backend validation and frontend affordances.

## Testing Expectations
- Canonical full validation for repo changes is `bash scripts/repo_check_compose.sh`.
- In sandboxed environments where Docker is blocked:
  - run app-level tests that are still feasible,
  - run frontend unit tests independently when possible,
  - clearly report which integration paths (webhook + DB + Celery) were not exercised.
