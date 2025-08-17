import logging
import asyncio
import re
from playwright.async_api import async_playwright
from playwright_stealth import Stealth, ALL_EVASIONS_DISABLED_KWARGS
import os
import json
import threading
import atexit
import signal
import sys
import subprocess

# --- Platform detection ---
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

try:
	import pystray
	from PIL import Image, ImageDraw
except Exception:
	pystray = None

# Notifications
Notification = None
# Optional notifier (single implementation: win10toast)
try:
	from win10toast import ToastNotifier  # type: ignore
except Exception:
	ToastNotifier = None

# --- Configuration ---
# Resolve base directory for consistent file paths regardless of CWD
BASE_DIR = os.path.abspath(os.path.dirname(__file__)) if '__file__' in globals() else os.getcwd()
LOG_FILE = os.path.join(BASE_DIR, 'drops_log.txt')
USER_DATA_DIR = os.path.join(BASE_DIR, 'user_data_stealth')
TWITCH_INVENTORY_URL = 'https://www.twitch.tv/drops/inventory'
TWITCH_RUST_DIRECTORY_URL = 'https://www.twitch.tv/directory/game/Rust'
FACEPUNCH_DROPS_URL = 'https://twitch.facepunch.com/#drops'
DEFAULT_HEADLESS = True  # Default when no config exists
BROWSER_CHANNEL = "chrome"  # Alternatives: "msedge"
FORCE_USER_AGENT: str | None = None  # e.g. "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
STEALTH_PROFILE = "minimal"  # Options: "full", "minimal", "off"
WARM_SSO = False  # Open id.twitch.tv to warm cookies if login is slow
PASSPORT_429_THRESHOLD = 3
INVENTORY_POLL_INTERVAL_SECONDS = 60
MAX_WATCH_HOURS_PER_REWARD = 8

# Persisted preferences/config
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
CONFIG_LOCK = threading.RLock()
EXIT_EVENT = threading.Event()

PREFERENCES = None
TRAY_ICON = None
ICON_PATH = None
NOTIFICATIONS_ENABLED = True
TRAY_IMAGE = None


def load_preferences():
	default_prefs = {"headless": DEFAULT_HEADLESS, "hide_console": True}
	try:
		if os.path.exists(CONFIG_PATH):
			with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
				data = json.load(f)
				if isinstance(data, dict):
					default_prefs.update(data)
	except Exception as e:
		logging.debug(f"Could not load preferences: {e}")
	return default_prefs


def save_preferences(prefs):
	try:
		with CONFIG_LOCK:
			with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
				json.dump(prefs, f, indent=2)
	except Exception as e:
		logging.warning(f"Could not save preferences: {e}")


def get_headless_preference() -> bool:
	try:
		return bool(PREFERENCES.get("headless", DEFAULT_HEADLESS))
	except Exception:
		return DEFAULT_HEADLESS

def ensure_icon_file(image) -> str | None:
	try:
		base = os.path.abspath(os.path.dirname(__file__)) if '__file__' in globals() else os.getcwd()
		path = os.path.join(base, 'tray.ico')
		if image is None:
			return None
		# Save ICO once if not present
		if not os.path.exists(path):
			# Convert to ICO-friendly size
			ico_img = image.copy().resize((64, 64))
			ico_img.save(path, format='ICO')
		return path
	except Exception as e:
		logging.debug(f"Could not create tray icon file: {e}")
		return None


def send_notification(title: str, message: str):
	global NOTIFICATIONS_ENABLED
	if not NOTIFICATIONS_ENABLED:
		return
	try:
		if IS_WINDOWS and ToastNotifier:
			icon_path = ICON_PATH if ICON_PATH and os.path.exists(ICON_PATH) else None
			toaster = ToastNotifier()
			try:
				if icon_path:
					toaster.show_toast(title, message, icon_path=icon_path, duration=3, threaded=False)
				else:
					toaster.show_toast(title, message, duration=3, threaded=False)
			except TypeError:
				toaster.show_toast(title, message, duration=3, threaded=False)
			logging.info("Notification queued via win10toast")
			return
		if IS_MAC:
			# Use AppleScript for native notifications without extra deps
			try:
				def _escape_applescript(s: str) -> str:
					return (s or "").replace("\\", "\\\\").replace("\"", "\\\"")
				script = f'display notification "{_escape_applescript(message)}" with title "{_escape_applescript(title)}"'
				subprocess.run(["osascript", "-e", script], check=False)
				logging.info("Notification sent via osascript")
				return
			except Exception as _:
				pass
		logging.debug("No compatible notifier available on this platform; skipping notification.")
	except Exception as e:
		logging.warning(f"Notification failed: {e}")


def restart_program():
	try:
		with CONFIG_LOCK:
			hide = bool(PREFERENCES.get("hide_console", True))
		script_path = os.path.join(BASE_DIR, os.path.basename(__file__) if '__file__' in globals() else 'twitch_drop_automator.py')
		interpreter = None
		creationflags = 0
		if IS_WINDOWS:
			venv_py = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')
			venv_pyw = os.path.join(BASE_DIR, 'venv', 'Scripts', 'pythonw.exe')
			if hide and os.path.exists(venv_pyw):
				interpreter = venv_pyw
			elif os.path.exists(venv_py):
				interpreter = venv_py
			else:
				interpreter = sys.executable
			if hide and interpreter.lower().endswith('python.exe'):
				creationflags |= 0x08000000  # CREATE_NO_WINDOW
		else:
			# POSIX: prefer venv/bin/python if present; no hidden-console concept
			venv_py = os.path.join(BASE_DIR, 'venv', 'bin', 'python')
			interpreter = venv_py if os.path.exists(venv_py) else sys.executable
		subprocess.Popen([interpreter, script_path], cwd=BASE_DIR, creationflags=creationflags)
	except Exception as e:
		logging.warning(f"Restart spawn failed: {e}")
	try:
		EXIT_EVENT.set()
		if TRAY_ICON:
			try:
				threading.Thread(target=TRAY_ICON.stop, daemon=True).start()
			except Exception:
				pass
		threading.Timer(1.5, lambda: os._exit(0)).start()
	except Exception:
		os._exit(0)

async def wait_with_exit(task: asyncio.Task):
	while True:
		done, _ = await asyncio.wait({task}, timeout=0.3)
		if task in done:
			return await task
		if EXIT_EVENT.is_set():
			try:
				task.cancel()
			except Exception:
				pass
			raise asyncio.CancelledError("Exit requested")

async def goto_with_exit(page, url: str, timeout: int = 120000, wait_until: str = "domcontentloaded"):
	t = asyncio.create_task(page.goto(url, timeout=timeout, wait_until=wait_until))
	return await wait_with_exit(t)


def _generate_tray_icon_image():
	try:
		img = Image.new("RGBA", (64, 64), (40, 44, 52, 255))
		d = ImageDraw.Draw(img)
		d.ellipse((6, 6, 58, 58), fill=(113, 89, 193, 255))
		d.rectangle((28, 18, 36, 46), fill=(255, 255, 255, 255))
		d.rectangle((22, 18, 42, 26), fill=(255, 255, 255, 255))
		return img
	except Exception:
		return None


def start_system_tray(block: bool = False):
	if pystray is None:
		logging.info("pystray/Pillow not available; tray icon disabled.")
		return None

	def on_toggle_headless(icon, item):
		try:
			with CONFIG_LOCK:
				PREFERENCES["headless"] = not bool(PREFERENCES.get("headless", DEFAULT_HEADLESS))
				new_val = PREFERENCES["headless"]
			threading.Thread(target=lambda: save_preferences(PREFERENCES), daemon=True).start()
			logging.info(f"Tray: headless set to {new_val}. Restarting to apply…")
			try:
				icon.update_menu()
			except Exception:
				pass
			threading.Thread(target=lambda: (send_notification("Twitch Drops", "Applying headless change…"), restart_program()), daemon=True).start()
		except Exception as e:
			logging.warning(f"Tray toggle failed: {e}")

	def is_headless_checked(item):
		try:
			with CONFIG_LOCK:
				return bool(PREFERENCES.get("headless", DEFAULT_HEADLESS))
		except Exception:
			return bool(DEFAULT_HEADLESS)

	def on_toggle_hide_console(icon, item):
		try:
			with CONFIG_LOCK:
				PREFERENCES["hide_console"] = not bool(PREFERENCES.get("hide_console", True))
				new_val = PREFERENCES["hide_console"]
			threading.Thread(target=lambda: save_preferences(PREFERENCES), daemon=True).start()
			logging.info(f"Tray: hide_console set to {new_val}. Restarting to apply…")
			try:
				icon.update_menu()
			except Exception:
				pass
			threading.Thread(target=lambda: (send_notification("Twitch Drops", "Applying console visibility change…"), restart_program()), daemon=True).start()
		except Exception as e:
			logging.warning(f"Tray toggle failed: {e}")

	def is_hide_console_checked(item):
		try:
			with CONFIG_LOCK:
				return bool(PREFERENCES.get("hide_console", True))
		except Exception:
			return True

	def on_quit(icon, item):
		# Send toast in background to avoid blocking tray thread
		try:
			threading.Thread(target=lambda: send_notification("Twitch Drops", "Exiting…"), daemon=True).start()
		except Exception:
			pass
		EXIT_EVENT.set()
		# Stop icon from a different thread to avoid potential deadlock
		try:
			threading.Thread(target=icon.stop, daemon=True).start()
		except Exception:
			pass
		# Fallback: force terminate if graceful exit hangs
		threading.Timer(5.0, lambda: os._exit(0)).start()

	image = _generate_tray_icon_image()
	menu = pystray.Menu(
		pystray.MenuItem("Headless mode", on_toggle_headless, checked=is_headless_checked),
		pystray.MenuItem("Hide console on startup", on_toggle_hide_console, checked=is_hide_console_checked),
		pystray.MenuItem("Quit", on_quit)
	)
	icon = pystray.Icon("TwitchDropAutomator", image, "Twitch Drop Automator", menu=menu)
	global TRAY_IMAGE
	TRAY_IMAGE = image
	if block:
		# On macOS, Cocoa requires the app loop on the main thread
		atexit.register(lambda: icon.stop())
		icon.run()
		return icon
	else:
		icon.run_detached()
		atexit.register(lambda: icon.stop())
		return icon

# --- Logger Setup ---
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - %(levelname)s - %(message)s',
	handlers=[
		logging.FileHandler(LOG_FILE),
		logging.StreamHandler()
	])

logging.info(f"Using base dir: {BASE_DIR}")
logging.info(f"Config path: {CONFIG_PATH}")
logging.info(f"Log file: {LOG_FILE}")
logging.info(f"User data dir: {USER_DATA_DIR}")

# Initialize preferences once logging is configured
PREFERENCES = load_preferences()

def _install_signal_handlers():
	def _handler(signum, frame):
		EXIT_EVENT.set()
	for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
		sig = getattr(signal, sig_name, None)
		if sig is not None:
			try:
				signal.signal(sig, _handler)
			except Exception:
				pass

async def apply_stealth_to_context(context, profile: str):
	if profile == "off":
		logging.info("Stealth: OFF")
		return
	if profile == "minimal":
		logging.info("Stealth: MINIMAL (navigator.webdriver only)")
		minimal_kwargs = {**ALL_EVASIONS_DISABLED_KWARGS, "navigator_webdriver": True}
		stealth = Stealth(init_scripts_only=True, **minimal_kwargs)
	else:
		logging.info("Stealth: FULL")
		stealth = Stealth(init_scripts_only=True)
	await stealth.apply_stealth_async(context)

async def maybe_accept_cookies(page):
	try:
		btn = await page.query_selector('#onetrust-accept-btn-handler')
		if btn:
			await btn.click()
			logging.info("Accepted OneTrust cookies banner")
			await asyncio.sleep(0.5)
	except Exception:
		pass

async def launch_context(p, compat_mode: bool):
	args = [
		"--disable-extensions",
		"--disable-features=BlockThirdPartyCookies,CookieDeprecationMessages",
	]
	ignore_default_args = None
	if not compat_mode:
		args.append("--disable-blink-features=AutomationControlled")
		ignore_default_args = ["--enable-automation"]

	# macOS: strictly require Google Chrome, no Chromium fallback
	if IS_MAC:
		def _find_macos_chrome_executable():
			candidates = [
				"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
				os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
				"/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
			]
			for path in candidates:
				if os.path.exists(path):
					return path
			return None
		chrome_exec = _find_macos_chrome_executable()
		if not chrome_exec:
			raise RuntimeError("Google Chrome is required on macOS. Please install it from https://www.google.com/chrome/")
		context = await p.chromium.launch_persistent_context(
			USER_DATA_DIR,
			headless=get_headless_preference(),
			executable_path=chrome_exec,
			slow_mo=50,
			ignore_default_args=ignore_default_args,
			args=args,
			user_agent=FORCE_USER_AGENT if FORCE_USER_AGENT else None,
			viewport={"width": 1366, "height": 768},
			locale="en-US",
		)
	else:
		# Other platforms: try preferred channel, then fall back to default Chromium
		try:
			context = await p.chromium.launch_persistent_context(
				USER_DATA_DIR,
				headless=get_headless_preference(),
				channel=BROWSER_CHANNEL,
				slow_mo=50,
				ignore_default_args=ignore_default_args,
				args=args,
				user_agent=FORCE_USER_AGENT if FORCE_USER_AGENT else None,
				viewport={"width": 1366, "height": 768},
				locale="en-US",
			)
		except Exception as e:
			logging.warning(f"Primary browser channel '{BROWSER_CHANNEL}' failed ({e}). Falling back to default Chromium.")
			context = await p.chromium.launch_persistent_context(
				USER_DATA_DIR,
				headless=get_headless_preference(),
				slow_mo=50,
				ignore_default_args=ignore_default_args,
				args=args,
				user_agent=FORCE_USER_AGENT if FORCE_USER_AGENT else None,
				viewport={"width": 1366, "height": 768},
				locale="en-US",
			)

	await apply_stealth_to_context(context, profile=("off" if compat_mode else STEALTH_PROFILE))

	page = await context.new_page()

	page.on("console", lambda msg: logging.debug(f"Console[{msg.type}]: {msg.text}"))
	page.on("pageerror", lambda err: logging.error(f"PageError: {err}"))
	page.on("framenavigated", lambda frame: logging.info(f"Navigated: {frame.url}"))

	logging.info(f"Browser launched. Mode: {'COMPAT' if compat_mode else 'NORMAL'}")

	try:
		ua = await page.evaluate("navigator.userAgent")
		webdriver = await page.evaluate("'webdriver' in navigator ? navigator.webdriver : undefined")
		langs = await page.evaluate("navigator.languages")
		logging.info(f"UA: {ua}")
		logging.info(f"navigator.webdriver: {webdriver}")
		logging.info(f"navigator.languages: {langs}")
	except Exception:
		pass

	return context, page

async def wait_until_logged_in(context, page) -> None:
	"""Keep the app open and poll Twitch inventory until the user is logged in (avatar present)."""
	try:
		# One-time reminder toast
		try:
			send_notification("Twitch Drops", "Waiting for login… Right-click tray → untick 'Headless mode'")
		except Exception:
			pass
		while not EXIT_EVENT.is_set():
			try:
				await goto_with_exit(page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
				await maybe_accept_cookies(page)
				await page.wait_for_timeout(500)
				avatar = await page.query_selector('img[alt="User Avatar"]')
				if avatar:
					logging.info("Detected user avatar; login complete.")
					return
				logging.info("Not logged in yet; still waiting…")
			except Exception:
				pass
			await asyncio.sleep(5)
	finally:
		return

async def run_flow(p):
	tried_compat = False

	while True:
		if EXIT_EVENT.is_set():
			raise asyncio.CancelledError("Exit requested")
		context = None
		page = None
		passport_429_count = {"count": 0}
		compat_mode = tried_compat
		success = False
		try:
			context, page = await launch_context(p, compat_mode=compat_mode)

			def on_response(resp):
				try:
					if "passport.twitch.tv" in resp.url and resp.status == 429:
						passport_429_count["count"] += 1
						logging.warning(f"Passport 429 detected ({passport_429_count['count']}).")
				except Exception:
					pass
			page.on("response", on_response)

			logging.info(f"Navigating to Twitch inventory: {TWITCH_INVENTORY_URL}")
			await goto_with_exit(page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(page)
			logging.info(f"Landed URL: {page.url}")

			warm_done = False
			if any(x in page.url for x in ["/login", "id.twitch.tv"]):
				logging.info("Detected Twitch login flow. Waiting for SSO to initialize...")
				try:
					await wait_with_exit(asyncio.create_task(page.wait_for_load_state("networkidle", timeout=30000)))
				except Exception:
					pass
				await maybe_accept_cookies(page)
				if WARM_SSO and not warm_done and any(x in page.url for x in ["/login", "id.twitch.tv"]):
					logging.info("Login seems slow. Opening id.twitch.tv to warm SSO context...")
					warm_page = await context.new_page()
					try:
						await goto_with_exit(warm_page, "https://id.twitch.tv/", timeout=30000, wait_until="domcontentloaded")
						await wait_with_exit(asyncio.create_task(warm_page.wait_for_load_state("domcontentloaded", timeout=15000)))
						await maybe_accept_cookies(warm_page)
					except Exception as warm_err:
						logging.warning(f"Warm-up visit failed: {warm_err}")
					finally:
						await warm_page.close()
						warm_done = True

			try:
				await wait_with_exit(asyncio.create_task(page.wait_for_selector('img[alt="User Avatar"]', timeout=45000)))
				logging.info("User appears to be logged in.")
				success = True
				return context
			except Exception:
				logging.warning("Could not find user avatar. User may not be logged in. Waiting for user to complete login.")
				await wait_until_logged_in(context, page)
				# After wait, verify again
				try:
					await page.wait_for_selector('img[alt="User Avatar"]', timeout=20000)
					logging.info("Login detected after waiting.")
					success = True
					return context
				except Exception:
					raise
		except Exception as e:
			if passport_429_count["count"] >= PASSPORT_429_THRESHOLD and not tried_compat:
				tried_compat = True
				logging.info("Closing current context to retry in compatibility mode...")
				try:
					if context:
						await context.close()
				finally:
					await asyncio.sleep(5)
				continue
			else:
				logging.error(f"Flow failed: {e}")
				raise
		finally:
			if not success:
				logging.info("Closing browser.")
				if context:
					await context.close()

# ---- Facepunch parsing ----

def _safe_int(val):
	try:
		return int(val)
	except Exception:
		return None

async def fetch_facepunch_drops(context):
	page = await context.new_page()
	try:
		await goto_with_exit(page, FACEPUNCH_DROPS_URL, timeout=120000, wait_until="domcontentloaded")
		await asyncio.sleep(1)
		# Prefer DOM parsing for streamer drops
		streamer_specific = []
		try:
			await page.wait_for_selector('.streamer-drops .drop-box', timeout=5000)
			boxes = await page.query_selector_all('.streamer-drops .drop-box')
			for box in boxes:
				try:
					online_node = await box.query_selector('.online-status, div.online-status')
					is_live = bool(online_node)
					streamer_el = await box.query_selector('.streamer-name')
					streamer = (await streamer_el.inner_text()).strip() if streamer_el else None
					url_el = await box.query_selector('.drop-box-header a.streamer-info')
					twitch_url = await url_el.get_attribute('href') if url_el else None
					item_el = await box.query_selector('.drop-box-footer .drop-type')
					item = (await item_el.inner_text()).strip() if item_el else None
					hours_el = await box.query_selector('.drop-box-footer .drop-time span')
					hours_text = (await hours_el.inner_text()).strip() if hours_el else ''
					hours_m = re.search(r'(\d+)', hours_text)
					hours = _safe_int(hours_m.group(1)) if hours_m else None
					if streamer and item:
						streamer_specific.append({
							"streamer": streamer,
							"item": item,
							"hours": hours,
							"url": twitch_url,
							"is_live": is_live,
						})
				except Exception:
					continue
		except Exception:
			pass
		# General drops (DOM first, regex fallback)
		general = []
		# DOM approach
		try:
			# Wait for section container if possible
			try:
				await page.wait_for_selector('#drops .drops-container', timeout=6000)
			except Exception:
				pass
			data = await page.evaluate(
				r"""
				() => {
				  const res = [];
				  const boxes = Array.from(document.querySelectorAll('#drops .drops-container .drop-box'));
				  const allBoxes = boxes.length ? boxes : Array.from(document.querySelectorAll('.drop-box'));
				  for (const box of allBoxes) {
				    const headerEl = box.querySelector('.drop-box-header');
				    let headerText = '';
				    if (headerEl) {
				      headerText = (headerEl.innerText || headerEl.textContent || '').trim();
				    }
				    const isGeneral = /\bgeneral\s+drop\b/i.test(headerText);
				    let alias = null;
				    try {
				      const m = headerText.match(/([A-Za-z0-9]+)\s+GENERAL\s+DROP/i);
				      if (m) alias = m[1];
				    } catch (e) {}
				    const itemEl = box.querySelector('.drop-box-footer .drop-type');
				    const item = itemEl && itemEl.textContent ? itemEl.textContent.trim() : null;
				    const timeEl = box.querySelector('.drop-box-footer .drop-time span');
				    let hours = null;
				    if (timeEl && timeEl.textContent) {
				      const m = timeEl.textContent.match(/(\d+)/);
				      if (m) hours = parseInt(m[1], 10);
				    }
				    res.push({ headerText, isGeneral, item, hours, alias });
				  }
				  return res;
				}
				"""
			)
			try:
				gen_candidates = [d for d in (data or []) if d and d.get('isGeneral')]
				headers_preview = ', '.join([(d.get('headerText') or '') for d in (data or [])][:5])
				logging.debug(f"General scan: boxes={len(data or [])}, general_candidates={len(gen_candidates)}, sample_headers=[{headers_preview}]")
			except Exception:
				pass
			for d in data or []:
				try:
					if d.get('isGeneral') and d.get('item'):
						general.append({"item": d.get('item'), "hours": d.get('hours'), "alias": d.get('alias'), "header": d.get('headerText')})
				except Exception:
					continue
		except Exception:
			pass

		# Regex fallback
		if not general:
			try:
				body = await page.inner_text('body')
				m = re.search(r"General Drops(.*?)(Streamer Drops|Drops Metrics|FAQ|Frequently Asked Questions|$)", body, flags=re.S)
				if m:
					general_section = m.group(1)
					for gm in re.finditer(r"General Drop\s+([A-Za-z0-9 \-]+?)\s+(\d+)\s+Hour", general_section):
						item = gm.group(1).strip()
						hours = _safe_int(gm.group(2))
						general.append({"item": item, "hours": hours})
			except Exception:
				pass

		# Console dump of general drops and longest
		try:
			if general:
				logging.info("General drops found:")
				for g in general:
					logging.info(f"- {g.get('item')} = {g.get('hours')} hours (alias={g.get('alias')})")
				vals = [g for g in general if isinstance(g.get('hours'), int)]
				if vals:
					longest = max(vals, key=lambda g: g.get('hours') or 0)
					logging.info(f"Longest general drop: {longest.get('item')} ({longest.get('hours')} hours)")
				else:
					logging.info("Could not determine longest general drop (missing hour values).")
			else:
				logging.info("No general drops parsed from Facepunch this cycle.")
		except Exception:
			pass

		logging.info(f"Facepunch drops parsed. General: {len(general)}, Streamer: {len(streamer_specific)}")
		return {"general": general, "streamer": streamer_specific}
	except Exception as e:
		logging.warning(f"Facepunch parsing failed: {e}")
		return {"general": [], "streamer": []}
	finally:
		try:
			await page.close()
		except Exception:
			pass

# ---- Drops workflow helpers ----

async def get_inventory_progress_map(inv_page):
	progress = {}
	try:
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(600)
		items = await inv_page.evaluate(
			r"""
			() => {
			  const out = [];
			  const findTitleFrom = (container) => {
				let node = container;
				while (node) {
				  let prev = node.previousElementSibling;
				  while (prev) {
					const titleEl = prev.querySelector('p.CoreText-sc-1txzju1-0, p');
					if (titleEl && titleEl.textContent && titleEl.textContent.trim()) {
					  return titleEl.textContent.trim();
					}
					prev = prev.previousElementSibling;
				  }
				  node = node.parentElement;
				}
				return null;
			  };
			  document.querySelectorAll('[role="progressbar"][aria-valuenow]').forEach(pb => {
				const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
				const container = pb.parentElement;
				const title = findTitleFrom(container);
				if (title) out.push({ title, percent });
			  });
			  return out;
			}
			"""
		)
		for it in items:
			title = it.get('title')
			if title:
				progress[title] = it.get('percent')
	except Exception as e:
		logging.warning(f"Progress map issue: {e}")
	return progress

async def get_incomplete_rust_rewards(inv_page):
	rewards = []
	try:
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(800)
		items = await inv_page.evaluate(
			r"""
			() => {
			  const out = [];
			  const findTitleFrom = (container) => {
				let node = container;
				while (node) {
				  let prev = node.previousElementSibling;
				  while (prev) {
					const p = prev.querySelector('p');
					if (p && p.textContent && p.textContent.trim()) {
					  return p.textContent.trim();
					}
					prev = prev.previousElementSibling;
				  }
				  node = node.parentElement;
				}
				return null;
			  };
			  document.querySelectorAll('[role="progressbar"][aria-valuenow]').forEach(pb => {
				const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
				const container = pb.parentElement; // wraps progressbar and text
				let title = findTitleFrom(container);
				let hours = null;
				const textEl = container ? container.querySelector('div p') : null;
				const text = textEl ? textEl.textContent : '';
				const m = text.match(/of\s+(\d+)\s+hours?/i);
				if (m) hours = parseInt(m[1], 10);
				if (title) out.push({ title, percent, hours });
			  });
			  return out;
			}
			"""
		)
		seen = set()
		for it in items:
			if not it or not it.get('title'):
				continue
			key = it['title']
			if key in seen:
				continue
			seen.add(key)
			if isinstance(it.get('percent'), int) and it['percent'] < 100:
				rewards.append({
					'title': it['title'],
					'percent': it['percent'],
					'hours': it.get('hours')
				})
	except Exception as e:
		logging.warning(f"Inventory scrape issue: {e}")
	rewards.sort(key=lambda r: (r["percent"] if r["percent"] is not None else -1))
	return rewards

async def get_claimed_days_for_streamer(inv_page, streamer_name: str) -> int | None:
	"""Return approximate days since this streamer's drop was claimed, or None if not found.
	We look for elements containing the streamer name, then within the same card search for a time label like
	'23 minutes ago', 'yesterday', '9 days ago', '2 months ago', 'last month'.
	"""
	target_lower = ((streamer_name or "").strip().lower())
	if not target_lower:
		return None
	try:
		# Ensure page is loaded
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(400)
		days = await inv_page.evaluate(
			r"""
			(args) => {
			  const targetLower = (args && args.targetLower) || '';
			  const isTimeText = (s) => {
				if (!s) return false;
				const t = s.trim().toLowerCase();
				return t.includes('yesterday') || t.includes('ago') || t.includes('last month') || t.includes('months ago') || t.includes('month ago');
			  };
			  const toDays = (s) => {
				if (!s) return null;
				const t = s.trim().toLowerCase();
				if (t.includes('yesterday')) return 1;
				let m;
				if ((m = t.match(/(\d+)\s*minutes?/))) return 0;
				if ((m = t.match(/(\d+)\s*hours?/))) return 0;
				if ((m = t.match(/(\d+)\s*days?/))) return parseInt(m[1], 10);
				if (t.includes('last month')) return 30;
				if ((m = t.match(/(\d+)\s*months?/))) return parseInt(m[1], 10) * 30;
				if ((m = t.match(/(\d+)\s*years?/))) return parseInt(m[1], 10) * 365;
				return null;
			  };
			  const all = Array.from(document.querySelectorAll('p, span, div, a'));
			  const nameEls = all.filter(el => {
				const txt = (el.textContent || '').toLowerCase();
				return txt && txt.includes(targetLower);
			  }).slice(0, 30);
			  const upDepth = 5;
			  for (const el of nameEls) {
				let node = el;
				for (let d = 0; d < upDepth && node; d++, node = node.parentElement) {
				  const timeEl = Array.from(node.querySelectorAll('p, span, div')).find(e => isTimeText(e.textContent || ''));
				  if (timeEl) {
					const days = toDays(timeEl.textContent || '');
					if (days !== null && days !== undefined) return days;
				  }
				}
			  }
			  return null;
			}
			""",
			{"targetLower": target_lower}
		)
		return days
	except Exception as e:
		logging.debug(f"Claimed days lookup failed for {streamer_name}: {e}")
		return None

async def pick_live_rust_stream_with_drops(context, preferred_streamers=None):
	preferred_streamers = [s.lower() for s in (preferred_streamers or [])]
	page = await context.new_page()
	try:
		await goto_with_exit(page, TWITCH_RUST_DIRECTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(page)
		await page.wait_for_timeout(1000)
		cards = await page.query_selector_all('article')
		preferred_candidate = None
		drops_candidates = []
		fallback_first = None
		for card in cards[:60]:
			try:
				link = await card.query_selector('a[data-a-target="preview-card-title-link"]')
				if not link:
					continue
				href = await link.get_attribute('href')
				if not href:
					continue
				url = 'https://www.twitch.tv' + href if href.startswith('/') else href
				if not fallback_first:
					fallback_first = url
				path = href.split('?')[0].strip('/') if href.startswith('/') else url.split('twitch.tv/')[-1]
				has_drops_tag = False
				tag_nodes = await card.query_selector_all('[data-a-target="tag"], a, span, div')
				for t in tag_nodes[:20]:
					txt = (await t.inner_text()).strip().lower()
					if 'drops enabled' in txt or txt == 'drops' or 'drops' in txt:
						has_drops_tag = True
						break
				if preferred_streamers and path.lower() in preferred_streamers:
					preferred_candidate = url
					break
				if has_drops_tag:
					drops_candidates.append(url)
			except Exception:
				continue
		if preferred_candidate:
			return preferred_candidate
		if drops_candidates:
			return drops_candidates[0]
		return fallback_first
	finally:
		await page.close()

async def ensure_stream_playing(stream_page):
	try:
		await stream_page.wait_for_selector('button[data-a-target="player-play-pause-button"]', timeout=30000)
		label = await stream_page.get_attribute('button[data-a-target="player-play-pause-button"]', 'aria-label')
		if label and 'Play' in label:
			await stream_page.click('button[data-a-target="player-play-pause-button"]')
			await asyncio.sleep(1)
	except Exception:
		pass
	try:
		# Ensure muted (only click if currently not muted)
		val = await stream_page.get_attribute('[data-a-target="player-volume-slider"]', 'aria-valuenow')
		if val is None or val != '0':
			await stream_page.click('button[data-a-target="player-mute-unmute-button"]')
			await asyncio.sleep(0.2)
		# Nudge the slider toward zero anyway
		try:
			await stream_page.hover('[data-a-target="player-volume-slider"]')
			for _ in range(3):
				await stream_page.keyboard.press('ArrowDown')
				await asyncio.sleep(0.05)
		except Exception:
			pass
	except Exception:
		pass

async def set_low_quality(stream_page):
	try:
		await stream_page.click('button[data-a-target="player-settings-button"]', timeout=15000)
		await asyncio.sleep(0.3)
		# Open Quality submenu using multiple selectors for robustness
		quality_opened = False
		for sel in [
			'div[role="menu"] [data-a-target="player-settings-menu-item-quality"]',
			'div[role="menu"] [data-a-target="player-settings-quality"]',
			'div[role="menu"] [role="menuitem"]:has-text("Quality")'
		]:
			try:
				btns = await stream_page.query_selector_all(sel)
				if btns:
					await btns[0].click()
					await asyncio.sleep(0.4)
					quality_opened = True
					break
			except Exception:
				continue
		if not quality_opened:
			# Close menu and exit
			try:
				await stream_page.click('button[data-a-target="player-settings-button"]')
			except Exception:
				pass
			return

		# Click the lowest available quality (Audio Only or lowest p)
		picked = await stream_page.evaluate(
			r"""
			() => {
			  const menu = document.querySelector('div[role="menu"]');
			  if (!menu) return false;
			  const candidates = Array.from(menu.querySelectorAll('[role="menuitemradio"], input[type="radio"], label'));
			  if (!candidates.length) return false;
			  const getQualityScore = (el) => {
				const t = (el.innerText || el.textContent || '').toLowerCase();
				if (t.includes('audio only')) return 0; // best for bandwidth
				const m = t.match(/(\d+)p/);
				if (m) return parseInt(m[1], 10);
				// Unknown labels get a high score so they won't be chosen over known low qualities
				return 9999;
			  };
			  let bestEl = null;
			  let best = 9999;
			  for (const el of candidates) {
				const score = getQualityScore(el);
				if (score < best) {
				  best = score;
				  bestEl = el;
				}
			  }
			  if (bestEl) {
				const clickable = bestEl.closest('[role="menuitemradio"]') || bestEl;
				clickable.click();
				return true;
			  }
			  return false;
			}
			"""
		)
		await asyncio.sleep(0.3)
		# Close settings menu
		try:
			await stream_page.click('button[data-a-target="player-settings-button"]')
		except Exception:
			pass
	except Exception:
		pass

async def claim_available_rewards(inv_page):
	try:
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(500)
		claim_buttons = await inv_page.query_selector_all('button:has-text("Claim")')
		for btn in claim_buttons:
			try:
				await btn.click()
				logging.info("Claimed a reward")
				await asyncio.sleep(0.5)
			except Exception:
				continue
	except Exception:
		pass

async def poll_until_reward_complete(context, inv_page, streamer_name: str):
	total_wait_seconds = MAX_WATCH_HOURS_PER_REWARD * 3600
	waited = 0
	last_percent = None
	target_lower = (streamer_name or "").strip().lower()
	logging.info(f"Tracking streamer '{streamer_name}' by inventory title match")
	while waited < total_wait_seconds:
		if EXIT_EVENT.is_set():
			logging.info("Exit requested; stopping progress polling.")
			return False
		# Check live status on Facepunch periodically (every ~2 minutes)
		try:
			if waited % max(1, (2 * 60)) == 0:
				status = await is_streamer_online_on_facepunch(context, streamer_name)
				if status is False:
					logging.info(f"Streamer '{streamer_name}' appears offline on Facepunch. Moving on.")
					return False
		except Exception:
			pass
		try:
			await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(inv_page)
			# ensure progressbars rendered
			# progressbars sometimes mount late; wait a bit and re-check
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
			await inv_page.wait_for_timeout(600)
			data = await inv_page.evaluate(
				r"""
								(args) => {
				  const targetLower = (args && args.targetLower) || '';
				  const results = [];
				  const findTitleFrom = (container) => {
					let node = container;
					while (node) {
					  let prev = node.previousElementSibling;
					  while (prev) {
						const p = prev.querySelector('p');
						if (p && p.textContent && p.textContent.trim()) {
						  return p.textContent.trim();
						}
						prev = prev.previousElementSibling;
					  }
					  node = node.parentElement;
					}
					return null;
				  };
				  document.querySelectorAll('[role="progressbar"][aria-valuenow]').forEach(pb => {
					const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
					const container = pb.parentElement;
					const title = findTitleFrom(container);
					const titleLower = (title || '').toLowerCase();
					if (title && titleLower.includes(targetLower)) {
					  results.push({ title, percent });
					}
				  });
				  return results;
				}
				""",
				{"targetLower": target_lower}
			)
			if data:
				best = data[0]
				percent = best.get('percent')
				title = best.get('title') or streamer_name
				logging.info(f"[{title}] Progress: {percent}%")
				last_percent = percent if isinstance(percent, int) else last_percent
				if isinstance(percent, int) and percent >= 100:
					await claim_available_rewards(inv_page)
					return True
			else:
				logging.info(f"No inventory entry found for streamer '{streamer_name}'.")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
		except Exception as e:
			logging.warning(f"Poll inventory issue: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
	logging.info("Max watch time reached without detecting completion.")
	return False

async def poll_until_title_complete(context, inv_page, target_title_substr: str) -> bool:
	"""Track progress for an inventory title substring until it reaches 100% or exit is requested."""
	total_wait_seconds = MAX_WATCH_HOURS_PER_REWARD * 3600
	waited = 0
	target_lower = (target_title_substr or '').strip().lower()
	while waited < total_wait_seconds:
		if EXIT_EVENT.is_set():
			return False
		try:
			await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(inv_page)
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
			await inv_page.wait_for_timeout(600)
			items = await inv_page.evaluate(
				r"""
				(titleNeedleLower) => {
				  const out = [];
				  const findTitleFrom = (container) => {
					let node = container;
					while (node) {
					  let prev = node.previousElementSibling;
					  while (prev) {
						const el = prev.querySelector('p, h1, h2, h3, h4, h5, h6, span');
						if (el && el.textContent && el.textContent.trim()) {
						  return el.textContent.trim();
						}
						prev = prev.previousElementSibling;
					  }
					  node = node.parentElement;
					}
					return null;
				  };
				  document.querySelectorAll('[role="progressbar"][aria-valuenow]').forEach(pb => {
					const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
					const container = pb.parentElement;
					let title = findTitleFrom(container) || '';
					if (!title) {
					  const card = pb.closest('[role="group"], [data-test-selector], .Layout-sc');
					  if (card) {
					    const t = card.querySelector('p, span, h3');
					    if (t && t.textContent) title = t.textContent.trim();
					  }
					}
					const tLower = (title || '').toLowerCase();
					out.push({ title, percent, match: titleNeedleLower && tLower.includes(titleNeedleLower) });
				  });
				  return out;
				}
				""",
				target_lower
			)
			match = None
			for it in items or []:
				if it.get('match'):
					match = it
					break
			if match and isinstance(match.get('percent'), int):
				p = match['percent']
				logging.info(f"[General] {match.get('title')} Progress: {p}%")
				if p >= 100:
					await claim_available_rewards(inv_page)
					return True
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
		except Exception as e:
			logging.warning(f"Poll general title issue: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
	return False


async def poll_general_until_complete_or_streamer_available(context, inv_page, target_title_substr: str, completed_streamers) -> tuple[bool, bool]:
	"""Poll the general drop progress by title substring. Return (completed, switch_to_streamer).

	Switch to streamer when any live streamer-specific drop is detected as present in inventory and < 100%.
	"""
	total_wait_seconds = MAX_WATCH_HOURS_PER_REWARD * 3600
	waited = 0
	target_lower = (target_title_substr or '').strip().lower()
	while waited < total_wait_seconds:
		if EXIT_EVENT.is_set():
			return (False, False)
		try:
			# Check general progress
			await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(inv_page)
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
			await inv_page.wait_for_timeout(600)
			items = await inv_page.evaluate(
				r"""
				(titleNeedleLower) => {
				  const out = [];
				  const findTitleFrom = (container) => {
					let node = container;
					while (node) {
					  let prev = node.previousElementSibling;
					  while (prev) {
						const el = prev.querySelector('p, h1, h2, h3, h4, h5, h6, span');
						if (el && el.textContent && el.textContent.trim()) {
						  return el.textContent.trim();
						}
						prev = prev.previousElementSibling;
					  }
					  node = node.parentElement;
					}
					return null;
				  };
				  document.querySelectorAll('[role=\"progressbar\"][aria-valuenow]').forEach(pb => {
					const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
					const container = pb.parentElement;
					let title = findTitleFrom(container) || '';
					if (!title) {
					  const card = pb.closest('[role=\"group\"], [data-test-selector], .Layout-sc');
					  if (card) {
					    const t = card.querySelector('p, span, h3');
					    if (t && t.textContent) title = t.textContent.trim();
					  }
					}
					const tLower = (title || '').toLowerCase();
					out.push({ title, percent, match: titleNeedleLower && tLower.includes(titleNeedleLower) });
				  });
				  return out;
				}
				""",
				target_lower
			)
			match = None
			for it in items or []:
				if it.get('match'):
					match = it
					break
			if match and isinstance(match.get('percent'), int):
				p = match['percent']
				logging.info(f"[General] {match.get('title')} Progress: {p}%")
				if p >= 100:
					await claim_available_rewards(inv_page)
					return (True, False)

			# After logging progress, also check if any streamer-specific items need progress
			fp = await fetch_facepunch_drops(context)
			streamer_targets = fp.get('streamer', []) if fp else []
			progress_map = await get_inventory_progress_map(inv_page)
			in_progress_titles = { (t or '').lower() for t in (progress_map or {}).keys() }
			inventory_entries = [((t or '').lower(), p) for t, p in (progress_map or {}).items()]
			for st in streamer_targets:
				name = (st.get('streamer') or '').strip()
				if not name or not bool(st.get('is_live')):
					continue
				name_lower = name.lower()
				if name_lower in completed_streamers:
					continue
				match_pct = None
				for t_lower, pct in inventory_entries:
					if name_lower in t_lower and isinstance(pct, int):
						match_pct = pct
						break
				if match_pct is None or match_pct >= 100:
					continue
				# Found a streamer-specific item that needs progress → switch
				logging.info(f"Streamer-specific drop available again: {name} ({match_pct}%). Switching from general mode.")
				return (False, True)

			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
		except Exception as e:
			logging.warning(f"Poll general/switch check issue: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
	return (False, False)

async def run_drops_workflow(context):
	inv_page = await context.new_page()
	completed_streamers = set()
	try:
		while True:
			if EXIT_EVENT.is_set():
				logging.info("Exit requested; stopping workflow loop.")
				return
			fp = await fetch_facepunch_drops(context)
			streamer_targets = fp.get('streamer', [])

			# Gather in-progress titles to prioritize watching those
			progress_map = await get_inventory_progress_map(inv_page)
			in_progress_titles = { (t or '').lower() for t in progress_map.keys() }

			# Defer general drop decision until after streamer candidates are considered
			longest_general = None

			# Build candidates only for streamer-specific items present in inventory (<100%)
			candidates = []
			live_any = []
			inventory_entries = [((t or '').lower(), p) for t, p in (progress_map or {}).items()]
			for st in streamer_targets:
				name = (st.get('streamer') or '').strip()
				if not name:
					continue
				if bool(st.get('is_live')):
					live_any.append(st)
				# Only consider live streamers for watching
				if not bool(st.get('is_live')):
					continue
				name_lower = name.lower()
				if name_lower in completed_streamers:
					continue
				# Find matching inventory entry and its percent
				match_pct = None
				for t_lower, pct in inventory_entries:
					if name_lower in t_lower and isinstance(pct, int):
						match_pct = pct
						break
				# Skip if no streamer-specific entry exists in inventory or it's already 100%
				if match_pct is None or match_pct >= 100:
					continue
				# Prioritize those already in progress (name present in any title)
				priority = 0 if any(name_lower in t for t in in_progress_titles) else 1
				if priority == 1:
					# Not in progress; check recent claimed age to avoid very recent repeats
					days = await get_claimed_days_for_streamer(inv_page, name)
					if days is not None and days < 20:
						continue
				candidates.append({"streamer": name, "url": st.get('url'), "is_live": st.get('is_live'), "priority": priority, "pct": match_pct})

			# Do not enter general mode here; prefer streamer-specific workflow below

			# Prefer live streamer-specific drops first (only when we actually have streamer-specific items to progress)
			if candidates:
				# Prefer in-progress, then live (all are live already)
				candidates.sort(key=lambda s: (s.get('priority', 1), s.get('pct', 101)))
				target_st = candidates[0]
				target_name = target_st.get('streamer')
				target_url = target_st.get('url')
				logging.info(f"Chosen streamer: {target_name} (live={bool(target_st.get('is_live'))}, priority={target_st.get('priority')})")
				# Open streamer and run per-streamer completion tracking
				fp_page = await context.new_page()
				stream_page = None
				try:
					try:
						await goto_with_exit(fp_page, FACEPUNCH_DROPS_URL, timeout=120000, wait_until="domcontentloaded")
						await asyncio.sleep(0.5)
						box = await fp_page.query_selector(f'.streamer-drops .drop-box:has(.streamer-name:has-text("{target_name}"))')
						if box:
							try:
								async with context.expect_page() as p_info:
									btn = await box.query_selector('a.drop-box-body, .drop-box-body')
									if btn:
										await btn.click()
									else:
										header_link = await box.query_selector('.drop-box-header a.streamer-info')
										if header_link:
											await header_link.click()
								stream_page = await p_info.value
							except Exception:
								stream_page = None
					except Exception:
						stream_page = None
					if not stream_page and target_url:
						stream_page = await context.new_page()
						await goto_with_exit(stream_page, target_url, timeout=120000, wait_until="domcontentloaded")
				finally:
					try:
						await fp_page.close()
					except Exception:
						pass
				if not stream_page:
					logging.error("Could not open stream for chosen target. Will refresh and pick again.")
					await asyncio.sleep(2)
					continue
				await maybe_accept_cookies(stream_page)
				send_notification("Twitch Drops", f"Now watching {target_name}")
				await ensure_stream_playing(stream_page)
				await set_low_quality(stream_page)
				completed = await poll_until_reward_complete(context, inv_page, streamer_name=target_name)
				try:
					await stream_page.close()
				except Exception:
					pass
				if completed:
					logging.info("Streamer reward completed or claimable. Attempting to claim any available rewards and moving to next.")
					await claim_available_rewards(inv_page)
					completed_streamers.add((target_name or "").lower())
				else:
					logging.info("Moving to next candidate.")
				# Refresh and next loop
				await asyncio.sleep(1)
				continue

			# If no streamer-specific items need progress, evaluate general drops
			if await are_all_general_drops_complete(inv_page, fp.get('general') if fp else None):
				logging.info("All general drops are complete. Exiting program.")
				EXIT_EVENT.set()
				return

			# If general drops remain and any live streamer exists, watch any live streamer while tracking the longest general drop
			longest_general = None
			try:
				if fp and fp.get('general'):
					vals = [g for g in fp['general'] if isinstance(g.get('hours'), int)]
					if vals:
						longest_general = max(vals, key=lambda g: g.get('hours') or 0)
			except Exception:
				longest_general = None
			if longest_general:
				# Use any live streamer while tracking the general item
				if not live_any:
					logging.info("No live streamers available to track general drops right now. Retrying later.")
					await asyncio.sleep(10)
					continue
				live_any.sort(key=lambda s: 0 if (s.get('streamer') or '').strip().lower() in in_progress_titles else 1)
				target_name = (live_any[0].get('streamer') or '').strip()
				target_url = live_any[0].get('url')
				logging.info(f"General drop mode: tracking '{longest_general.get('item')}' (hours={longest_general.get('hours')}) while watching {target_name}")
				fp_page = await context.new_page()
				stream_page = None
				try:
					try:
						await goto_with_exit(fp_page, FACEPUNCH_DROPS_URL, timeout=120000, wait_until="domcontentloaded")
						await asyncio.sleep(0.5)
						box = await fp_page.query_selector(f'.streamer-drops .drop-box:has(.streamer-name:has-text("{target_name}"))')
						if box:
							try:
								async with context.expect_page() as p_info:
									btn = await box.query_selector('a.drop-box-body, .drop-box-body')
									if btn:
										await btn.click()
									else:
										header_link = await box.query_selector('.drop-box-header a.streamer-info')
										if header_link:
											await header_link.click()
								stream_page = await p_info.value
							except Exception:
								stream_page = None
					except Exception:
						stream_page = None
					if not stream_page and target_url:
						stream_page = await context.new_page()
						await goto_with_exit(stream_page, target_url, timeout=120000, wait_until="domcontentloaded")
				finally:
					try:
						await fp_page.close()
					except Exception:
						pass
				if not stream_page:
					logging.error("Could not open stream for chosen target in general mode. Will refresh and pick again.")
					await asyncio.sleep(2)
					continue
				await maybe_accept_cookies(stream_page)
				alias_txt = (longest_general.get('alias') or '').strip()
				item_txt = (longest_general.get('item') or '').strip()
				desc = item_txt if item_txt else alias_txt
				if item_txt and alias_txt:
					desc = f"{item_txt} ({alias_txt})"
				send_notification("Twitch Drops", f"Watching {target_name} for general drop '{desc}'")
				await ensure_stream_playing(stream_page)
				await set_low_quality(stream_page)
				completed, switch = await poll_general_until_complete_or_streamer_available(
					context,
					inv_page,
					target_title_substr=((longest_general.get('alias') or longest_general.get('item') or '')),
					completed_streamers=completed_streamers
				)
				try:
					await stream_page.close()
				except Exception:
					pass
				if completed:
					logging.info("General drop completed or claimable.")
					await claim_available_rewards(inv_page)
				elif switch:
					logging.info("Switching to streamer-specific mode; a needed streamer drop is available again.")
					await asyncio.sleep(1)
					continue
				# Loop continues; streamer-specific drops have priority when available
				await asyncio.sleep(1)
				continue

			# No streamers and no general strategy applicable; wait briefly
			logging.info("No live streamer drops and no general progress to track. Retrying shortly.")
			await asyncio.sleep(10)
			continue

	finally:
		try:
			await inv_page.close()
		except Exception:
			pass

async def main(start_tray: bool = True):
	logging.info("--- Starting Twitch Drop Automator ---")
	# Start tray icon for quick toggles (optionally skipped on macOS; see __main__)
	try:
		global TRAY_ICON, ICON_PATH
		if start_tray:
			TRAY_ICON = start_system_tray(block=False)
		# Prepare .ico for notifications regardless of tray availability
		ICON_PATH = ensure_icon_file(_generate_tray_icon_image())
		logging.info(f"Notification icon: {ICON_PATH}")
		# One-time startup notification to verify toasts
		send_notification("Twitch Drops", "Automator started")
	except Exception as e:
		logging.debug(f"Tray start failed: {e}")
	_install_signal_handlers()
	async with async_playwright() as p:
		try:
			context = await run_flow(p)
		except asyncio.CancelledError:
			logging.info("Startup cancelled.")
			return
		try:
			await run_drops_workflow(context)
		finally:
			logging.info("Closing browser.")
			await context.close()
	logging.info("--- Automator finished ---")

async def is_streamer_online_on_facepunch(context, streamer_name: str) -> bool | None:
	"""Return True if Facepunch page shows the given streamer online, False if found and offline,
	None if not found on page (unknown)."""
	if not streamer_name:
		return None
	page = await context.new_page()
	try:
		await goto_with_exit(page, FACEPUNCH_DROPS_URL, timeout=120000, wait_until="domcontentloaded")
		await asyncio.sleep(0.5)
		box = await page.query_selector(f'.streamer-drops .drop-box:has(.streamer-name:has-text("{streamer_name}"))')
		if not box:
			return None
		online_node = await box.query_selector('.online-status, div.online-status')
		return True if online_node else False
	except Exception:
		return None
	finally:
		try:
			await page.close()
		except Exception:
			pass

async def are_all_general_drops_complete(inv_page, general_list) -> bool:
	try:
		if not general_list:
			return True
		progress_map = await get_inventory_progress_map(inv_page)
		if not progress_map:
			return False
		def find_percent_for(item_name: str) -> int | None:
			needle = (item_name or '').strip().lower()
			for title, pct in progress_map.items():
				t = (title or '').lower()
				if needle and needle in t:
					return pct if isinstance(pct, int) else None
			return None
		for g in general_list:
			name = g.get('item') if isinstance(g, dict) else None
			if not name:
				continue
			pct = find_percent_for(name)
			if pct is None or pct < 100:
				return False
		return True
	except Exception:
		return False


def _get_preferred_interpreter_for_visibility() -> tuple[str, int]:
	try:
		if IS_WINDOWS:
			with CONFIG_LOCK:
				hide = bool(PREFERENCES.get("hide_console", True))
			venv_py = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')
			venv_pyw = os.path.join(BASE_DIR, 'venv', 'Scripts', 'pythonw.exe')
			if hide and os.path.exists(venv_pyw):
				return (venv_pyw, 0)
			if (not hide) and os.path.exists(venv_py):
				return (venv_py, 0)
			# Fallback to current interpreter
			interp = sys.executable
			flags = 0
			if hide and interp.lower().endswith('python.exe'):
				flags = 0x08000000  # CREATE_NO_WINDOW
			return (interp, flags)
		# Non-Windows: do not attempt to toggle visibility; just use current interpreter
		return (sys.executable, 0)
	except Exception:
		return (sys.executable, 0)


def ensure_process_visibility_matches_preference():
	try:
		preferred, flags = _get_preferred_interpreter_for_visibility()
		current = (sys.executable or '').lower()
		want_hidden = preferred.lower().endswith('pythonw.exe') or (flags & 0x08000000) != 0
		is_hidden_now = current.endswith('pythonw.exe')
		if want_hidden != is_hidden_now or (os.path.abspath(preferred) != os.path.abspath(sys.executable)):
			script_path = os.path.join(BASE_DIR, os.path.basename(__file__) if '__file__' in globals() else 'twitch_drop_automator.py')
			try:
				logging.info("Restarting to match console visibility preference…")
			except Exception:
				pass
			subprocess.Popen([preferred, script_path], cwd=BASE_DIR, creationflags=flags)
			os._exit(0)
	except Exception:
		pass


if __name__ == "__main__":
	# Ensure process matches the user's console visibility preference before running
	try:
		ensure_process_visibility_matches_preference()
	except Exception:
		pass
	# On macOS, run tray in the main thread and the async app in a background thread
	if IS_MAC and pystray is not None:
		def _run_async_app():
			try:
				asyncio.run(main(start_tray=False))
			except KeyboardInterrupt:
				pass
			except Exception:
				# Ensure any exception doesn't kill the process silently
				logging.exception("Async app crashed")
		thread = threading.Thread(target=_run_async_app, daemon=True)
		thread.start()
		try:
			# Block here to keep Cocoa on main thread
			start_system_tray(block=True)
		except Exception:
			logging.debug("Tray main loop exited")
		# Request shutdown and wait briefly
		EXIT_EVENT.set()
		try:
			thread.join(timeout=5.0)
		except Exception:
			pass
	else:
		try:
			asyncio.run(main(start_tray=True))
		except KeyboardInterrupt:
			pass
