"""Per-user Twitch drop automation with CDP screencast streaming."""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from app.twitch_pages import (
    MATURE_GATE_SELECTOR,
    accept_mature_content_gate,
    collect_virtualized_cards,
    ensure_live_video_playing,
    normalize_twitch_channel_login,
    normalize_twitch_game_url,
    read_twitch_channel_metadata,
    twitch_channel_login_from_url,
    twitch_directory_path,
    twitch_directories_match,
)

logger = logging.getLogger(__name__)

TWITCH_INVENTORY_URL = "https://www.twitch.tv/drops/inventory"
TWITCH_DROPS_ENABLED_URL = "https://www.twitch.tv/directory/all/tags/dropsenabled"
TWITCH_LOGIN_URL = "https://www.twitch.tv/login"

BROWSER_ARGS = [
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

_GAME_CARD_EXTRACTOR_JS = r"""
() => {
    const out = [];
    document.querySelectorAll('article').forEach(card => {
        const game = card.querySelector(
            'a[data-a-target="preview-card-game-link"], '
            'a[href*="/directory/category/"], a[href*="/directory/game/"]'
        );
        if (!game) return;
        const name = (game.textContent || '').trim();
        let parsed;
        try { parsed = new URL(game.getAttribute('href') || '', location.origin); }
        catch { return; }
        if (!/^(www\.)?twitch\.tv$/i.test(parsed.hostname)) return;
        if (!/^\/directory\/(category|game)\/[^/]+\/?$/i.test(parsed.pathname)) return;
        const viewers = (card.querySelector(
            '[data-a-target="animated-channel-viewers-count"], [data-a-target*="viewers"]'
        )?.textContent || '').trim();
        if (name) out.push({name, url: parsed.href, viewers});
    });
    return out;
}
"""

_STREAM_CARD_EXTRACTOR_JS = r"""
() => {
    const out = [];
    document.querySelectorAll('article').forEach(card => {
        const channel = card.querySelector(
            'a[data-a-target="preview-card-image-link"], '
            'a[data-a-target="preview-card-channel-link"], '
            'a[data-a-target="preview-card-title-link"]'
        );
        if (!channel) return;
        let parsed;
        try { parsed = new URL(channel.getAttribute('href') || '', location.origin); }
        catch { return; }
        const parts = parsed.pathname.split('/').filter(Boolean);
        if (!/^(www\.)?twitch\.tv$/i.test(parsed.hostname)) return;
        if (parts.length !== 1 || !/^[a-z0-9_]{1,25}$/i.test(parts[0])) return;
        const login = parts[0].toLowerCase();
        const tags = Array.from(card.querySelectorAll(
            '[aria-label^="Tag, "], [data-a-target="tag"], '
            'a[href*="/directory/all/tags/"]'
        ));
        const hasDrops = tags.some(node => {
            const values = [
                node.textContent || '',
                (node.getAttribute('aria-label') || '').replace(/^Tag,\s*/i, ''),
            ].map(value => value.replace(/[^a-z0-9]/gi, '').toLowerCase());
            let path = '';
            try { path = new URL(node.getAttribute('href') || '', location.origin).pathname; }
            catch {}
            return values.includes('dropsenabled')
                || path.toLowerCase().endsWith('/dropsenabled');
        });
        const game = card.querySelector(
            'a[data-a-target="preview-card-game-link"], '
            'a[href*="/directory/category/"], a[href*="/directory/game/"]'
        );
        let gameUrl = '';
        try { gameUrl = new URL(game?.getAttribute('href') || '', location.origin).href; }
        catch {}
        const viewers = (card.querySelector(
            '[data-a-target="animated-channel-viewers-count"], [data-a-target*="viewers"]'
        )?.textContent || '').trim();
        out.push({
            name: login,
            login,
            url: `https://www.twitch.tv/${login}`,
            viewers,
            drops: hasDrops,
            gameName: (game?.textContent || '').trim(),
            gameUrl,
        });
    });
    return out;
}
"""


def screencast_emit_interval(max_fps: int) -> float:
    """Return the minimum delay between preview frames sent to the client."""
    return 1.0 / max(1, min(10, int(max_fps)))


def normalize_drop_name(value: str | None) -> str:
    """Remove Twitch's progress suffix while preserving the visible reward name."""
    text = " ".join((value or "").split())
    text = re.sub(r"(?:^|\s)\d+%\s*(?:of\s+.*)?$", "", text, flags=re.IGNORECASE)
    return text.strip() or "Drop"


def screencast_options(quality: int) -> dict:
    """Build CDP options that always deliver the first frame of a static page."""
    return {
        "format": "jpeg",
        "quality": max(10, min(100, int(quality))),
        "maxWidth": VIEWPORT["width"],
        "maxHeight": VIEWPORT["height"],
        "everyNthFrame": 1,
    }


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
        self._preview_clients: dict[int, int] = {}
        self._shutting_down = threading.Event()
        self._lock = threading.RLock()
        self._reconcile_thread: threading.Thread | None = None
        self._state_versions: dict[int, int] = {}
        self._automator_snapshot: tuple["UserAutomator", ...] = ()

    def _data_dir_for_user(self, user_id: int) -> str:
        return os.path.join(self.app.config.get("BROWSER_DATA_DIR", "/data/browser"), str(user_id))

    def _refresh_automator_snapshot(self) -> None:
        """Publish a mutation-safe worker snapshot while the manager lock is held."""
        self._automator_snapshot = tuple(self.automators.values())

    @classmethod
    def init(cls, socketio, app):
        cls._instance = cls(socketio, app)
        return cls._instance

    @classmethod
    def get(cls):
        return cls._instance

    def _set_automation_enabled(self, user_id: int, enabled: bool) -> bool:
        try:
            with self.app.app_context():
                from app.extensions import db
                from app.models import UserSettings

                settings = UserSettings.query.filter_by(user_id=user_id).first()
                if not settings:
                    return False
                settings.automation_enabled = enabled
                db.session.commit()
                return True
        except Exception:
            logger.exception(
                "Could not persist automation state for user %s",
                user_id,
            )
            return False

    def _automation_is_enabled(self, user_id: int) -> bool:
        try:
            with self.app.app_context():
                from app.models import User, UserSettings

                return (
                    UserSettings.query.join(User)
                    .filter(
                        UserSettings.user_id == user_id,
                        UserSettings.automation_enabled.is_(True),
                        User.is_active.is_(True),
                    )
                    .first()
                    is not None
                )
        except Exception:
            logger.exception(
                "Could not verify desired automation state for user %s",
                user_id,
            )
            return False

    def _enabled_user_ids(self) -> list[int]:
        """Load active users whose persisted desired state is enabled."""
        with self.app.app_context():
            from app.models import User, UserSettings

            return [
                row.user_id
                for row in UserSettings.query.join(User).filter(
                    UserSettings.automation_enabled.is_(True),
                    User.is_active.is_(True),
                )
            ]

    def reconcile_enabled_users(self) -> None:
        """Restart missing workers whose persisted desired state is enabled."""
        if self._shutting_down.is_set():
            return
        try:
            user_ids = self._enabled_user_ids()
        except Exception:
            logger.exception("Could not reconcile enabled automations")
            return

        for user_id in user_ids:
            with self._lock:
                existing = self.automators.get(user_id)
                if existing and existing.is_alive():
                    continue
                state_version = self._state_versions.get(user_id, 0)
            enabled = self._automation_is_enabled(user_id)
            with self._lock:
                if self._shutting_down.is_set():
                    return
                if self._state_versions.get(user_id, 0) != state_version:
                    continue
                existing = self.automators.get(user_id)
                if existing and existing.is_alive():
                    continue
                if not enabled:
                    continue
                if not self.start_for_user(user_id, persist=False):
                    logger.error("Could not reconcile automation for user %s", user_id)

    def restore_enabled_users(self) -> None:
        """Start every automation that was enabled before the process restarted."""
        self.reconcile_enabled_users()

    def start_reconciler(self) -> None:
        """Continuously repair enabled workers after rare thread-level exits."""
        with self._lock:
            if self._shutting_down.is_set():
                return
            if self._reconcile_thread and self._reconcile_thread.is_alive():
                return
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                name="automation-reconciler",
                daemon=True,
            )
            self._reconcile_thread.start()

    def _reconcile_loop(self) -> None:
        interval = int(self.app.config.get("AUTOMATION_RECONCILE_INTERVAL_SECONDS", 30))
        while not self._shutting_down.wait(max(5, interval)):
            try:
                self.reconcile_enabled_users()
            except Exception:
                # Keep the watchdog alive even if a future reconciliation path
                # gains an exception outside its current defensive boundary.
                logger.exception("Unexpected automation reconciliation failure")

    def reconciler_is_alive(self) -> bool:
        thread = self._reconcile_thread
        return bool(thread and thread.is_alive())

    def start_for_user(self, user_id: int, *, persist: bool = True) -> bool:
        with self._lock:
            if self._shutting_down.is_set():
                return False
            existing = self.automators.get(user_id)
            if existing and existing.is_alive():
                return False
            active_count = sum(automator.is_alive() for automator in self.automators.values())
            if active_count >= int(self.app.config.get("MAX_AUTOMATORS", 2)):
                logger.warning("Automation capacity reached; rejected user %s", user_id)
                return False
            state_persisted = persist and self._set_automation_enabled(user_id, True)
            if persist and not state_persisted:
                return False
            if state_persisted:
                self._state_versions[user_id] = self._state_versions.get(user_id, 0) + 1
            if self._shutting_down.is_set():
                # Preserve the enabled desired state for the next process, but
                # do not create a worker after graceful shutdown has begun.
                return False
            try:
                data_dir = self._data_dir_for_user(user_id)
                os.makedirs(data_dir, exist_ok=True)
                automator = UserAutomator(
                    user_id,
                    data_dir,
                    self.socketio,
                    self.app,
                    preview_enabled=self._preview_clients.get(user_id, 0) > 0,
                )
                self.automators[user_id] = automator
                self._refresh_automator_snapshot()
                if self._shutting_down.is_set():
                    return False
                automator.start()
                if self._shutting_down.is_set():
                    # Shutdown may set its event without acquiring this lock.
                    # A start already past the earlier check must self-signal
                    # so it cannot escape a timed fallback snapshot.
                    automator.stop()
                    return False
            except Exception:
                self.automators.pop(user_id, None)
                self._refresh_automator_snapshot()
                if state_persisted:
                    if self._set_automation_enabled(user_id, False):
                        self._state_versions[user_id] = self._state_versions.get(user_id, 0) + 1
                logger.exception("Could not start automation for user %s", user_id)
                return False
            return True

    def stop_for_user(self, user_id: int, *, persist: bool = True) -> bool:
        with self._lock:
            state_changed = self._set_automation_enabled(user_id, False) if persist else False
            if persist and not state_changed:
                return False
            if state_changed:
                self._state_versions[user_id] = self._state_versions.get(user_id, 0) + 1
            automator = self.automators.get(user_id)
            if automator and automator.is_alive():
                automator.stop()
                return True
            return state_changed

    def set_preview_connected(self, user_id: int, connected: bool) -> None:
        with self._lock:
            count = self._preview_clients.get(user_id, 0)
            count = count + 1 if connected else max(0, count - 1)
            if count:
                self._preview_clients[user_id] = count
            else:
                self._preview_clients.pop(user_id, None)
            automator = self.automators.get(user_id)
            enabled = count > 0
        if automator:
            automator.set_preview_enabled(enabled)

    def is_shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    def shutdown(self, timeout: float = 45) -> None:
        """Stop all browser workers without clearing their persisted desired state."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        deadline = time.monotonic() + max(0, timeout)
        lock_timeout = max(0, min(1, deadline - time.monotonic()))
        acquired = self._lock.acquire(timeout=lock_timeout)
        try:
            automators = (
                list(self.automators.values()) if acquired else list(self._automator_snapshot)
            )
            reconcile_thread = self._reconcile_thread
        finally:
            if acquired:
                self._lock.release()
        for automator in automators:
            if automator.is_alive():
                automator.stop()
        if reconcile_thread and reconcile_thread is not threading.current_thread():
            reconcile_thread.join(max(0, deadline - time.monotonic()))
        for automator in automators:
            automator.wait_until_stopped(max(0, deadline - time.monotonic()))

    def get_automator(self, user_id: int):
        return self.automators.get(user_id)

    def get_status(self, user_id: int) -> dict:
        automator = self.automators.get(user_id)
        if automator:
            status = automator.get_status()
        else:
            status = {
                "running": False,
                "browser_ready": False,
                "logged_in": False,
            }
        try:
            with self.app.app_context():
                from app.models import UserSettings

                settings = UserSettings.query.filter_by(user_id=user_id).first()
                status["automation_enabled"] = bool(settings and settings.automation_enabled)
        except Exception:
            status["automation_enabled"] = status.get("running", False)
        return status


# ======================================================================
# Per-user automator
# ======================================================================


class UserAutomator:
    def __init__(
        self,
        user_id: int,
        data_dir: str,
        socketio,
        app,
        preview_enabled: bool = False,
    ):
        self.user_id = user_id
        self.data_dir = data_dir
        self.socketio = socketio
        self.app = app

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._stop = threading.Event()
        self._preview_enabled = threading.Event()
        if preview_enabled:
            self._preview_enabled.set()

        self.running = False
        self.context = None
        self.page = None
        self.cdp_session = None
        self._screencast_min_interval = screencast_emit_interval(3)
        self._last_screencast_emit = 0.0

        self._watch_start: float | None = None
        self._total_watch_secs: float = 0
        self._passport_429: int = 0
        self._completed_games: set[str] = set()  # games with all rewards claimed

        self.status: dict = {
            "running": False,
            "logged_in": False,
            "browser_channel": None,
            "browser_ready": False,
            "watching": None,
            "watching_game": None,
            "watching_game_url": None,
            "stream_name": None,
            "watch_seconds": 0,
            "message": "Idle",
            "drops_in_progress": [],
            "drops_claimed": [],
            "last_check": None,
            "last_update": None,
            "restart_count": 0,
        }

    # ---- lifecycle ----

    def start(self):
        self.running = True
        self._stop.clear()
        self._total_watch_secs = 0
        self._watch_start = None

        self._update_status(
            running=True,
            browser_ready=False,
            message="Starting…",
        )
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wait_until_stopped(self, timeout: float) -> bool:
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        return not self.is_alive()

    def stop(self):
        self._stop.set()
        self._update_status(message="Stopping…")
        if self._loop and self._loop.is_running() and self._main_task:
            try:
                self._loop.call_soon_threadsafe(self._main_task.cancel)
            except RuntimeError:
                # The worker may close its loop after is_running().
                pass

    def set_preview_enabled(self, enabled: bool) -> None:
        if enabled:
            self._preview_enabled.set()
        else:
            self._preview_enabled.clear()
        if not self._loop or not self._loop.is_running():
            return
        coroutine = self._start_screencast() if enabled else self._stop_screencast()
        try:
            asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            # The browser thread may close its loop between the state check and
            # scheduling. Close the coroutine so Python does not warn or leak it.
            coroutine.close()

    def get_status(self) -> dict:
        s = dict(self.status)
        if self._watch_start:
            s["watch_seconds"] = int(self._total_watch_secs + (time.time() - self._watch_start))
        return s

    # ---- thread / async bridge ----

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        failure = None
        try:
            self._main_task = self._loop.create_task(self._async_main())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            failure = exc
            logger.exception("User %s automation crashed", self.user_id)
        finally:
            try:
                self._loop.run_until_complete(self._cleanup())
            except Exception:
                logger.debug("User %s final cleanup failed", self.user_id, exc_info=True)
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            self._main_task = None
            self.running = False
            if failure:
                detail = str(failure).strip() or failure.__class__.__name__
                self._update_status(
                    running=False,
                    message=f"Automation error: {detail[:200]}",
                )
            else:
                message = (
                    "Stopped" if self._stop.is_set() else self.status.get("message", "Stopped")
                )
                self._update_status(running=False, message=message)

    async def _async_main(self):
        tried_compat = False
        failure_count = 0
        while not self._stop.is_set():
            self._passport_429 = 0
            cycle_started = time.monotonic()
            delay = 0
            try:
                # Restart the Playwright driver as well as Chromium after a
                # failure. A dead driver must not strand an enabled worker.
                async with async_playwright() as p:
                    await self._launch_browser(p, compat_mode=tried_compat)
                    if self._preview_enabled.is_set():
                        await self._start_screencast()
                    await self._full_automation()
                    if not self._stop.is_set():
                        raise RuntimeError("Automation flow ended unexpectedly")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("User %s flow error", self.user_id)
                if self._passport_429 >= 3 and not tried_compat:
                    tried_compat = True
                    logger.info(
                        "User %s: switching to compat mode after %d 429s",
                        self.user_id,
                        self._passport_429,
                    )
                if time.monotonic() - cycle_started >= 300:
                    failure_count = 0
                failure_count += 1
                base_delay = int(self.app.config.get("AUTOMATION_RETRY_BASE_SECONDS", 5))
                max_delay = int(self.app.config.get("AUTOMATION_RETRY_MAX_SECONDS", 300))
                delay = min(max_delay, base_delay * (2 ** min(failure_count - 1, 8)))
                detail = str(exc).strip() or exc.__class__.__name__
                self._update_status(
                    browser_ready=False,
                    logged_in=False,
                    restart_count=failure_count,
                    message=f"Automation error: {detail[:120]}; retrying in {delay}s",
                )
            finally:
                await self._cleanup()
            if not self._stop.is_set() and delay:
                await self._sleep(delay)

    # ---- browser launch ----

    async def _launch_browser(self, p, compat_mode: bool = False):
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
            chromium_sandbox=True,
        )
        self.context = await p.chromium.launch_persistent_context(**kw)
        logger.info("User %s bundled Chromium launched", self.user_id)
        self._update_status(browser_channel="Bundled Chromium")

        # Apply stealth via Playwright init scripts (covers main page frames)
        stealth = Stealth(init_scripts_only=True, navigator_webdriver=True)
        await stealth.apply_stealth_async(self.context)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Apply stealth via CDP — this injects into ALL frames including
        # cross-origin Kasada iframes that context.add_init_script misses.
        try:
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": _STEALTH_JS,
                    "runImmediately": True,
                },
            )
            await cdp.detach()
        except Exception:
            # Fallback to context-level script
            await self.context.add_init_script(_STEALTH_JS)

        self._update_status(browser_ready=True, message="Browser launched")

    # ---- screencast ----

    async def _start_screencast(self):
        if not self.page or self.cdp_session:
            return
        quality, max_fps = 50, 3
        try:
            with self.app.app_context():
                from app.models import UserSettings

                s = UserSettings.query.filter_by(user_id=self.user_id).first()
                if s:
                    quality = max(10, min(100, s.screencast_quality or 50))
                    max_fps = max(1, min(10, s.screencast_max_fps or 3))
        except Exception:
            pass
        try:
            self.cdp_session = await self.context.new_cdp_session(self.page)
            self.cdp_session.on("Page.screencastFrame", self._on_frame)
            self._screencast_min_interval = screencast_emit_interval(max_fps)
            self._last_screencast_emit = 0.0
            await self.cdp_session.send("Page.startScreencast", screencast_options(quality))
        except Exception:
            logger.exception("User %s screencast init failed", self.user_id)
            await self._stop_screencast()

    async def _stop_screencast(self):
        session = self.cdp_session
        self.cdp_session = None
        if not session:
            return
        try:
            await session.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await session.detach()
        except Exception:
            pass

    def _on_frame(self, params):
        try:
            now = time.monotonic()
            if now - self._last_screencast_emit >= self._screencast_min_interval:
                self._last_screencast_emit = now
                self.socketio.emit(
                    "screencast_frame",
                    {"data": params["data"]},
                    room=f"user_{self.user_id}",
                )
            if self.cdp_session and self._loop and self._loop.is_running():
                acknowledgement = self.cdp_session.send(
                    "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
                )
                try:
                    asyncio.run_coroutine_threadsafe(acknowledgement, self._loop)
                except RuntimeError:
                    acknowledgement.close()
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
                await self.page.mouse.wheel(
                    float(data.get("deltaX", 0)), float(data.get("deltaY", 0))
                )
        except Exception:
            pass

    # ==================================================================
    # Full automation flow
    # ==================================================================

    async def _full_automation(self):
        # Track passport 429s for compat-mode switch
        def _on_resp(resp):
            try:
                if "passport.twitch.tv" in resp.url and resp.status == 429:
                    self._passport_429 += 1
            except Exception:
                pass

        self.page.on("response", _on_resp)

        # Navigate to inventory — Twitch will redirect to login if needed.
        self._update_status(message="Navigating to Twitch…")
        if not await self._goto(TWITCH_INVENTORY_URL):
            raise RuntimeError("Could not reach Twitch inventory")
        await asyncio.sleep(4)
        await self._accept_cookies()

        # Wait for page to settle (Kasada iframes load here)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        logged_in = await self._is_logged_in()
        self._update_status(logged_in=logged_in)

        # Imported tokens and normal browser logins persist in the browser profile.
        # If that session has expired, wait for the owner to reconnect through the preview.
        if not logged_in:
            if "/login" not in (self.page.url or ""):
                await self._goto(TWITCH_LOGIN_URL)
                await asyncio.sleep(3)

            await self._accept_cookies()
            self._update_status(message="Twitch login required — connect a token in the dashboard")
            logged_in = await self._wait_for_login()
            if not logged_in:
                return

        self._update_status(logged_in=True, message="Logged in to Twitch!")

        # Main monitoring loop. Any unexpected failure returns to the outer
        # browser supervisor so the whole context is recreated with backoff.
        while not self._stop.is_set():
            await self._check_and_claim_drops()
            await self._watch_loop_cycle()

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
            display = await self.page.query_selector('[data-a-target="user-display-name"]')
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

    # ---- cookie import ----

    async def import_cookies(self, auth_token: str):
        """Import a Twitch auth-token cookie so the bot is logged in
        without ever visiting the login page (bypasses Kasada entirely)."""
        if not self.context:
            return False
        try:
            await self.context.add_cookies(
                [
                    {
                        "name": "auth-token",
                        "value": auth_token.strip(),
                        "domain": ".twitch.tv",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "None",
                    },
                ]
            )
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
        if not self.context:
            raise RuntimeError("Browser context is not available")
        inventory_page = await self.context.new_page()
        try:
            claimed, in_progress = await self._inspect_inventory_page(inventory_page)
        finally:
            await inventory_page.close()

        all_claimed = self.status.get("drops_claimed", []) + claimed
        self._update_status(
            drops_in_progress=in_progress,
            drops_claimed=all_claimed[-20:],
            completed_games=list(self._completed_games),
            last_check=datetime.now(timezone.utc).isoformat(),
            message=f"Drops: {len(in_progress)} active, {len(claimed)} claimed",
        )
        self._persist_drops(in_progress, claimed)

    async def _inspect_inventory_page(self, page):
        await page.goto(
            TWITCH_INVENTORY_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(5)

        # Scroll the full page to trigger lazy-loading of all drop items.
        prev_height = 0
        for _ in range(15):
            cur_height = await page.evaluate("document.body.scrollHeight")
            if cur_height == prev_height:
                break
            prev_height = cur_height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        claimed: list[dict] = []
        in_progress: list[dict] = []
        try:
            # Claim ready drops only when the user has enabled auto-claim.
            claim_btns = (
                await page.query_selector_all('button:has-text("Claim")')
                if self._get_auto_claim()
                else []
            )
            for btn in claim_btns:
                try:
                    claim_info = await btn.evaluate(r"""
                        button => {
                            let container = button;
                            for (let i = 0; i < 10; i++) {
                                container = container?.parentElement;
                                if (!container) break;
                                if (container.querySelector('img') &&
                                    container.querySelector('button')) break;
                            }
                            if (!container) return {name: 'Drop claimed', game: ''};
                            const texts = Array.from(
                                container.querySelectorAll('h3, h4, p, span')
                            ).map(el => (el.textContent || '').trim()).filter(text =>
                                text.length > 2 && text.length < 120 &&
                                !/^claim(ed)?$/i.test(text) && !/^\d+%$/.test(text)
                            );
                            let gameLink = null;
                            let campaign = container;
                            for (let i = 0; i < 12 && campaign; i++) {
                                const links = Array.from(campaign.querySelectorAll(
                                    'a[href*="/directory/category/"], '
                                    'a[href*="/directory/game/"]'
                                ));
                                const paths = new Set(links.map(link => {
                                    try { return new URL(link.href, location.origin).pathname; }
                                    catch { return ''; }
                                }).filter(Boolean));
                                if (paths.size === 1) { gameLink = links[0]; break; }
                                if (paths.size > 1) break;
                                campaign = campaign.parentElement;
                            }
                            return {
                                name: texts[0] || 'Drop claimed',
                                game: (gameLink?.textContent || '').trim(),
                            };
                        }
                    """)
                    await btn.click()
                    await asyncio.sleep(1.5)
                    if not isinstance(claim_info, dict):
                        claim_info = {"name": claim_info or "Drop claimed", "game": ""}
                    claimed.append(
                        {
                            "name": normalize_drop_name(claim_info.get("name")),
                            "game": (claim_info.get("game") or "").strip() or None,
                            "time": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                except Exception:
                    pass

            # Scrape full inventory: find each progress bar's container which
            # holds the reward image (twitch-quests-assets/REWARD/...) and
            # the progress text.
            inventory = await page.evaluate(r"""
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

                        // Twitch renders this nearest reward container as
                        // "Reward name/type 1% of 2 hours". Preserve the
                        // visible text here; Python strips the progress suffix.
                        const name = (container.innerText || '').trim();
                        let gameLink = null;
                        let campaign = container;
                        for (let i = 0; i < 12 && campaign; i++) {
                            const links = Array.from(campaign.querySelectorAll(
                                'a[href*="/directory/category/"], '
                                'a[href*="/directory/game/"]'
                            ));
                            const paths = new Set(links.map(link => {
                                try { return new URL(link.href, location.origin).pathname; }
                                catch { return ''; }
                            }).filter(Boolean));
                            if (paths.size === 1) { gameLink = links[0]; break; }
                            if (paths.size > 1) break;
                            campaign = campaign.parentElement;
                        }
                        const game = (gameLink?.textContent || '').trim();
                        let gameUrl = '';
                        try { gameUrl = new URL(gameLink?.href || '', location.origin).pathname; }
                        catch {}
                        const key = `${gameUrl}|${name}|${image}|${pct}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        items.push({ name, progress: pct, image, game });
                    });

                    const campaigns = [];
                    const seenCampaigns = new Set();
                    document.querySelectorAll('a[href*="/directory/category/"]').forEach(anchor => {
                        let container = anchor;
                        let categoryPaths = new Set();
                        let hasEvidence = false;
                        for (let depth = 0; depth < 10 && container; depth++) {
                            categoryPaths = new Set(Array.from(container.querySelectorAll(
                                'a[href*="/directory/category/"]'
                            )).map(link => {
                                try { return new URL(link.href, location.origin).pathname; }
                                catch { return ''; }
                            }).filter(Boolean));
                            const hasCompletionLabel = Array.from(container.querySelectorAll(
                                'p, span, h1, h2, h3, h4, h5, h6'
                            )).some(el => /^campaign completed!?$/i.test(
                                (el.textContent || '').trim()
                            ));
                            hasEvidence = Boolean(
                                container.querySelector('[role="progressbar"]') ||
                                Array.from(container.querySelectorAll('button')).some(button =>
                                    /^(claim|claimed)\b/i.test((button.textContent || '').trim())
                                ) || hasCompletionLabel
                            );
                            if (hasEvidence && categoryPaths.size === 1) break;
                            container = container.parentElement;
                        }
                        if (!container || !hasEvidence || categoryPaths.size !== 1) return;

                        const gamePath = Array.from(categoryPaths)[0];
                        const completionLabel = Array.from(container.querySelectorAll(
                            'p, span, h1, h2, h3, h4, h5, h6'
                        )).some(el => /^campaign completed!?$/i.test(
                            (el.textContent || '').trim()
                        ));
                        const hasIncompleteProgress = Array.from(container.querySelectorAll(
                            '[role="progressbar"]'
                        )).some(bar => {
                            const value = Number.parseFloat(bar.getAttribute('aria-valuenow'));
                            return Number.isFinite(value) && value < 100;
                        });
                        const hasClaimableReward = Array.from(container.querySelectorAll(
                            'button:not([disabled])'
                        )).some(button => /^claim\b/i.test(
                            (button.textContent || '').trim()
                        ));
                        const key = `${gamePath}|${completionLabel}|${hasIncompleteProgress}|${hasClaimableReward}`;
                        if (seenCampaigns.has(key)) return;
                        seenCampaigns.add(key);
                        campaigns.push({
                            gamePath,
                            complete: completionLabel && !hasIncompleteProgress && !hasClaimableReward,
                        });
                    });

                    return { items, campaigns };
                }
            """)

            for item in inventory.get("items") or []:
                in_progress.append(
                    {
                        "name": normalize_drop_name(item.get("name")),
                        "progress": item.get("progress", 0),
                        "image": item.get("image", ""),
                        "game": (item.get("game") or "").strip() or None,
                    }
                )

            self._detect_completed_games(inventory.get("campaigns") or [])

        except Exception:
            self._completed_games.clear()
            logger.debug("Drop check error", exc_info=True)
        return claimed, in_progress

    def _get_auto_claim(self) -> bool:
        try:
            with self.app.app_context():
                from app.models import UserSettings

                settings = UserSettings.query.filter_by(user_id=self.user_id).first()
                return bool(settings and settings.auto_claim)
        except Exception:
            logger.debug("auto-claim setting load failed", exc_info=True)
            return False

    def _detect_completed_games(self, campaigns: list):
        """Mark games complete only from affirmative, exact-category campaign records."""
        targets = self._load_watch_targets()
        records: list[tuple[str, bool]] = []
        for campaign in campaigns:
            game_path = str(campaign.get("gamePath") or "").strip()
            if not game_path.casefold().startswith(("/directory/category/", "/directory/game/")):
                continue
            records.append((game_path, campaign.get("complete") is True))

        completed_games = set()
        for target in targets:
            game_name = target.get("game_name")
            game_url = target.get("game_url")
            if not game_name or not game_url:
                continue
            matching_records = [
                complete
                for campaign_url, complete in records
                if twitch_directories_match(game_url, campaign_url)
            ]
            if matching_records and all(matching_records):
                completed_games.add(game_name)

        self._completed_games.clear()
        self._completed_games.update(completed_games)

    def _persist_drops(self, in_progress: list, claimed: list):
        try:
            with self.app.app_context():
                from app.models import DropLog
                from app.extensions import db

                def find_log(name: str, status: str, game: str | None):
                    query = DropLog.query.filter_by(
                        user_id=self.user_id,
                        drop_name=name,
                        status=status,
                    )
                    if game:
                        exact = query.filter_by(game=game).first()
                        if exact:
                            return exact
                        legacy = query.filter(DropLog.game.is_(None)).first()
                        if legacy:
                            legacy.game = game
                        return legacy
                    return query.filter(DropLog.game.is_(None)).first()

                normalized_progress = [
                    {
                        **d,
                        "name": normalize_drop_name(d.get("name"))[:255],
                        "game": (d.get("game") or "").strip() or None,
                    }
                    for d in in_progress
                ]
                claimed_keys: set[tuple[str, str | None]] = set()
                resolved_game_less_claims: dict[str, str | None] = {}
                for d in claimed:
                    name = normalize_drop_name(d.get("name"))[:255]
                    game = (d.get("game") or "").strip() or None
                    if not game:
                        database_games = {
                            row.game
                            for row in DropLog.query.filter_by(
                                user_id=self.user_id,
                                drop_name=name,
                            )
                            .filter(DropLog.status.in_(("in_progress", "claimed")))
                            .all()
                            if row.game
                        }
                        incoming_games = {
                            item["game"]
                            for item in normalized_progress
                            if item["name"] == name and item["game"]
                        }
                        candidate_games = database_games | incoming_games
                        if len(candidate_games) == 1:
                            game = next(iter(candidate_games))
                        resolved_game_less_claims[name] = game
                    existing = find_log(name, "in_progress", game)
                    if existing:
                        existing.status = "claimed"
                        existing.progress = 100
                        existing.claimed_at = datetime.now(timezone.utc)
                    else:
                        existing = find_log(name, "claimed", game)
                        if existing:
                            existing.progress = 100
                            existing.claimed_at = existing.claimed_at or datetime.now(timezone.utc)
                        else:
                            db.session.add(
                                DropLog(
                                    user_id=self.user_id,
                                    drop_name=name,
                                    game=game,
                                    status="claimed",
                                    progress=100,
                                    claimed_at=datetime.now(timezone.utc),
                                )
                            )
                    claimed_keys.add((name, game))
                for d in normalized_progress:
                    name = d["name"]
                    game = d["game"]
                    if not game and name in resolved_game_less_claims:
                        game = resolved_game_less_claims[name]
                    if (name, game) in claimed_keys:
                        continue
                    progress = d.get("progress", 0)
                    if progress >= 100 and find_log(name, "claimed", game):
                        continue
                    ex = find_log(name, "in_progress", game)
                    if ex:
                        ex.progress = progress
                    else:
                        db.session.add(
                            DropLog(
                                user_id=self.user_id,
                                drop_name=name,
                                game=game,
                                status="in_progress",
                                progress=progress,
                            )
                        )

                retention_days = int(self.app.config.get("DROP_LOG_RETENTION_DAYS", 365))
                max_rows = int(self.app.config.get("DROP_LOG_MAX_ROWS_PER_USER", 10_000))
                cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
                DropLog.query.filter(
                    DropLog.user_id == self.user_id,
                    DropLog.created_at < cutoff,
                ).delete(synchronize_session=False)
                overflow_ids = [
                    row.id
                    for row in DropLog.query.filter_by(user_id=self.user_id)
                    .order_by(DropLog.created_at.desc(), DropLog.id.desc())
                    .offset(max_rows)
                    .limit(1000)
                    .all()
                ]
                if overflow_ids:
                    DropLog.query.filter(DropLog.id.in_(overflow_ids)).delete(
                        synchronize_session=False
                    )
                db.session.commit()
        except Exception:
            logger.debug("persist error", exc_info=True)

    # ==================================================================
    # Smart stream watching — uses user's game selections
    # ==================================================================

    async def _watch_loop_cycle(self):
        """Check current stream, switch if offline, find best target."""

        if self.status.get("watching"):
            watching_game = self.status.get("watching_game")
            if watching_game in self._completed_games:
                self._stop_watch_timer()
                self._update_status(
                    watching=None,
                    watching_game=None,
                    watching_game_url=None,
                    stream_name=None,
                    message=f"{watching_game} campaign complete — finding another…",
                )
            elif await self._is_stream_live():
                metadata = await self._read_channel_metadata()
                expected_game_url = self.status.get("watching_game_url")
                expected_login = twitch_channel_login_from_url(self.status.get("watching"))
                if (
                    metadata
                    and expected_login
                    and metadata.get("login") == expected_login
                    and metadata.get("drops_enabled")
                    and (
                        not expected_game_url
                        or twitch_directories_match(
                            expected_game_url,
                            metadata.get("game_url"),
                        )
                    )
                ):
                    self._update_watch_time()
                    self._update_status(message=f"Watching: {self.status.get('stream_name', '?')}")
                    await self._sleep(self._get_check_interval())
                    return
                self._stop_watch_timer()
                self._update_status(
                    watching=None,
                    watching_game=None,
                    watching_game_url=None,
                    stream_name=None,
                    message=(
                        "Stream redirected, changed category, or lost Drops Enabled "
                        "— finding another…"
                    ),
                )
            else:
                self._stop_watch_timer()
                self._update_status(
                    watching=None,
                    watching_game=None,
                    watching_game_url=None,
                    stream_name=None,
                    message="Stream went offline — finding another…",
                )
                await asyncio.sleep(3)

        await self._find_best_stream()
        await self._sleep(self._get_check_interval())

    async def _is_stream_live(self) -> bool:
        try:
            if not self.page or not twitch_channel_login_from_url(self.page.url):
                return False

            content_gate = await self.page.query_selector(MATURE_GATE_SELECTOR)
            if content_gate:
                try:
                    if not await content_gate.is_visible():
                        content_gate = None
                except Exception:
                    pass
            if content_gate:
                gate_text = (await content_gate.text_content() or "").lower()
                if any(
                    marker in gate_text
                    for marker in ("offline", "unavailable", "has ended", "not available")
                ):
                    return False
                is_mature_gate = any(
                    marker in gate_text
                    for marker in (
                        "mature",
                        "certain audiences",
                        "continue watching",
                        "start watching",
                    )
                )
                if not is_mature_gate or not await self._accept_mature_content():
                    return False

            return await ensure_live_video_playing(self.page)
        except Exception:
            logger.debug("User %s live-state detection failed", self.user_id, exc_info=True)
            return False

    async def _read_channel_metadata(self) -> dict | None:
        """Read canonical channel, category, and Drops eligibility from the page."""
        try:
            return await read_twitch_channel_metadata(self.page)
        except Exception:
            logger.debug("User %s channel metadata read failed", self.user_id, exc_info=True)
            return None

    def _stream_matches_target(
        self,
        metadata: dict | None,
        target_game_url: str,
        expected_login: str | None = None,
    ) -> bool:
        if not metadata or not metadata.get("drops_enabled"):
            return False
        if expected_login and metadata.get("login") != expected_login:
            return False
        return twitch_directories_match(target_game_url, metadata.get("game_url"))

    async def _find_best_stream(self):
        """Pick the best stream from the user's selected games, skipping completed ones."""
        targets = self._load_watch_targets()
        if not targets:
            self._update_status(message="No games selected — browsing all drops")
            targets = [
                {
                    "game_name": "All Drops",
                    "game_url": TWITCH_DROPS_ENABLED_URL,
                }
            ]

        # Filter out completed games
        active_targets = [t for t in targets if t.get("game_name", "") not in self._completed_games]
        if not active_targets and targets:
            self._update_status(
                message="All selected games complete! Add more games or wait for new campaigns."
            )
            await self._sleep(60)
            # Re-check in case new campaigns appear
            self._completed_games.clear()
            return

        for target in active_targets or targets:
            if self._stop.is_set():
                return
            game_url = target.get("game_url") or ""
            game_name = target.get("game_name") or "Unknown"
            preferred_streamer = target.get("streamer")

            if preferred_streamer:
                # Specific streamer requested — go directly
                try:
                    preferred_login = normalize_twitch_channel_login(preferred_streamer)
                except ValueError:
                    logger.warning(
                        "User %s has invalid preferred streamer: %r",
                        self.user_id,
                        preferred_streamer,
                    )
                    continue
                self._update_status(message=f"Checking {preferred_login}…")
                stream_url = f"https://www.twitch.tv/{preferred_login}"
                if not await self._goto(stream_url):
                    continue
                await asyncio.sleep(4)
                metadata = await self._read_channel_metadata()
                if (
                    await self._is_stream_live()
                    and self._stream_matches_target(metadata, game_url, preferred_login)
                    and await self._start_watching(
                        metadata["display_name"],
                        metadata["url"],
                        metadata["game_name"] or game_name,
                        metadata["game_url"],
                    )
                ):
                    return
                continue

            # Browse game's directory for any live streamer with drops
            self._update_status(message=f"Finding drops stream for {game_name}…")
            try:
                url = normalize_twitch_game_url(game_url)
            except ValueError:
                logger.warning("User %s has invalid game URL: %r", self.user_id, game_url)
                continue
            if not await self._goto(url):
                continue
            await asyncio.sleep(4)

            try:
                drops_directory = "/tags/dropsenabled" in url.lower()
                candidates = await collect_virtualized_cards(
                    self.page,
                    _STREAM_CARD_EXTRACTOR_JS,
                    key=lambda item: str(item.get("login") or "").casefold() or None,
                    max_scrolls=8,
                    scroll_delay=0.75,
                )
                candidates = [
                    candidate
                    for candidate in candidates or []
                    if isinstance(candidate, dict)
                    and (drops_directory or candidate.get("drops") is True)
                ]
                seen_logins = set()
                for candidate in candidates:
                    expected_login = twitch_channel_login_from_url(candidate.get("url"))
                    if not expected_login or expected_login in seen_logins:
                        continue
                    seen_logins.add(expected_login)
                    stream_url = f"https://www.twitch.tv/{expected_login}"
                    if not await self._goto(stream_url):
                        continue
                    await asyncio.sleep(5)
                    metadata = await self._read_channel_metadata()
                    if not await self._is_stream_live() or not self._stream_matches_target(
                        metadata, url, expected_login
                    ):
                        continue
                    if await self._start_watching(
                        metadata["display_name"],
                        metadata["url"],
                        metadata["game_name"] or game_name,
                        metadata["game_url"],
                    ):
                        return
            except Exception:
                logger.debug("User %s stream selection failed", self.user_id, exc_info=True)

        self._update_status(
            watching=None,
            watching_game=None,
            watching_game_url=None,
            stream_name=None,
            message="No live streams found — will retry",
        )

    async def _start_watching(
        self,
        name: str,
        url: str,
        game: str,
        game_url: str,
    ) -> bool:
        if not await self._accept_mature_content() or not await self._is_stream_live():
            return False
        await self._set_low_quality()
        self._start_watch_timer()
        self._update_status(
            watching=url,
            watching_game=game,
            watching_game_url=game_url,
            stream_name=name,
            message=f"Watching: {name} ({game})",
        )
        return True

    def _load_watch_targets(self) -> list[dict]:
        try:
            with self.app.app_context():
                from app.models import WatchTarget

                rows = WatchTarget.query.filter_by(user_id=self.user_id, enabled=True).all()
                targets = [
                    {"game_name": r.game_name, "game_url": r.game_url, "streamer": r.streamer}
                    for r in rows
                ]
                return sorted(targets, key=lambda target: not bool(target.get("streamer")))
        except Exception:
            logger.exception("Could not load watch targets for user %s", self.user_id)
            raise RuntimeError("Could not load saved watch targets")

    # ---- game discovery (class method, no browser needed) ----

    @staticmethod
    async def discover_games(context) -> list[dict]:
        """Scrape twitch.tv/directory/all/tags/dropsenabled for games with active drops."""
        page = await context.new_page()
        try:
            response = await page.goto(
                "https://www.twitch.tv/directory/all/tags/dropsenabled",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if isinstance(getattr(response, "status", None), int) and response.status >= 400:
                raise RuntimeError(f"Twitch directory returned HTTP {response.status}")
            if twitch_directory_path(page.url) != twitch_directory_path(TWITCH_DROPS_ENABLED_URL):
                raise RuntimeError("Twitch redirected game discovery away from the directory")
            await asyncio.sleep(3)
            games = await collect_virtualized_cards(
                page,
                _GAME_CARD_EXTRACTOR_JS,
                key=lambda item: str(item.get("url") or "").casefold() or None,
                max_scrolls=20,
                scroll_delay=1.5,
            )
            normalized = []
            for game in games:
                try:
                    game_url = normalize_twitch_game_url(game.get("url") or "")
                except ValueError:
                    continue
                normalized.append(
                    {
                        "name": (game.get("name") or "").strip(),
                        "url": game_url,
                        "viewers": (game.get("viewers") or "").strip(),
                    }
                )
            return [game for game in normalized if game["name"]]
        finally:
            await page.close()

    @staticmethod
    async def discover_streamers(context, game_url: str) -> list[dict]:
        """Scrape live streamers with drops for a specific game."""
        page = await context.new_page()
        try:
            url = normalize_twitch_game_url(game_url)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if isinstance(getattr(response, "status", None), int) and response.status >= 400:
                raise RuntimeError(f"Twitch directory returned HTTP {response.status}")
            if not twitch_directories_match(url, page.url):
                raise RuntimeError("Twitch redirected channel discovery away from the game")
            await asyncio.sleep(3)
            streamers = await collect_virtualized_cards(
                page,
                _STREAM_CARD_EXTRACTOR_JS,
                key=lambda item: str(item.get("login") or "").casefold() or None,
                max_scrolls=15,
                scroll_delay=1.5,
            )
            eligible = []
            for streamer in streamers:
                if streamer.get("drops") is not True:
                    continue
                if streamer.get("gameUrl") and not twitch_directories_match(
                    url, streamer.get("gameUrl")
                ):
                    continue
                try:
                    login = normalize_twitch_channel_login(streamer.get("login") or "")
                except ValueError:
                    continue
                eligible.append(
                    {
                        "name": login,
                        "url": f"https://www.twitch.tv/{login}",
                        "viewers": (streamer.get("viewers") or "").strip(),
                        "drops": True,
                    }
                )
            return eligible
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
            self.status["watch_seconds"] = int(
                self._total_watch_secs + (time.time() - self._watch_start)
            )

    # ---- stream helpers ----

    async def _accept_mature_content(self) -> bool:
        try:
            return await accept_mature_content_gate(self.page)
        except Exception:
            logger.debug("User %s mature gate handling failed", self.user_id, exc_info=True)
            return False

    async def _set_low_quality(self):
        try:
            sb = await self.page.query_selector('[data-a-target="player-settings-button"]')
            if not sb:
                return
            await sb.click()
            await asyncio.sleep(0.5)
            qb = await self.page.query_selector(
                '[data-a-target="player-settings-menu-item-quality"]'
            )
            if qb:
                await qb.click()
                await asyncio.sleep(0.5)
                opts = await self.page.query_selector_all(
                    '[data-a-target="player-settings-submenu-quality-option"]'
                )
                if opts:
                    await opts[-1].click()
        except Exception:
            pass

    # ---- navigation / utility ----

    async def _goto(self, url: str, timeout: int = 60000) -> bool:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception:
            logger.warning("User %s navigation to %s failed", self.user_id, url, exc_info=True)
            return False

    async def _accept_cookies(self):
        try:
            btn = await self.page.query_selector("#onetrust-accept-btn-handler")
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
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
        self._update_status(browser_ready=False)
        try:
            await self._stop_screencast()
            if self.context:
                await self.context.close()
        except Exception:
            pass
        self.context = self.page = self.cdp_session = None
