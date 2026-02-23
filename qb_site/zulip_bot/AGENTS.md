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
- Assignment command flow is split for clarity:
  - parse: `assignment_command_parser.py`,
  - validate: `assignment_validation.py`,
  - preflight/mutation orchestration: `assignment_execution.py` + `assignment_preflight.py`.
- Keep user-facing command responses explicit and safe for partial failures.
- Prefer private failure responses for sensitive mutation/policy errors.

## Policy and Safety Notes
- Command availability and context restrictions are controlled by `ZULIP_COMMAND_POLICY`.
- Mutation paths are feature-flagged (`ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`) and depend on GitHub operation-token services.
- Do not log secrets/tokens or raw sensitive payload fragments.

## Registration and Preferences
- Registration-link/state behavior is in:
  - `registration_links.py`,
  - `registration_oauth_state.py`,
  - `registration_linking.py`.
- Zulip prefs form/UI behavior spans Django forms/views and `frontend/` tests; keep behavior parity across backend validation and frontend affordances.

## Testing Expectations
- Canonical full validation for repo changes is `bash scripts/repo_check_compose.sh`.
- In sandboxed environments where Docker is blocked:
  - run app-level tests that are still feasible,
  - run frontend unit tests independently when possible,
  - clearly report which integration paths (webhook + DB + Celery) were not exercised.
