# Running Docker checks from a sandboxed environment

Why `DOCKER_CONFIG` and `DOCKER_HOST` may be set for a sandboxed shell on this machine, and how to
rebuild or undo that setup. If you are reading this because you found those variables in a sandbox
config and cannot remember why: [the short version](#the-short-version).

Only needed when a **sandbox wraps the shell** (e.g. an agent harness restricting filesystem
writes). A normal terminal needs none of this — `bash scripts/repo_check_compose.sh` just works.

## The short version

Docker Desktop's credential helper crashes when it cannot write to `~/Library/Containers/`, which
takes down every image pull — even though this project pulls only public images and needs no
credentials at all. Pointing `DOCKER_CONFIG` at a config that omits `credsStore` skips the helper
entirely. `DOCKER_HOST` is required because overriding `DOCKER_CONFIG` also moves where the CLI
looks for its *contexts*.

Verified 2026-08-27: with this in place, `scripts/repo_check_compose.sh` completes all 12 steps
inside the sandbox, image build included.

## Symptom

```
failed to solve: error getting credentials - err: exit status 1, out: ``
```

preceded by a `docker-credential-desktop` stack trace:

```
[docker-credential-desktop.paths][F] creating <HOME>/Library/Containers/com.docker.docker/Data:
    mkdir <HOME>/Library/Containers: file exists
```

## Diagnosis in one command

```bash
echo '{"ServerURL":"https://index.docker.io/v1/"}' | docker-credential-desktop get
```

A `[F] creating <HOME>/Library/Containers/...` line means the helper is the problem. Anything else
— including "credentials not found" — means it is healthy and your build is failing for some other
reason. Use this before changing anything, and again to confirm a fix.

## Root cause

`docker-credential-desktop` calls `MkdirAll` on `~/Library/Containers/com.docker.docker/Data`
during startup. Under the sandbox every `stat` up that chain is denied, so it walks to the top and
tries `mkdir ~/Library/Containers`, gets `EEXIST`, and cannot `stat` to confirm it is a directory —
which it treats as fatal. It dies before doing any credential work, and BuildKit reports the
generic "error getting credentials".

The helper runs only because `~/.docker/config.json` sets `"credsStore": "desktop"`. Every image
this stack pulls is public — `python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine` — so
nothing here needs a credential helper.

**Read-only access to those paths does not help.** The failure is a write.

## Setup

Everything lives in the repo and is gitignored, so this works from inside the sandbox and needs no
host step:

```bash
mkdir -p .docker-nocreds
printf '{"auths":{}}\n' > .docker-nocreds/config.json
ln -sfn /Applications/Docker.app/Contents/Resources/cli-plugins .docker-nocreds/cli-plugins

cat > .docker-env <<EOF
DOCKER_CONFIG=$(pwd)/.docker-nocreds
DOCKER_HOST=unix://$HOME/.docker/run/docker.sock
EOF
```

`.docker-env.example` in the repo root carries the same thing with the commentary.

**Why in the repo and not `~/.docker-nocreds`?** Because a new directory under `$HOME` is not
readable from the sandbox. This is worth knowing because the failure is easy to misread: the
sandbox returns `ENOENT` for home paths that do not exist, but `EPERM`
(`Operation not permitted`) for ones that exist outside its allowlist — so a `~/.docker-nocreds`
you just created looks identical to a permissions bug. The repo directory is both readable and
writable, which also means the setup can be recreated without leaving the sandbox.

The symlink points at Docker Desktop's own plugin directory, so this never references `~/.docker`
at all — see [Does this affect my other Docker projects?](#does-this-affect-my-other-docker-projects).
(`~/.docker/cli-plugins` works equally well; its entries are themselves symlinks into that same
bundle. Pointing at the bundle keeps the two configurations visibly independent.)

Docker only ever **reads** this directory, so it can be read-only (`chmod 555`) if you prefer —
verified. Note the contrast with `~/Library/Containers/com.docker.docker/`, where read-only is
*not* enough, because there the helper is trying to create directories.

## Passing it to the sandbox

Point your sandbox wrapper's env-file option at `.docker-env`, or source it:

```bash
set -a; . ./.docker-env; set +a
```

Use **absolute paths** — a `~` will not be expanded by Docker or, in most cases, by the wrapper.
The generator above writes absolute paths for you.

## Does this affect my other Docker projects?

No. Nothing here writes to, modifies, or is read from `~/.docker`.

- **The symlink goes the other way than it may read.** `ln -s TARGET LINK` creates `LINK`, so the
  new symlink lives *inside* `~/.docker-nocreds/` and points outward at a directory Docker Desktop
  owns. No file is created or changed anywhere else.
- **Other projects never see any of this.** `DOCKER_CONFIG` is set only for the sandboxed process.
  Any other shell falls back to `~/.docker/config.json`, `credsStore: "desktop"` included, and
  keeps using the credential helper normally — including for private registries.
- **The undo is safe.** `rm -rf ~/.docker-nocreds` removes the symlink itself, not what it points
  at; `rm -rf` does not follow symlinks. Verified explicitly, because getting this wrong would
  delete your Docker CLI plugins.
- **The only shared thing is the daemon socket** (`DOCKER_HOST`), which is the same socket every
  Docker client on the machine already talks to. Containers, images and volumes are unaffected.

The one real trade-off is the one in [When this stops working](#when-this-stops-working): inside
the sandbox you have no credential helper, so a private-registry image would fail there. Outside
the sandbox, nothing changes.

## Verify

Run these **inside the sandbox** — that is the environment the problem appears in:

```bash
echo '{"ServerURL":"https://index.docker.io/v1/"}' | docker-credential-desktop get   # no [F] line
bash scripts/repo_check_compose.sh                                                   # exit 0, [0/12]..[12/12]
```

Run the script **unpiped** — `… | tail` reports `tail`'s status, not the script's, so a failure
reads as success. See the Testing Guidelines in the root `AGENTS.md`.

## When this stops working

This trades away credential support, which is free *only* while every image is public. **If a
private-registry image is ever added to `docker-compose.yml` or `Dockerfile`, this setup breaks**
with an authentication error on that image. At that point you need the real helper, which means
granting the sandbox **write** access to `~/Library/Containers/com.docker.docker/` and dropping
these two variables.

That is also the alternative if you would rather not maintain this at all: open that path, keep
Docker's normal credential flow, and delete `~/.docker-nocreds`.

## Undo

```bash
rm -rf .docker-nocreds .docker-env
```

and stop passing the env file to the sandbox. Nothing outside the repo was ever changed — `rm -rf`
removes the symlink, not Docker Desktop's plugin directory that it points at (verified, since
getting that wrong would delete your Docker CLI plugins).
