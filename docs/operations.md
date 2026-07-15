# Operations

All commands in this guide assume the repository is checked out at `/opt/twitch-drop-automator`. Run them from that directory unless an absolute path is shown.

## Service model

Docker Compose runs two long-lived services and one initializer:

- `app`: Gunicorn, Flask, Socket.IO, the automation manager, Xvfb, and bundled Playwright Chromium;
- `db`: PostgreSQL 16.14, reachable only on the internal Compose network.
- `data-init`: a short-lived, capability-limited ownership repair for the persistent browser volume; it must exit successfully before `app` starts.

Both use `restart: unless-stopped`, bounded JSON-file logs, health checks, a 60-second graceful stop period, and Docker's init process. The app has a 1 GiB `/dev/shm` allocation for Chromium, a 4 GiB memory ceiling, and a 512-process ceiling. PostgreSQL has a 1 GiB memory ceiling and a 256-process ceiling. The app intentionally uses one Gunicorn worker so only one process owns the persistent browser and scheduler state.

An automation run that was active is persisted and resumes after a normal process, container, Docker, or VM restart. An explicit operator **Stop** is persisted and does not resume. Browser and Twitch failures use bounded exponential backoff and relaunch Chromium when appropriate. A manager watchdog reconciles persisted-enabled users every 30 seconds, so a rare browser worker-thread exit is repaired even though Docker does not restart a merely unhealthy container.

## Routine checks

Use the bundled diagnostic first:

```bash
sudo bash scripts/doctor.sh
```

It checks the supported OS, Docker access, `.env` permissions and non-placeholder secrets, Compose validity, container health, both HTTP probes, PostgreSQL, free disk, backup recency, available memory, and clock synchronization. It does not print secret values. For automated monitoring, use `sudo bash scripts/doctor.sh --strict`; strict mode exits nonzero when a monitored filesystem reaches 85%, the backup location is unavailable, or the latest completed backup is older than 48 hours.

Useful direct commands are:

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 app db
sudo docker compose logs --follow app
sudo docker stats --no-stream
df -h
```

Docker rotates each container's logs at 10 MiB and retains five files. Application state does not belong in the logs.

## Health monitoring

The probes do not require an application login and must remain reachable only on the trusted network:

- `/health/live`: the web process is serving requests;
- `/health/ready`: startup completed and required dependencies, including PostgreSQL, are usable.

Example private monitor:

```bash
curl --fail --max-time 10 http://192.168.1.50:5000/health/ready
```

Alert after repeated readiness failures rather than one failure during an update. Docker allows a 90-second application health start period for migrations and browser-manager initialization.

## Start, stop, and restart

```bash
# Start or reconcile configuration, then wait for health
sudo docker compose up -d --wait --wait-timeout 300

# Gracefully restart only the application
sudo docker compose restart --timeout 60 app

# Follow the restart until it is ready
sudo docker compose up -d --wait --wait-timeout 300 app

# Administratively stop the whole stack
sudo docker compose stop --timeout 60
```

A manual `docker compose stop` intentionally suppresses automatic restart until the stack is started again. Normal VM shutdown is graceful; after a later VM boot, Docker restarts containers that were not administratively stopped.

## Backups

The installer creates the dedicated mode-`0700` `BACKUP_ROOT`. Create a backup on a filesystem with enough free space:

```bash
sudo bash scripts/backup.sh
```

The script waits for PostgreSQL, briefly stops the app to quiesce Chromium, takes a custom-format PostgreSQL dump, archives `/data`, copies the current environment and Compose file, records the Git revision in a manifest, creates SHA-256 checksums, and restores both long-lived services to their previous running state. Expect a short automation interruption.

Each timestamped directory contains:

- `database.dump`;
- `app-data.tar.gz`;
- `environment.env`;
- `compose.yaml`;
- `manifest.txt`;
- `SHA256SUMS`.

The environment copy and browser archive contain credentials or authenticated session material. Keep backup directories mode `0700`, encrypt off-host copies, and limit access to the operator. After a successful backup and service restart, the script prunes timestamped local backups older than 14 days. Set `BACKUP_RETENTION_DAYS` on the backup service to a value from 7 through 3650 when a different local recovery window is required. This local rotation is not an off-host copy.

### Schedule a daily backup

Create `/etc/systemd/system/twitch-drop-automator-backup.service`:

```ini
[Unit]
Description=Back up Twitch Drop Automator
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
Environment=BACKUP_RETENTION_DAYS=14
ExecStart=/usr/bin/bash /opt/twitch-drop-automator/scripts/backup.sh
TimeoutStartSec=2h
```

Create `/etc/systemd/system/twitch-drop-automator-backup.timer`:

```ini
[Unit]
Description=Daily Twitch Drop Automator backup

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
```

Enable and test it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now twitch-drop-automator-backup.timer
sudo systemctl start twitch-drop-automator-backup.service
sudo journalctl -u twitch-drop-automator-backup.service --no-pager
```

Copy every completed backup to encrypted storage outside the Proxmox host before local retention expires. Prefer mounting dedicated backup storage at `BACKUP_ROOT` or synchronizing the completed timestamped directory after the oneshot succeeds. The diagnostic script checks the backup filesystem and warns when the latest completed local backup is older than 48 hours. Periodically restore one into an isolated test VM; an untested backup is not a recovery plan.

## Restore

Restore replaces the current database and browser/application volume. The script validates checksums and archive paths before making changes, and requires typing `RESTORE` unless `--yes` is explicitly supplied.

```bash
sudo bash scripts/restore.sh \
  /srv/backups/twitch-drop-automator/20260715T031500Z
```

The active `.env` is retained so a replacement VM can use its own bind address and newly initialized database password. If it differs from `environment.env`, the script warns without displaying either value. Review the backed-up environment separately when investigating a recovery, and never copy an old bind address blindly to a new VM.

Before replacement, restore validates the manifest, checksums, archive paths, and PostgreSQL dump, then writes a safety backup beside the selected backup under `pre-restore-safety/`. It stops and verifies the app, recreates the application database, replaces `/data`, starts the current image, applies current Alembic migrations, and waits for readiness. If it fails after replacement begins, the app is stopped; inspect the error and retry from the untouched backup directory.

For disaster recovery onto a fresh VM:

1. deploy a clean checkout using the Proxmox guide;
2. securely copy a complete timestamped backup directory to the VM;
3. run `restore.sh` against it;
4. run `doctor.sh`, sign in, and verify the automation status and Twitch session;
5. reimport a Twitch token only if Twitch has expired the restored browser session.

## Updates

The guarded update path requires a clean checkout on `main`:

```bash
bash scripts/update.sh
```

Run the update as the checkout owner, not through `sudo`; the script escalates Docker commands when needed. This avoids root-owned Git files and Git's unsafe-repository protection. It fetches `origin/main`, refuses divergent or locally modified checkouts, creates a pre-update backup, fast-forwards the checkout, pulls/builds images, waits for health, and runs diagnostics. The existing container continues running while the new app image builds. If deployment fails, the script reports the previous revision and backup location; it does not perform an unsafe automatic database downgrade.

Read release notes before updating. A database migration may make application rollback require restoring the matching pre-update backup.

## Configuration and secrets

`.env` must remain mode `0600`. It contains:

- `BIND_ADDRESS`: a specific private LAN or VPN IPv4 address assigned to the VM;
- `PORT`: the private web port;
- `SECRET_KEY`: application session integrity secret;
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`: internal database bootstrap credentials.
- `BOOTSTRAP_TOKEN`: required to claim the first operator account; ignored after that account exists, but retained as a deployment secret.
- `BACKUP_ROOT`: absolute local backup destination inspected by diagnostics; the installer defaults it to `/srv/backups/twitch-drop-automator`.

After editing the bind address or port, run `sudo docker compose up -d --wait`. Changing `SECRET_KEY` invalidates existing web sessions. Do not change only `POSTGRES_PASSWORD` in `.env` on an initialized database: PostgreSQL retains the old role password and the app will lose access. Plan and test credential rotation as a database operation, then update `.env` in the same maintenance window.

The imported Twitch token is not retained in PostgreSQL. It is a one-time input used to create the browser session. Protect the `twitch-drop-automator_browser_data` volume and backups as credentials.

## Legacy Compose volumes

The production names are `twitch-drop-automator_browser_data` and `twitch-drop-automator_postgres_data`. Older Compose files derived their prefix from the checkout directory, so an installation run from a differently named directory may have volumes such as `twitchautomation_browser_data` and `twitchautomation_postgres_data`. The installer detects recognizable labeled pairs and stops rather than creating an apparently healthy but empty deployment.

Do not rename or copy a live PostgreSQL volume by hand. The pre-production repository did not include `backup.sh`, so export it from its original checkout before replacing or updating that checkout. With the legacy stack running, use:

```bash
(
  set -Eeuo pipefail
  EXPORT="$HOME/twitch-legacy-export"
  mkdir -p "$EXPORT"
  chmod 700 "$EXPORT"
  LEGACY_APP_ID="$(docker compose ps -q app)"
  test -n "$LEGACY_APP_ID"
  LEGACY_BROWSER_VOLUME="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$LEGACY_APP_ID")"
  test -n "$LEGACY_BROWSER_VOLUME"
  docker compose stop app
  docker compose exec -T db sh -c \
    'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
    >"$EXPORT/database.dump"
  docker run --rm \
    -v "$LEGACY_BROWSER_VOLUME:/source:ro" \
    -v "$EXPORT:/backup" \
    alpine:3.22 tar -C /source -czf /backup/app-data.tar.gz .
  (cd "$EXPORT" && sha256sum database.dump app-data.tar.gz >SHA256SUMS)
)
```

Treat that export as credentials and leave the legacy app stopped for cutover consistency. Deploy this production version onto a fresh VM, but do not create its owner account. Copy the two verified export files to the new VM, then replace the empty new data set:

```bash
(
  set -Eeuo pipefail
  cd /opt/twitch-drop-automator
  EXPORT=/secure/path/twitch-legacy-export
  (cd "$EXPORT" && sha256sum --check SHA256SUMS)
  tar -tzf "$EXPORT/app-data.tar.gz" >/dev/null
  POSTGRES_USER="$(sed -n 's/^POSTGRES_USER=//p' .env)"
  POSTGRES_DB="$(sed -n 's/^POSTGRES_DB=//p' .env)"
  test -n "$POSTGRES_USER" && test -n "$POSTGRES_DB"
  sudo docker compose exec -T db pg_restore --list \
    <"$EXPORT/database.dump" >/dev/null
  sudo docker compose stop --timeout 60 app
  sudo docker compose exec -T db dropdb --username "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB"
  sudo docker compose exec -T db createdb --username "$POSTGRES_USER" --owner "$POSTGRES_USER" "$POSTGRES_DB"
  sudo docker compose exec -T db pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --no-owner --no-privileges --exit-on-error <"$EXPORT/database.dump"
  sudo docker compose run --rm --no-deps --entrypoint sh app -ceu \
    'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -xzf - -C /data' \
    <"$EXPORT/app-data.tar.gz"
  sudo docker compose up -d --wait --wait-timeout 300
  sudo bash scripts/doctor.sh
)
```

The current entrypoint applies the credential-removal adoption migration before serving. Sign in with the migrated owner account, verify the Twitch session and automation state, and take a current-format off-host backup. Preserve the legacy VM, volumes, `.env`, and export until all of those checks pass. If the source stack cannot be started or either export command fails, stop rather than initializing empty replacement data.

## Troubleshooting

### App is unhealthy

```bash
sudo docker compose ps
sudo docker compose logs --tail=300 app db
sudo bash scripts/doctor.sh
```

Migration failures appear before Gunicorn starts. Read the first error rather than repeatedly recreating the container. A database health failure should be resolved before restarting the app.

### Browser is temporarily unavailable

The automation manager retries transient launch, navigation, and Twitch failures with backoff and relaunches Chromium. Check the dashboard status and logs. If retries continue indefinitely, verify free memory, disk space, system time, Twitch reachability, and whether the saved Twitch session expired. Reimport the Twitch token only when reauthentication is required.

### Service is unreachable from the LAN or VPN

Confirm that `BIND_ADDRESS` still belongs to the VM, that the published port matches `.env`, that the Proxmox VM firewall permits the intended source CIDR, and that both health probes work inside the app container:

```bash
ip -4 address
sudo docker compose port app 5000
sudo docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5000/health/ready').read().decode())"
```

### Resource pressure

Keep at least 2 GiB memory available during normal operation and investigate any checkout, Docker-data, or backup filesystem at 85% use. Chromium can be killed abruptly under memory pressure. The supported VM minimum is 8 GiB because the app and database have separate 4 GiB and 1 GiB ceilings plus Ubuntu/Docker overhead. Increase VM memory before changing the app ceiling or reducing its 1 GiB shared-memory allocation.

Drop history is pruned after 365 days and capped at 10,000 rows per user. Chromium profile data is not disposable and is therefore not automatically deleted; the browser runs with its HTTP cache disabled, but operators must still monitor volume and backup growth. Use `docker system df`, `df -h`, and backup-destination monitoring during routine maintenance.

## Permanent removal

The following command irreversibly deletes the database and authenticated browser profile:

```bash
sudo docker compose down --volumes --remove-orphans
```

Take and verify a final off-host backup first. Then remove the checkout and any scheduled backup units separately.
