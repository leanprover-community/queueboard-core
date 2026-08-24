# Session-Authenticated Per-PR Action Pages (`close-pr`, `label-pr`)

> Status: **Deferred.** No implementation. This records the question, the two ways to answer it, and
> the reason we are not answering it yet, so the next person does not re-derive it. Design doc 022
> retired the expiring prefs link in favour of the console session; the obvious follow-on question is
> whether the two remaining private-link commands should follow. They should not follow *the same
> way*, and the blocking question is about admission, not about tokens.

## Context

- Two Zulip commands still hand out expiring Fernet links to a web form:
  - `close-pr` → `/api/zulip/close-pr/<token>/` (`views.close_pr_form`)
  - `label-pr` → `/api/zulip/label-pr/<token>/` (`views.label_pr_form`)
- Design doc 022 removed the third (`prefs`) and moved that form to `/console/preferences/` under the
  reviewer console's GitHub-OAuth session.

### Why the prefs migration was pure subtraction

The prefs token carried `{user_id, zulip_user_id, preference_ids, iat, exp}` — **authentication** plus
**row scoping**. Both had exact session-era replacements: OAuth identity, and a queryset filtered by
the session reviewer on GET and POST (022 invariant 3). Nothing else was in the token, so retiring it
removed a bearer secret from a URL and gained a bookmarkable page.

### Why these two are not the same shape

The close/label tokens carry four things, and a session replaces only the first:

1. **Who you are** — `zulip_user_id`, `github_login`. ← a console session replaces this
2. **Which PR** — `pr_owner`, `pr_repo`, `pr_number`.
3. **A permission decision already made.** `check_close_pr_permission` /
   `check_label_pr_permission` run at *command* time, in the command module. Neither
   `views.close_pr_form` nor `views.label_pr_form` imports them. **Possession of the token is the
   authorization.**
4. **A 30-minute window** — which, given (3), is the only thing that currently expires a permission
   that has since been revoked.

So a straight port is not subtraction. (3) has to be replaced by a live permission check on every GET
*and* again immediately before the mutation. That is the bulk of the work — and it is also the main
argument *for* doing it eventually, because it is strictly better than what ships today:

- Revoked access is honoured. Today a token minted while you had write access stays good for its full
  TTL after the access is gone.
- OAuth proves the acting `github_login` directly. Today the chain is Zulip sender → `core.User` →
  `github_login`, verified at registration and trusted thereafter; doc 041 explicitly accepts the
  staleness ("the close still proceeds using the embedded login").

Note what is *not* an argument. The TTL is not the safety mechanism for a destructive action — the
two-step confirmation form is, and that survives either way. And the ergonomic wins from prefs
(bookmarkable, re-editable, live-scoped) do not transfer: a one-shot per-PR confirmation reached from
a DM gains only "does not expire while you read it".

## The blocking question: whose console is it?

Console admission is deliberately **"is a reviewer here"** — a session is granted only to a `core.User`
with ≥1 `ReviewerPreference` row **or** an active `AssignmentProposal` for their login, enforced at
`console.views.oauth_callback` *and* per view via `_reviewer_from_session` (022 invariant 2, which
*tightened* the console shipped in doc 050).

The two commands admit a different, wider population:

| Command | Admits | Overlap with reviewers |
| --- | --- | --- |
| `label-pr` | write/admin collaborators only (no author exception) | high, not total |
| `close-pr` | write/admin collaborators **or the PR author** | the author branch is mostly *outside* it |

A PR author closing their own PR is precisely the person with no `ReviewerPreference` row. Hosting
`close-pr` behind today's gate would lock out a core part of its audience.

### Option A — widen the console session, authorize per page

`oauth_callback` grants a session to any resolvable GitHub identity. `_reviewer_from_session` keeps
guarding the dashboard and `/console/preferences/`; the action pages guard themselves with the live
GitHub permission check, which is the real authorization anyway.

- **For:** one auth model everywhere; each page states its own requirement, which is honest about the
  fact that "reviewer" and "may close this PR" were never the same predicate.
- **Against:** partially reverses 022 invariant 2 — contributors could sign in again. Invariant 1 (the
  console never creates `ReviewerPreference` rows, so nobody self-serves into the assignment pool) is
  untouched and does the load-bearing safety work, so the cost is surface area and a confusing empty
  dashboard rather than a pool-membership hole. Still a deliberate reversal, not a side effect.

### Option B — keep the console reviewer-only, host the pages elsewhere

The action pages live outside `/console/` (staying in `zulip_bot`, or a new app) but reuse
`console.session` plus the same OAuth round-trip.

- **For:** 022 invariant 2 stands verbatim; the console keeps one meaning.
- **Against:** two sign-in surfaces over one session cookie, and `console.session.set_reviewer` gains a
  third caller. The console `AGENTS.md` already flags that bar ("Any future caller of `set_reviewer`
  outside this app must clear that same bar") — and a caller admitting non-reviewers does not clear
  it, so the helper would need to grow a distinction between "reviewer session" and "identity
  session". At which point this is Option A with extra steps and a worse name.

**If forced today: Option A**, with the reviewer predicate moved wholly into the views that need it.
Option B's separation is nominal — the same cookie either way — and it pays for that nominal
separation with a second sign-in surface.

### What neither option fixes

`core.services.github_identity.resolve_user_from_identity` is resolve-only. A write-access
collaborator the syncer has never ingested has no `core.User` and gets a 403. This is **not** a
regression — the command path already requires a linked `core.User` (`User.objects.filter(
zulip_user_id=...)`) — but it means the migration does not widen reach, only changes how identity is
proven.

## Why deferred

- The win is auth consistency plus fresher permission checks. Both are real; neither is urgent, and no
  reviewer has asked.
- The cost is a live permission check per page load (1–2 GitHub API calls, one of them needing the
  `queueboard-org-read` app's `check_collaborator_permission` token), plus resolving the admission
  question above, plus new form/CSRF/expiry-free page design for two flows.
- Nothing about the current flow is broken or unsafe. The one sharp edge — a token outliving the
  permission it encodes — is bounded at 30 minutes.

## If and when we do it

1. **Settle admission first** and amend this doc; everything else follows from it.
2. **`label-pr` first.** Non-destructive, idempotent, write-access-only, and its form already re-reads
   live state (`fetch_issue_details_for_form`, because `PUT /labels` replaces the whole set). Its
   audience overlaps reviewers most, so it exercises the least of the admission change.
3. **`close-pr` after that soaks.** Destructive, and its author branch is what makes the admission
   answer load-bearing.
4. Keep the Zulip command as the entry point in both cases — it DMs a session URL instead of a token.
   The DM is the natural way in; nobody bookmarks a per-PR confirmation form.
5. Re-check permission on GET *and* immediately before the mutation, not once at page load.

## Done already (independent of this decision)

Consolidating the duplicated link-token services did **not** wait on this: `close_pr_links.py` and
`label_pr_links.py` were byte-identical apart from one URL path, and both hand-rolled the Fernet
primitive that `core.services.signed_payloads` already provides. They are now one
`zulip_bot/services/pr_action_links.py` and `registration_links.py` delegates to the same primitive —
wire-compatible, so in-flight tokens still validate. Less code to migrate later, and less to keep
correct if we never do.

## Related Decisions

- `022-zulip-prefs-form-design.md` — the migration this one asks about repeating; invariants 1–3
- `041-zulip-close-pr-command.md` — the close-pr token model and its permission check
- `042-zulip-label-pr-command.md` — the label-pr token model
- `050-reviewer-assignment-acceptance-gate.md` — the console and its auth model
- `025-zulip-self-serve-registration-and-github-verification.md` — the registration link, the one
  bearer-secret link that must stay one (it authenticates someone who has no account yet)
