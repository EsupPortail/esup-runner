# Upgrade Guide — ESUP Runner (Manager)

This document describes a safe **production** upgrade procedure for the *Manager*.

> TL;DR
> - **PATCH**: you can usually upgrade Manager and Runner independently.
> - **MINOR/MAJOR**: the Manager **rejects** runners whose `MAJOR.MINOR` does not match its own → plan a **coordinated** upgrade.

---

## 0) Prerequisites

- Installed under `/opt/esup-runner` (or equivalent).
- systemd service installed: `esup-runner-manager`.
- Configuration file: `/opt/esup-runner/manager/.env`.

Useful docs:
- Installation: `INSTALLATION.md`
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
  - the `LOG_DIRECTORY` directory defined in `.env` (default: `/var/log/esup-runner`)

---

## 3) Update sources

This deployment uses a **monorepo clone**: the Git working tree is `/opt/esup-runner`, and the projects live in subdirectories (`manager/`, `runner/`).

If you use sparse-checkout, ensure the correct directories are included **depending on what is installed on this machine**:

- If **Runner + Manager are installed on the same machine**: include both.
- If **only Manager is installed**: include only `manager`.

```bash
cd /opt/esup-runner

# Fetch history and tags
git fetch --tags

# Update the current branch (recommended in prod: ff-only)
git pull --ff-only

# Option: switch to a tagged version
# git checkout vX.Y.Z

# Sparse-checkout selection:
# - same machine (runner + manager):
#   git sparse-checkout set manager runner
# - manager only:
#   git sparse-checkout set manager
git sparse-checkout set manager
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
> `make create-service` **overwrites** `/etc/systemd/system/esup-runner-manager.service` with `production/esup-runner-manager.service`.
> If you customized the unit, diff it first.

```bash
cd /opt/esup-runner/manager
sudo make create-service
```

This performs: service copy → `daemon-reload` → `enable` → `restart`.

---

## 8) Post-upgrade validation

### Service

```bash
systemctl status esup-runner-manager
journalctl -u esup-runner-manager -n 200 --no-pager
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
sudo systemctl stop esup-runner-manager
```

2) Go back to a known tag/commit:

```bash
cd /opt/esup-runner
git fetch --tags
git checkout vX.Y.Z
```

3) Resync dependencies and restart:

```bash
cd /opt/esup-runner/manager
make sync
sudo systemctl start esup-runner-manager
```

4) Check logs.
