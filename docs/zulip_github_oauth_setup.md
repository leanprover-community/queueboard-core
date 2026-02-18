# Zulip Registration GitHub OAuth Setup

This guide covers how to configure GitHub OAuth for Queueboard's Zulip registration flow.

## Scope
- Flow entrypoint: `/api/zulip/register/<token>/`
- OAuth start endpoint: `/api/zulip/register/<token>/github/`
- OAuth callback endpoint: `/api/zulip/register/github/callback/`
- Current behavior:
  - OAuth verifies GitHub identity and returns a callback confirmation page.
  - DB account linking/bootstrap is implemented separately.

## 1) Create the GitHub OAuth App

1. Go to GitHub settings for the owning account/org:
   - `Settings` -> `Developer settings` -> `OAuth Apps` -> `New OAuth App`
2. Fill in:
   - `Application name`: `Queueboard OAuth` (or env-specific name)
   - `Homepage URL`: any URL (for example `https://github.com/leanprover-community/queueboard-core`)
   - `Authorization callback URL`:
     `https://<your-host>/api/zulip/register/github/callback/`
   - You can leave `Enable Device Flow` unchecked.
3. Create the app.
4. Copy:
   - `Client ID`
   - Generate and copy `Client Secret`

Notes:
- GitHub OAuth Apps have a single callback URL setting. Use one app per environment if callback hosts differ (recommended).
- Keep client secret out of git and out of logs.

## 2) Configure Environment Variables

Set these in your Queueboard environment (`.env` or deployment secrets):

- Required:
  - `GITHUB_OAUTH_CLIENT_ID`
  - `GITHUB_OAUTH_CLIENT_SECRET`
- Recommended:
  - `GITHUB_OAUTH_REDIRECT_URI`
    Use the same URL configured in GitHub app callback settings, e.g.
    `https://queueboard.example/api/zulip/register/github/callback/`

Optional overrides (defaults shown):
- `GITHUB_OAUTH_AUTHORIZE_URL=https://github.com/login/oauth/authorize`
- `GITHUB_OAUTH_TOKEN_URL=https://github.com/login/oauth/access_token`
- `GITHUB_API_URL=https://api.github.com`
- `GITHUB_OAUTH_SCOPE=read:user`

Related registration token settings:
- `ZULIP_PREFS_URL_BASE` (used for registration links too)
- `ZULIP_PREFS_TOKEN_SECRET` (shared secret material)
- `ZULIP_REGISTRATION_TOKEN_SALT` (registration token namespace)
- `ZULIP_REGISTRATION_TOKEN_TTL_SECONDS`
- `ZULIP_REGISTRATION_OAUTH_STATE_SALT` (OAuth state token namespace)
- `ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS`

## 3) Local Development Example

For local Django at `http://localhost:8000`:

- GitHub OAuth app callback URL:
  - `http://localhost:8000/api/zulip/register/github/callback/`
- Local env:
  - `GITHUB_OAUTH_CLIENT_ID=<client-id>`
  - `GITHUB_OAUTH_CLIENT_SECRET=<client-secret>`
  - `GITHUB_OAUTH_REDIRECT_URI=http://localhost:8000/api/zulip/register/github/callback/`
  - `ZULIP_PREFS_URL_BASE=http://localhost:8000`

If your team shares a deployed app, prefer separate local vs production OAuth apps.

## 4) Quick Verification Checklist

0. Add a command policy entry for live testing, for example:
   - `register_test`: allow your admin/test Zulip group in `dm`.
1. Send `register_test` to the bot in a DM.
   - Bot should return a fresh registration link regardless of whether a Queueboard user row already exists.
2. Open registration link:
   - Page should show `Continue with GitHub` when OAuth is configured.
3. Click `Continue with GitHub`:
   - Browser should redirect to GitHub authorize page.
4. Authorize:
   - GitHub should redirect back to `/api/zulip/register/github/callback/`.
5. Callback page should display:
   - verified GitHub login
   - GitHub node id
   - Zulip user id from registration claims

Example policy snippet:

```json
{
  "register_test": {
    "allowed_groups": [1234],
    "allowed_user_ids": [101],
    "allowed_contexts": ["dm"]
  }
}
```

## 5) Common Failure Modes

- "GitHub OAuth is not configured yet"
  - Missing `GITHUB_OAUTH_CLIENT_ID` or `GITHUB_OAUTH_CLIENT_SECRET`.
- OAuth callback invalid/failed
  - `GITHUB_OAUTH_REDIRECT_URI` mismatch with GitHub app callback URL.
  - Expired or tampered OAuth `state` token.
  - Registration token expired before callback completed.
- Link expired
  - User waited past token TTL; ask user to run `prefs` again.

## 6) Security Guidance

- Use HTTPS in non-local environments.
- Rotate `GITHUB_OAUTH_CLIENT_SECRET` via deployment secret manager.
- Do not expose OAuth client secret in frontend code.
- Avoid very long TTLs for registration tokens and OAuth state tokens.

## 7) References

- Design decision: `docs/design-decisions/025-zulip-self-serve-registration-and-github-verification.md`
- Zulip bot architecture: `docs/design-decisions/021-zulip-bot-architecture.md`
