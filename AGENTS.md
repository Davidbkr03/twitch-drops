# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a **Twitch Drop Automator** — a multi-user web application that automates watching Twitch streams to earn in-game drops. It runs as a Docker Compose stack with:
- **App container**: Python/Flask with Playwright browser automation and CDP screencast
- **PostgreSQL container**: Stores user accounts, settings, and drop history

### Running with Docker (recommended)

Single command to start everything:

```bash
docker compose up -d
```

The app runs at `http://localhost:5000`. Users register/login individually, each getting isolated browser sessions.

To rebuild after code changes:

```bash
docker compose up -d --build
```

### Running without Docker (legacy single-user mode)

The original `twitch_drop_automator.py` still works standalone. See `README.md` for setup instructions. Use `--no-tray` and `--test` flags in headless environments.

### Architecture

- `app/` — Flask package (multi-user mode): auth, routes, models, automator, extensions
- `run.py` — Entry point for Docker/multi-user mode
- `twitch_drop_automator.py` — Original single-user monolith (still functional)
- `docker-compose.yml` — App + PostgreSQL
- `Dockerfile` — Python 3.12 + Playwright Chromium

### Key API endpoints (multi-user mode)

- `GET /` — Dashboard (requires auth)
- `POST /api/start` — Start per-user automation
- `POST /api/stop` — Stop automation
- `GET /api/status` — Current automation status
- `GET|POST /api/settings` — User settings
- `GET /api/drops` — Drop history

### Screencast

Uses Chrome DevTools Protocol `Page.startScreencast` instead of periodic screenshots. Frames are streamed via Socket.IO to the dashboard canvas. Users can click/type in the preview to interact with the browser (needed for Twitch login).

### Gotchas

- Docker requires the `fuse-overlayfs` storage driver and `iptables-legacy` in Cloud VM environments (already configured).
- Each user's Playwright browser data is stored at `/data/browser/<user_id>` inside the container (persisted via Docker volume).
- The PostgreSQL data persists via the `postgres_data` Docker volume.
- After `docker compose down`, data is retained in volumes. Use `docker compose down -v` to wipe.
- The app uses `async_mode='threading'` for Flask-SocketIO to bridge async Playwright with sync Flask.
- Chrome runs in **headed mode on Xvfb** (virtual display :99) to bypass Twitch's headless browser detection. The entrypoint starts Xvfb automatically.
- Stale `Singleton*` lock files are cleaned before each browser launch to prevent "Opening in existing browser session" errors.
- Google Chrome Stable is installed in the Docker image; Playwright Chromium is the fallback. `--no-sandbox` is required in Docker.
