"""Per-user Twitch drop automation with CDP screencast streaming."""

import asyncio
import logging
import os
import threading
import re
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from playwright_stealth import Stealth, ALL_EVASIONS_DISABLED_KWARGS

logger = logging.getLogger(__name__)

TWITCH_INVENTORY_URL = "https://www.twitch.tv/drops/inventory"
TWITCH_RUST_DIRECTORY_URL = "https://www.twitch.tv/directory/game/Rust"
TWITCH_LOGIN_URL = "https://www.twitch.tv/login"

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=BlockThirdPartyCookies,CookieDeprecationMessages",
    "--disable-features=TranslateUI",
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


class AutomationManager:
    """Manages all per-user automation instances."""

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
        return {"running": False, "logged_in": False}


class UserAutomator:
    """Runs Twitch drop automation for a single user in its own thread/event-loop."""

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

        self.status: dict = {
            "running": False,
            "logged_in": False,
            "watching": None,
            "stream_name": None,
            "message": "Idle",
            "drops_in_progress": [],
            "drops_claimed": [],
            "last_check": None,
            "last_update": None,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self.running = True
        self._stop.clear()
        self._update_status(running=True, message="Starting…")
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.running = False
        self._update_status(running=False, message="Stopping…")
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def get_status(self) -> dict:
        return dict(self.status)

    # ------------------------------------------------------------------
    # Thread / async bridge
    # ------------------------------------------------------------------

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
                await self._automation_loop()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("User %s flow error", self.user_id)
            finally:
                await self._cleanup()

    # ------------------------------------------------------------------
    # Browser launch — uses real Google Chrome with full stealth
    # ------------------------------------------------------------------

    async def _launch_browser(self, p):
        # Run in HEADED mode on Xvfb virtual display.
        # This completely avoids Twitch's headless browser fingerprinting.

        # Clean stale lock files from previous sessions so Chrome doesn't
        # think another instance owns this profile.
        import glob
        for lock in glob.glob(os.path.join(self.data_dir, "Singleton*")):
            try:
                os.remove(lock)
            except OSError:
                pass

        launch_kwargs = dict(
            user_data_dir=self.data_dir,
            headless=False,
            slow_mo=50,
            ignore_default_args=["--enable-automation"],
            args=BROWSER_ARGS,
            viewport=VIEWPORT,
            locale="en-US",
        )

        try:
            self.context = await p.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            logger.info("User %s: launched with Google Chrome (headed on Xvfb)", self.user_id)
        except Exception as e:
            logger.warning(
                "User %s: Chrome channel failed (%s), falling back to Chromium",
                self.user_id, e,
            )
            self.context = await p.chromium.launch_persistent_context(**launch_kwargs)

        # Stealth — navigator.webdriver override
        stealth_kwargs = {**ALL_EVASIONS_DISABLED_KWARGS, "navigator_webdriver": True}
        stealth = Stealth(init_scripts_only=True, **stealth_kwargs)
        await stealth.apply_stealth_async(self.context)

        try:
            await self.context.add_init_script(
                """(() => {
                    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e){}
                })();"""
            )
        except Exception:
            pass

        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )

        # Log actual UA for debugging
        try:
            ua = await self.page.evaluate("navigator.userAgent")
            wd = await self.page.evaluate(
                "'webdriver' in navigator ? navigator.webdriver : 'not present'"
            )
            logger.info("User %s UA: %s | webdriver: %s", self.user_id, ua, wd)
        except Exception:
            pass

        self._update_status(message="Browser launched")

    # ------------------------------------------------------------------
    # CDP Screencast
    # ------------------------------------------------------------------

    async def _start_screencast(self):
        if not self.page:
            return
        try:
            self.cdp_session = await self.context.new_cdp_session(self.page)
            self.cdp_session.on("Page.screencastFrame", self._on_screencast_frame)
            await self.cdp_session.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 50,
                    "maxWidth": VIEWPORT["width"],
                    "maxHeight": VIEWPORT["height"],
                    "everyNthFrame": 3,
                },
            )
        except Exception:
            logger.exception("User %s: screencast init failed", self.user_id)

    def _on_screencast_frame(self, params):
        try:
            self.socketio.emit(
                "screencast_frame",
                {"data": params["data"]},
                room=f"user_{self.user_id}",
            )
            if self.cdp_session and self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.cdp_session.send(
                        "Page.screencastFrameAck",
                        {"sessionId": params["sessionId"]},
                    ),
                    self._loop,
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Input forwarding (for Twitch login via screencast)
    # ------------------------------------------------------------------

    async def handle_input(self, data: dict):
        if not self.page:
            return
        try:
            kind = data.get("type")
            if kind == "click":
                await self.page.mouse.click(
                    float(data.get("x", 0)), float(data.get("y", 0))
                )
            elif kind == "type":
                await self.page.keyboard.type(data.get("text", ""), delay=30)
            elif kind == "press":
                await self.page.keyboard.press(data.get("key", ""))
            elif kind == "scroll":
                await self.page.mouse.wheel(
                    float(data.get("deltaX", 0)), float(data.get("deltaY", 0))
                )
        except Exception:
            logger.debug("User %s input error", self.user_id, exc_info=True)

    # ------------------------------------------------------------------
    # Auto-login: fill Twitch login form programmatically
    # ------------------------------------------------------------------

    async def auto_login(self, username: str, password: str):
        """Navigate to Twitch login and fill credentials automatically."""
        self._update_status(message="Logging in to Twitch…")

        await self._goto(TWITCH_LOGIN_URL)
        await asyncio.sleep(3)
        await self._accept_cookies()

        try:
            # Wait for login form
            await self.page.wait_for_selector(
                'input[autocomplete="username"], #login-username', timeout=15000
            )
            await asyncio.sleep(1)

            # Fill username
            username_input = await self.page.query_selector(
                'input[autocomplete="username"]'
            )
            if not username_input:
                username_input = await self.page.query_selector("#login-username")
            if username_input:
                await username_input.click()
                await asyncio.sleep(0.3)
                await username_input.fill("")
                await username_input.type(username, delay=50)

            await asyncio.sleep(0.5)

            # Fill password
            password_input = await self.page.query_selector(
                'input[autocomplete="current-password"]'
            )
            if not password_input:
                password_input = await self.page.query_selector("#password-input")
            if password_input:
                await password_input.click()
                await asyncio.sleep(0.3)
                await password_input.fill("")
                await password_input.type(password, delay=50)

            await asyncio.sleep(0.5)

            # Click login button
            login_btn = await self.page.query_selector(
                'button[data-a-target="passport-login-button"]'
            )
            if not login_btn:
                login_btn = await self.page.query_selector(
                    'button:has-text("Log In")'
                )
            if login_btn:
                await login_btn.click()
                self._update_status(message="Credentials submitted — waiting…")
                await asyncio.sleep(5)
            else:
                self._update_status(message="Could not find login button")
                return False

            # Check if login succeeded
            for _ in range(30):
                if self._stop.is_set():
                    return False
                if await self._is_logged_in():
                    self._update_status(logged_in=True, message="Logged in!")
                    return True
                # Check for 2FA or error
                error_el = await self.page.query_selector(
                    '[data-a-target="passport-error"]'
                )
                if error_el:
                    err_text = await error_el.text_content()
                    self._update_status(
                        message=f"Login error: {(err_text or '').strip()[:100]}"
                    )
                    return False
                await asyncio.sleep(2)

            self._update_status(message="Login timeout — check for 2FA in preview")
            return False
        except Exception:
            logger.exception("User %s auto-login error", self.user_id)
            self._update_status(message="Auto-login failed — use preview to log in manually")
            return False

    # ------------------------------------------------------------------
    # Main automation loop
    # ------------------------------------------------------------------

    async def _automation_loop(self):
        self._update_status(message="Navigating to Twitch…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(3)
        await self._accept_cookies()

        logged_in = await self._is_logged_in()
        self._update_status(logged_in=logged_in)

        if not logged_in:
            self._update_status(
                message="Not logged in — use the login form or interact with the preview"
            )
            logged_in = await self._wait_for_login()
            if not logged_in:
                return
            self._update_status(logged_in=True, message="Logged in!")

        while not self._stop.is_set():
            try:
                await self._check_and_claim_drops()
                await self._find_and_watch_stream()

                for _ in range(60):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(1)
            except Exception:
                logger.exception("User %s loop iteration error", self.user_id)
                self._update_status(message="Error — retrying in 15s…")
                await asyncio.sleep(15)

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url
            if "/login" in url or "id.twitch.tv" in url:
                return False
            menu = await self.page.query_selector(
                '[data-a-target="user-menu-toggle"]'
            )
            if menu:
                return True
            return "twitch.tv/drops/inventory" in url and "/login" not in url
        except Exception:
            return False

    async def _wait_for_login(self) -> bool:
        for _ in range(600):
            if self._stop.is_set():
                return False
            if await self._is_logged_in():
                return True
            await asyncio.sleep(2)
        self._update_status(message="Login timeout — stopping")
        return False

    # ------------------------------------------------------------------
    # Drop checking & claiming
    # ------------------------------------------------------------------

    async def _check_and_claim_drops(self):
        self._update_status(message="Checking drops…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(3)

        claimed = []
        in_progress = []

        try:
            claim_buttons = await self.page.query_selector_all(
                'button[data-test-selector="DropsCampaignInProgressRewardPresentation__Action"]'
            )
            if not claim_buttons:
                claim_buttons = await self.page.query_selector_all(
                    'button:has-text("Claim")'
                )
            for btn in claim_buttons:
                try:
                    await btn.click()
                    await asyncio.sleep(1.5)
                    claimed.append(
                        {
                            "name": "Drop claimed",
                            "time": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                except Exception:
                    pass
        except Exception:
            pass

        try:
            progress_cards = await self.page.query_selector_all(
                '[data-test-selector="DropsCampaignInProgressRewardPresentation"],'
                '[class*="InProgressReward"]'
            )
            for card in progress_cards:
                try:
                    text = (await card.text_content() or "").strip()
                    pct = 0
                    m = re.search(r"(\d+)\s*[/%]", text)
                    if m:
                        pct = int(m.group(1))
                    in_progress.append({"name": text[:120], "progress": pct})
                except Exception:
                    pass

            if not progress_cards:
                all_text = await self.page.text_content("body") or ""
                pct_matches = re.findall(
                    r"(\d{1,3})%\s*(?:complete|progress)", all_text, re.IGNORECASE
                )
                for pct_str in pct_matches[:5]:
                    in_progress.append(
                        {"name": "Drop progress", "progress": int(pct_str)}
                    )
        except Exception:
            pass

        self._update_status(
            drops_in_progress=in_progress,
            drops_claimed=self.status.get("drops_claimed", []) + claimed,
            last_check=datetime.now(timezone.utc).isoformat(),
            message=f"Checked drops — {len(in_progress)} active, {len(claimed)} claimed",
        )
        self._persist_drops(in_progress, claimed)

    def _persist_drops(self, in_progress: list, claimed: list):
        try:
            with self.app.app_context():
                from app.models import DropLog
                from app.extensions import db

                for d in claimed:
                    db.session.add(
                        DropLog(
                            user_id=self.user_id,
                            drop_name=d.get("name", "Unknown"),
                            game="Rust",
                            status="claimed",
                            claimed_at=datetime.now(timezone.utc),
                        )
                    )
                for d in in_progress:
                    existing = DropLog.query.filter_by(
                        user_id=self.user_id,
                        drop_name=d.get("name", "")[:255],
                        status="in_progress",
                    ).first()
                    if existing:
                        existing.progress = d.get("progress", 0)
                    else:
                        db.session.add(
                            DropLog(
                                user_id=self.user_id,
                                drop_name=d.get("name", "")[:255],
                                game="Rust",
                                status="in_progress",
                                progress=d.get("progress", 0),
                            )
                        )
                db.session.commit()
        except Exception:
            logger.debug("User %s drop persist error", self.user_id, exc_info=True)

    # ------------------------------------------------------------------
    # Stream finding
    # ------------------------------------------------------------------

    async def _find_and_watch_stream(self):
        if self.status.get("watching"):
            try:
                if (
                    self.page.url
                    and "twitch.tv/" in self.page.url
                    and "/directory" not in self.page.url
                    and "/drops" not in self.page.url
                ):
                    self._update_status(message="Watching stream…")
                    return
            except Exception:
                pass

        self._update_status(message="Finding a stream with drops…")
        await self._goto(TWITCH_RUST_DIRECTORY_URL)
        await asyncio.sleep(4)

        try:
            cards = await self.page.query_selector_all(
                'a[data-a-target="preview-card-image-link"]'
            )
            if not cards:
                cards = await self.page.query_selector_all('article a[href*="/"]')

            if cards:
                await cards[0].click()
                await asyncio.sleep(5)
                await self._accept_mature_content()
                await self._set_low_quality()

                stream_name = ""
                try:
                    title_el = await self.page.query_selector(
                        'h1, [data-a-target="stream-title"]'
                    )
                    if title_el:
                        stream_name = (await title_el.text_content() or "").strip()
                except Exception:
                    pass

                self._update_status(
                    watching=self.page.url,
                    stream_name=stream_name or self.page.url.split("/")[-1],
                    message=f"Watching: {stream_name or 'stream'}",
                )
            else:
                self._update_status(
                    watching=None,
                    stream_name=None,
                    message="No Rust streams found — will retry",
                )
        except Exception:
            logger.debug("User %s stream find error", self.user_id, exc_info=True)
            self._update_status(message="Error finding stream — will retry")

    async def _accept_mature_content(self):
        try:
            btn = await self.page.query_selector(
                '[data-a-target="player-overlay-mature-accept"]'
            )
            if btn:
                await btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass

    async def _set_low_quality(self):
        try:
            settings_btn = await self.page.query_selector(
                '[data-a-target="player-settings-button"]'
            )
            if settings_btn:
                await settings_btn.click()
                await asyncio.sleep(0.5)
                quality_btn = await self.page.query_selector(
                    '[data-a-target="player-settings-menu-item-quality"]'
                )
                if quality_btn:
                    await quality_btn.click()
                    await asyncio.sleep(0.5)
                    options = await self.page.query_selector_all(
                        '[data-a-target="player-settings-submenu-quality-option"],'
                        'input[type="radio"]'
                    )
                    if options:
                        await options[-1].click()
                        await asyncio.sleep(0.3)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    async def _goto(self, url: str, timeout: int = 60000):
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            logger.debug("User %s nav to %s failed", self.user_id, url, exc_info=True)

    async def _accept_cookies(self):
        try:
            btn = await self.page.query_selector("#onetrust-accept-btn-handler")
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _update_status(self, **kwargs):
        self.status.update(kwargs)
        self.status["last_update"] = datetime.now(timezone.utc).isoformat()
        try:
            self.socketio.emit(
                "automation_status",
                self.status,
                room=f"user_{self.user_id}",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self):
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
        self.context = None
        self.page = None
        self.cdp_session = None
