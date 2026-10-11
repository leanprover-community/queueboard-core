# Settings From Files (`<NAME>_FILE`)

## Context
- Every setting is read from the process environment (design doc 000), and deployments usually
  supply it through an env file.
- Some values do not survive an env file intact:
  - `GITHUB_APP_TOKEN_CONFIG` is a JSON object, usually with each App's private key inline as a
    `\n`-escaped PEM. `ZULIP_COMMAND_POLICY` and `ZULIP_REPO_LOG` are JSON too.
  - Secrets generated elsewhere can contain `$`, `#` or quotes (Django's key generator draws from
    `!@#$%^&*(-_=+)`), which some env-file formats interpolate, treat as a comment, or strip.
  - Most env-file formats cannot hold a multi-line value at all.
- A widespread container convention covers this: mount the secret as a read-only file and set
  `<NAME>_FILE` to its path.
- Not every reader goes through Django settings, so a fix inside the settings modules alone would
  miss some:
  - `syncer/services/github_client.py` and `syncer/services/archive_bootstrap.py` read
    `GH_TOKEN`/`GITHUB_TOKEN` from `os.environ` at call time.
  - Celery reads `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` from `os.environ` itself, ahead of
    the values in settings (`celery.app.utils.Settings.broker_url` / `.result_backend`).
  - `local.py` and `ci.py` re-read `DJANGO_SECRET_KEY` after importing `base.py`.

## Decision
- For each name in `FILE_BACKED_ENV_VARS` (`qb_site/qb_site/settings/env_files.py`), a deployment
  may set `<NAME>_FILE` to the path of a file holding the value, instead of setting `<NAME>`.
- The allowlist is the secrets plus the JSON settings: `DJANGO_SECRET_KEY`, `DJANGO_DB_PASSWORD`,
  `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` (both can embed a password), `GH_TOKEN`,
  `GITHUB_TOKEN`, `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_TOKEN_CONFIG`,
  `ZULIP_WEBHOOK_TOKEN`, `ZULIP_BOT_API_KEY`, `ZULIP_USER_API_KEY`, `ZULIP_LINK_TOKEN_SECRET` and
  its legacy alias `ZULIP_PREFS_TOKEN_SECRET`, `ZULIP_CLOSE_PR_TOKEN_SECRET`,
  `ZULIP_LABEL_PR_TOKEN_SECRET`, `ZULIP_COMMAND_POLICY`, `ZULIP_REPO_LOG`,
  `SITE_ANALYTICS_HASH_SALT`. Any other `*_FILE` variable is ignored; many belong to other tools
  (`SSL_CERT_FILE`, ...).
- `resolve_env_files()` runs straight after the imports in `base.py`, before any setting is read.
  Every entry point loads settings before any of these values is used: `manage.py`, WSGI/ASGI, and
  Celery (which loads its config from Django settings before it reads the broker URL). For each
  allowlisted name whose `<NAME>_FILE` is non-empty, it reads the file, stores the contents in
  `os.environ[<NAME>]`, and deletes `<NAME>_FILE`. From then on, every reader sees the value as if
  `<NAME>` had been set directly.
- Rules:
  - `<NAME>` and `<NAME>_FILE` both set → `RuntimeError` naming both variables.
  - An empty value counts as unset, for either variable (the same rule as the `file_env` helper in
    the official `postgres` image). A template line `NAME=` left blank does not conflict with
    `NAME_FILE`.
  - A file that is missing, unreadable, not UTF-8, or contains a NUL byte → `RuntimeError` naming
    the variable and the path.
  - A file that is empty, or holds only a newline → `RuntimeError`. An empty file is a secret that
    failed to render; with a blank value the process would start and fail later, on first use (a
    blank `DJANGO_SECRET_KEY` passes the production check and fails on the first signed request).
  - One trailing newline (`\n` or `\r\n`) is stripped. Nothing else is: leading and trailing
    whitespace stays.
  - Error messages never include a value. The UTF-8 and NUL errors are raised with no exception
    chained to them, because a decode error holds the file's bytes.

## Consequences
- Plain environment variables behave exactly as before. Nothing changes unless a `<NAME>_FILE` is
  set.
- One mechanism, in one place, covers settings, direct `os.environ` readers, Celery, and the
  settings overlays. No read site changes, and a new reader of an allowlisted name is covered
  without doing anything.
- The value ends up in the process environment, just as when it is set directly, and child
  processes inherit it. Deleting `<NAME>_FILE` matters for those children: `runserver`'s
  autoreloader starts a child with the parent's environment, and with both variables it would fail
  the conflict check.
- The file is read once, when the process starts. A changed file needs a restart, as a changed
  environment variable does.
- A value must be one that an environment variable can hold, so it cannot contain a NUL byte.
- A file's value goes through the same parsing as the variable's. A JSON file can be
  pretty-printed. A `GH_TOKEN`/`GITHUB_TOKEN` file keeps the variable's format: comma-separated
  tokens on one line.
- A relative `private_key_path` inside `GITHUB_APP_TOKEN_CONFIG` still resolves against the
  process's working directory, not the config file's directory. Only the `github_app_config`
  helper resolves it against the file. Use absolute paths in a config file the app loads.
- Adding a setting that holds a secret or JSON means adding its name to `FILE_BACKED_ENV_VARS`.

## Operational Notes
- Example with Compose secrets (remove `GITHUB_APP_TOKEN_CONFIG` from the env file):

  ```yaml
  services:
    web:
      environment:
        GITHUB_APP_TOKEN_CONFIG_FILE: /run/secrets/github_app_token_config
      secrets:
        - github_app_token_config
  secrets:
    github_app_token_config:
      file: ./github-app-config.json
  ```

- `python qb_site/manage.py github_app_config validate <file>` checks the same file the app will
  load, and `to-env` is not needed.
- Tests: `core/tests/test_env_files.py` covers the resolver, and runs `base.py` afresh under a
  patched environment. The direct readers are covered in
  `syncer/tests/client/test_github_client.py` and `syncer/tests/services/test_archive_bootstrap.py`.

## Alternatives
- **A helper (`env_value("NAME")`) called at each read site.** Every new read site would have to
  remember to use it. It cannot reach Celery's own `os.environ` lookup. `local.py`, `ci.py` and
  both syncer services would each need changing.
- **Honouring every `*_FILE` variable.** It would capture variables meant for other tools, and turn
  any environment variable into a file-read primitive. With an allowlist, the set of file-backed
  names can be reviewed.
- **Stripping all surrounding whitespace.** Secrets are opaque and may legitimately begin or end
  with a space, so a file and a variable holding the same bytes could yield different values.
  Readers that do want stripping (the JSON settings, the token lists, `QUEUEBOARD_BASE_URL`)
  already strip, and still do.
- **A settings library.** Design doc 000 chose the standard library, and this needs a few dozen
  lines.
