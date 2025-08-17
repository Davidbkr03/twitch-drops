# Twitch Drop Automator

Automates watching Twitch streams to earn drops (e.g., Rust).

## Quick Install (Windows)

Run this in PowerShell from the folder where you want everything stored. The installer will download the repo, install Python if needed, create a venv, install dependencies, and optionally enable Startup.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# The installer defaults to this repo; -Repo is optional now
.\install.ps1 -Branch "main"
```

- You’ll be warned that all data (logs, user data) is stored in the install folder.
- Answer Y to the Startup prompt if you want it to run on login.
- To start immediately, double‑click `run_automator.bat` in the install folder.

If you prefer a direct zip URL instead of repo/branch:

```powershell
.\install.ps1 -ZipUrl "https://github.com/Davidbkr03/twitch-drops/archive/refs/heads/main.zip"
## Quick Install (macOS)

Run this in Terminal from the folder where you want everything stored. The installer will download the repo, set up a venv, install dependencies, install Playwright browsers, optionally add a Login Item, and launch the app.

```bash
curl -L -o install_macos.sh "https://raw.githubusercontent.com/Davidbkr03/twitch-drops/main/install_macos.sh" && \
bash install_macos.sh -r "Davidbkr03/twitch-drops" -b "main"
```

Options:
- `-d PATH` to choose install directory (default: `./TwitchDropAutomator`)
- `-z URL` to use a direct .zip URL instead of repo/branch
- `-q` to run quietly (no prompts)
- `-l` to add a Login Item (LaunchAgent)

The macOS installer will:
- Create a venv, install requirements, and `playwright install`
- Ensure Homebrew is installed
- Install Google Chrome via Homebrew if not present
- Optionally add a Login Item and then launch the app

```

## Tray Menu

- Headless mode: toggles Playwright headless on/off.
- Hide console on startup: launches with a hidden console (`pythonw.exe`).
- Changing either option will automatically restart the app to apply it.

## Manual Setup (Alternative)

1) Install Python from `https://www.python.org/` (add to PATH during setup).

2) In this project folder, create a virtual environment:
```sh
python -m venv venv
```

3) Activate it (PowerShell):
```sh
.\venv\Scripts\activate
```

4) Install libraries and Playwright browsers:
```sh
pip install -r requirements.txt
playwright install
```

5) First run:
```sh
python twitch_drop_automator.py
```
Log into Twitch in the opened browser once. Your session will persist.

## macOS Setup

1) Ensure `python3` is installed (from System or `https://www.python.org/`).

2) Create and activate a virtual environment in the project folder:

```bash
python3 -m venv venv
source venv/bin/activate
```

3) Install dependencies and Playwright browsers:

```bash
pip install -r requirements.txt
playwright install
```

4) Run it:

```bash
python3 twitch_drop_automator.py
```

Optional helper script:

```bash
chmod +x ./run_automator.sh
./run_automator.sh

Notes:
- Notifications use native macOS notifications via `osascript`.
- The tray icon requires `pystray` and `Pillow` (installed via `requirements.txt`).
- Google Chrome is required on macOS (no Chromium fallback). Install from `https://www.google.com/chrome/`.
```

## Start on Login

- Using the installer: answer Y when prompted, and it will add a Startup shortcut to run `run_automator.bat`.
- Manual method:
  - Press Win+R, type `shell:startup`, and press Enter.
  - Create a shortcut in that folder pointing to `run_automator.bat` in your install directory.

On macOS, add `run_automator.sh` to Login Items (System Settings → General → Login Items). Ensure it has execute permission and its working directory is this project folder.

## Data & Logs

- All files (e.g., `drops_log.txt`, `config.json`, user data folders) are stored in the same folder as the program.
- You can safely delete the folder to remove the app and its data (close the app first).

## Configuration

- `config.json` holds preferences:
  - `headless`: true/false
  - `hide_console`: true/false
- Both can be changed from the tray menu; the app restarts automatically to apply changes.

## Uninstall

- Remove the Startup shortcut (Win+R → `shell:startup`) if present.
- Close the app, then delete the install folder.
