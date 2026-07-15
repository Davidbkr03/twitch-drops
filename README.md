# Twitch Drop Automator

Twitch Drop Automator is a private, self-hosted service that keeps one Twitch drop-watching browser running continuously. It is packaged for one supported production deployment:

- a QEMU virtual machine on Proxmox;
- Ubuntu Server 24.04 LTS;
- Docker Engine with Docker Compose;
- access only from a trusted private LAN or VPN.

There are no supported desktop, standalone Python, Windows, or macOS launch paths.

## Production behaviour

- Docker starts the application and PostgreSQL after a VM reboot and restarts either container after a failure.
- Automation that was running before a clean application or VM restart resumes automatically. An operator-requested **Stop** remains stopped.
- Transient Twitch or Chromium failures use bounded exponential backoff and relaunch the browser instead of leaving the service permanently idle.
- The first application account requires the deployment bootstrap token and becomes the sole operator account; public registration closes immediately afterward.
- A Twitch `auth-token` is imported once, written into the persistent Chromium profile, and removed from the application database. The browser profile remains sensitive data.
- Database schema upgrades run through Alembic before the web process starts.
- Gunicorn runs a single worker deliberately because it owns the browser automation process.

## Deploy

Follow the complete [Proxmox deployment guide](docs/proxmox-deployment.md). The short form, after creating the Ubuntu VM and assigning it a static private IPv4 address, is:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/twitch-drop-automator
git clone --branch main --single-branch \
  https://github.com/Davidbkr03/twitch-drops.git \
  /opt/twitch-drop-automator
cd /opt/twitch-drop-automator
BIND_ADDRESS=192.168.1.50 PORT=5000 bash install.sh
```

`install.sh` is the only deployment bootstrap. It must be run from a checked-out repository. It verifies Ubuntu 24.04, installs Docker from Docker's Ubuntu repository if required, creates `.env` with mode `0600` and random secrets, builds the image, and waits until both services are healthy.

Retrieve the enrollment value with `sed -n 's/^BOOTSTRAP_TOKEN=//p' .env`. From the trusted client, open an encrypted SSH tunnel with `ssh -N -L 127.0.0.1:15000:192.168.1.50:5000 operator@192.168.1.50`, replacing the address and user, then browse to `http://127.0.0.1:15000`. Create the first account with the token and import the Twitch token through that tunnel. The bootstrap token is ignored after the owner exists, but remains a deployment secret and must stay in `.env` for deterministic configuration.

Do not expose this HTTP service or its port to the public internet. Restrict the VM interface with the Proxmox firewall; UFW alone does not filter Docker-published ports. The service itself uses HTTP, so use the SSH tunnel for enrollment and other credential-bearing administration, or connect through an encrypted VPN.

## Operate

Run commands from the repository directory:

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 app db
sudo bash scripts/doctor.sh
sudo bash scripts/backup.sh /srv/backups/twitch-drop-automator
bash scripts/update.sh
```

The [operations guide](docs/operations.md) covers monitoring, backup scheduling, disaster recovery, updates, resource checks, and troubleshooting.

The unauthenticated health probes are intended for private monitoring:

- `GET /health/live` confirms that the web process is alive.
- `GET /health/ready` confirms that startup is complete and required dependencies are available.

## Persistent data

Compose preserves the two named volumes used by the earlier Docker layout:

- `twitch-drop-automator_browser_data` contains the Chromium profile and application runtime state;
- `twitch-drop-automator_postgres_data` contains PostgreSQL.

Both are required for recovery. A short-lived, capability-limited `data-init` service normalizes legacy browser-volume ownership before the non-root app starts. The backup script captures the volumes as an application-consistent PostgreSQL dump and browser-data archive. Backup directories also contain `.env`, so they must be stored with the same care as account credentials.

The explicit names preserve an older deployment only when its Compose project resolved to `twitch-drop-automator`. The installer refuses recognizable legacy volume pairs under a different project name instead of starting with empty data; follow the recovery procedure in the operations guide.

## Security boundary

PostgreSQL has no host port. The web port binds only to `BIND_ADDRESS`; Compose refuses to start when required addresses or secrets are absent. The application container runs as a non-root user with a read-only root filesystem, only the Chromium-sandbox `SYS_CHROOT` capability, a private writable `/tmp`, bounded memory/process/shared-memory resources, graceful shutdown, health checks, and rotated Docker logs. Chromium keeps its sandbox enabled through Playwright's version-pinned seccomp profile.

This is still a credential-bearing browser automation service. Keep the VM patched, restrict the host firewall to the trusted subnet or VPN interface, protect backups, and do not install unrelated software on the VM.
