# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a **Twitch Drop Automator** — a single-file Python application (`twitch_drop_automator.py`) that uses Playwright to automate watching Twitch streams and claiming in-game drops. It includes a Flask + Socket.IO web dashboard on `localhost:5000`.

There are no automated tests, linter configs, or build steps in this repository.

### Running the application

```bash
source /workspace/venv/bin/activate
python twitch_drop_automator.py --no-tray --test
```

- `--no-tray`: Required in headless/server environments (no display server for pystray).
- `--test`: Keeps the browser open for screenshot testing via the web dashboard.
- `--no-web`: Disables the Flask web interface (rarely needed).
- The app defaults to headless browser mode via `config.json` (`"headless": true`).

### Key endpoints

- `GET /` — Web dashboard UI
- `GET /api/status` — Application status JSON
- `GET /api/settings` — Current settings
- `POST /api/settings` — Update settings (JSON body with keys like `headless`, `test_mode`, `debug_mode`)

### Gotchas

- The app uses `BROWSER_CHANNEL = "chrome"` by default but falls back to Playwright's bundled Chromium if Chrome is unavailable. Google Chrome is pre-installed in the Cloud VM.
- `python3.12-venv` must be installed via apt before creating the virtual environment (`sudo apt-get install -y python3.12-venv`). This is a one-time system setup already done.
- Playwright browsers must be installed after pip dependencies: `playwright install --with-deps chromium`.
- The application will attempt to connect to Twitch on startup. Without a logged-in Twitch session, it will hit the login page and loop. The web dashboard remains fully functional regardless.
- Logs are written to `drops_log.txt` in the project root.
- Configuration is stored in `config.json` (auto-created on first run).
