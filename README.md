# Twitch Drop Automator

Automates watching eligible Twitch streams to earn drops. The repository contains two run modes:

- **Multi-user web app (recommended):** Flask, PostgreSQL, Playwright, and an interactive browser preview, run with Docker Compose.
- **Legacy single-user app:** `twitch_drop_automator.py`, run directly in a Python virtual environment.

## Docker quick start (recommended)

### Prerequisites

- Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux.
- On Windows, Docker Desktop must be using Linux containers.

### Start the application

The stack can start without a `.env` file. On first start, the app generates and persists a secret key in the data volume. For a shared or deployed instance, create `.env` and set an explicit, securely generated `SECRET_KEY` instead:

```powershell
Copy-Item .env.example .env
notepad .env
```

The bundled web server is intended for localhost or a trusted private network. Do not expose it directly to the public internet: registration is open, and saved Twitch credentials and auth tokens are stored in the application database. A public deployment needs a production web server or reverse proxy, TLS, access controls, and encrypted secret storage.

Replace `SECRET_KEY` and `POSTGRES_PASSWORD` with long random values. `DATABASE_URL` in `.env.example` is informational; Compose constructs the app connection string from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`.

Build and start the stack:

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

Open [http://localhost:5000](http://localhost:5000) and create a local app account. For first-time Twitch authentication in Docker, import the `auth-token` from an already signed-in browser using the **Auth Token** field, then press **Start**. The Linux container cannot launch a browser on the host desktop; **Open Twitch Login in Normal Browser** is available only when the web app runs directly on Windows. Twitch credentials are not required in `.env`.

To use another host port, set `PORT` in `.env`, for example `PORT=8080`, then open `http://localhost:8080`.

### Stop or reset

```powershell
docker compose down
```

Browser profiles and PostgreSQL data remain in Docker volumes. To permanently delete them:

```powershell
docker compose down -v
```

## Windows local development

With a normal Python installation:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

If Python is not installed globally but `uv` is available:

```powershell
uv venv --python 3.12 venv
uv pip install --python .\venv\Scripts\python.exe -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

Run the multi-user web app locally:

```powershell
.\venv\Scripts\python.exe run.py
```

Without Docker, the app uses SQLite and stores its database, generated secret, and browser profiles under `.runtime/`. To override those defaults, set `DATA_DIR`, `DATABASE_URL`, or `SECRET_KEY` in the PowerShell session before starting. A Compose `.env` file is not automatically loaded by direct Python runs.

For Twitch login compatibility, the app prefers installed stable Microsoft Edge, then stable Google Chrome, before falling back to Playwright's bundled Chromium. Set `TWITCH_BROWSER_CHANNEL=chrome` or `TWITCH_BROWSER_CHANNEL=msedge` to force a particular installed browser channel.

Open [http://localhost:5000](http://localhost:5000), register a local account, and use **Open Twitch Login in Normal Browser** for the first Twitch sign-in. Close the login browser after authentication, then press **Start**. The automation browser reuses the same persistent profile.

## Web app target selection

Press **Start** and wait for the dashboard to report **Browser launched**, then use **Browse** under **Games & Streamers** to load the games currently represented in Twitch's Drops Enabled directory. Add a game with **+** to let the automator choose any eligible live channel, or open **Channels** and add a specific Twitch channel. All specific-channel targets are tried before general game targets.

The watcher validates the final channel URL, current game/category, live state, and exact `DropsEnabled` tag before starting or continuing its watch timer. It rejects failed redirects, login/directory/VOD routes, offline or unavailable players, channels that changed games, and channels that lost Drops eligibility. Twitch recycles directory cards while scrolling, so discovery accumulates every rendered batch instead of reading only the final viewport.

When Twitch displays a mature-audience gate, both the current **Continue Watching** / **Start Watching** controls and the older overlay control are supported. Watch time does not start until that gate clears. If no eligible channel remains, the app reports that state and retries on the configured check interval.

Browser profiles and watch targets persist across restarts, but an automation session does not start itself after the app or container restarts; sign in to the dashboard and press **Start** again.

## Legacy single-user mode

Run the original standalone application:

```powershell
.\venv\Scripts\python.exe twitch_drop_automator.py --no-tray
```

Open [http://localhost:5000](http://localhost:5000). On the first run, use **Start Guided Login** on the dashboard to restart in visible-browser mode, sign in to Twitch, then use **Restore Normal Mode**. The Playwright profile persists the Twitch session under `user_data_stealth/`.

For an indefinite browser/UI diagnostic session, add `--test`:

```powershell
.\venv\Scripts\python.exe twitch_drop_automator.py --no-tray --test
```

The helper `run_automator.bat` launches the same legacy application with the repository's `venv`.

## Development validation

Install the development tools after installing the runtime requirements:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run the automated checks from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest
.\venv\Scripts\python.exe -m ruff check .
```

## macOS legacy setup

The macOS installer downloads the repository, creates a virtual environment, installs dependencies and Playwright, ensures Google Chrome is available, and can add a LaunchAgent:

```bash
curl -L -o install_macos.sh "https://raw.githubusercontent.com/Davidbkr03/twitch-drops/main/install_macos.sh"
bash install_macos.sh -r "Davidbkr03/twitch-drops" -b "main"
```

Useful options:

- `-d PATH`: choose the installation directory.
- `-z URL`: use a direct repository zip URL.
- `-q`: run without prompts.
- `-l`: install a LaunchAgent.

Google Chrome is required for the legacy app on macOS.

## Ubuntu Server installer

`install.sh` installs and starts the Docker Compose multi-user application. It supports `INSTALL_DIR` and `PORT` environment variables:

```bash
PORT=8080 INSTALL_DIR="$HOME/twitch-drops" bash install.sh
```

## Configuration and data

### Docker mode

- `.env` can set `SECRET_KEY`, `PORT`, and PostgreSQL credentials. Keep `DATA_DIR=/data` so generated state remains in the mounted volume.
- Per-user browser data and the generated fallback secret key are stored in the `browser_data` Docker volume.
- PostgreSQL data is stored in the `postgres_data` Docker volume.

### Legacy mode

- `config.json` stores preferences such as `headless` and `hide_console`.
- `user_data_stealth/` stores the persistent browser profile.
- `drops_log.txt` contains runtime logs.

These files are ignored by Git. Stop the application before deleting browser profile data.

## Troubleshooting

- **Compose cannot connect to Docker:** start Docker Desktop and confirm `docker compose version` succeeds.
- **Port 5000 is already used:** set `PORT` in `.env` and recreate the stack with `docker compose up -d`.
- **Browser does not start locally:** run `.\venv\Scripts\python.exe -m playwright install chromium`.
- **Twitch rejects the embedded login as unsupported:** for a local multi-user install, stop automation and use **Open Twitch Login in Normal Browser**. Close that browser after login, then press **Start**.
- **Docker or remote server needs Twitch authentication:** import the `auth-token` from an already signed-in browser because the container cannot open a native desktop browser.
- **Legacy mode needs authentication:** use **Start Guided Login**.
- **Preview says Watching but Twitch shows a content gate:** update and restart or rebuild the application/container, then press **Start** in the dashboard. The current version clicks Twitch's **Continue Watching** or **Start Watching** gate automatically and refuses to count a gate that remains visible.
- **A selected channel is skipped:** confirm it is live in the selected game and visibly has the `DropsEnabled` tag. Preferred channels are intentionally rejected after a category change, a VOD/login redirect, or loss of Drops eligibility.
- **Browse returns no games or channels:** Twitch may have no matching live cards, the saved login may have expired, or Twitch may have changed the directory markup. Check the browser preview and reauthenticate before retrying.
- **Inspect container failures:** run `docker compose ps` and `docker compose logs --tail=200 app db`.

## Uninstall

- Docker mode: run `docker compose down -v`, then delete the repository folder if desired.
- Legacy mode: close the app, remove any Startup/Login Item entry, and delete its installation folder.
