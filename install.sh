#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Twitch Drop Automator — One-command installer for Ubuntu Server
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Davidbkr03/twitch-drops/main/install.sh | bash
#
# Or clone first then run:
#   git clone https://github.com/Davidbkr03/twitch-drops.git
#   cd twitch-drops && bash install.sh
# ─────────────────────────────────────────────────────────────────────
set -e

REPO="https://github.com/Davidbkr03/twitch-drops.git"
BRANCH="main"
INSTALL_DIR="${INSTALL_DIR:-$HOME/twitch-drops}"
PORT="${PORT:-5000}"

echo "╔══════════════════════════════════════════════╗"
echo "║   Twitch Drop Automator — Installer          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Install Docker if missing ──────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "→ Installing Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    echo "  Docker installed."
else
    echo "✓ Docker already installed."
fi

# ── 2. Clone or update the repo ───────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "→ Updating existing install at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull origin "$BRANCH" --ff-only 2>/dev/null || git pull origin "$BRANCH"
else
    echo "→ Cloning repository to $INSTALL_DIR..."
    git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 3. Generate .env if it doesn't exist ──────────────────────────
if [ ! -f .env ]; then
    echo "→ Generating .env with random secret key..."
    SECRET=$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | base64 | tr -d '/+=' | head -c 64)
    cat > .env <<EOF
SECRET_KEY=${SECRET}
DATA_DIR=/data
PORT=${PORT}
POSTGRES_USER=twitch
POSTGRES_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "twitch_$(date +%s)")
POSTGRES_DB=twitch_drops
EOF
    echo "  .env created with secure random passwords."
else
    echo "✓ .env already exists."
fi

# ── 4. Build and start ────────────────────────────────────────────
echo "→ Building and starting containers (this may take a few minutes on first run)..."
sudo env PORT="${PORT}" docker compose up -d --build

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   ✓ Installation complete!                    ║"
echo "╠══════════════════════════════════════════════╣"
echo "║                                              ║"
echo "║   Open in your browser:                      ║"
echo "║   http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'YOUR_SERVER_IP'):${PORT}             ║"
echo "║                                              ║"
echo "║   Default port: ${PORT}                          ║"
echo "║   Install dir:  ${INSTALL_DIR}    ║"
echo "║                                              ║"
echo "║   Commands:                                  ║"
echo "║   cd ${INSTALL_DIR}                ║"
echo "║   sudo docker compose logs -f    (view logs) ║"
echo "║   sudo docker compose restart    (restart)   ║"
echo "║   sudo docker compose down       (stop)      ║"
echo "║   sudo docker compose up -d      (start)     ║"
echo "║                                              ║"
echo "╚══════════════════════════════════════════════╝"
