# Reviewer Preferences Form (Console-Served, Session-Authenticated)

> Status: **Implemented.** Reviewers edit their preferences at `/console/preferences/`, authenticated
> by the reviewer console's GitHub-OAuth session. The original design — an expiring Fernet link DM'd
> by the Zulip `prefs` command — shipped first and was retired in full; the history is kept below
> because the constraints it was built for still explain parts of the current shape. This closes the
> deferred follow-up recorded in `050-reviewer-assignment-acceptance-gate.md` ("Stable `/prefs` URL
> sharing the console session").

## Context

- Reviewers must be able to self-serve edits to `core.ReviewerPreference` (capacity, auto-assign,
  away time, topic labels, nudge thresholds, acceptance mode) without a Django account.
- **The original model (stages A/B, now removed):** the Zulip `prefs` command DM'd a link containing
  a Fernet token whose claims were `{user_id, zulip_user_id, preference_ids, iat, exp}` with a
  30-minute TTL. The page authenticated the *link*, not the person.
- What that cost: the URL was a bearer secret, so it could not be bookmarked and had to be
  re-requested from Zulip for every edit; the editable set was a snapshot taken when the link was
  issued, so a repository activated later needed a fresh link; and reviewers with no Zulip link could
  not get one at all — `prefs` requires a `core.User` matched by `zulip_user_id`, so reviewers created
  by `core.services.reviewer_topics_importer` were locked out of their own preferences.
- Doc 050 then shipped the reviewer console, which already had the auth this form wanted: GitHub
  OAuth → a Django session holding a resolved `core.User` id, at a stable token-less URL. Keeping two
  auth models over the same writable rows was the drift risk this repo avoids elsewhere (one validity
  authority, one chunking helper, one `_prepare_assignment_inputs`).

## Decision

Serve the form from the reviewer console at **`/console/preferences/`** under the console session.
Admission to being a reviewer is unchanged; only authentication moved.

- **Admission** — becoming a reviewer at all — stays where it was: the deny-by-default
  `ZULIP_COMMAND_POLICY` gate (`zulip_bot/webhook/policy.py`) on the commands that hand out a
  registration link (`prefs` for an unknown sender, `register-test`), plus the two maintainer-side
  paths, `import_reviewer_topics` and the Django admin.
- **Authentication** — proving you are that reviewer — is GitHub OAuth via `console.session`.
- The console adds **no new way to become a reviewer**; invariants 1–2 are what make that true in code
  rather than by convention.

### One authority per concern

| Concern | Owner |
| --- | --- |
| Editable field set + validation | `core.forms.ReviewerPreferenceForm` |
| Formset assembly, ownership scoping, label catalog | `core.services.reviewer_prefs` |
| Field markup | `templates/shared/_reviewer_prefs_fields.html` |
| Timezone for local times | `zulip_bot.services.user_timezone` |
| The page itself | `console.views.prefs` + `templates/console/prefs.html` |

`core.services.reviewer_prefs` reads `syncer.models.LabelDef` at module scope, matching the existing
`core/admin.py` precedent. Timezone resolution lives in `zulip_bot` because its authoritative source
is Zulip's own user record and `core/services` otherwise carries no app dependencies; console access
stays independent of Zulip *reachability* — an unlinked or unreachable Zulip falls through to
`core.User.timezone`, then the Django default.

### Invariants

1. **The console never creates `ReviewerPreference` rows.**
   `core.services.github_identity.resolve_user_from_identity` is resolve-only, but it resolves *any*
   `core.User` — and `syncer.services.sub.pull_request_sync` upserts one for every PR author
   (`upsert_user_from_github(..., create_missing=True)`), so "known GitHub account" means thousands of
   contributors, not reviewers. Because a `ReviewerPreference` row *is* candidate-pool membership
   (`analyzer.services.reviewer_assignment.build_reviewer_catalog`) and `auto_assign` defaults to
   `True`, a "create my preferences" affordance on a public sign-in surface would let any contributor
   into the assignment pool. The page edits existing rows only and shows an explanatory empty state
   otherwise; bootstrapping stays in `zulip_bot.services.registration_bootstrap`.
2. **Console admission is "is a reviewer here", not "is a known GitHub account".** A session is
   granted only to a user with ≥1 `ReviewerPreference` row **or** an active `AssignmentProposal` for
   their login — the OR clause keeps a reviewer whose row is removed mid-flight able to answer the
   proposal already made to them (the hourly expiry sweep retires it otherwise). Enforced at
   `console.views.oauth_callback` (refuse the session, 403) and on every view via
   `_reviewer_from_session`. This *tightened* the shipped console, where any syncer-ingested
   contributor could previously sign in and see an empty dashboard.
3. **Ownership scoping replaces token scoping.** `build_preferences_formset` narrows the queryset to
   the supplied rows **and** their owner on GET *and* POST, so a posted `form-<n>-id` naming another
   reviewer's row fails validation (Django validates formset ids against the queryset it is handed).
   The token-era anti-tamper checks are gone, not reimplemented.
4. **Scope is live, not a snapshot.** The page always edits the reviewer's current rows, so a
   repository activated after registration appears on its own.

### Entry points

- **`prefs` (Zulip)** DMs the stable URL. It stays a *DM* command even though the URL is not secret:
  an accidental mention in a public stream must not post a reply there, so nothing is returned to the
  triggering conversation. (`console` is the deliberate exception — an in-place reply by design, doc
  050.) Its registration-link branch is a DM for the stronger reason, that link being a bearer
  secret; its "no preferences to edit" branch remains, because a reviewer with no rows would be
  refused by invariant 2 and Zulip is a better place to say so than a 403.
- **Registration** (`zulip_bot.views.register_github_callback`) advertises the same URL in its success
  DM and page, and **opens the console session** (`console.session.set_reviewer`) so the link lands
  signed in with no second OAuth round-trip. Success path only, and only when the new reviewer
  actually has rows. That promotion is strictly stronger than a console sign-in: registration proves
  Zulip identity (the registration token) *and* GitHub identity (OAuth).
- **The console** links it from the home dashboard, and the `console` command mentions it.

### Session, not TTL

Nothing about the page expires. The Django session bounds an editing window instead, and
`SESSION_SAVE_EVERY_REQUEST = True` slides the two-week `SESSION_COOKIE_AGE` on activity so a long
edit cannot POST into a lapsed cookie; the `beforeunload` dirty guard is the backstop.

### Front-end assets

`console/static/console/prefs_form.{css,js}` are console-owned. The JS is progressive enhancement
only — an unsaved-changes guard and the per-row "clear away time" buttons — with **no countdown**,
which is what let the expiry helpers stay behind in `zulip_bot/static/zulip_bot/expiry.js` for the
close-pr / label-pr token pages that still need them. (Those pages import them as a *sibling* module,
and a relative ES import must resolve both as a served path in the browser and an on-disk path under
vitest, so colocation there is load-bearing.) Vitest specs live in `qb_site/zulip_bot/frontend/tests/`
and cover both apps' modules; CI runs them (`.github/workflows/ci.yml`).

## Consequences

- One auth model across every reviewer-facing surface, and a bookmarkable URL. Reviewers with no
  Zulip link can finally edit their own preferences.
- Preferences editing now depends on GitHub OAuth being configured (sign-in renders 503 otherwise);
  `QUEUEBOARD_BASE_URL` and the root-registered OAuth callback
  (`docs/zulip_github_oauth_setup.md`) are prerequisites for prefs, not just for the console.
- Contributors who could previously sign in to an empty console now get "only for registered
  reviewers". Intended (invariant 2), but user-visible.
- Editing scope widened from a token snapshot to all current rows, which is why ownership filtering
  must be applied on POST as well as GET.
- `core.User.timezone` is still only ever set by hand in the Django admin, so in practice the
  timezone comes from Zulip's user record per render, or the project default. See Follow-Ups.

## Operational Notes

- **Settings.** No prefs-specific settings remain. `ZULIP_PREFS_TOKEN_SECRET` / `_SALT` /
  `_TTL_SECONDS` were removed with the token flow. The registration token and its OAuth state shared
  that secret, so it was renamed to `ZULIP_LINK_TOKEN_SECRET`, whose `base.py` definition still reads
  the legacy `ZULIP_PREFS_TOKEN_SECRET` env name — a silent fall back to `SECRET_KEY` would have
  invalidated every in-flight registration link on deploy. `CONSOLE_PREFS_ENABLED`, which staged the
  rollout, is also gone: with the token path retired the console page is the only prefs surface, so a
  flag that could disable it would only be a foot-gun. Deployments may drop all four env vars.
- **Retired in phase 3:** the `/api/zulip/prefs/<token>/` route, `zulip_bot/services/prefs_links.py`,
  `templates/zulip_bot/prefs_form.html`, `templates/zulip_bot/prefs_invalid.html`, and the token test
  module (its form-behavior coverage moved to `console/tests/test_prefs_form_fields.py`, its
  field-accounting guard to `core/tests/test_reviewer_preference_form.py`, and its timezone-chain
  coverage to `zulip_bot/tests/test_user_timezone.py`).
- **Pre-flight for the retirement:** every `ReviewerPreference` row's user must have a usable
  `github_login` — under GitHub OAuth that, not `zulip_user_id`, is what would lock someone out.
  Registration always sets it (it goes through GitHub OAuth), as does `import_reviewer_topics`.
- **Tests.** `console/tests/test_prefs.py` (auth, admission gate, no-row-creation, cross-reviewer POST,
  timezone), `console/tests/test_prefs_form_fields.py` (fields, validation, label catalog),
  `zulip_bot/tests/test_user_timezone.py`, `core/tests/test_reviewer_preference_form.py`, and the
  vitest specs above.

## Follow-Ups

- **Persist a browser-reported timezone.** `core.User.timezone` is admin-only today; storing
  `Intl.DateTimeFormat().resolvedOptions().timeZone` on first visit would make the fallback real and
  let us drop the per-render Zulip API round-trip.
- Optional confirmation DM after a successful preferences change (stage C3 of the original plan).
- Structured logging for saves/rejections (stage C1) — never token or form contents.

## Alternatives (discarded)

- **Keeping both auth paths** (token link *and* session) permanently: two writable surfaces over the
  same rows, one of them a bearer secret in a URL. Rejected; the token path was retired after a soak.
- **Stateful one-time links / token revocation lists** (original stage C2): moot once the session is
  the credential — signing out is the revocation.
- **A prefs editor under `/admin/` only:** rejected in the original design because the requirement is
  self-service for reviewers, who have no Django account. Still true.
- **Moving the prefs JS/CSS to `static/shared/`:** attempted; `close_pr_form.js` and
  `label_pr_form.js` import the expiry helpers from `./prefs_form.js` as a sibling, and the served
  and on-disk layouts differ, so only colocation satisfied both. Resolved instead by splitting
  `expiry.js` out and giving the console its own countdown-free module.

## Related Decisions

- `021-zulip-bot-architecture.md` (command policy, name space)
- `025-zulip-self-serve-registration-and-github-verification.md`
- `050-reviewer-assignment-acceptance-gate.md` (the console and its auth model)
