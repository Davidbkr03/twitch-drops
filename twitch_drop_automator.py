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
from datetime import datetime, timezone, timedelta
import base64
import io
import argparse
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

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

# Web interface configuration
WEB_PORT = 5000
WEB_HOST = '127.0.0.1'
SCREENSHOT_INTERVAL = 2  # seconds between screenshots

# Testing configuration
TEST_MODE = False  # Set to True to keep browser open for testing

# Persisted preferences/config
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
CONFIG_LOCK = threading.RLock()
EXIT_EVENT = threading.Event()

PREFERENCES = None
TRAY_ICON = None
ICON_PATH = None
NOTIFICATIONS_ENABLED = True
TRAY_IMAGE = None

# Web server globals
app = None
socketio = None
current_browser_context = None
current_working_page = None  # Track the page currently being worked on
screenshot_thread = None
web_server_thread = None

# Drops data cache for web interface
cached_drops_data = {
	"in_progress": [],
	"not_started": [],
	"completed": [],
	"last_updated": None
}
drops_data_lock = threading.RLock()

# Track current working item
current_working_item = None
current_working_lock = threading.RLock()


def get_current_version():
	"""Get the current version of the application."""
	try:
		# Try to read version from a version file first
		version_file = os.path.join(BASE_DIR, 'version.txt')
		if os.path.exists(version_file):
			with open(version_file, 'r') as f:
				return f.read().strip()
		
		# Fallback: try to extract from git or use a default
		try:
			import subprocess
			result = subprocess.run(['git', 'describe', '--tags', '--always'], 
								  capture_output=True, text=True, cwd=BASE_DIR, timeout=5)
			if result.returncode == 0:
				return result.stdout.strip().lstrip('v')
		except:
			pass
		
		# Default version if nothing else works
		return "1.0.0"
	except:
		return "1.0.0"

def get_current_commit_hash():
	"""Get the current git commit hash."""
	try:
		import subprocess
		result = subprocess.run(['git', 'rev-parse', 'HEAD'], 
							  capture_output=True, text=True, cwd=BASE_DIR, timeout=5)
		if result.returncode == 0:
			return result.stdout.strip()[:8]  # Return short hash
	except:
		pass
	
	# Fallback: try to get from git log
	try:
		import subprocess
		result = subprocess.run(['git', 'log', '-1', '--format=%H'], 
							  capture_output=True, text=True, cwd=BASE_DIR, timeout=5)
		if result.returncode == 0:
			return result.stdout.strip()[:8]
	except:
		pass
	
	# If no git info available, return a placeholder
	return "unknown"

def compare_versions(version1, version2):
	"""Compare two version strings. Returns 1 if version1 > version2, -1 if version1 < version2, 0 if equal."""
	try:
		from packaging import version
		v1 = version.parse(version1)
		v2 = version.parse(version2)
		if v1 > v2:
			return 1
		elif v1 < v2:
			return -1
		else:
			return 0
	except:
		# Fallback simple comparison
		try:
			v1_parts = [int(x) for x in version1.split('.')]
			v2_parts = [int(x) for x in version2.split('.')]
			
			# Pad with zeros to make same length
			max_len = max(len(v1_parts), len(v2_parts))
			v1_parts.extend([0] * (max_len - len(v1_parts)))
			v2_parts.extend([0] * (max_len - len(v2_parts)))
			
			for i in range(max_len):
				if v1_parts[i] > v2_parts[i]:
					return 1
				elif v1_parts[i] < v2_parts[i]:
					return -1
			return 0
		except:
			return 0

def update_current_working_item(item_info):
	"""Update the current working item for the web interface."""
	global current_working_item
	
	with current_working_lock:
		current_working_item = item_info
		logging.debug(f"Updated current working item: {item_info}")


def intelligent_item_matching(item_name, inventory_progress):
	"""
	Intelligent matching for items where facepunch names differ from inventory names.
	Uses keyword matching to find similar items.
	"""
	# Define keyword mappings for common item types
	keyword_mappings = {
		'chestplate': ['chestplate', 'chest'],
		'facemask': ['facemask', 'mask'],
		'kilt': ['kilt'],
		'fridge': ['fridge'],
		'locker': ['locker'],
		'bag': ['bag'],
		'helmet': ['helmet'],
		'pants': ['pants'],
		'shirt': ['shirt'],
		'jacket': ['jacket'],
		'gloves': ['gloves'],
		'boots': ['boots'],
		'shoes': ['shoes']
	}
	
	item_lower = item_name.lower()
	
	# Find matching keywords
	matching_keywords = []
	for keyword, variations in keyword_mappings.items():
		for variation in variations:
			if variation in item_lower:
				matching_keywords.extend(variations)
				break
	
	# If we found keywords, try to match against inventory titles
	if matching_keywords:
		for title, percent in inventory_progress.items():
			title_lower = title.lower()
			for keyword in matching_keywords:
				if keyword in title_lower:
					logging.info(f"Intelligent match: '{item_name}' -> '{title}' (keyword: '{keyword}')")
					return percent, title
	
	return None, None

def update_cached_drops_data(facepunch_data, inventory_progress, recently_claimed_streamers=None):
	"""Update the cached drops data for the web interface."""
	global cached_drops_data
	
	with drops_data_lock:
		# If this is a partial update (only progress data), merge with existing cache
		if facepunch_data is None and inventory_progress:
			# Update existing cache with new progress data
			if cached_drops_data and cached_drops_data.get("in_progress"):
				for drop in cached_drops_data["in_progress"]:
					# Update progress for matching items
					for title, percent in inventory_progress.items():
						# For streamer drops, match by streamer name in title
						if drop.get("type") == "streamer" and drop.get("streamer") and drop["streamer"].lower() in title.lower():
							drop["progress"] = percent
							drop["progress_title"] = title
							break
						# For general drops, match by exact item name (not partial match)
						elif drop.get("type") == "general" and drop.get("item"):
							# Check if the title contains the exact item name as a standalone word
							item_lower = drop["item"].lower()
							title_lower = title.lower()
							# Use word boundaries to avoid partial matches
							import re
							if re.search(r'\b' + re.escape(item_lower) + r'\b', title_lower):
								drop["progress"] = percent
								drop["progress_title"] = title
								break
			
			cached_drops_data["last_updated"] = datetime.now().isoformat()
			
			# Add current working item to the data
			with current_working_lock:
				cached_drops_data["current_working"] = current_working_item
			
			# Emit update via WebSocket if available
			if socketio:
				try:
					socketio.emit('drops_update', cached_drops_data)
					logging.debug("Emitted partial drops update via WebSocket")
				except Exception as e:
					logging.debug(f"Failed to emit partial drops update via WebSocket: {e}")
			return
		
		# Full update with Facepunch data
		drops_data = {
			"in_progress": [],
			"not_started": [],
			"completed": [],
			"last_updated": datetime.now().isoformat()
		}
		
		# Include recently-claimed (<=21 days) list for the web UI to mark as complete
		drops_data["recently_claimed_streamers"] = recently_claimed_streamers or []

		# Process streamer-specific drops
		streamer_drops = facepunch_data.get('streamer', []) if facepunch_data else []
		for drop in streamer_drops:
			streamer_name = drop.get('streamer', '')
			item_name = drop.get('item', '')
			hours = drop.get('hours', 0)
			is_live = drop.get('is_live', False)
			url = drop.get('url', '')
			
			# Find matching inventory progress
			progress = None
			progress_title = None
			for title, percent in inventory_progress.items():
				if streamer_name.lower() in title.lower():
					progress = percent
					progress_title = title
					break
			
			drop_info = {
				"type": "streamer",
				"streamer": streamer_name,
				"item": item_name,
				"hours": hours,
				"is_live": is_live,
				"url": url,
				"progress": progress,
				"progress_title": progress_title,  # The actual title from Twitch inventory
				"video": drop.get('video'),
				"streamer_avatar": drop.get('streamer_avatar')
			}
			
			if progress is None:
				drops_data["not_started"].append(drop_info)
			elif progress >= 100:
				drops_data["completed"].append(drop_info)
			else:
				drops_data["in_progress"].append(drop_info)
		
		# Process general drops
		general_drops = facepunch_data.get('general', []) if facepunch_data else []
		for drop in general_drops:
			item_name = drop.get('item', '')
			hours = drop.get('hours', 0)
			alias = drop.get('alias', '')
			
			# Find matching inventory progress
			progress = None
			progress_title = None
			search_terms = [item_name]
			if alias:
				search_terms.append(alias)
			
			for title, percent in inventory_progress.items():
				for term in search_terms:
					# Use word boundaries to avoid partial matches (e.g., "fridge" shouldn't match "Abe Fridge")
					import re
					if re.search(r'\b' + re.escape(term.lower()) + r'\b', title.lower()):
						progress = percent
						progress_title = title
						break
				if progress is not None:
					break
			
			# If no exact match found, try a more flexible search
			if progress is None:
				for title, percent in inventory_progress.items():
					for term in search_terms:
						# Try partial matching for common words like "Chestplate", "Kilt", etc.
						if term.lower() in title.lower() and len(term) > 3:
							progress = percent
							progress_title = title
							break
					if progress is not None:
						break
			
			# If still no match, try intelligent keyword matching
			if progress is None:
				progress, progress_title = intelligent_item_matching(item_name, inventory_progress)
				if progress is not None:
					logging.info(f"Successfully matched '{item_name}' to '{progress_title}' with {progress}% progress")
			
			# Debug logging for unmatched items
			if progress is None and item_name:
				logging.info(f"Could not find progress for general drop: '{item_name}' (alias: '{alias}')")
				logging.info(f"Available inventory titles: {list(inventory_progress.keys())}")
				logging.info(f"Searched terms: {search_terms}")
			
			drop_info = {
				"type": "general",
				"item": item_name,
				"hours": hours,
				"alias": alias,
				"progress": progress,
				"progress_title": progress_title,  # The actual title from Twitch inventory
				"video": drop.get('video'),
				"streamer_avatar": None  # General drops don't have streamer avatars
			}
			
			if progress is None:
				# For general drops, no progress bar means completed (not not_started)
				# General drops should always show a progress bar when active
				drops_data["completed"].append(drop_info)
			elif progress >= 100:
				drops_data["completed"].append(drop_info)
			else:
				drops_data["in_progress"].append(drop_info)
		
		cached_drops_data = drops_data
		
		# Add current working item to the data
		with current_working_lock:
			drops_data["current_working"] = current_working_item
		
		# Log the update for debugging
		logging.info(f"Updated drops cache: {len(drops_data['in_progress'])} in progress, {len(drops_data['not_started'])} not started, {len(drops_data['completed'])} completed")
		
		# Emit update via WebSocket if available
		if socketio:
			try:
				socketio.emit('drops_update', drops_data)
				logging.debug("Emitted drops update via WebSocket")
			except Exception as e:
				logging.debug(f"Failed to emit drops update via WebSocket: {e}")


def load_preferences():
	default_prefs = {
		"headless": DEFAULT_HEADLESS, 
		"hide_console": True,
		"test_mode": False,
		"debug_mode": False,
		"enable_web_interface": True
	}
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
# --- Time helpers ---

def _format_start_time_uk(epoch_ms: int) -> tuple[str, int, int]:
    """Return (formatted_time_str, days_until, hours_until) in UK time.
    - Formats using Europe/London; if unavailable, computes GMT/BST manually.
    - hours_until is ceil of remaining hours.
    """
    try:
        dt_utc = datetime.fromtimestamp(max(0, (epoch_ms or 0)) / 1000.0, tz=timezone.utc)

        def _uk_is_bst(dt_utc_in: datetime) -> bool:
            y = dt_utc_in.year
            # Last Sunday of March
            march_last = datetime(y, 3, 31, 1, 0, tzinfo=timezone.utc)
            while march_last.weekday() != 6:
                march_last -= timedelta(days=1)
            # Last Sunday of October
            oct_last = datetime(y, 10, 31, 1, 0, tzinfo=timezone.utc)
            while oct_last.weekday() != 6:
                oct_last -= timedelta(days=1)
            return march_last <= dt_utc_in < oct_last

        label = "unknown"
        try:
            from zoneinfo import ZoneInfo  # type: ignore
            uk_tz = ZoneInfo("Europe/London")
            dt_local = dt_utc.astimezone(uk_tz)
            label = dt_local.strftime("%d %B %Y at %H:%M %Z")
        except Exception:
            is_bst = _uk_is_bst(dt_utc)
            dt_local = dt_utc + (timedelta(hours=1) if is_bst else timedelta(0))
            tz_abbr = "BST" if is_bst else "GMT"
            label = dt_local.strftime(f"%d %B %Y at %H:%M {tz_abbr}")

        now_utc = datetime.now(timezone.utc)
        delta = dt_utc - now_utc
        total_seconds = max(0, int(delta.total_seconds()))
        hours_until = (total_seconds + 3599) // 3600
        days_until = total_seconds // 86400
        return (label, days_until, hours_until)
    except Exception:
        return ("unknown", 0, 0)



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
			except Exception as menu_err:
				logging.debug(f"Menu update failed: {menu_err}")
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
		pystray.MenuItem("Open Web Interface", lambda icon, item: open_web_interface()),
		pystray.MenuItem("Quit", on_quit)
	)
	icon = pystray.Icon("TwitchDropAutomator", image, "Twitch Drop Automator", menu=menu)
	global TRAY_IMAGE
	TRAY_IMAGE = image
	
	try:
		if block:
			# On macOS, Cocoa requires the app loop on the main thread
			atexit.register(lambda: safe_icon_stop(icon))
			icon.run()
			return icon
		else:
			icon.run_detached()
			atexit.register(lambda: safe_icon_stop(icon))
			return icon
	except Exception as e:
		logging.error(f"Failed to start system tray: {e}")
		return None

def safe_icon_stop(icon):
	"""Safely stop the tray icon with error handling."""
	try:
		if icon:
			icon.stop()
	except Exception as e:
		logging.debug(f"Error stopping tray icon: {e}")

# --- Web Server Functions ---
def create_web_app():
	"""Create and configure the Flask web application"""
	global app, socketio
	
	app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))
	app.config['SECRET_KEY'] = 'twitch_drops_secret_key'
	socketio = SocketIO(app, cors_allowed_origins="*")
	
	@app.route('/')
	def index():
		return render_template('index.html')
	
	@app.route('/api/status')
	def api_status():
		"""API endpoint to get current status"""
		return jsonify({
			'status': 'running',
			'headless': PREFERENCES.get('headless', DEFAULT_HEADLESS) if PREFERENCES else DEFAULT_HEADLESS,
			'test_mode': PREFERENCES.get('test_mode', False) if PREFERENCES else False,
			'debug_mode': PREFERENCES.get('debug_mode', False) if PREFERENCES else False,
			'enable_web_interface': PREFERENCES.get('enable_web_interface', True) if PREFERENCES else True,
			'version': get_current_version(),
			'timestamp': datetime.now().isoformat()
		})
	
	@app.route('/api/settings', methods=['GET', 'POST'])
	def api_settings():
		"""API endpoint to get and update settings"""
		if request.method == 'GET':
			return jsonify({
				'headless': PREFERENCES.get('headless', DEFAULT_HEADLESS) if PREFERENCES else DEFAULT_HEADLESS,
				'hide_console': PREFERENCES.get('hide_console', True) if PREFERENCES else True,
				'test_mode': PREFERENCES.get('test_mode', False) if PREFERENCES else False,
				'debug_mode': PREFERENCES.get('debug_mode', False) if PREFERENCES else False,
				'enable_web_interface': PREFERENCES.get('enable_web_interface', True) if PREFERENCES else True,
			})
		elif request.method == 'POST':
			try:
				data = request.get_json()
				with CONFIG_LOCK:
					if 'headless' in data:
						PREFERENCES['headless'] = bool(data['headless'])
					if 'hide_console' in data:
						PREFERENCES['hide_console'] = bool(data['hide_console'])
					if 'test_mode' in data:
						PREFERENCES['test_mode'] = bool(data['test_mode'])
					if 'debug_mode' in data:
						PREFERENCES['debug_mode'] = bool(data['debug_mode'])
					if 'enable_web_interface' in data:
						PREFERENCES['enable_web_interface'] = bool(data['enable_web_interface'])
					
					# Save preferences
					save_preferences(PREFERENCES)
					
				return jsonify({'success': True, 'message': 'Settings updated successfully'})
			except Exception as e:
				return jsonify({'success': False, 'message': f'Error updating settings: {str(e)}'}), 500
	
	@app.route('/api/drops', methods=['GET'])
	def api_drops():
		"""Get all drops data from cache."""
		try:
			with drops_data_lock:
				logging.info(f"API request for drops data: {len(cached_drops_data.get('in_progress', []))} in progress, {len(cached_drops_data.get('not_started', []))} not started, {len(cached_drops_data.get('completed', []))} completed")
				return jsonify(cached_drops_data)
		except Exception as e:
			logging.error(f"Error fetching drops data: {e}")
			return jsonify({'error': str(e)}), 500

	@app.route('/api/restart', methods=['POST'])
	def api_restart():
		"""API endpoint to restart the application"""
		try:
			import subprocess
			import sys
			import os
			
			# Get the current script path
			script_path = os.path.abspath(__file__)
			
			# Get current arguments (preserve test mode, etc.)
			args = [sys.executable, script_path]
			if PREFERENCES.get('test_mode', False):
				args.append('--test')
			if not PREFERENCES.get('enable_web_interface', True):
				args.append('--no-web')
			if not PREFERENCES.get('show_tray', True):
				args.append('--no-tray')
			
			# Start the new process
			subprocess.Popen(args, cwd=os.path.dirname(script_path))
			
			# Give it a moment to start
			import time
			time.sleep(1)
			
			# Exit current process
			os._exit(0)
			
		except Exception as e:
			return jsonify({'success': False, 'message': f'Error initiating restart: {str(e)}'}), 500

	@app.route('/api/check-updates', methods=['POST'])
	def api_check_updates():
		"""API endpoint to check for updates"""
		try:
			import requests
			import re
			import hashlib
			
			# Get current version from the script
			current_version = get_current_version()
			logging.info(f"Checking for updates. Current version: {current_version}")
			
			# Get current commit hash from git if available
			current_commit = get_current_commit_hash()
			logging.info(f"Current commit hash: {current_commit}")
			
			# Get latest commit info from GitHub API
			repo_url = "https://api.github.com/repos/Davidbkr03/twitch-drops/commits/main"
			logging.info(f"Fetching commit info from: {repo_url}")
			
			response = requests.get(repo_url, timeout=10)
			logging.info(f"GitHub API response status: {response.status_code}")
			
			if response.status_code == 200:
				commit_data = response.json()
				latest_commit = commit_data.get('sha', '')[:8]  # Short hash
				latest_date = commit_data.get('commit', {}).get('author', {}).get('date', '')
				commit_message = commit_data.get('commit', {}).get('message', '')
				
				logging.info(f"Latest commit from GitHub: {latest_commit}")
				logging.info(f"Latest commit date: {latest_date}")
				
				# Compare commits
				update_available = current_commit != latest_commit
				logging.info(f"Update available: {update_available}")
				
				return jsonify({
					'success': True,
					'update_available': update_available,
					'current_version': current_version,
					'current_commit': current_commit,
					'latest_version': latest_commit,
					'latest_commit': latest_commit,
					'latest_date': latest_date,
					'release_notes': commit_message[:200] + '...' if len(commit_message) > 200 else commit_message
				})
			else:
				error_msg = f'Failed to fetch commit information. Status: {response.status_code}'
				logging.error(error_msg)
				return jsonify({'success': False, 'message': error_msg}), 500
				
		except requests.exceptions.RequestException as e:
			error_msg = f'Network error checking for updates: {str(e)}'
			logging.error(error_msg)
			return jsonify({'success': False, 'message': error_msg}), 500
		except Exception as e:
			error_msg = f'Error checking for updates: {str(e)}'
			logging.error(error_msg)
			return jsonify({'success': False, 'message': error_msg}), 500

	@app.route('/api/update', methods=['POST'])
	def api_update():
		"""API endpoint to perform the update"""
		try:
			import requests
			import zipfile
			import shutil
			import tempfile
			import threading
			import time
			
			# Use the same approach as the installer - download from main branch archive
			repo = "Davidbkr03/twitch-drops"
			branch = "main"
			download_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
			
			logging.info(f"Downloading update from {download_url}")
			download_response = requests.get(download_url, timeout=60)
			
			if download_response.status_code != 200:
				return jsonify({'success': False, 'message': f'Failed to download update. Status: {download_response.status_code}'}), 500
			
			# Create temporary directory for extraction
			temp_dir = tempfile.mkdtemp()
			try:
				zip_path = os.path.join(temp_dir, 'update.zip')
				
				# Save zip file
				with open(zip_path, 'wb') as f:
					f.write(download_response.content)
				
				# Extract zip file
				extract_dir = os.path.join(temp_dir, 'extracted')
				with zipfile.ZipFile(zip_path, 'r') as zip_ref:
					zip_ref.extractall(extract_dir)
				
				# Find the extracted folder (usually has the repo name with branch suffix)
				extracted_folders = [f for f in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, f))]
				if not extracted_folders:
					return jsonify({'success': False, 'message': 'Invalid zip file structure'}), 500
				
				source_dir = os.path.join(extract_dir, extracted_folders[0])
				logging.info(f"Extracted to: {source_dir}")
				
				# Backup current files (except user data and config)
				backup_dir = os.path.join(BASE_DIR, 'backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
				os.makedirs(backup_dir, exist_ok=True)
				logging.info(f"Creating backup at: {backup_dir}")
				
				# Files to backup
				files_to_backup = [
					'twitch_drop_automator.py',
					'templates',
					'static',
					'requirements.txt',
					'README.md',
					'version.txt'
				]
				
				for item in files_to_backup:
					src = os.path.join(BASE_DIR, item)
					if os.path.exists(src):
						dst = os.path.join(backup_dir, item)
						if os.path.isdir(src):
							shutil.copytree(src, dst)
						else:
							shutil.copy2(src, dst)
						logging.info(f"Backed up: {item}")
				
				# Copy new files
				for item in os.listdir(source_dir):
					src = os.path.join(source_dir, item)
					dst = os.path.join(BASE_DIR, item)
					
					# Skip user data and config files
					if item in ['user_data_stealth', 'config.json', 'drops_log.txt', 'venv', '__pycache__', '.git']:
						logging.info(f"Skipping: {item}")
						continue
					
					if os.path.isdir(src):
						if os.path.exists(dst):
							shutil.rmtree(dst)
						shutil.copytree(src, dst)
					else:
						shutil.copy2(src, dst)
					
					logging.info(f"Updated: {item}")
				
				# Install/update dependencies if requirements.txt was updated
				requirements_path = os.path.join(BASE_DIR, 'requirements.txt')
				if os.path.exists(requirements_path):
					logging.info("Installing/updating dependencies...")
					try:
						import subprocess
						venv_python = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')
						venv_pip = os.path.join(BASE_DIR, 'venv', 'Scripts', 'pip.exe')
						
						# Check if virtual environment exists
						if os.path.exists(venv_python) and os.path.exists(venv_pip):
							# Upgrade pip first
							logging.info("Upgrading pip...")
							subprocess.run([venv_python, '-m', 'pip', 'install', '--upgrade', 'pip'], 
										  cwd=BASE_DIR, timeout=60, check=True)
							
							# Install/update requirements
							logging.info("Installing requirements...")
							subprocess.run([venv_pip, 'install', '-r', requirements_path], 
										  cwd=BASE_DIR, timeout=300, check=True)
							
							# Install Playwright browsers if needed
							logging.info("Installing Playwright browsers...")
							subprocess.run([venv_python, '-m', 'playwright', 'install'], 
										  cwd=BASE_DIR, timeout=300, check=True)
							
							logging.info("Dependencies updated successfully")
						else:
							logging.warning("Virtual environment not found, skipping dependency installation")
					except subprocess.TimeoutExpired:
						logging.error("Dependency installation timed out")
					except subprocess.CalledProcessError as e:
						logging.error(f"Dependency installation failed: {e}")
					except Exception as e:
						logging.error(f"Error installing dependencies: {e}")
				
				logging.info("Update completed successfully")
				
			finally:
				# Clean up temporary directory
				try:
					shutil.rmtree(temp_dir)
					logging.info("Cleaned up temporary files")
				except Exception as e:
					logging.warning(f"Failed to clean up temporary directory: {e}")
			
			# Return success first, then restart properly like settings do
			# This ensures the HTTP response is sent before restarting
			def delayed_restart():
				time.sleep(2)  # Give time for HTTP response to be sent
				logging.info("Restarting application after update...")
				try:
					import subprocess
					import sys
					
					# Get the current script path
					script_path = os.path.abspath(__file__)
					
					# Get current arguments (preserve test mode, etc.)
					args = [sys.executable, script_path]
					if PREFERENCES.get('test_mode', False):
						args.append('--test')
					if not PREFERENCES.get('enable_web_interface', True):
						args.append('--no-web')
					if not PREFERENCES.get('show_tray', True):
						args.append('--no-tray')
					
					# Start the new process
					subprocess.Popen(args, cwd=os.path.dirname(script_path))
					
					# Give it a moment to start
					time.sleep(1)
					
					# Exit current process
					os._exit(0)
				except Exception as e:
					logging.error(f"Failed to restart after update: {e}")
					os._exit(1)
			
			restart_thread = threading.Thread(target=delayed_restart, daemon=True)
			restart_thread.start()
			
			return jsonify({'success': True, 'message': 'Update completed successfully. Restarting...', 'restart': True})
			
		except Exception as e:
			logging.error(f"Update failed: {e}")
			return jsonify({'success': False, 'message': f'Update failed: {str(e)}'}), 500
	
	@socketio.on('connect')
	def handle_connect():
		logging.info('Web client connected')
		emit('status', {'message': 'Connected to Twitch Drop Automator'})
	
	@socketio.on('disconnect')
	def handle_disconnect():
		logging.info('Web client disconnected')

	@socketio.on('request_drops_update')
	def handle_drops_update_request():
		"""Handle client request for drops data update - return cached data"""
		try:
			with drops_data_lock:
				emit('drops_update', cached_drops_data)
		except Exception as e:
			logging.error(f"Error handling drops update request: {e}")
			emit('drops_error', {'error': str(e)})
	
	return app, socketio

def start_web_server():
	"""Start the web server in a separate thread"""
	global web_server_thread, app, socketio
	
	if app is None or socketio is None:
		app, socketio = create_web_app()
	
	def run_server():
		try:
			logging.info(f"Starting web server on http://{WEB_HOST}:{WEB_PORT}")
			socketio.run(app, host=WEB_HOST, port=WEB_PORT, debug=False, allow_unsafe_werkzeug=True)
		except Exception as e:
			logging.error(f"Web server error: {e}")
	
	web_server_thread = threading.Thread(target=run_server, daemon=True)
	web_server_thread.start()
	return web_server_thread

async def capture_screenshot_async():
	"""Async version of screenshot capture for use in async context"""
	global current_browser_context, current_working_page, socketio
	
	if current_browser_context is None or socketio is None:
		logging.debug("Screenshot capture skipped: context or socketio not available")
		return False
	
	try:
		# Get all pages from the context
		pages = current_browser_context.pages
		if not pages:
			logging.debug("Screenshot capture skipped: no pages available")
			return False
		
		# First, try to use the tracked working page if it's still valid
		active_page = None
		if current_working_page is not None and not current_working_page.is_closed():
			try:
				# Verify the page is still accessible
				url = current_working_page.url
				if url and url != 'about:blank':
					active_page = current_working_page
					logging.debug(f"Using tracked working page: {url}")
			except Exception:
				# Page is no longer valid, reset it
				current_working_page = None
		
		# If no tracked page, find the best available page
		if active_page is None:
			best_page = None
			
			for page in pages:
				if not page.is_closed():
					try:
						url = page.url
						if url and url != 'about:blank':
							# Prefer pages that are likely to be the main working page
							# Priority: Twitch streams > Facepunch > other pages
							if 'twitch.tv' in url and ('/streams/' in url or '/videos/' in url):
								active_page = page
								break  # This is definitely the active stream page
							elif 'facepunch.com' in url:
								best_page = page  # Good fallback
							elif best_page is None:
								best_page = page  # Any valid page as last resort
					except Exception:
						continue
			
			# Use the best page we found
			if active_page is None:
				active_page = best_page
			
			# If still no page found, use the first available page
			if active_page is None and pages:
				active_page = pages[0]
				if active_page.is_closed():
					logging.debug("Screenshot capture skipped: all pages are closed")
					return False
		
		# Check if page is still valid
		if active_page.is_closed():
			logging.debug("Screenshot capture skipped: active page is closed")
			return False
		
		# Try to get the current URL for debugging
		try:
			current_url = active_page.url
			logging.debug(f"Taking screenshot of active page: {current_url}")
		except Exception:
			current_url = "unknown"
		
		# Wait for page to be loaded and visible
		try:
			# Wait for the page to have some content
			await active_page.wait_for_load_state('domcontentloaded', timeout=3000)
			# Wait a bit more for any dynamic content
			await asyncio.sleep(0.5)
		except Exception as e:
			logging.debug(f"Page load wait failed: {e}")
		
		# Check if we're in headless mode
		is_headless = get_headless_preference()
		
		# Take screenshot with different options based on mode and timeout handling
		if is_headless:
			# In headless mode, take a full page screenshot with timeout
			try:
				screenshot_bytes = await asyncio.wait_for(
					active_page.screenshot(
						type='png', 
						full_page=True,  # Full page in headless
						animations='disabled'  # Disable animations for cleaner screenshots
					),
					timeout=10.0  # 10 second timeout
				)
			except asyncio.TimeoutError:
				logging.debug("Screenshot timeout in headless mode")
				return False
		else:
			# In non-headless mode, try to take viewport screenshot with timeout
			try:
				screenshot_bytes = await asyncio.wait_for(
					active_page.screenshot(
						type='png', 
						full_page=False,  # Just the visible viewport
						animations='disabled'
					),
					timeout=10.0  # 10 second timeout
				)
			except asyncio.TimeoutError:
				logging.debug("Viewport screenshot timeout, trying full page")
				try:
					screenshot_bytes = await asyncio.wait_for(
						active_page.screenshot(
							type='png', 
							full_page=True,
							animations='disabled'
						),
						timeout=10.0  # 10 second timeout
					)
				except asyncio.TimeoutError:
					logging.debug("Full page screenshot timeout")
					return False
			except Exception as e:
				logging.debug(f"Viewport screenshot failed, trying full page: {e}")
				# Fallback to full page if viewport fails
				try:
					screenshot_bytes = await asyncio.wait_for(
						active_page.screenshot(
							type='png', 
							full_page=True,
							animations='disabled'
						),
						timeout=10.0  # 10 second timeout
					)
				except asyncio.TimeoutError:
					logging.debug("Fallback screenshot timeout")
					return False
		
		if not screenshot_bytes:
			logging.debug("Screenshot capture failed: no data returned")
			return False
		
		# Check if screenshot is mostly white/blank
		if len(screenshot_bytes) < 1000:  # Very small file might be blank
			logging.debug("Screenshot appears to be blank (very small file)")
			return False
		
		# Convert to base64
		screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
		
		# Send via WebSocket
		socketio.emit('screenshot', {
			'image': f'data:image/png;base64,{screenshot_b64}',
			'timestamp': datetime.now().isoformat(),
			'url': current_url
		})
		
		logging.debug("Screenshot captured and sent successfully")
		return True
		
	except Exception as e:
		logging.debug(f"Screenshot capture failed: {e}")
		return False

async def start_screenshot_capture_async(test_mode=False):
	"""Start async screenshot capture task"""
	global current_browser_context, socketio
	
	if current_browser_context is None or socketio is None:
		logging.debug("Screenshot capture skipped: context or socketio not available")
		return
	
	logging.info("Async screenshot capture started")
	screenshot_count = 0
	consecutive_failures = 0
	max_failures = 5
	
	# In test mode, run indefinitely until Ctrl+C. In normal mode, respect EXIT_EVENT
	while True:
		if not test_mode and EXIT_EVENT.is_set():
			break
		
		try:
			success = await capture_screenshot_async()
			if success:
				screenshot_count += 1
				consecutive_failures = 0
				if screenshot_count % 10 == 0:  # Log every 10 screenshots
					logging.info(f"Captured {screenshot_count} screenshots")
			else:
				consecutive_failures += 1
				if consecutive_failures >= max_failures:
					logging.warning(f"Screenshot capture failed {consecutive_failures} times in a row")
					# Emit a special event to trigger page reload
					if socketio:
						socketio.emit('screenshot_failed', {
							'message': 'Screenshot capture failed multiple times. Page may be frozen.',
							'consecutive_failures': consecutive_failures
						})
					# Reset failure counter to avoid spam
					consecutive_failures = 0
		except Exception as e:
			logging.debug(f"Screenshot error: {e}")
			consecutive_failures += 1
		
		# In test mode, check for Ctrl+C more frequently
		if test_mode:
			try:
				await asyncio.sleep(SCREENSHOT_INTERVAL)
			except asyncio.CancelledError:
				logging.info("Screenshot capture cancelled by user")
				break
		else:
			await asyncio.sleep(SCREENSHOT_INTERVAL)
	
	logging.info("Async screenshot capture stopped")

def open_web_interface():
	"""Open the web interface in the default browser"""
	try:
		import webbrowser
		webbrowser.open(f'http://{WEB_HOST}:{WEB_PORT}')
		logging.info(f"Opened web interface: http://{WEB_HOST}:{WEB_PORT}")
	except Exception as e:
		logging.error(f"Failed to open web interface: {e}")

def parse_arguments():
	"""Parse command-line arguments"""
	parser = argparse.ArgumentParser(description='Twitch Drop Automator with Web Interface')
	parser.add_argument('--test', action='store_true', 
						help='Enable test mode - keeps browser open for screenshot testing')
	parser.add_argument('--no-tray', action='store_true',
						help='Disable system tray icon')
	parser.add_argument('--no-web', action='store_true',
						help='Disable web interface')
	return parser.parse_args()

# --- Logger Setup ---
logging.basicConfig(
	level=logging.DEBUG,
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
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
		"--disable-background-networking",
		"--force-device-scale-factor=1",
		"--disable-gpu-sandbox",
		"--disable-features=VizDisplayCompositor",
		"--run-all-compositor-stages-before-draw",
		"--disable-ipc-flooding-protection",
		"--disable-hang-monitor",
		"--disable-prompt-on-repost",
		"--disable-sync",
		"--disable-translate",
		"--disable-web-security",
		"--disable-features=TranslateUI",
		"--disable-component-extensions-with-background-pages",
		# Cache-busting arguments
		"--disable-http-cache",
		"--aggressive-cache-discard",
		"--disable-background-timer-throttling",
		"--disable-renderer-backgrounding",
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

	# Disable console logging to avoid Unicode encoding issues
	# page.on("console", lambda msg: logging.debug(f"Console[{msg.type}]: {msg.text.encode('utf-8', errors='replace').decode('utf-8')}"))
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
	"""Keep the app open and poll Twitch inventory until the user is logged in (avatar present).

	This uses a separate background tab so we don't interrupt whatever tab the user is using to log in.
	"""
	poll_page = None
	try:
		# One-time reminder toast
		try:
			send_notification("Twitch Drops", "Waiting for login… Right-click tray → untick 'Headless mode'")
		except Exception:
			pass
		# Open a separate tab for polling login status
		try:
			poll_page = await context.new_page()
		except Exception:
			poll_page = None
		while not EXIT_EVENT.is_set():
			try:
				if poll_page is None:
					poll_page = await context.new_page()
				await goto_with_exit(poll_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
				await maybe_accept_cookies(poll_page)
				await poll_page.wait_for_timeout(500)
				avatar = await poll_page.query_selector('img[alt="User Avatar"]')
				if avatar:
					logging.info("Detected user avatar; login complete.")
					return
				logging.info("Not logged in yet; still waiting…")
			except Exception:
				pass
			await asyncio.sleep(5)
	finally:
		try:
			if poll_page:
				await poll_page.close()
		except Exception:
			pass
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
				# Try multiple selectors for user avatar/login detection
				avatar_selectors = [
					'img[alt="User Avatar"]',
					'[data-a-target="user-avatar"]',
					'[data-testid="user-avatar"]',
					'.user-avatar',
					'[aria-label*="avatar"]',
					'[alt*="avatar"]'
				]
				
				avatar_found = False
				for selector in avatar_selectors:
					try:
						await wait_with_exit(asyncio.create_task(page.wait_for_selector(selector, timeout=10000)))
						logging.info(f"User appears to be logged in (found selector: {selector}).")
						avatar_found = True
						break
					except Exception:
						continue
				
				if avatar_found:
					success = True
					return context
				else:
					logging.warning("Could not find user avatar with any selector. User may not be logged in.")
					raise Exception("No avatar selectors found")
					
			except Exception:
				logging.warning("Could not find user avatar. User may not be logged in. Waiting for user to complete login.")
				await wait_until_logged_in(context, page)
				# After wait, verify again with multiple selectors
				avatar_found = False
				for selector in avatar_selectors:
					try:
						await page.wait_for_selector(selector, timeout=5000)
						logging.info(f"Login detected after waiting (found selector: {selector}).")
						avatar_found = True
						break
					except Exception:
						continue
				
				if avatar_found:
					success = True
					return context
				else:
					raise Exception("Login verification failed - no avatar found after waiting")
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
		# Add cache-busting headers specifically for Facepunch
		await page.set_extra_http_headers({
			'Cache-Control': 'no-cache, no-store, must-revalidate',
			'Pragma': 'no-cache',
			'Expires': '0'
		})
		
		# Add timestamp to URL to prevent caching
		import time
		cache_bust_url = f"{FACEPUNCH_DROPS_URL}?t={int(time.time() * 1000)}"
		
		await goto_with_exit(page, cache_bust_url, timeout=120000, wait_until="domcontentloaded")
		
		# Clear Facepunch-specific cache storage
		try:
			await page.evaluate("""
				// Clear only Facepunch-related cache
				if (caches && caches.keys) {
					caches.keys().then(keys => {
						keys.forEach(key => {
							if (key.includes('facepunch') || key.includes('twitch.facepunch')) {
								caches.delete(key);
							}
						});
					});
				}
			""")
		except Exception:
			pass
		try:
			await page.reload(ignore_http_errors=True, timeout=120000)
			await page.wait_for_load_state("domcontentloaded")
		except Exception:
			# Fallback: navigate again if reload fails
			await goto_with_exit(page, FACEPUNCH_DROPS_URL, timeout=120000, wait_until="domcontentloaded")
		await asyncio.sleep(1)
		# Detect campaign not-started state and event start time
		not_started = False
		start_epoch_ms = None
		try:
			not_started = await page.evaluate(
				r"""
				() => !!document.querySelector('.campaign.not-started, .drops.not-started, .streamer-drops.not-started')
				"""
			)
			start_epoch_ms = await page.evaluate(
				r"""
				() => {
				  const scripts = Array.from(document.querySelectorAll('script'));
				  for (const s of scripts) {
				    const txt = s.textContent || '';
				    let m = txt.match(/setupCountdown\([^,]*,[^,]*,\s*(\d{10,})\s*\)/);
				    if (m) return parseInt(m[1], 10);
				    m = txt.match(/new\s+Date\(\s*(\d{10,})\s*\)/);
				    if (m) return parseInt(m[1], 10);
				  }
				  const dateEl = document.querySelector('.event-date .date[data-date-id]');
				  if (dateEl) {
				    const id = dateEl.getAttribute('data-date-id') || '';
				    const seg = id.split('-').pop();
				    if (seg && /\d+/.test(seg)) {
				      const n = parseInt(seg, 10);
				      return n < 2e12 ? n * 1000 : n;
				    }
				  }
				  return null;
				}
				"""
			)
		except Exception:
			pass
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
						# Try to get item video/image and streamer profile picture
						video_url = None
						streamer_avatar = None
						try:
							# Try multiple selectors for item media
							media_selectors = [
								'.drop-box-body video',
								'.drop-box video', 
								'video',
								'.drop-box-body img[src*="item"]',
								'.drop-box-body img[src*="drop"]',
								'.drop-box img[src*="item"]',
								'.drop-box img[src*="drop"]',
								'.drop-box-body img',
								'.drop-box img',
								'img'
							]
							
							for selector in media_selectors:
								media_el = await box.query_selector(selector)
								if media_el:
									# Check if it's a video element
									tag_name = await media_el.evaluate('el => el.tagName.toLowerCase()')
									if selector.startswith('video') or tag_name == 'video':
										# Try to get video source from <source> tag
										source_el = await media_el.query_selector('source')
										if source_el:
											media_src = await source_el.get_attribute('src')
											if media_src:
												# Convert relative URLs to absolute
												if media_src.startswith('/'):
													media_src = f"https://twitch.facepunch.com{media_src}"
												video_url = media_src
												logging.debug(f"Found video source for {streamer} - {item}: {video_url}")
												break
										else:
											# Fallback to video element's src attribute
											media_src = await media_el.get_attribute('src')
											if media_src:
												if media_src.startswith('/'):
													media_src = f"https://twitch.facepunch.com{media_src}"
												video_url = media_src
												logging.debug(f"Found video src for {streamer} - {item}: {video_url}")
												break
									else:
										# It's an image element
										media_src = await media_el.get_attribute('src')
										if media_src:
											# Convert relative URLs to absolute
											if media_src.startswith('/'):
												media_src = f"https://twitch.facepunch.com{media_src}"
											video_url = media_src
											logging.debug(f"Found image for {streamer} - {item}: {video_url}")
											break
							
							# Try to get streamer profile picture
							avatar_selectors = [
								'.streamer-avatar img',
								'.streamer-info img',
								'.drop-box-header img[src*="profile"]',
								'.drop-box-header img[src*="avatar"]',
								'.streamer-name + img',
								'.drop-box-header img'
							]
							
							for selector in avatar_selectors:
								avatar_el = await box.query_selector(selector)
								if avatar_el:
									avatar_src = await avatar_el.get_attribute('src')
									if avatar_src:
										# Convert relative URLs to absolute
										if avatar_src.startswith('/'):
											avatar_src = f"https://twitch.facepunch.com{avatar_src}"
										streamer_avatar = avatar_src
										logging.debug(f"Found streamer avatar for {streamer}: {streamer_avatar}")
										break
							
							if not video_url:
								logging.debug(f"No media found for {streamer} - {item}")
								
						except Exception as e:
							logging.debug(f"Error getting media for {streamer} - {item}: {e}")
						
						streamer_specific.append({
							"streamer": streamer,
							"item": item,
							"hours": hours,
							"url": twitch_url,
							"is_live": is_live,
							"video": video_url,
							"streamer_avatar": streamer_avatar,
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
				    const inGeneralSection = !!box.closest('#drops');
				    const isGeneral = inGeneralSection || /\bgeneral\s+drop\b/i.test(headerText);
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
				    const isLocked = !!box.querySelector('.drop-lock');
				    
				    // Try to get item video
				    let video = null;
				    try {
				      // First try to get video element
				      const videoEl = box.querySelector('.drop-box-body video, .drop-box video, video');
				      if (videoEl && videoEl.src) {
				        video = videoEl.src;
				        // Convert relative URLs to absolute
				        if (video.startsWith('/')) {
				          video = 'https://twitch.facepunch.com' + video;
				        }
				        console.log('Found general drop video:', video);
				      } else {
				        // Fallback to image if no video
				        const imgEl = box.querySelector('.drop-box-body img, .drop-box img, img');
				        if (imgEl && imgEl.src) {
				          video = imgEl.src;
				          // Convert relative URLs to absolute
				          if (video.startsWith('/')) {
				            video = 'https://twitch.facepunch.com' + video;
				          }
				          console.log('Found general drop image (fallback):', video);
				        } else {
				          console.log('No video or image found for general drop');
				        }
				      }
				    } catch (e) {
				      console.log('Error getting general drop video:', e);
				    }
				    
				    res.push({ headerText, isGeneral, item, hours, alias, isLocked, video });
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
						general.append({
							"item": d.get('item'), 
							"hours": d.get('hours'), 
							"alias": d.get('alias'), 
							"header": d.get('headerText'), 
							"is_locked": d.get('isLocked'),
							"video": d.get('video')  # Will be populated by the evaluate function
						})
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
		return {"general": general, "streamer": streamer_specific, "not_started": bool(not_started), "start_epoch_ms": start_epoch_ms}
	except Exception as e:
		logging.warning(f"Facepunch parsing failed: {e}")
		return {"general": [], "streamer": [], "not_started": False, "start_epoch_ms": None}
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

def emit_debug(message: str, level: str = "info"):
	# Debug emitter disabled per request; keep as no-op to avoid refactor churn.
	return

def generate_search_variations(base_name: str) -> list[str]:
	"""Generate lowercase search variations for a given name.
	Examples: "FOOLISH - VAGABOND JACKET" -> ["foolish - vagabond jacket", "foolish", "vagabond jacket"].
	"""
	name = (base_name or "").strip().lower()
	if not name:
		return []
	variations = [name]
	if ' ' in name:
		first_word = name.split()[0]
		if len(first_word) > 3:
			variations.append(first_word)
	for sep in [' - ', ' + ', ' & ', ' and ']:
		if sep in name:
			for part in name.split(sep):
				p = part.strip()
				if len(p) > 3:
					variations.append(p)
	# Deduplicate
	seen = set()
	out = []
	for v in variations:
		if v and v not in seen:
			seen.add(v)
			out.append(v)
	return out

async def get_claimed_days_for_streamer(inv_page, streamer_name: str) -> int | None:
	"""Return approximate days since this streamer's drop was claimed, or None if not found.
	We look for elements containing the streamer name (with flexible matching), then within the same card search for a time label like
	'23 minutes ago', 'yesterday', '9 days ago', '2 months ago', 'last month'.
	
	Uses multiple search variations to handle name differences between Facepunch and Twitch:
	- For "FOOLISH - VAGABOND JACKET" also searches for "foolish"
	- Handles separators like " - ", " + ", " & ", " and "
	- Only returns drops claimed within the last 3 weeks (21 days)
	"""
	target_lower = ((streamer_name or "").strip().lower())
	if not target_lower:
		return None

async def scrape_recent_claimed_items(inv_page):
	"""Scrape the Twitch inventory page for claimed items within the last 21 days.

	Returns a list of dicts: [{"name": str, "days": int}].
	An item is considered claimed if either:
	- It appears under the Claimed section, or
	- Its card contains the checkmark/tick icon SVG.
	Only items with a recognizable timestamp <= 21 days are returned.
	Assumes caller may reuse the same page; this function ensures navigation.
	"""
	try:
		# Ensure page is loaded
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(400)
		emit_debug("[claimed-sweep] Navigating inventory for sweep")
		items = await inv_page.evaluate(
			r"""
			() => {
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

			  // Find Claimed section container
			  const claimedHeader = Array.from(document.querySelectorAll('h5')).find(h5 => (h5.textContent || '').trim().toLowerCase() === 'claimed');
			  let claimedSection = null;
			  if (claimedHeader) {
				claimedSection = claimedHeader.closest('div')?.querySelector('.ScTower-sc-1sjzzes-0, .tw-tower') || null;
			  }
			  if (!claimedSection) {
				claimedSection = Array.from(document.querySelectorAll('div, section')).find(el => {
				  const text = (el.textContent || '').toLowerCase();
				  return text.includes('claimed') && text.length < 100;
				}) || null;
			  }

			  const searchScopes = [];
			  if (claimedSection) searchScopes.push(claimedSection);
			  searchScopes.push(document);

			  const results = [];
			  const seenKeys = new Set();
			  const isCheckmarkPath = (d) => {
				if (!d) return false;
				return d.includes('m4 10 5 5 8-8-1.5-1.5L9 12 5.5 8.5 4 10z') || d.includes('m4 10 5 5 8-8') || (d.includes('4 10') && d.includes('5 5') && d.includes('8-8'));
			  };

			  for (const scope of searchScopes) {
				const cards = Array.from(scope.querySelectorAll('div.Layout-sc-1xcs6mc-0.fHdBNk'));
				for (const card of cards) {
				  // Identify name and time within the card
				  const nameEl = card.querySelector('p.CoreText-sc-1txzju1-0.kGfRxP, p[class*="kGfRxP"]');
				  const timeEl = card.querySelector('p.CoreText-sc-1txzju1-0.jPfhdt, p[class*="jPfhdt"]');
				  const name = (nameEl?.textContent || '').trim();
				  const timeText = (timeEl?.textContent || '').trim();
				  if (!name) continue;
				  // Treat as claimed if inside Claimed section OR contains a checkmark icon
				  const inClaimed = !!claimedSection && claimedSection.contains(card);
				  let hasTick = false;
				  const path = card.querySelector('svg path');
				  if (path) {
					const d = path.getAttribute('d') || '';
					hasTick = isCheckmarkPath(d);
				  }
				  if (!inClaimed && !hasTick) continue;

				  if (!isTimeText(timeText)) continue;
				  const days = toDays(timeText);
				  if (days === null || days === undefined) continue;
				  if (days > 21) continue;
				  const key = name.toLowerCase();
				  if (seenKeys.has(key)) continue;
				  seenKeys.add(key);
				  results.push({ name, days });
				}
			  }
			  return results;
			}
			"""
		)
		emit_debug(f"[claimed-sweep] Found {len(items or [])} claimed candidates (<=21d)")
		return items or []
	except Exception as e:
		emit_debug(f"[claimed-sweep] Failed: {e}", 'warning')
		return []
	
	# Create multiple search variations for better matching
	search_variations = generate_search_variations(target_lower)
	emit_debug(f"[claimed-check] Variations for '{streamer_name}': {search_variations}")
	
	try:
		# Ensure page is loaded
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(400)
		emit_debug(f"[claimed-check] Navigating inventory for '{streamer_name}'")
		days = await inv_page.evaluate(
			r"""
			(args) => {
			  const searchVariations = (args && args.searchVariations) || [];
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
			  
			  // Find the claimed section by looking for the "Claimed" header and its container
			  const claimedHeader = Array.from(document.querySelectorAll('h5')).find(h5 => 
				(h5.textContent || '').trim().toLowerCase() === 'claimed'
			  );
			  
			  let claimedSection = null;
			  if (claimedHeader) {
				// Find the tower container that comes after the header
				claimedSection = claimedHeader.closest('div').querySelector('.ScTower-sc-1sjzzes-0, .tw-tower');
			  }
			  
			  // Fallback: try to find any element containing "claimed" text
			  if (!claimedSection) {
				claimedSection = Array.from(document.querySelectorAll('div, section')).find(el => {
					const text = (el.textContent || '').toLowerCase();
					return text.includes('claimed') && text.length < 100;
				});
			  }
			  
			  // Define search scope - prefer claimed section if found, otherwise search entire page
			  const searchScope = claimedSection || document;
			  
			  // Look for checkmark/tick icons in the claimed section
			  // The checkmark SVG has a specific path: "m4 10 5 5 8-8-1.5-1.5L9 12 5.5 8.5 4 10z"
			  const checkmarkSvgs = Array.from(searchScope.querySelectorAll('svg path')).filter(svg => {
				const path = svg.getAttribute('d') || '';
				return path.includes('m4 10 5 5 8-8-1.5-1.5L9 12 5.5 8.5 4 10z') || 
					   path.includes('m4 10 5 5 8-8') || // Partial match for the checkmark path
					   (path.includes('4 10') && path.includes('5 5') && path.includes('8-8')); // Key parts of checkmark
			  });
			  
			  // For each checkmark found, try to match it with our search variations
			  for (const checkmarkSvg of checkmarkSvgs) {
				// Find the drop container that contains this checkmark
				const dropContainer = checkmarkSvg.closest('div.Layout-sc-1xcs6mc-0.fHdBNk');
				if (dropContainer) {
				  // Look for the drop name in this container
				  const nameEl = dropContainer.querySelector('p.CoreText-sc-1txzju1-0.kGfRxP, p[class*="kGfRxP"]');
				  if (nameEl) {
					const dropName = (nameEl.textContent || '').toLowerCase();
					
					// Check if this drop name matches any of our search variations
					for (const targetLower of searchVariations) {
					  if (dropName.includes(targetLower)) {
						// Found a match! Now get the timestamp
						const timeEl = dropContainer.querySelector('p.CoreText-sc-1txzju1-0.jPfhdt, p[class*="jPfhdt"]');
						if (timeEl && isTimeText(timeEl.textContent || '')) {
						  const days = toDays(timeEl.textContent || '');
						  if (days !== null && days !== undefined) {
							// Only return if the drop is not older than 3 weeks (21 days)
							if (days <= 21) {
							  return days;
							}
						  }
						}
					  }
					}
				  }
				}
			  }
			  
			  return null;
			}
			""",
			{"searchVariations": search_variations}
		)
		return days
	except Exception as e:
		emit_debug(f"[claimed-check] Failed for '{streamer_name}': {e}", 'warning')
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

async def is_browser_context_valid(context) -> bool:
	"""Check if the browser context is still valid and responsive."""
	try:
		if context is None:
			return False
		# Try to get pages to test if context is responsive
		pages = context.pages
		return len(pages) >= 0  # If we can get pages, context is valid
	except Exception:
		return False

async def recover_browser_context(p, current_context=None):
	"""Attempt to recover from a closed browser context by creating a new one."""
	try:
		logging.info("Attempting to recover browser context...")
		if current_context:
			try:
				await current_context.close()
			except Exception:
				pass
		
		# Create new context using the same method as run_flow
		context, page = await launch_context(p, compat_mode=False)
		logging.info("Successfully recovered browser context")
		return context, page
	except Exception as e:
		logging.error(f"Failed to recover browser context: {e}")
		return None, None

async def claim_available_rewards(inv_page, navigate: bool = True) -> int:
	"""Click all visible 'Claim Now' buttons on the inventory page.

	Returns the number of claims clicked. When navigate=False, assumes caller is already on the inventory page.
	"""
	claimed = 0
	try:
		# Check if browser context is still valid
		if inv_page.is_closed():
			logging.warning("Inventory page is closed, cannot claim rewards")
			return -1  # Return -1 to indicate browser context issue
		
		# Check if browser context is still responsive
		try:
			context = inv_page.context
			if not await is_browser_context_valid(context):
				logging.warning("Browser context is not responsive, cannot claim rewards")
				return -1
		except Exception:
			logging.warning("Cannot access browser context, cannot claim rewards")
			return -1
		
		if navigate:
			await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(inv_page)
			await inv_page.wait_for_timeout(500)
		
		# Multiple selector strategies for robustness
		claim_selectors = [
			'button:has-text("Claim Now")',
			'button[data-a-target="tw-core-button-label-text"]:has-text("Claim Now")',
			'button.ScCoreButton-sc-ocjdkq-0:has-text("Claim Now")',
			'button:has([data-a-target="tw-core-button-label-text"]:has-text("Claim Now"))',
			'button:has-text("Claim")',  # Fallback for older versions
		]
		
		claim_buttons = []
		for selector in claim_selectors:
			try:
				buttons = await inv_page.query_selector_all(selector)
				if buttons:
					claim_buttons = buttons
					logging.debug(f"Found {len(buttons)} claim buttons using selector: {selector}")
					break
			except Exception as e:
				logging.debug(f"Selector '{selector}' failed: {e}")
				continue
		
		if not claim_buttons:
			logging.debug("No claim buttons found on inventory page")
			return 0
		
		for btn in claim_buttons:
			try:
				# Double-check button is still valid and visible
				if await btn.is_visible():
					await btn.click()
					claimed += 1
					logging.info("Claimed a reward")
					await asyncio.sleep(0.5)
				else:
					logging.debug("Claim button found but not visible, skipping")
			except Exception as e:
				logging.warning(f"Failed to click claim button: {e}")
				continue
				
	except Exception as e:
		# Check if it's a browser context closure issue
		if "Target page, context or browser has been closed" in str(e) or "TargetClosedError" in str(e):
			logging.error("Browser context closed during claim operation")
			return -1  # Return -1 to indicate browser context issue
		else:
			logging.error(f"Unexpected error during claim operation: {e}")
			return -1  # Return -1 to indicate error
	
	return claimed

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
			# Early claim: if Claim button present, click and finish (progressbar may be gone at 100%)
			await inv_page.wait_for_timeout(400)
			try:
				if await inv_page.query_selector('button:has-text("Claim Now")') or await inv_page.query_selector('button:has-text("Claim")'):
					c = await claim_available_rewards(inv_page, navigate=False)
					if c > 0:
						return True
					elif c == -1:
						logging.error("Browser context issue during claim check, stopping polling")
						return False
			except Exception:
				pass
			# ensure progressbars rendered (if present)
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				try:
					await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
				except Exception:
					pass
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
				
				# Update cache with current progress
				try:
					progress_map = {title: percent}
					update_cached_drops_data(None, progress_map)
				except Exception as e:
					logging.debug(f"Failed to update cache during progress tracking: {e}")
				
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
			# Early claim if present
			await inv_page.wait_for_timeout(400)
			try:
				if await inv_page.query_selector('button:has-text("Claim")'):
					c = await claim_available_rewards(inv_page, navigate=False)
					if c > 0:
						return True
			except Exception:
				pass
			# Progressbars may not exist when the item is claimable; do not treat as error
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				try:
					await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
				except Exception:
					pass
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
				
				# Update cache with current progress
				try:
					progress_map = {match.get('title'): p}
					update_cached_drops_data(None, progress_map)
				except Exception as e:
					logging.debug(f"Failed to update cache during general progress tracking: {e}")
				
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
			# Early claim if present
			await inv_page.wait_for_timeout(400)
			try:
				if await inv_page.query_selector('button:has-text("Claim")'):
					c = await claim_available_rewards(inv_page, navigate=False)
					if c > 0:
						return (True, False)
			except Exception:
				pass
			# Progressbars may not exist when the item is claimable; do not treat as error
			try:
				await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=15000)
			except Exception:
				await inv_page.wait_for_timeout(800)
				try:
					await inv_page.wait_for_selector('[role="progressbar"][aria-valuenow]', timeout=8000)
				except Exception:
					pass
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
				
				# Update cache with current progress
				try:
					progress_map = {match.get('title'): p}
					update_cached_drops_data(None, progress_map)
				except Exception as e:
					logging.debug(f"Failed to update cache during general progress tracking: {e}")
				
				if p >= 100:
					await claim_available_rewards(inv_page)
					return (True, False)

			# After logging progress, also check if any streamer-specific items need progress
			fp = await fetch_facepunch_drops(context)
			streamer_targets = fp.get('streamer', []) if fp else []
			for st in streamer_targets:
				name = (st.get('streamer') or '').strip()
				if not name:
					continue
				# Only use Facepunch for live status
				if not bool(st.get('is_live')):
					continue
				name_lower = name.lower()
				if name_lower in completed_streamers:
					continue
				# If we haven't already completed this streamer drop historically, switch now
				try:
					days = await get_claimed_days_for_streamer(inv_page, name)
				except Exception:
					days = None
				if days is not None:
					# Already claimed previously; skip
					continue
				# Found a live streamer whose drop has not been claimed yet → switch
				logging.info(f"Streamer-specific drop available again: {name}. Switching from general mode.")
				return (False, True)

			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
		except Exception as e:
			logging.warning(f"Poll general/switch check issue: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
	return (False, False)

async def run_drops_workflow(context, test_mode=False):
	global current_working_page
	inv_page = await context.new_page()
	completed_streamers = set()
	try:
		while True:
			if EXIT_EVENT.is_set():
				logging.info("Exit requested; stopping workflow loop.")
				return
			fp = await fetch_facepunch_drops(context)
			# If the campaign hasn't started yet, notify and exit early (unless in test mode)
			try:
				if fp and bool(fp.get('not_started')):
					start_ms = fp.get('start_epoch_ms')
					label, days_until, hours_until = _format_start_time_uk(start_ms if isinstance(start_ms, int) else 0)
					msg = f"Twitch drop event hasn't started yet. Starts {label}."
					logging.info(msg)
					try:
						when = f"{days_until} days" if days_until >= 1 else f"{hours_until} hours"
						send_notification("Twitch Drops", f"Twitch drop starting in {when} ({label})")
					except Exception:
						pass
					if test_mode:
						logging.info("Test mode: Continuing despite event not started yet.")
						# In test mode, just wait a bit and continue
						await asyncio.sleep(10)
						continue
					else:
						logging.info("Cannot continue; exiting until event begins.")
						EXIT_EVENT.set()
						return
			except Exception:
				pass
			streamer_targets = fp.get('streamer', [])

			# Gather in-progress titles to prioritize watching those
			progress_map = await get_inventory_progress_map(inv_page)
			in_progress_titles = { (t or '').lower() for t in progress_map.keys() }
			
			# Update cached drops data for web interface (will be updated again with claimed list below)
			update_cached_drops_data(fp, progress_map)

			# Defer general drop decision until after streamer candidates are considered
			longest_general = None

			# One-time sweep of recently claimed items (<= 21 days)
			recently_claimed = []
			try:
				recently_claimed = await scrape_recent_claimed_items(inv_page)
			except Exception:
				recently_claimed = []

			# Helper to check if a streamer appears in recently_claimed
			def is_streamer_recently_claimed(streamer: str) -> int | None:
				name_vars = generate_search_variations(streamer)
				for it in (recently_claimed or []):
					n = (it.get('name') or '').lower()
					for v in name_vars:
						if v and v in n:
							return int(it.get('days')) if isinstance(it.get('days'), int) else 0
				return None

			# Build candidates only for streamer-specific items present in inventory (<100%)
			candidates = []
			live_any = []
			inventory_entries = [((t or '').lower(), p) for t, p in (progress_map or {}).items()]
			
			# Debug lists for better visibility
			streamer_drops_with_progress = []
			streamer_drops_no_progress = []
			general_drops_available = []
			
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
				emit_debug(f"[candidates] {name}: inventory match_pct={match_pct}")
				
				# If match_pct is None, it means the drop hasn't started yet (not on Twitch inventory)
				# This is exactly what we want to work on!
				# Only skip if it's already 100% complete on Twitch
				if match_pct is not None and match_pct >= 100:
					continue
				# Prioritize drops based on their state:
				# Priority 1: Streamers with progress (1-99% on Twitch) - highest priority
			# Priority 2: Streamers with no progress (not on Twitch inventory) - medium priority
			# Priority 3: Recently claimed (avoid repeats) - lowest priority
				if match_pct is not None:
					# Already in progress on Twitch
					priority = 1
					item_name = st.get('item', 'Unknown')
					streamer_drops_with_progress.append(f"{name} ({match_pct}%) - {item_name}")
					emit_debug(f"[candidates] {name}: set priority=1 (in-progress {match_pct}%)")
				else:
					# Not on Twitch inventory = unstarted drop
					# Before considering, check if this was recently claimed (<= 21 days). If so, skip entirely.
					priority = 2
					# Use one-time sweep to avoid repeated page work
					days = is_streamer_recently_claimed(name)
					if days is None:
						# Fallback direct check if needed
						try:
							days = await get_claimed_days_for_streamer(inv_page, name)
						except Exception:
							days = None
					if days is not None and days <= 21:
						logging.info(f"Skipping streamer '{name}': claimed {days} day(s) ago.")
						continue
					item_name = st.get('item', 'Unknown')
					streamer_drops_no_progress.append(f"{name} - {item_name}")
				candidates.append({"streamer": name, "url": st.get('url'), "is_live": st.get('is_live'), "priority": priority, "pct": match_pct, "item": st.get('item')})

			# Populate general drops debug list
			if fp and fp.get('general'):
				for g in fp['general']:
					item_name = g.get('item', 'Unknown')
					hours = g.get('hours', 0)
					general_drops_available.append(f"{item_name} ({hours}h)")
			
			# Log debug information
			logging.info(f"=== DROP DEBUG INFO ===")
			logging.info(f"Streamer drops with progress: {streamer_drops_with_progress}")
			logging.info(f"Streamer drops with no progress: {streamer_drops_no_progress}")
			logging.info(f"General drops available: {general_drops_available}")
			logging.info(f"Total candidates: {len(candidates)}")
			logging.info(f"========================")

			# Update cache with recently claimed list for web UI
			recently_claimed_names = [it.get('name') for it in (recently_claimed or [])]
			update_cached_drops_data(fp, progress_map, recently_claimed_names)

			# Do not enter general mode here; prefer streamer-specific workflow below

			# Prefer live streamer-specific drops first (only when we actually have streamer-specific items to progress)
			if candidates:
				# Sort by priority: 1=streamers with progress, 2=streamers with no progress, 3=recently claimed
				# Within same priority, prefer lower progress percentages
				candidates.sort(key=lambda s: (s.get('priority', 2), s.get('pct', 0) if s.get('pct') is not None else 0))
				target_st = candidates[0]
				target_name = target_st.get('streamer')
				target_url = target_st.get('url')
				target_item = target_st.get('item', 'Unknown Item')
				priority = target_st.get('priority', 2)
				progress_pct = target_st.get('pct')
				if priority == 1:
					status = f"streamer with progress ({progress_pct}%)"
				elif priority == 2:
					status = "streamer with no progress (not on Twitch inventory)"
				else:
					status = "recently claimed"
				logging.info(f"Chosen streamer: {target_name} (live={bool(target_st.get('is_live'))}, status={status})")
				
				# Update current working item
				update_current_working_item({
					"type": "streamer",
					"streamer": target_name,
					"item": target_item,
					"status": status,
					"progress": progress_pct
				})
				
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
								# Update the current working page for screenshots
								current_working_page = stream_page
							except Exception:
								stream_page = None
					except Exception:
						stream_page = None
					if not stream_page and target_url:
						stream_page = await context.new_page()
						await goto_with_exit(stream_page, target_url, timeout=120000, wait_until="domcontentloaded")
						# Update the current working page for screenshots
						current_working_page = stream_page
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
				send_notification("Twitch Drops", f"Now watching {target_name} for '{target_item}'")
				await ensure_stream_playing(stream_page)
				await set_low_quality(stream_page)
				completed = await poll_until_reward_complete(context, inv_page, streamer_name=target_name)
				try:
					await stream_page.close()
				except Exception:
					pass
				if completed:
					logging.info("Streamer reward completed or claimable. Attempting to claim any available rewards and moving to next.")
					claim_result = await claim_available_rewards(inv_page)
					if claim_result == -1:
						logging.error("Browser context issue during claim operation, stopping workflow")
						EXIT_EVENT.set()
						return
					completed_streamers.add((target_name or "").lower())
					# Clear current working item
					update_current_working_item(None)
				else:
					logging.info("Moving to next candidate.")
				# Refresh and next loop
				await asyncio.sleep(1)
				continue

			# If no streamer-specific items need progress, evaluate general drops
			logging.info("No streamer drops available, falling back to general drops")
			# First, claim any available rewards to ensure we don't exit with pending claims
			try:
				claim_result = await claim_available_rewards(inv_page)
				if claim_result == -1:
					logging.error("Browser context issue during general drops claim operation, stopping workflow")
					EXIT_EVENT.set()
					return
			except Exception:
				pass
			if await are_all_general_drops_complete(inv_page, fp.get('general') if fp else None):
				logging.info("All general drops are complete. Exiting program.")
				EXIT_EVENT.set()
				return
			# Additional guard: if Facepunch general list is empty but site shows not-started or locked items, handle gracefully
			try:
				if fp and (not fp.get('general')) and (fp.get('not_started') or True in [bool((g or {}).get('is_locked')) for g in fp.get('general') or []]):
					start_ms = fp.get('start_epoch_ms')
					label, days_until, hours_until = _format_start_time_uk(start_ms if isinstance(start_ms, int) else 0)
					logging.info(f"No general drops available yet; event may be locked or upcoming. Starts {label}.")
					try:
						when = f"{days_until} days" if days_until >= 1 else f"{hours_until} hours"
						send_notification("Twitch Drops", f"Twitch drop starting in {when} ({label})")
					except Exception:
						pass
					EXIT_EVENT.set()
					return
			except Exception:
				pass

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
				
				# Update current working item
				update_current_working_item({
					"type": "general",
					"item": longest_general.get('item'),
					"hours": longest_general.get('hours'),
					"streamer": target_name,
					"status": "general drop tracking"
				})
				
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
								# Update the current working page for screenshots
								current_working_page = stream_page
							except Exception:
								stream_page = None
					except Exception:
						stream_page = None
					if not stream_page and target_url:
						stream_page = await context.new_page()
						await goto_with_exit(stream_page, target_url, timeout=120000, wait_until="domcontentloaded")
						# Update the current working page for screenshots
						current_working_page = stream_page
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
					# Clear current working item
					update_current_working_item(None)
				elif switch:
					logging.info("Switching to streamer-specific mode; a needed streamer drop is available again.")
					# Clear current working item when switching
					update_current_working_item(None)
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

async def main(start_tray: bool = True, test_mode: bool = False, enable_web: bool = True):
	logging.info("--- Starting Twitch Drop Automator ---")
	
	# Check if test mode is enabled in config (overrides command line)
	config_test_mode = PREFERENCES.get('test_mode', False) if PREFERENCES else False
	if config_test_mode:
		test_mode = True
		logging.info("TEST MODE ENABLED (from config) - Browser will stay open for screenshot testing")
	elif test_mode:
		logging.info("TEST MODE ENABLED (from command line) - Browser will stay open for screenshot testing")
	
	# Start web server
	if enable_web:
		try:
			start_web_server()
			logging.info("Web server started")
		except Exception as e:
			logging.error(f"Failed to start web server: {e}")
	
	# Start tray icon for quick toggles (optionally skipped on macOS; see __main__)
	try:
		global TRAY_ICON, ICON_PATH
		if start_tray:
			try:
				TRAY_ICON = start_system_tray(block=False)
				if TRAY_ICON is None:
					logging.warning("System tray failed to start, continuing without tray icon")
			except Exception as tray_err:
				logging.error(f"Failed to start system tray: {tray_err}")
				TRAY_ICON = None
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
			# Set global browser context for screenshot capture
			global current_browser_context
			current_browser_context = context
		except asyncio.CancelledError:
			logging.info("Startup cancelled.")
			return
		try:
			if test_mode:
				# In test mode, just keep the browser open and let screenshots run
				logging.info("Test mode: Keeping browser open. Check web interface for screenshots.")
				logging.info("Press Ctrl+C to exit test mode.")
				# Open web interface automatically in test mode
				if enable_web:
					open_web_interface()
				# Start async screenshot capture - this will run indefinitely until Ctrl+C
				await start_screenshot_capture_async(test_mode=True)
			else:
				# For normal mode, start screenshot capture as background task
				screenshot_task = asyncio.create_task(start_screenshot_capture_async(test_mode=False))
				try:
					await run_drops_workflow(context, test_mode=False)
				finally:
					screenshot_task.cancel()
					try:
						await screenshot_task
					except asyncio.CancelledError:
						pass
		finally:
			if not test_mode:
				logging.info("Closing browser.")
				current_browser_context = None
				await context.close()
			else:
				logging.info("Test mode: Browser kept open for testing")
	logging.info("--- Automator finished ---")

async def is_streamer_online_on_facepunch(context, streamer_name: str) -> bool | None:
	"""Return True if Facepunch page shows the given streamer online, False if found and offline,
	None if not found on page (unknown)."""
	if not streamer_name:
		return None
	page = await context.new_page()
	try:
		# Add cache-busting headers specifically for Facepunch
		await page.set_extra_http_headers({
			'Cache-Control': 'no-cache, no-store, must-revalidate',
			'Pragma': 'no-cache',
			'Expires': '0'
		})
		
		# Add timestamp to URL to prevent caching
		import time
		cache_bust_url = f"{FACEPUNCH_DROPS_URL}?t={int(time.time() * 1000)}"
		
		await goto_with_exit(page, cache_bust_url, timeout=120000, wait_until="domcontentloaded")
		
		# Clear Facepunch-specific cache storage
		try:
			await page.evaluate("""
				// Clear only Facepunch-related cache
				if (caches && caches.keys) {
					caches.keys().then(keys => {
						keys.forEach(key => {
							if (key.includes('facepunch') || key.includes('twitch.facepunch')) {
								caches.delete(key);
							}
						});
					});
				}
			""")
		except Exception:
			pass
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

async def is_general_item_claimed_on_inventory(inv_page, item_name: str) -> bool | None:
	"""Best-effort check if a general drop item appears claimed on the inventory page.

	Looks for the item name nearby a time/claimed label like 'x days ago', 'yesterday', or text containing 'claimed'.
	Assumes caller has already navigated to the inventory page.
	"""
	name_lower = (item_name or '').strip().lower()
	if not name_lower:
		return None
	try:
		await inv_page.wait_for_timeout(200)
		return await inv_page.evaluate(
			r"""
			(args) => {
			  const needle = (args && args.itemLower) || '';
			  if (!needle) return null;
			  const isTimeOrClaim = (s) => {
			    if (!s) return false;
			    const t = s.trim().toLowerCase();
			    if (t.includes('claimed')) return true;
			    if (t.includes('yesterday')) return true;
			    if (t.includes('last month')) return true;
			    if (/\bminutes?\s+ago\b/.test(t)) return true;
			    if (/\bhours?\s+ago\b/.test(t)) return true;
			    if (/\bdays?\s+ago\b/.test(t)) return true;
			    if (/\bmonths?\s+ago\b/.test(t)) return true;
			    if (/\byears?\s+ago\b/.test(t)) return true;
			    return false;
			  };
			  const all = Array.from(document.querySelectorAll('p, span, div, a'));
			  const nameEls = all.filter(el => {
			    const txt = (el.textContent || '').toLowerCase();
			    return txt && txt.includes(needle);
			  }).slice(0, 40);
			  const upDepth = 6;
			  for (const el of nameEls) {
			    let node = el;
			    for (let d = 0; d < upDepth && node; d++, node = node.parentElement) {
			      const timeEl = Array.from(node.querySelectorAll('p, span, div'))
			        .find(e => isTimeOrClaim(e.textContent || ''));
			      if (timeEl) return true;
			      // Also detect disabled Awarded button within the same card
			      const btn = Array.from(node.querySelectorAll('button[aria-label], button[disabled]'))
			        .find(b => {
			          const al = (b.getAttribute('aria-label') || '').toLowerCase();
			          const tc = (b.textContent || '').toLowerCase();
			          const disabled = b.hasAttribute('disabled');
			          return disabled && (al.includes('awarded') || tc.includes('awarded'));
			        });
			      if (btn) return true;
			    }
			  }
			  return null;
			}
			""",
			{"itemLower": name_lower}
		)
	except Exception:
		return None

async def are_all_general_drops_complete(inv_page, general_list) -> bool:
	try:
		if not general_list:
			return True
		# Ensure we are on inventory and scrape current progressbars once
		progress_map = await get_inventory_progress_map(inv_page)
		if progress_map is None:
			progress_map = {}
		def find_percent_for_any(needles: list[str]) -> int | None:
			cands = [n.strip().lower() for n in (needles or []) if n and n.strip()]
			if not cands:
				return None
			for title, pct in progress_map.items():
				t = (title or '').lower()
				if any(n in t for n in cands):
					return pct if isinstance(pct, int) else None
			return None
		for g in general_list:
			item_name = g.get('item') if isinstance(g, dict) else None
			alias = g.get('alias') if isinstance(g, dict) else None
			if not item_name and not alias:
				continue
			pct = find_percent_for_any([item_name or '', alias or ''])
			if isinstance(pct, int):
				if pct < 100:
					return False
				continue
			# Not found in progress map = no progress bar = completed
			# This means the item is either completed or not started yet
			# For general drops, if there's no progress bar, we consider it completed
			# (since general drops should always show a progress bar when active)
			continue
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
	# Parse command-line arguments
	args = parse_arguments()
	
	# Ensure process matches the user's console visibility preference before running
	try:
		ensure_process_visibility_matches_preference()
	except Exception:
		pass
	
	# On macOS, run tray in the main thread and the async app in a background thread
	if IS_MAC and pystray is not None and not args.no_tray:
		def _run_async_app():
			try:
				asyncio.run(main(start_tray=False, test_mode=args.test, enable_web=not args.no_web))
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
		except Exception as e:
			logging.debug(f"Tray main loop exited: {e}")
		# Request shutdown and wait briefly
		EXIT_EVENT.set()
		try:
			thread.join(timeout=5.0)
		except Exception:
			pass
	else:
		try:
			asyncio.run(main(start_tray=not args.no_tray, test_mode=args.test, enable_web=not args.no_web))
		except KeyboardInterrupt:
			pass
