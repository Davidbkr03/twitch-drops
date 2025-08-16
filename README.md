# Twitch Drop Automator

Automates watching Twitch streams to earn drops (e.g., Rust).

## Quick Install (Windows)

Run this in PowerShell from the folder where you want everything stored. The installer will download the repo, install Python if needed, create a venv, install dependencies, and optionally enable Startup.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# Replace owner/repo and branch if needed
.\install.ps1 -Repo "owner/repo" -Branch "main"
```

- You’ll be warned that all data (logs, user data) is stored in the install folder.
- Answer Y to the Startup prompt if you want it to run on login.
- To start immediately, double‑click `run_automator.bat` in the install folder.

If you prefer a direct zip URL instead of repo/branch:

```powershell
.\install.ps1 -ZipUrl "https://github.com/owner/repo/archive/refs/heads/main.zip"
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

## Start on Login

- Using the installer: answer Y when prompted, and it will add a Startup shortcut to run `run_automator.bat`.
- Manual method:
  - Press Win+R, type `shell:startup`, and press Enter.
  - Create a shortcut in that folder pointing to `run_automator.bat` in your install directory.

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
