"""Per-user Twitch drop automation with CDP screencast streaming."""

import asyncio
import glob
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

TWITCH_INVENTORY_URL = "https://www.twitch.tv/drops/inventory"
TWITCH_RUST_DIRECTORY_URL = "https://www.twitch.tv/directory/game/Rust"
TWITCH_LOGIN_URL = "https://www.twitch.tv/login"

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=BlockThirdPartyCookies,CookieDeprecationMessages,TranslateUI",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-networking",
    "--disable-ipc-flooding-protection",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--disable-translate",
    "--disable-component-extensions-with-background-pages",
    "--disable-http-cache",
    "--disable-dev-shm-usage",
    "--disable-popup-blocking",
    "--disable-field-trial-config",
    "--disable-back-forward-cache",
    "--disable-default-apps",
    "--force-device-scale-factor=1",
    "--run-all-compositor-stages-before-draw",
    "--enable-features=NetworkService,NetworkServiceLogging",
    "--enable-webgl",
    "--ignore-gpu-blocklist",
    "--enable-accelerated-2d-canvas",
    "--enable-gpu-rasterization",
    "--no-first-run",
    "--no-default-browser-check",
]

VIEWPORT = {"width": 1366, "height": 768}


# ======================================================================
# Manager
# ======================================================================

class AutomationManager:
    _instance = None

    def __init__(self, socketio, app):
        self.socketio = socketio
        self.app = app
        self.automators: dict[int, "UserAutomator"] = {}

    @classmethod
    def init(cls, socketio, app):
        cls._instance = cls(socketio, app)
        return cls._instance

    @classmethod
    def get(cls):
        return cls._instance

    def start_for_user(self, user_id: int) -> bool:
        if user_id in self.automators and self.automators[user_id].running:
            return False
        data_dir = os.path.join(
            self.app.config.get("BROWSER_DATA_DIR", "/data/browser"), str(user_id)
        )
        os.makedirs(data_dir, exist_ok=True)
        automator = UserAutomator(user_id, data_dir, self.socketio, self.app)
        self.automators[user_id] = automator
        automator.start()
        return True

    def stop_for_user(self, user_id: int) -> bool:
        automator = self.automators.get(user_id)
        if automator and automator.running:
            automator.stop()
            return True
        return False

    def get_automator(self, user_id: int):
        return self.automators.get(user_id)

    def get_status(self, user_id: int) -> dict:
        automator = self.automators.get(user_id)
        if automator:
            return automator.get_status()
        return {"running": False, "logged_in": False, "twitch_saved": False}


# ======================================================================
# Per-user automator
# ======================================================================

class UserAutomator:

    def __init__(self, user_id: int, data_dir: str, socketio, app):
        self.user_id = user_id
        self.data_dir = data_dir
        self.socketio = socketio
        self.app = app

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()

        self.running = False
        self.context = None
        self.page = None
        self.cdp_session = None

        self._watch_start: float | None = None
        self._total_watch_secs: float = 0

        self.status: dict = {
            "running": False,
            "logged_in": False,
            "twitch_user": None,
            "twitch_saved": False,
            "watching": None,
            "stream_name": None,
            "watch_seconds": 0,
            "message": "Idle",
            "drops_in_progress": [],
            "drops_claimed": [],
            "last_check": None,
            "last_update": None,
        }

    # ---- lifecycle ----

    def start(self):
        self.running = True
        self._stop.clear()
        self._total_watch_secs = 0
        self._watch_start = None

        twitch_saved = False
        try:
            with self.app.app_context():
                from app.models import UserSettings
                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s and s.twitch_username:
                    twitch_saved = True
        except Exception:
            pass

        self._update_status(running=True, twitch_saved=twitch_saved, message="Starting…")
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.running = False
        self._update_status(running=False, message="Stopping…")
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def get_status(self) -> dict:
        s = dict(self.status)
        if self._watch_start:
            s["watch_seconds"] = int(self._total_watch_secs + (time.time() - self._watch_start))
        return s

    # ---- thread / async bridge ----

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception:
            logger.exception("User %s automation crashed", self.user_id)
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            self.running = False
            self._update_status(running=False, message="Stopped")

    async def _async_main(self):
        async with async_playwright() as p:
            try:
                await self._launch_browser(p)
                await self._start_screencast()
                await self._full_automation()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("User %s flow error", self.user_id)
            finally:
                await self._cleanup()

    # ---- browser launch ----

    async def _launch_browser(self, p):
        for lock in glob.glob(os.path.join(self.data_dir, "Singleton*")):
            try:
                os.remove(lock)
            except OSError:
                pass

        kw = dict(
            user_data_dir=self.data_dir,
            headless=False,
            slow_mo=50,
            ignore_default_args=["--enable-automation"],
            args=BROWSER_ARGS,
            viewport=VIEWPORT,
            locale="en-US",
        )
        try:
            self.context = await p.chromium.launch_persistent_context(channel="chrome", **kw)
        except Exception:
            self.context = await p.chromium.launch_persistent_context(**kw)

        stealth = Stealth(init_scripts_only=True, navigator_webdriver=True)
        await stealth.apply_stealth_async(self.context)
        try:
            await self.context.add_init_script(
                "(() => { try { Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); } catch(e){} })();"
            )
        except Exception:
            pass

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self._update_status(message="Browser launched")

    # ---- screencast ----

    async def _start_screencast(self):
        if not self.page:
            return
        quality, every_nth = 50, 3
        try:
            with self.app.app_context():
                from app.models import UserSettings
                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s:
                    quality = max(10, min(100, s.screencast_quality or 50))
                    every_nth = max(1, min(10, s.screencast_max_fps or 3))
        except Exception:
            pass
        try:
            self.cdp_session = await self.context.new_cdp_session(self.page)
            self.cdp_session.on("Page.screencastFrame", self._on_frame)
            await self.cdp_session.send("Page.startScreencast", {
                "format": "jpeg", "quality": quality,
                "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
                "everyNthFrame": every_nth,
            })
        except Exception:
            logger.exception("User %s screencast init failed", self.user_id)

    def _on_frame(self, params):
        try:
            self.socketio.emit("screencast_frame", {"data": params["data"]}, room=f"user_{self.user_id}")
            if self.cdp_session and self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.cdp_session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}),
                    self._loop,
                )
        except Exception:
            pass

    # ---- input forwarding ----

    async def handle_input(self, data: dict):
        if not self.page:
            return
        try:
            k = data.get("type")
            if k == "click":
                await self.page.mouse.click(float(data.get("x", 0)), float(data.get("y", 0)))
            elif k == "type":
                await self.page.keyboard.type(data.get("text", ""), delay=30)
            elif k == "press":
                await self.page.keyboard.press(data.get("key", ""))
            elif k == "scroll":
                await self.page.mouse.wheel(float(data.get("deltaX", 0)), float(data.get("deltaY", 0)))
        except Exception:
            pass

    # ==================================================================
    # Full automation flow
    # ==================================================================

    async def _full_automation(self):
        # 1. Load stored Twitch credentials
        twitch_user, twitch_pass = self._load_twitch_creds()
        if twitch_user:
            self._update_status(twitch_user=twitch_user, twitch_saved=True)

        # 2. Navigate to Twitch and check login
        self._update_status(message="Navigating to Twitch…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(3)
        await self._accept_cookies()

        logged_in = await self._is_logged_in()
        self._update_status(logged_in=logged_in)

        # 3. Auto-login if not logged in and credentials are saved
        if not logged_in and twitch_user and twitch_pass:
            self._update_status(message=f"Logging in as {twitch_user}…")
            logged_in = await self._do_auto_login(twitch_user, twitch_pass)
            self._update_status(logged_in=logged_in)

        # 4. If still not logged in, wait for manual login or credentials via UI
        if not logged_in:
            self._update_status(message="Waiting for Twitch login — save credentials above or use the preview")
            logged_in = await self._wait_for_login()
            if not logged_in:
                return

        self._update_status(logged_in=True, message="Logged in to Twitch!")

        # 5. Main monitoring loop
        while not self._stop.is_set():
            try:
                await self._check_and_claim_drops()
                await self._watch_loop_cycle()
            except Exception:
                logger.exception("User %s loop error", self.user_id)
                self._update_status(message="Error — retrying in 15 s")
                await asyncio.sleep(15)

    # ---- auto-login ----

    async def auto_login(self, username: str, password: str):
        """Called from the Socket.IO handler when user submits credentials via UI."""
        self._save_twitch_creds(username, password)
        self._update_status(twitch_user=username, twitch_saved=True, message=f"Logging in as {username}…")
        ok = await self._do_auto_login(username, password)
        self._update_status(logged_in=ok)
        if ok:
            self._update_status(message="Logged in to Twitch!")
        return ok

    async def _do_auto_login(self, username: str, password: str) -> bool:
        await self._goto(TWITCH_LOGIN_URL)
        await asyncio.sleep(3)
        await self._accept_cookies()

        try:
            await self.page.wait_for_selector(
                'input[autocomplete="username"], #login-username', timeout=15000
            )
            await asyncio.sleep(1)

            for sel in ['input[autocomplete="username"]', '#login-username']:
                el = await self.page.query_selector(sel)
                if el:
                    await el.click(); await asyncio.sleep(0.2)
                    await el.fill(""); await el.type(username, delay=40)
                    break

            await asyncio.sleep(0.4)

            for sel in ['input[autocomplete="current-password"]', '#password-input']:
                el = await self.page.query_selector(sel)
                if el:
                    await el.click(); await asyncio.sleep(0.2)
                    await el.fill(""); await el.type(password, delay=40)
                    break

            await asyncio.sleep(0.4)

            btn = (
                await self.page.query_selector('button[data-a-target="passport-login-button"]')
                or await self.page.query_selector('button:has-text("Log In")')
            )
            if btn:
                await btn.click()
                self._update_status(message="Credentials submitted — waiting…")
                await asyncio.sleep(5)
            else:
                self._update_status(message="Login button not found")
                return False

            for _ in range(30):
                if self._stop.is_set():
                    return False
                if await self._is_logged_in():
                    return True
                err = await self.page.query_selector('[data-a-target="passport-error"]')
                if err:
                    txt = (await err.text_content() or "").strip()[:100]
                    self._update_status(message=f"Login error: {txt}")
                    return False
                await asyncio.sleep(2)

            self._update_status(message="Login timeout — may need 2FA via preview")
            return False
        except Exception:
            logger.exception("User %s auto-login error", self.user_id)
            self._update_status(message="Auto-login failed")
            return False

    # ---- login helpers ----

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url
            if "/login" in url or "id.twitch.tv" in url:
                return False
            return await self.page.query_selector('[data-a-target="user-menu-toggle"]') is not None
        except Exception:
            return False

    async def _wait_for_login(self) -> bool:
        for _ in range(600):
            if self._stop.is_set():
                return False
            if await self._is_logged_in():
                return True
            await asyncio.sleep(2)
        self._update_status(message="Login timeout")
        return False

    # ---- credential storage ----

    def _load_twitch_creds(self) -> tuple[str | None, str | None]:
        try:
            with self.app.app_context():
                from app.models import UserSettings
                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s and s.twitch_username:
                    return s.twitch_username, s.twitch_password
        except Exception:
            pass
        return None, None

    def _save_twitch_creds(self, username: str, password: str):
        try:
            with self.app.app_context():
                from app.models import UserSettings
                from app.extensions import db
                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s:
                    s.twitch_username = username
                    s.twitch_password = password
                    db.session.commit()
        except Exception:
            logger.debug("cred save error", exc_info=True)

    # ==================================================================
    # Drop checking & claiming
    # ==================================================================

    async def _check_and_claim_drops(self):
        self._update_status(message="Checking drops…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(4)

        claimed: list[dict] = []
        in_progress: list[dict] = []

        # ---- claim ready drops ----
        try:
            btns = await self.page.query_selector_all(
                'button[data-test-selector="DropsCampaignInProgressRewardPresentation__Action"]'
            )
            if not btns:
                btns = await self.page.query_selector_all('button:has-text("Claim")')
            for btn in btns:
                try:
                    label = ""
                    parent = await btn.evaluate_handle("el => el.closest('[class*=\"Reward\"]')")
                    if parent:
                        label = (await parent.text_content() or "")[:120].strip()
                    await btn.click()
                    await asyncio.sleep(1.5)
                    claimed.append({"name": label or "Drop", "time": datetime.now(timezone.utc).isoformat()})
                except Exception:
                    pass
        except Exception:
            pass

        # ---- scrape progress ----
        try:
            cards = await self.page.query_selector_all(
                '[data-test-selector="DropsCampaignInProgressRewardPresentation"],'
                '[class*="InProgressReward"]'
            )
            for card in cards:
                try:
                    text = (await card.text_content() or "").strip()
                    pct = 0
                    m = re.search(r"(\d+)\s*%", text)
                    if m:
                        pct = int(m.group(1))
                    name = text[:120]
                    img = await card.query_selector("img")
                    if img:
                        alt = await img.get_attribute("alt")
                        if alt:
                            name = alt.strip()
                    in_progress.append({"name": name, "progress": pct})
                except Exception:
                    pass
        except Exception:
            pass

        # ---- fallback: scan page for percentage text ----
        if not in_progress:
            try:
                body_text = await self.page.text_content("body") or ""
                for m in re.finditer(r"(\d{1,3})%", body_text):
                    pct = int(m.group(1))
                    if 0 < pct < 100:
                        in_progress.append({"name": "Drop progress", "progress": pct})
                    if len(in_progress) >= 5:
                        break
            except Exception:
                pass

        all_claimed = self.status.get("drops_claimed", []) + claimed
        self._update_status(
            drops_in_progress=in_progress,
            drops_claimed=all_claimed[-20:],
            last_check=datetime.now(timezone.utc).isoformat(),
            message=f"Drops: {len(in_progress)} active, {len(claimed)} just claimed",
        )
        self._persist_drops(in_progress, claimed)

    def _persist_drops(self, in_progress: list, claimed: list):
        try:
            with self.app.app_context():
                from app.models import DropLog
                from app.extensions import db
                for d in claimed:
                    db.session.add(DropLog(
                        user_id=self.user_id, drop_name=d.get("name", "Unknown"),
                        game="Rust", status="claimed", claimed_at=datetime.now(timezone.utc),
                    ))
                for d in in_progress:
                    ex = DropLog.query.filter_by(
                        user_id=self.user_id, drop_name=d.get("name", "")[:255], status="in_progress",
                    ).first()
                    if ex:
                        ex.progress = d.get("progress", 0)
                    else:
                        db.session.add(DropLog(
                            user_id=self.user_id, drop_name=d.get("name", "")[:255],
                            game="Rust", status="in_progress", progress=d.get("progress", 0),
                        ))
                db.session.commit()
        except Exception:
            logger.debug("persist error", exc_info=True)

    # ==================================================================
    # Stream watching with offline detection
    # ==================================================================

    async def _watch_loop_cycle(self):
        """Find a stream, watch it, detect offline, switch."""

        # If already on a stream page, check if it's still live
        if self.status.get("watching"):
            still_live = await self._is_stream_live()
            if still_live:
                self._update_watch_time()
                self._update_status(message=f"Watching: {self.status.get('stream_name', '?')}")
                await self._sleep(self._get_check_interval())
                return
            else:
                self._stop_watch_timer()
                self._update_status(watching=None, stream_name=None, message="Stream went offline — finding another…")
                await asyncio.sleep(3)

        # Find a new stream
        await self._find_and_start_stream()
        await self._sleep(self._get_check_interval())

    async def _is_stream_live(self) -> bool:
        try:
            url = self.page.url or ""
            if "/directory" in url or "/drops" in url or "/login" in url:
                return False
            # Check for offline indicator
            offline = await self.page.query_selector('[data-a-target="player-overlay-content-gate"]')
            if offline:
                return False
            # Check for the live indicator or video player
            live = await self.page.query_selector(
                '[data-a-target="player-state-overlay"], video, .video-player'
            )
            return live is not None
        except Exception:
            return False

    async def _find_and_start_stream(self):
        self._update_status(message="Finding a stream with drops…")
        await self._goto(TWITCH_RUST_DIRECTORY_URL)
        await asyncio.sleep(4)

        try:
            cards = await self.page.query_selector_all('a[data-a-target="preview-card-image-link"]')
            if not cards:
                cards = await self.page.query_selector_all('article a[href*="/"]')

            if cards:
                await cards[0].click()
                await asyncio.sleep(5)
                await self._accept_mature_content()
                await self._set_low_quality()

                name = ""
                try:
                    el = await self.page.query_selector('[data-a-target="stream-title"], h1')
                    if el:
                        name = (await el.text_content() or "").strip()
                except Exception:
                    pass
                name = name or self.page.url.split("/")[-1]

                self._start_watch_timer()
                self._update_status(
                    watching=self.page.url, stream_name=name,
                    message=f"Watching: {name}",
                )
            else:
                self._update_status(watching=None, stream_name=None, message="No streams found — will retry")
        except Exception:
            self._update_status(message="Error finding stream — will retry")

    # ---- watch time tracking ----

    def _start_watch_timer(self):
        self._watch_start = time.time()

    def _stop_watch_timer(self):
        if self._watch_start:
            self._total_watch_secs += time.time() - self._watch_start
            self._watch_start = None

    def _update_watch_time(self):
        if self._watch_start:
            self.status["watch_seconds"] = int(self._total_watch_secs + (time.time() - self._watch_start))

    # ---- stream helpers ----

    async def _accept_mature_content(self):
        try:
            btn = await self.page.query_selector('[data-a-target="player-overlay-mature-accept"]')
            if btn:
                await btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass

    async def _set_low_quality(self):
        try:
            sb = await self.page.query_selector('[data-a-target="player-settings-button"]')
            if not sb:
                return
            await sb.click(); await asyncio.sleep(0.5)
            qb = await self.page.query_selector('[data-a-target="player-settings-menu-item-quality"]')
            if qb:
                await qb.click(); await asyncio.sleep(0.5)
                opts = await self.page.query_selector_all(
                    '[data-a-target="player-settings-submenu-quality-option"], input[type="radio"]'
                )
                if opts:
                    await opts[-1].click()
        except Exception:
            pass

    # ---- navigation / utility ----

    async def _goto(self, url: str, timeout: int = 60000):
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            logger.debug("nav to %s failed", url, exc_info=True)

    async def _accept_cookies(self):
        try:
            btn = await self.page.query_selector("#onetrust-accept-btn-handler")
            if btn:
                await btn.click(); await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _sleep(self, seconds: int):
        for _ in range(seconds):
            if self._stop.is_set():
                return
            await asyncio.sleep(1)

    def _get_check_interval(self) -> int:
        try:
            with self.app.app_context():
                from app.models import UserSettings
                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s:
                    return max(10, s.check_interval or 60)
        except Exception:
            pass
        return 60

    # ---- status ----

    def _update_status(self, **kw):
        self.status.update(kw)
        self.status["last_update"] = datetime.now(timezone.utc).isoformat()
        self._update_watch_time()
        try:
            self.socketio.emit("automation_status", self.status, room=f"user_{self.user_id}")
        except Exception:
            pass

    # ---- cleanup ----

    async def _cleanup(self):
        self._stop_watch_timer()
        try:
            if self.cdp_session:
                try:
                    await self.cdp_session.send("Page.stopScreencast")
                except Exception:
                    pass
            if self.context:
                await self.context.close()
        except Exception:
            pass
        self.context = self.page = self.cdp_session = None
