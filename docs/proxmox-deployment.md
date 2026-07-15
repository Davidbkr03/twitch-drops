# Proxmox deployment

This guide is the supported production installation: Docker Compose inside an Ubuntu Server 24.04 QEMU VM on Proxmox, reachable only over a private LAN or VPN.

## 1. Create the VM

Upload an Ubuntu Server 24.04 LTS ISO to Proxmox and create a QEMU VM with these starting values:

- machine type: `q35`;
- CPU: 2 vCPU minimum, 4 vCPU recommended; use CPU type `host` unless live migration requires a common CPU model;
- memory: 8 GiB minimum, 12 GiB recommended; avoid aggressive ballooning because Chromium is sensitive to memory pressure;
- disk: 32 GiB minimum on VirtIO SCSI, with discard enabled when the storage supports it;
- network: VirtIO attached to the private bridge;
- QEMU guest agent: enabled.

The sizing assumes one operator and one automation browser. Increase memory and disk before adding other workloads. Do not use an LXC container for this deployment; the QEMU VM gives Chromium and Docker a clear kernel and sandbox boundary.

Install Ubuntu Server without a desktop environment. Use a DHCP reservation or static network configuration so the VM keeps the same private IPv4 address. Do not assign a public IP and do not configure router port forwarding.

In Proxmox, enable **Start at boot** for the VM. If other network or storage VMs are dependencies, place this VM later in the Proxmox start order.

## 2. Prepare Ubuntu

Log in over SSH and install host updates, Git, the guest agent, and the firewall:

```bash
sudo apt-get update
sudo apt-get dist-upgrade -y
sudo apt-get install -y git qemu-guest-agent ufw unattended-upgrades
sudo systemctl enable --now qemu-guest-agent
sudo systemctl enable --now unattended-upgrades
sudo reboot
```

After reconnecting, confirm the intended static address:

```bash
ip -4 address
timedatectl status
```

Keep the guest firewall for host services such as SSH. This example permits SSH only from `192.168.1.0/24`:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

Do not rely on UFW to restrict the Compose-published application port: Docker documents that published-container traffic is diverted before UFW's normal rules. Enforce the trusted source at the Proxmox VM boundary instead:

1. enable the Proxmox firewall at the Datacenter level;
2. enable **Firewall** on this VM's network device and in the VM's firewall options;
3. add inbound TCP accept rules from the actual trusted LAN or VPN CIDR to port `22` and the configured application `PORT` (`5000` by default);
4. set the VM inbound policy to drop only after the accept rules are present.

The integrated Proxmox firewall filters packets on the VM interface before they reach Docker. For VPN-only access, accept the configured application `PORT` only from the VPN address range. Keep the Proxmox console and an active SSH session available while enabling either firewall so a bad rule can be corrected. See the [Proxmox VE firewall chapter](https://pve.proxmox.com/pve-docs/pve-admin-guide.html#chapter_pve_firewall) and [Docker's packet-filtering warning](https://docs.docker.com/engine/network/packet-filtering-firewalls/#docker-and-ufw).

## 3. Check out and install

Create an operator-owned installation directory and clone `main`:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/twitch-drop-automator
git clone --branch main --single-branch \
  https://github.com/Davidbkr03/twitch-drops.git \
  /opt/twitch-drop-automator
cd /opt/twitch-drop-automator
```

Run the repository bootstrap with the VM's private address. The address must already be assigned to the VM. `PORT` defaults to `5000`.

```bash
BIND_ADDRESS=192.168.1.50 PORT=5000 bash install.sh
```

The bootstrap:

1. refuses unsupported host operating systems;
2. installs Docker Engine and the Compose plugin from Docker's Ubuntu repository when absent;
3. creates `.env` with random URL-safe secrets and permissions `0600`;
4. pulls PostgreSQL, builds the application with bundled Playwright Chromium, and starts the stack;
5. waits for both health checks rather than reporting success early.

The installer refuses to run over any existing production volumes. Use `scripts/update.sh` for an installed service. If and only if the first installation failed after creating new empty volumes, diagnose the original failure, confirm that `.env` belongs to that same attempt, and resume explicitly with `RESUME_INSTALL=1 bash install.sh`.

The application runs Chromium as an unprivileged user. Compose applies the
version-pinned Playwright seccomp profile in `seccomp_profile.json` so Chromium
can create its own sandbox namespaces without granting the container
`SYS_ADMIN`.

`BIND_ADDRESS` and `PORT` are used only when `.env` is first created. For a later address or port change, edit `.env` directly and recreate the app with `sudo docker compose up -d --wait`.

Log out and back in after installation if you want to run Docker without `sudo`; group membership changes do not affect the current login session.

## 4. Enrol the operator and Twitch session

The application deliberately has no public TLS endpoint. Protect the application password and Twitch bearer token during enrollment with an SSH tunnel (or an already-established encrypted VPN). On the trusted client, run the following and keep it open, replacing the user and VM address:

```bash
ssh -N -L 127.0.0.1:15000:192.168.1.50:5000 operator@192.168.1.50
```

Browse to `http://127.0.0.1:15000` through the tunnel and complete these steps promptly:

1. On the VM, retrieve the enrollment value without printing any other secret: `sed -n 's/^BOOTSTRAP_TOKEN=//p' .env`.
2. Create the first application account with that deployment bootstrap token. It becomes the only operator account and closes registration.
3. Sign in to Twitch in a normal browser on the trusted device.
4. In that browser's developer tools, inspect the cookies for `https://www.twitch.tv` and copy the value of the `auth-token` cookie.
5. Paste it into the application's token import field once, submit it, and clear the clipboard.
6. Select the desired game or channel targets and start automation.

The deployment bootstrap token is ignored after the first account exists. Keep it in the mode-`0600` `.env` because Compose requires the complete deployment configuration, and continue treating it as a secret.

Treat the token like a password. The application consumes the imported value and removes it from PostgreSQL after writing it to the persistent browser profile. It is not an ongoing environment variable or database secret. The browser volume and every backup remain sensitive because they contain the resulting Twitch session.

The service uses plain HTTP inside the private network. Use the SSH tunnel for credential-bearing administration and prefer an encrypted VPN for routine remote access. Direct LAN HTTP is acceptable only when every device and network segment on that LAN is inside the trusted security boundary; never expose the configured application port directly to the internet.

## 5. Verify 24/7 operation

Run the diagnostic script and inspect the health endpoints:

```bash
cd /opt/twitch-drop-automator
sudo bash scripts/doctor.sh
curl --fail http://192.168.1.50:5000/health/live
curl --fail http://192.168.1.50:5000/health/ready
sudo docker compose ps
```

Reboot the VM once as an acceptance test:

```bash
sudo reboot
```

After it returns, both containers should become healthy without an operator login. Automation that was running should resume. Transient browser launch or Twitch failures are retried with backoff and a fresh browser launch; they should not require a container restart.

Verify the network boundary from one allowed client and, when possible, one client outside the accepted CIDR. The allowed client should reach `/health/ready`; the disallowed client must not establish a TCP connection to the configured application `PORT`.

Finish the deployment by configuring scheduled off-host backups as described in [Operations](operations.md). Proxmox VM backups are useful additional protection, but a live VM snapshot is not a substitute for the application-consistent PostgreSQL dump and browser-profile archive.
