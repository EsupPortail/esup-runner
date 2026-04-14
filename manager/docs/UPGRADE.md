# Upgrade Guide — ESUP Runner (Manager)

This document describes a safe **production** upgrade procedure for the *Manager*.

> TL;DR
>
> - **PATCH**: you can usually upgrade Manager and Runner independently.
> - **MINOR/MAJOR**: the Manager **rejects** runners whose `MAJOR.MINOR` does not match its own → plan a **coordinated** upgrade.

---

## 0) Prerequisites

- Installed under `/opt/esup-runner` (or equivalent).
- systemd user service installed: `esup-runner-manager`.
- Configuration file: `/opt/esup-runner/manager/.env`.

Useful docs:

- Installation: `INSTALLATION.md`
- Docker installation: `DOCKER.md` (if your manager is deployed in a container)
- Configuration: `CONFIGURATION.md`
- Parameters: `PARAMETERS.md`
- Changelog: `CHANGELOG.md`
- Version management: `VERSION_MANAGEMENT.md`

---

## 1) Manager ⇄ Runner compatibility (important)

The Runner sends its version number in the `X-Runner-Version` header during registration and heartbeats.

The Manager enforces **MAJOR + MINOR** compatibility:

- Runner `X.Y.*` is accepted only by a Manager `X.Y.*`
- `PATCH` versions may differ

Consequence:

- If you upgrade the Manager to `X.(Y+1).Z` while your runners are still `X.Y.*`, they will no longer be able to register.
- Same issue if you upgrade a runner to `X.(Y+1).Z` before the Manager.

Recommendation:

- For a **MINOR/MAJOR** bump, prepare the upgrade of both the Manager **and** the runners and do a short cutover.

---

## 2) Recommended backups

Before any upgrade:

- Backup configuration:
  - `/opt/esup-runner/manager/.env`
- Backup task persistence (if you need to keep history):
  - `/opt/esup-runner/manager/data/` (daily rotation, one JSON per task)
- Backup logs if needed:
  - the `LOG_DIR` directory defined in `.env` (default: `/var/log/esup-runner`)
- Optional (to keep local uv cache warm):
  - `CACHE_DIR` from `.env` (default: `/home/esup-runner/.cache/esup-runner`)

Compatibility note: legacy names `LOG_DIRECTORY` and `RUNNERS_STORAGE_PATH` are still accepted.

---

## 3) Update sources

This deployment uses a **monorepo clone**: the Git working tree is `/opt/esup-runner`, and the projects live in subdirectories (`manager/`, `runner/`).

If you use sparse-checkout, ensure the correct paths are included **depending on what is installed on this machine**:

- If **Runner + Manager are installed on the same machine**: include both.
- If **only Manager is installed**: include only `manager`.

```bash
cd /opt/esup-runner

# If you use sparse checkout and the automated helper script:
# - manager only host: git sparse-checkout set manager update-stack.sh
# - manager + runner host: git sparse-checkout set manager runner update-stack.sh

# Fetch history and tags
git fetch --tags

# Update the current branch (recommended in prod: ff-only)
git pull --ff-only

# Option: switch to a tagged Manager version
# git checkout manager-vX.Y.Z
```

---

## 4) Review configuration (.env)

After an upgrade, it is common for new environment variables to appear.

Recommended: compare your `.env` with `.env.example`.

```bash
cd /opt/esup-runner/manager

# Warning: .env contains secrets
diff -u .env.example .env || true
```

- Add any new missing variables to `.env`.
- Do not accidentally remove your existing tokens.

---

## 5) Update Python dependencies

### If needed, update `uv` first

If `uv sync --locked` reports that `uv.lock` needs to be updated even though you already pulled the latest repository changes, first verify the installed `uv` version:

```bash
uv --version
```

Update `uv` for the current user with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
cd /opt/esup-runner/manager
make sync
```

Recommended quick checks:

```bash
uv run scripts/check_version.py
```

(Optional) Run tests:

```bash
make test
```

---

## 6) Re-initialize directories (if needed)

If the new version introduces new directories (logs, shared storage, etc.), run again:

```bash
cd /opt/esup-runner/manager
sudo make init
```

---

## 7) Update / redeploy the systemd service

If the systemd unit file changed, you must re-install it.

> Warning
> `make create-service` **overwrites** `~/.config/systemd/user/esup-runner-manager.service` with `production/esup-runner-manager.service`.
> If you customized the unit, diff it first.

```bash
cd /opt/esup-runner/manager
make create-service
```

Run this as the service user (without `sudo`) so the unit is installed under `~/.config/systemd/user/`.

This performs: service copy → `systemctl --user daemon-reload` → `systemctl --user enable --now`.

---

## 8) Post-upgrade validation

### Service

```bash
systemctl --user status esup-runner-manager
journalctl --user -u esup-runner-manager -n 200 --no-pager
```

### Version endpoint

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/api/version"
```

### Runner compatibility

- Check the Manager logs to confirm runners (re-)register.
- If compatibility errors happen, you will typically see a rejection related to `X-Runner-Version`.

---

## 9) Rollback procedure

1) Stop the service:

```bash
systemctl --user stop esup-runner-manager
```

2) Go back to a known tag/commit:

```bash
cd /opt/esup-runner
git fetch --tags
git checkout manager-vX.Y.Z
```

3) Resync dependencies and restart:

```bash
cd /opt/esup-runner/manager
make sync
systemctl --user start esup-runner-manager
```

4) Check logs.

---

## 10) Automated stack update script

A helper script is available in monorepo deployments:

```bash
cd /opt/esup-runner
./update-stack.sh --help
```

Detection rules used by the script:
- Manager is considered installed when `/opt/esup-runner/manager/.env` exists.
- Runner is considered installed when `/opt/esup-runner/runner/.env` exists.

Examples:

```bash
cd /opt/esup-runner

# Update detected components (manager and/or runner)
./update-stack.sh

# Update manager only
./update-stack.sh --manager-only

# Display commands without executing them
./update-stack.sh --dry-run --skip-uv-update --skip-git-update
```

Cron example:

```cron
0 3 * * 1 cd /opt/esup-runner && ./update-stack.sh >> /var/log/esup-runner/update-stack.log 2>&1
```

Notes:
- By default, `make init` is skipped. Use `--with-init` only when directories/permissions changed.
- By default, restart policy is `if-changed` (restart only when `manager/` changed after git update). You can force a restart with `--always-restart`, or disable it with `--no-restart`.
- User services require an active user manager (`systemd --user`). For unattended reboots/cron, enable lingering once: `sudo loginctl enable-linger esup-runner`.
- If `make init` needs elevated privileges (for example when `.env` paths are under `/var`), run the script as `esup-runner` and configure restricted passwordless `sudo` for `make init`.
- The script can also update `runner` on the same host if both `.env` files are present and no `--manager-only/--runner-only` flag is used.
