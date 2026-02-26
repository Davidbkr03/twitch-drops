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
TWITCH_DROPS_ENABLED_URL = "https://www.twitch.tv/directory/all/tags/dropsenabled"
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

# Comprehensive stealth patches injected into every page.
# Twitch specifically checks WebGL renderer (SwiftShader = virtual env)
# and chrome.runtime (absent in automated Chrome).
_STEALTH_JS = """(() => {
    // 1. Hide webdriver flag
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e){}

    // 2. Fake WebGL renderer — SwiftShader is a dead giveaway
    const fakeVendor = 'Google Inc. (NVIDIA)';
    const fakeRenderer = 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    const patchGL = (proto) => {
        if (!proto) return;
        const orig = proto.getParameter;
        proto.getParameter = function(p) {
            if (p === 37445) return fakeVendor;
            if (p === 37446) return fakeRenderer;
            return orig.call(this, p);
        };
    };
    try { patchGL(WebGLRenderingContext.prototype); } catch(e){}
    try { patchGL(WebGL2RenderingContext.prototype); } catch(e){}

    // 3. Canvas fingerprint noise — add subtle per-session noise to toDataURL
    try {
        const _toBlob = HTMLCanvasElement.prototype.toBlob;
        const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
        const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
        const noise = () => (Math.random() - 0.5) * 2;
        CanvasRenderingContext2D.prototype.getImageData = function() {
            const data = _getImageData.apply(this, arguments);
            if (data.width > 16 && data.height > 16) {
                for (let i = 0; i < Math.min(data.data.length, 40); i += 4) {
                    data.data[i]   = Math.max(0, Math.min(255, data.data[i]   + noise()));
                    data.data[i+1] = Math.max(0, Math.min(255, data.data[i+1] + noise()));
                    data.data[i+2] = Math.max(0, Math.min(255, data.data[i+2] + noise()));
                }
            }
            return data;
        };
    } catch(e){}

    // 4. Provide chrome.runtime to look like a real Chrome install
    try {
        if (window.chrome) {
            window.chrome.runtime = {
                OnInstalledReason: { CHROME_UPDATE:'chrome_update', INSTALL:'install',
                    SHARED_MODULE_UPDATE:'shared_module_update', UPDATE:'update' },
                OnRestartRequiredReason: { APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic' },
                PlatformArch: { ARM:'arm', ARM64:'arm64', X86_32:'x86-32', X86_64:'x86-64' },
                PlatformOs: { ANDROID:'android', CROS:'cros', LINUX:'linux', MAC:'mac', WIN:'win' },
                RequestUpdateCheckStatus: { NO_UPDATE:'no_update', THROTTLED:'throttled',
                    UPDATE_AVAILABLE:'update_available' },
                connect: function() { return { onDisconnect:{addListener:function(){}},
                    onMessage:{addListener:function(){}}, postMessage:function(){} }; },
                sendMessage: function(a,b,c) { if(typeof c==='function') c(); },
                getManifest: function() { return {}; },
                getURL: function(p) { return ''; },
                id: undefined,
            };
        }
    } catch(e){}

    // 5. Consistent Notification permission
    try { Object.defineProperty(Notification, 'permission', { get: () => 'default' }); } catch(e){}

    // 6. AudioContext fingerprint noise
    try {
        const origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
        AnalyserNode.prototype.getFloatFrequencyData = function(arr) {
            origGetFloatFreq.call(this, arr);
            for (let i = 0; i < Math.min(arr.length, 10); i++) {
                arr[i] += (Math.random() - 0.5) * 0.01;
            }
        };
    } catch(e){}
})();"""


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
        self._passport_429: int = 0
        self._completed_games: set[str] = set()  # games with all rewards claimed

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
        tried_compat = False
        async with async_playwright() as p:
            while not self._stop.is_set():
                self._passport_429 = 0
                try:
                    await self._launch_browser(p, compat_mode=tried_compat)
                    await self._start_screencast()
                    await self._full_automation()
                    break
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("User %s flow error", self.user_id)
                    if self._passport_429 >= 3 and not tried_compat:
                        tried_compat = True
                        self._update_status(message="Retrying with compatibility mode…")
                        logger.info("User %s: switching to compat mode after %d 429s",
                                    self.user_id, self._passport_429)
                        await self._cleanup()
                        await asyncio.sleep(5)
                        continue
                    break
                finally:
                    await self._cleanup()

    # ---- browser launch ----

    async def _launch_browser(self, p, compat_mode: bool = False):
        for lock in glob.glob(os.path.join(self.data_dir, "Singleton*")):
            try:
                os.remove(lock)
            except OSError:
                pass

        args = list(BROWSER_ARGS)
        ignore_defaults = ["--enable-automation"]
        if compat_mode:
            # Compat mode: remove anti-automation flags (paradoxically helps
            # because Kasada may flag the ABSENCE of default args).
            args = [a for a in args if a != "--disable-blink-features=AutomationControlled"]
            ignore_defaults = None

        kw = dict(
            user_data_dir=self.data_dir,
            headless=False,
            slow_mo=50,
            ignore_default_args=ignore_defaults,
            args=args,
            viewport=VIEWPORT,
            locale="en-US",
        )
        try:
            self.context = await p.chromium.launch_persistent_context(channel="chrome", **kw)
        except Exception:
            self.context = await p.chromium.launch_persistent_context(**kw)

        # Apply stealth via Playwright init scripts (covers main page frames)
        stealth = Stealth(init_scripts_only=True, navigator_webdriver=True)
        await stealth.apply_stealth_async(self.context)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Apply stealth via CDP — this injects into ALL frames including
        # cross-origin Kasada iframes that context.add_init_script misses.
        try:
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": _STEALTH_JS,
                "runImmediately": True,
            })
            await cdp.detach()
        except Exception:
            # Fallback to context-level script
            await self.context.add_init_script(_STEALTH_JS)

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
        import random

        # Track passport 429s for compat-mode switch
        def _on_resp(resp):
            try:
                if "passport.twitch.tv" in resp.url and resp.status == 429:
                    self._passport_429 += 1
            except Exception:
                pass
        self.page.on("response", _on_resp)

        # 1. Load stored Twitch credentials
        twitch_user, twitch_pass = self._load_twitch_creds()
        if twitch_user:
            self._update_status(twitch_user=twitch_user, twitch_saved=True)

        # 2. Navigate to inventory — Twitch will redirect to login if needed
        self._update_status(message="Navigating to Twitch…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(4)
        await self._accept_cookies()

        # Wait for page to settle (Kasada iframes load here)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        logged_in = await self._is_logged_in()
        self._update_status(logged_in=logged_in)

        # 3. Try cookie-based login first (if auth_token is stored)
        if not logged_in:
            try:
                with self.app.app_context():
                    from app.models import UserSettings
                    s = UserSettings.query.filter_by(user_id=self.user_id).first()
                    if s and s.twitch_auth_token:
                        self._update_status(message="Logging in with saved auth token…")
                        logged_in = await self.import_cookies(s.twitch_auth_token)
                        self._update_status(logged_in=logged_in)
            except Exception:
                pass

        # 4. If still not logged in, pre-fill credentials on login page
        if not logged_in:
            # Check if we got redirected to login
            if "/login" not in (self.page.url or ""):
                await self._goto(TWITCH_LOGIN_URL)
                await asyncio.sleep(3)

            await self._accept_cookies()
            await asyncio.sleep(2 + random.random() * 2)

            if twitch_user and twitch_pass:
                self._update_status(message="Pre-filling credentials — click Log In in the preview")
                try:
                    await self.page.wait_for_selector(
                        'input[autocomplete="username"]', timeout=10000
                    )
                    await asyncio.sleep(0.5 + random.random())
                    u_el = await self.page.query_selector('input[autocomplete="username"]')
                    if u_el:
                        await u_el.click()
                        await asyncio.sleep(0.2 + random.random() * 0.3)
                        await u_el.press("Control+a")
                        await u_el.type(twitch_user, delay=55 + random.randint(0, 45))
                    await asyncio.sleep(0.3 + random.random() * 0.4)
                    p_el = await self.page.query_selector('input[autocomplete="current-password"]')
                    if p_el:
                        await p_el.click()
                        await asyncio.sleep(0.2 + random.random() * 0.3)
                        await p_el.press("Control+a")
                        await p_el.type(twitch_pass, delay=55 + random.randint(0, 45))
                except Exception:
                    pass

            self._update_status(
                message="Click Log In in the browser preview to complete first-time login"
            )
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
        import random

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if self._stop.is_set():
                return False
            self._update_status(
                message=f"Login attempt {attempt}/{max_attempts}…"
                if attempt > 1 else f"Logging in as {username}…"
            )

            await self._goto(TWITCH_LOGIN_URL)
            await asyncio.sleep(2)

            # Dismiss cookie banner first — lets Kasada fingerprint scripts
            # settle before we interact with the login form.
            try:
                proceed = await self.page.query_selector(
                    'button:has-text("Proceed"), #onetrust-accept-btn-handler'
                )
                if proceed:
                    await proceed.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            # Wait for Kasada/fingerprint iframes to finish initial load
            await asyncio.sleep(3 + random.random() * 2)

            try:
                await self.page.wait_for_selector(
                    'input[autocomplete="username"], #login-username', timeout=15000
                )
            except Exception:
                continue

            await asyncio.sleep(0.5 + random.random())

            # Type username with human-like timing
            self._update_status(message="Entering credentials…")
            u_el = (
                await self.page.query_selector('input[autocomplete="username"]')
                or await self.page.query_selector('#login-username')
            )
            if u_el:
                await u_el.click()
                await asyncio.sleep(0.2 + random.random() * 0.3)
                await u_el.press("Control+a")
                await u_el.type(username, delay=50 + random.randint(0, 40))

            await asyncio.sleep(0.3 + random.random() * 0.5)

            p_el = (
                await self.page.query_selector('input[autocomplete="current-password"]')
                or await self.page.query_selector('#password-input')
            )
            if p_el:
                await p_el.click()
                await asyncio.sleep(0.2 + random.random() * 0.3)
                await p_el.press("Control+a")
                await p_el.type(password, delay=50 + random.randint(0, 40))

            await asyncio.sleep(0.4 + random.random() * 0.5)

            # Click login
            btn = (
                await self.page.query_selector('button[data-a-target="passport-login-button"]')
                or await self.page.query_selector('button:has-text("Log In")')
            )
            if not btn:
                self._update_status(message="Login button not found")
                continue
            await btn.click()
            self._update_status(message="Waiting for Twitch…")

            # Poll for result — spinner might hang due to Kasada 429
            spinner_seconds = 0
            result = await self._poll_login_result(timeout_sec=30)

            if result == "success":
                logger.info("User %s: Twitch login succeeded", self.user_id)
                return True
            elif result == "error":
                return False
            elif result == "2fa":
                self._update_status(
                    message="Verification code required — enter it in the browser preview"
                )
                for _ in range(300):
                    if self._stop.is_set():
                        return False
                    if await self._is_logged_in():
                        return True
                    await asyncio.sleep(2)
                self._update_status(message="Verification timeout")
                return False
            else:
                # "timeout" — spinner hung, Kasada likely blocked it
                logger.warning("User %s: login attempt %d timed out (Kasada 429), retrying",
                               self.user_id, attempt)
                self._update_status(message=f"Login stalled — retrying ({attempt}/{max_attempts})…")
                await asyncio.sleep(3)

        self._update_status(message="Login failed after retries — try using the browser preview to log in manually")
        return False

    async def _poll_login_result(self, timeout_sec: int = 30) -> str:
        """Poll the login page after clicking Log In.
        Returns: 'success', 'error', '2fa', or 'timeout'.
        """
        for _ in range(timeout_sec):
            if self._stop.is_set():
                return "timeout"

            if await self._is_logged_in():
                return "success"

            err = await self.page.query_selector('[data-a-target="passport-error"]')
            if err:
                txt = (await err.text_content() or "").strip()[:120]
                self._update_status(message=f"Login error: {txt}")
                return "error"

            # Real 2FA: a new visible input appears for the verification code
            # and the login form fields are hidden/replaced
            u_el = await self.page.query_selector('input[autocomplete="username"]')
            if not u_el:
                # Login form disappeared — likely moved to 2FA/verification page
                return "2fa"

            await asyncio.sleep(1)

        return "timeout"

    # ---- login helpers ----

    async def _is_logged_in(self) -> bool:
        """Robust check: logged in = no Login/Sign Up buttons visible."""
        try:
            url = self.page.url
            if "/login" in url or "id.twitch.tv" in url:
                return False
            # The definitive test: if a Sign Up button exists, user is anonymous
            signup = await self.page.query_selector(
                '[data-a-target="login-button"], button:has-text("Sign Up")'
            )
            if signup:
                return False
            # Double-check: user display name only exists when logged in
            display = await self.page.query_selector(
                '[data-a-target="user-display-name"]'
            )
            if display:
                return True
            # Fallback: if no signup button AND no display name, page might
            # still be loading. Check for the user menu avatar image.
            avatar = await self.page.query_selector(
                'figure[class*="ScAvatar"] img, img[alt*="avatar" i]'
            )
            return avatar is not None
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

    # ---- cookie import ----

    async def import_cookies(self, auth_token: str):
        """Import a Twitch auth-token cookie so the bot is logged in
        without ever visiting the login page (bypasses Kasada entirely)."""
        if not self.context:
            return False
        try:
            await self.context.add_cookies([
                {
                    "name": "auth-token",
                    "value": auth_token.strip(),
                    "domain": ".twitch.tv",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                },
            ])
            # Verify by navigating to Twitch
            await self._goto(TWITCH_INVENTORY_URL)
            await asyncio.sleep(4)
            if await self._is_logged_in():
                self._update_status(logged_in=True, message="Logged in via auth token!")
                logger.info("User %s: cookie import succeeded", self.user_id)
                return True
            else:
                self._update_status(message="Auth token didn't work — it may be expired")
                return False
        except Exception:
            logger.exception("User %s: cookie import error", self.user_id)
            return False

    # ==================================================================
    # Drop checking & claiming
    # ==================================================================

    async def _check_and_claim_drops(self):
        self._update_status(message="Checking drops…")
        await self._goto(TWITCH_INVENTORY_URL)
        await asyncio.sleep(5)

        # Scroll the full page to trigger lazy-loading of all drop items
        prev_height = 0
        for _ in range(15):
            cur_height = await self.page.evaluate("document.body.scrollHeight")
            if cur_height == prev_height:
                break
            prev_height = cur_height
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        claimed: list[dict] = []
        in_progress: list[dict] = []

        try:
            # Claim any ready drops
            claim_btns = await self.page.query_selector_all('button:has-text("Claim")')
            for btn in claim_btns:
                try:
                    await btn.click()
                    await asyncio.sleep(1.5)
                    claimed.append({"name": "Drop claimed", "time": datetime.now(timezone.utc).isoformat()})
                except Exception:
                    pass

            # Scrape full inventory: find each progress bar's container which
            # holds the reward image (twitch-quests-assets/REWARD/...) and
            # the progress text.
            inventory = await self.page.evaluate(r"""
                () => {
                    const items = [];
                    const seen = new Set();
                    const bars = document.querySelectorAll('[role="progressbar"]');
                    bars.forEach(pb => {
                        const pct = parseInt(pb.getAttribute('aria-valuenow') || '0');
                        // Walk up until we find a container with BOTH an img and this progressbar
                        let container = pb;
                        for (let i = 0; i < 12; i++) {
                            container = container?.parentElement;
                            if (!container) break;
                            if (container.querySelector('img') &&
                                container.querySelector('[role="progressbar"]')) break;
                        }
                        if (!container) return;

                        // Reward image — prefer twitch-quests-assets URLs (actual item art)
                        let image = '';
                        const imgs = container.querySelectorAll('img');
                        for (const img of imgs) {
                            const src = img.src || '';
                            if (src.includes('twitch-quests-assets') || src.includes('REWARD')) {
                                image = src; break;
                            }
                        }
                        if (!image) {
                            for (const img of imgs) {
                                if ((img.naturalWidth || img.width) > 30) {
                                    image = img.src || ''; break;
                                }
                            }
                        }

                        // Item name / time text
                        const texts = [];
                        container.querySelectorAll('p, span').forEach(t => {
                            const v = (t.textContent || '').trim();
                            if (v && v.length > 2 && v.length < 100 && !/^\d+%$/.test(v)
                                && !['Drops','In Progress','Inventory'].includes(v)) {
                                texts.push(v);
                            }
                        });
                        const name = texts.find(t => /of \d+ hour/i.test(t)) || texts[0] || '';
                        const key = image + '_' + pct;
                        if (seen.has(key)) return;
                        seen.add(key);
                        items.push({ name, progress: pct, image });
                    });

                    const campaignNames = [];
                    document.querySelectorAll('p, h3, h4, h5').forEach(el => {
                        const t = (el.textContent || '').trim();
                        if (t.length > 5 && t.length < 80 &&
                            (t.includes('Drop') || t.includes('Campaign') || t.includes('Rush') ||
                             t.includes('Reward') || t.includes('Event'))) {
                            if (!campaignNames.includes(t)) campaignNames.push(t);
                        }
                    });

                    return { items, campaignNames };
                }
            """)

            for item in (inventory.get("items") or []):
                in_progress.append({
                    "name": item.get("name", "Drop"),
                    "progress": item.get("progress", 0),
                    "image": item.get("image", ""),
                })

            # Detect completed games: check selected games against active campaigns
            campaign_names = inventory.get("campaignNames", [])
            active_progress = [i for i in in_progress if i["progress"] < 100]
            self._detect_completed_games(campaign_names, active_progress)

        except Exception:
            logger.debug("Drop check error", exc_info=True)

        all_claimed = self.status.get("drops_claimed", []) + claimed
        self._update_status(
            drops_in_progress=in_progress,
            drops_claimed=all_claimed[-20:],
            completed_games=list(self._completed_games),
            last_check=datetime.now(timezone.utc).isoformat(),
            message=f"Drops: {len(in_progress)} active, {len(claimed)} claimed",
        )
        self._persist_drops(in_progress, claimed)

    def _detect_completed_games(self, campaign_names: list, active_items: list):
        """Check which selected games have no active drops left."""
        targets = self._load_watch_targets()
        active_text = " ".join(i.get("name", "") for i in active_items).lower()
        campaign_text = " ".join(campaign_names).lower()

        for target in targets:
            game = target.get("game_name", "")
            if not game:
                continue
            game_lower = game.lower()
            # A game is "active" if its name appears in campaign names or progress items
            game_words = game_lower.split()
            is_active = any(
                w in campaign_text or w in active_text
                for w in game_words if len(w) > 3
            )
            if not is_active and game_lower not in active_text:
                self._completed_games.add(game)
            else:
                self._completed_games.discard(game)

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
    # Smart stream watching — uses user's game selections
    # ==================================================================

    async def _watch_loop_cycle(self):
        """Check current stream, switch if offline, find best target."""

        if self.status.get("watching"):
            if await self._is_stream_live():
                self._update_watch_time()
                self._update_status(message=f"Watching: {self.status.get('stream_name', '?')}")
                await self._sleep(self._get_check_interval())
                return
            self._stop_watch_timer()
            self._update_status(watching=None, stream_name=None,
                                message="Stream went offline — finding another…")
            await asyncio.sleep(3)

        await self._find_best_stream()
        await self._sleep(self._get_check_interval())

    async def _is_stream_live(self) -> bool:
        try:
            url = self.page.url or ""
            if any(x in url for x in ["/directory", "/drops", "/login"]):
                return False
            offline = await self.page.query_selector(
                '[data-a-target="player-overlay-content-gate"]'
            )
            if offline:
                return False
            live = await self.page.query_selector('video, .video-player')
            return live is not None
        except Exception:
            return False

    async def _find_best_stream(self):
        """Pick the best stream from the user's selected games, skipping completed ones."""
        targets = self._load_watch_targets()
        if not targets:
            self._update_status(message="No games selected — browsing all drops")
            targets = [{"game_url": TWITCH_DROPS_ENABLED_URL}]

        # Filter out completed games
        active_targets = [
            t for t in targets
            if t.get("game_name", "") not in self._completed_games
        ]
        if not active_targets and targets:
            self._update_status(
                message="All selected games complete! Add more games or wait for new campaigns."
            )
            await self._sleep(60)
            # Re-check in case new campaigns appear
            self._completed_games.clear()
            return

        for target in (active_targets or targets):
            if self._stop.is_set():
                return
            game_url = target.get("game_url") or ""
            game_name = target.get("game_name") or "Unknown"
            preferred_streamer = target.get("streamer")

            if preferred_streamer:
                # Specific streamer requested — go directly
                self._update_status(message=f"Checking {preferred_streamer}…")
                stream_url = f"https://www.twitch.tv/{preferred_streamer}"
                await self._goto(stream_url)
                await asyncio.sleep(4)
                if await self._is_stream_live():
                    await self._start_watching(preferred_streamer, stream_url, game_name)
                    return
                continue

            # Browse game's directory for any live streamer with drops
            self._update_status(message=f"Finding drops stream for {game_name}…")
            url = game_url if game_url.startswith("http") else f"https://www.twitch.tv{game_url}"
            await self._goto(url)
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
                    name = ""
                    try:
                        el = await self.page.query_selector(
                            '[data-a-target="stream-title"], h1'
                        )
                        if el:
                            name = (await el.text_content() or "").strip()
                    except Exception:
                        pass
                    name = name or self.page.url.split("/")[-1]
                    await self._start_watching(name, self.page.url, game_name)
                    return
            except Exception:
                pass

        self._update_status(watching=None, stream_name=None,
                            message="No live streams found — will retry")

    async def _start_watching(self, name: str, url: str, game: str):
        await self._accept_mature_content()
        await self._set_low_quality()
        self._start_watch_timer()
        self._update_status(
            watching=url, stream_name=name,
            message=f"Watching: {name} ({game})",
        )

    def _load_watch_targets(self) -> list[dict]:
        try:
            with self.app.app_context():
                from app.models import WatchTarget
                rows = WatchTarget.query.filter_by(
                    user_id=self.user_id, enabled=True
                ).all()
                return [
                    {"game_name": r.game_name, "game_url": r.game_url,
                     "streamer": r.streamer}
                    for r in rows
                ]
        except Exception:
            return []

    # ---- game discovery (class method, no browser needed) ----

    @staticmethod
    async def discover_games(context) -> list[dict]:
        """Scrape twitch.tv/directory/all/tags/dropsenabled for games with active drops."""
        page = await context.new_page()
        try:
            await page.goto(
                "https://www.twitch.tv/directory/all/tags/dropsenabled",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(3)
            # Scroll to load more
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)

            games = await page.evaluate(r"""
                () => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('article').forEach(card => {
                        const gameEl = card.querySelector(
                            'a[data-a-target="preview-card-game-link"], ' +
                            'a[href*="/directory/category/"], a[href*="/directory/game/"]'
                        );
                        if (!gameEl) return;
                        const name = (gameEl.textContent || '').trim();
                        const href = (gameEl.getAttribute('href') || '').trim();
                        if (!name || seen.has(name)) return;
                        seen.add(name);
                        const viewers = (card.querySelector(
                            '[data-a-target="animated-channel-viewers-count"]'
                        ) || {}).textContent || '';
                        out.push({ name, url: href, viewers: viewers.trim() });
                    });
                    return out;
                }
            """)
            return games or []
        finally:
            await page.close()

    @staticmethod
    async def discover_streamers(context, game_url: str) -> list[dict]:
        """Scrape live streamers with drops for a specific game."""
        page = await context.new_page()
        try:
            url = game_url if game_url.startswith("http") else f"https://www.twitch.tv{game_url}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            streamers = await page.evaluate(r"""
                () => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('article').forEach(card => {
                        const a = card.querySelector(
                            'a[data-a-target="preview-card-title-link"], ' +
                            'a[data-a-target="preview-card-image-link"]'
                        );
                        if (!a) return;
                        const href = (a.getAttribute('href') || '').trim();
                        const login = href.replace(/^\//, '').split('/')[0].toLowerCase();
                        if (!login || seen.has(login)) return;
                        seen.add(login);
                        const tags = Array.from(
                            card.querySelectorAll('[data-a-target="tag"], span')
                        ).map(n => (n.textContent||'').trim().toLowerCase());
                        const hasDrops = tags.some(t => t.includes('drops'));
                        const viewers = (card.querySelector(
                            '[data-a-target="animated-channel-viewers-count"]'
                        ) || {}).textContent || '';
                        out.push({
                            name: login,
                            url: 'https://www.twitch.tv/' + login,
                            viewers: viewers.trim(),
                            drops: hasDrops
                        });
                    });
                    return out;
                }
            """)
            return streamers or []
        finally:
            await page.close()

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
