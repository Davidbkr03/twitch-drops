#!/usr/bin/env bash

set -euo pipefail

REPO="Davidbkr03/twitch-drops"
BRANCH="main"
ZIP_URL=""
INSTALL_DIR=""
QUIET=0
LOGIN=0

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  -r, --repo OWNER/REPO         GitHub repo to install from (default: ${REPO})
  -b, --branch BRANCH           Git branch (default: ${BRANCH})
  -z, --zip-url URL             Direct .zip URL (overrides repo/branch)
  -d, --dir PATH                Install directory (default: ./TwitchDropAutomator)
  -q, --quiet                   Non-interactive (no prompts)
  -l, --login                   Add to Login Items via LaunchAgent
  -h, --help                    Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--repo) REPO="$2"; shift 2 ;;
    -b|--branch) BRANCH="$2"; shift 2 ;;
    -z|--zip-url) ZIP_URL="$2"; shift 2 ;;
    -d|--dir|--install-dir) INSTALL_DIR="$2"; shift 2 ;;
    -q|--quiet) QUIET=1; shift ;;
    -l|--login) LOGIN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

cwd="$(pwd)"
if [[ -z "${INSTALL_DIR}" ]]; then
  INSTALL_DIR="${cwd}/TwitchDropAutomator"
fi

echo "This installer will set up Twitch Drop Automator at:"
echo "  ${INSTALL_DIR}"
echo "All data (logs, user data) will be stored INSIDE this folder."
if [[ "${QUIET}" -eq 0 ]]; then
  read -r -p "Continue? (Y/N) " resp
  case "${resp}" in
    y|Y) : ;;
    *) echo "Aborted by user."; exit 1 ;;
  esac
fi

TMP_DIR="$(mktemp -d -t tda_XXXXXXXX)"
ZIP_PATH="${TMP_DIR}/repo.zip"
EXTRACT_DIR="${TMP_DIR}/extract"
mkdir -p "${EXTRACT_DIR}"

cleanup() { rm -rf "${TMP_DIR}" 2>/dev/null || true; }
trap cleanup EXIT

if [[ -z "${ZIP_URL}" ]]; then
  if [[ -z "${REPO}" ]]; then
    echo "No --repo or --zip-url provided." >&2
    exit 1
  fi
  ZIP_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
fi
echo "[INFO] Repo archive: ${ZIP_URL}"

mkdir -p "${INSTALL_DIR}"

echo "[INFO] Downloading repository…"
curl -L "${ZIP_URL}" -o "${ZIP_PATH}"
echo "[INFO] Extracting…"
unzip -q "${ZIP_PATH}" -d "${EXTRACT_DIR}"

# Find top-level extracted folder
ROOT_DIR="$(find "${EXTRACT_DIR}" -mindepth 1 -maxdepth 1 -type d -not -name "__MACOSX" | head -n 1)"
if [[ -z "${ROOT_DIR}" ]]; then
  echo "[ERROR] Unexpected archive layout." >&2
  exit 1
fi

echo "[INFO] Copying files…"
rsync -a "${ROOT_DIR}/" "${INSTALL_DIR}/"

# Ensure Python 3
if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Please install Python 3 (https://www.python.org/) and re-run." >&2
  exit 1
fi

# Create venv
VENV_DIR="${INSTALL_DIR}/venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[INFO] Creating venv…"
  python3 -m venv "${VENV_DIR}"
fi
PY_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

echo "[INFO] Upgrading pip…"
"${PY_BIN}" -m pip install --upgrade pip

if [[ -f "${INSTALL_DIR}/requirements.txt" ]]; then
  echo "[INFO] Installing requirements…"
  "${PIP_BIN}" install -r "${INSTALL_DIR}/requirements.txt"
else
  echo "[WARN] requirements.txt not found. Skipping."
fi

echo "[INFO] Installing Playwright browsers…"
"${PY_BIN}" -m playwright install

# Ensure Google Chrome exists (required on macOS). If missing, install via Homebrew.
ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  echo "[INFO] Homebrew not found. Installing Homebrew…"
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to current shell session PATH
  if [ -x "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x "/usr/local/bin/brew" ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

has_chrome() {
  [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] || \
  [[ -x "${HOME}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] || \
  [[ -x "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" ]]
}

if ! has_chrome; then
  echo "[INFO] Google Chrome not found. Attempting to install via Homebrew…"
  ensure_brew
  brew install --cask google-chrome || true
  if ! has_chrome; then
    echo "[ERROR] Failed to detect Google Chrome after Homebrew install. Please install from https://www.google.com/chrome/ and re-run." >&2
    exit 1
  fi
fi

# Ensure runner script exists and is executable
RUNNER="${INSTALL_DIR}/run_automator.sh"
if [[ ! -f "${RUNNER}" ]]; then
  cat > "${RUNNER}" <<'EOS'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
if [ -x "./venv/bin/python" ]; then
  ./venv/bin/python twitch_drop_automator.py &>/dev/null &
else
  if command -v python3 >/dev/null 2>&1; then
    python3 twitch_drop_automator.py &>/dev/null &
  else
    echo "python3 not found. Please install Python 3 and create a venv (see README)." >&2
    exit 1
  fi
fi
echo "Twitch Drop Automator started. Check drops_log.txt for logs."
EOS
fi
chmod +x "${RUNNER}"

# Optional: add to Login Items via LaunchAgent
if [[ "${LOGIN}" -eq 1 ]]; then
  PLIST_DIR="${HOME}/Library/LaunchAgents"
  mkdir -p "${PLIST_DIR}"
  PLIST_PATH="${PLIST_DIR}/com.twitchdrops.automator.plist"
  cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.twitchdrops.automator</string>
    <key>ProgramArguments</key>
    <array>
      <string>${RUNNER}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/launchd.err.log</string>
  </dict>
  </plist>
EOF
  echo "[INFO] Installing LaunchAgent…"
  # Try unloading if already loaded
  launchctl unload "${PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${PLIST_PATH}"
  echo "[INFO] Added to Login Items (LaunchAgent)."
fi

echo "[INFO] Launching Twitch Drop Automator…"
"${RUNNER}"

cat <<'EON'
Tip: To log in the first time, right-click the tray icon and untick 'Headless mode'.
The app will restart and open a browser window. After login, you can re-enable headless.
EON

echo "Install complete."
echo "- Folder: ${INSTALL_DIR}"
echo "- To run later: ${RUNNER}"

