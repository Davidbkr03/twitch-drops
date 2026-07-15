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
import time
from datetime import datetime, timezone, timedelta
import base64
import argparse
from urllib.parse import urlparse
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

from app.twitch_pages import (
	accept_mature_content_gate,
	ensure_live_video_playing,
	normalize_twitch_game_url as normalize_app_twitch_game_url,
	read_twitch_channel_metadata,
	twitch_channel_login_from_url,
	twitch_directories_match,
)

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
TWITCH_DROPS_ENABLED_DIRECTORY_URL = 'https://www.twitch.tv/directory/all/tags/dropsenabled'
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
STREAMER_MAPPINGS_PATH = os.path.join(BASE_DIR, 'streamer_mappings.json')
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
cached_games_data = {
	"games": [],
	"last_updated": None,
	"source": "twitch_directory",
	"error": None
}
cached_login_status = {
	"state": "starting",
	"logged_in": False,
	"message": "Starting browser session",
	"last_updated": None
}

# Integrity/header management
INTEGRITY_RENEW_REQUESTED = threading.Event()
GAMES_REFRESH_REQUESTED = threading.Event()
GAMES_CACHE_TTL_SECONDS = 10 * 60
games_data_lock = threading.RLock()
login_status_lock = threading.RLock()

def _now_ts() -> int:
	try:
		return int(time.time())
	except Exception:
		return 0

def set_login_status(state: str, logged_in: bool, message: str, extra: dict | None = None):
	"""Update login status cache for the web dashboard."""
	global cached_login_status
	with login_status_lock:
		payload = {
			"state": state,
			"logged_in": bool(logged_in),
			"message": message or "",
			"last_updated": datetime.now().isoformat()
		}
		if isinstance(extra, dict):
			payload.update(extra)
		cached_login_status = payload

def get_login_status_snapshot() -> dict:
	with login_status_lock:
		return dict(cached_login_status)

def update_cached_games_data(
	games: list[dict] | None = None,
	source: str = "twitch_directory",
	error: str | None = None,
	keep_existing_on_empty: bool = True
):
	"""Update game discovery cache and push to dashboard clients."""
	global cached_games_data
	with games_data_lock:
		if games is not None:
			if games or not keep_existing_on_empty or not cached_games_data.get("games"):
				cached_games_data["games"] = list(games)
			cached_games_data["source"] = source
		cached_games_data["error"] = error
		cached_games_data["last_updated"] = datetime.now().isoformat()
		payload = dict(cached_games_data)
	if socketio:
		try:
			socketio.emit('games_update', payload)
		except Exception:
			pass

def get_cached_games_data_snapshot() -> dict:
	with games_data_lock:
		return {
			"games": list(cached_games_data.get("games", [])),
			"last_updated": cached_games_data.get("last_updated"),
			"source": cached_games_data.get("source"),
			"error": cached_games_data.get("error")
		}

def should_refresh_games_cache(force: bool = False) -> bool:
	if force:
		return True
	with games_data_lock:
		last_updated = cached_games_data.get("last_updated")
	if not last_updated:
		return True
	try:
		last_dt = datetime.fromisoformat(last_updated)
		return (datetime.now() - last_dt).total_seconds() >= GAMES_CACHE_TTL_SECONDS
	except Exception:
		return True

def derive_game_key(game_url: str | None = None, game_name: str | None = None) -> str:
	raw_url = (game_url or "").strip()
	if raw_url:
		try:
			parsed = urlparse(raw_url if raw_url.startswith("http") else f"https://www.twitch.tv{raw_url}")
			parts = [p.lower() for p in parsed.path.split('/') if p]
			for marker in ("category", "game"):
				if marker in parts:
					idx = parts.index(marker)
					if idx + 1 < len(parts):
						return parts[idx + 1]
			if parts:
				return parts[-1]
		except Exception:
			pass
	name_norm = _normalize_match_text(game_name or "")
	if not name_norm:
		return ""
	key = re.sub(r"[^a-z0-9]+", "-", name_norm).strip("-")
	return key

def _sanitize_watch_preferences(raw: dict | None) -> dict:
	clean = {"games": {}}
	if not isinstance(raw, dict):
		return clean
	games = raw.get("games", {})
	if not isinstance(games, dict):
		return clean
	for input_key, entry in games.items():
		if not isinstance(entry, dict):
			continue
		game_name = (entry.get("game") or "").strip()
		game_url = (entry.get("game_url") or "").strip()
		game_key = derive_game_key(game_url, game_name) or _normalize_match_text(str(input_key))
		if not game_key:
			continue
		streamers_raw = entry.get("streamers", {})
		streamers = {}
		if isinstance(streamers_raw, dict):
			for streamer, enabled in streamers_raw.items():
				if not bool(enabled):
					continue
				login = _extract_channel_login(str(streamer)) or _compact_match_text(str(streamer))
				if login:
					streamers[login] = True
		clean["games"][game_key] = {
			"game": game_name,
			"game_url": game_url,
			"enabled": bool(entry.get("enabled", False)),
			"streamers": streamers
		}
	return clean

def get_watch_preferences_snapshot() -> dict:
	with CONFIG_LOCK:
		if isinstance(PREFERENCES, dict):
			raw = PREFERENCES.get("watch_preferences", {"games": {}})
		else:
			raw = {"games": {}}
		if not isinstance(raw, dict):
			raw = {"games": {}}
	return _sanitize_watch_preferences(raw)

def update_watch_preferences(new_preferences: dict) -> dict:
	clean = _sanitize_watch_preferences(new_preferences)
	with CONFIG_LOCK:
		PREFERENCES["watch_preferences"] = clean
		save_preferences(PREFERENCES)
	return clean

def upsert_watch_preference_game(game_name: str, game_url: str, enabled: bool | None = None, streamers: dict | None = None) -> dict:
	game_key = derive_game_key(game_url, game_name)
	if not game_key:
		return get_watch_preferences_snapshot()
	with CONFIG_LOCK:
		current = _sanitize_watch_preferences(PREFERENCES.get("watch_preferences", {"games": {}}))
		entry = current["games"].get(game_key, {
			"game": game_name or "",
			"game_url": game_url or "",
			"enabled": False,
			"streamers": {}
		})
		if game_name:
			entry["game"] = game_name
		if game_url:
			entry["game_url"] = game_url
		if enabled is not None:
			entry["enabled"] = bool(enabled)
		if isinstance(streamers, dict):
			entry["streamers"] = {k: bool(v) for k, v in streamers.items() if bool(v)}
		current["games"][game_key] = entry
		PREFERENCES["watch_preferences"] = current
		save_preferences(PREFERENCES)
	return get_watch_preferences_snapshot()

def get_enabled_game_preferences(watch_preferences: dict | None = None) -> list[dict]:
	prefs = watch_preferences or get_watch_preferences_snapshot()
	out = []
	for key, game in (prefs.get("games", {}) or {}).items():
		if not isinstance(game, dict):
			continue
		if game.get("enabled"):
			entry = dict(game)
			entry["game_key"] = key
			out.append(entry)
	return out

def is_rust_game_preference(game_entry: dict) -> bool:
	if not isinstance(game_entry, dict):
		return False
	key = (game_entry.get("game_key") or derive_game_key(game_entry.get("game_url"), game_entry.get("game")) or "").lower()
	name = (game_entry.get("game") or "").lower()
	return key == "rust" or name == "rust"

def is_streamer_allowed_for_game_preference(game_entry: dict, streamer_name: str, streamer_url: str | None = None) -> bool:
	"""If no specific streamers selected, all streamers are allowed."""
	if not isinstance(game_entry, dict):
		return True
	streamers = game_entry.get("streamers", {})
	if not isinstance(streamers, dict) or not streamers:
		return True
	selected_streamers = [s for s, enabled in streamers.items() if bool(enabled)]
	if not selected_streamers:
		return True
	candidate_login = _extract_channel_login(streamer_url) or _compact_match_text(streamer_name)
	if not candidate_login:
		return False
	return any(
		candidate_login == (_extract_channel_login(selected) or _compact_match_text(selected))
		for selected in selected_streamers
	)

def get_integrity_prefs():
	try:
		return {
			"headers": dict(PREFERENCES.get("integrity_headers", {})),
			"fetched_at": int(PREFERENCES.get("integrity_fetched_at", 0)),
			"ttl_hours": int(PREFERENCES.get("integrity_ttl_hours", 6)),
			"auto_renew": bool(PREFERENCES.get("integrity_auto_renew", True)),
		}
	except Exception:
		return {"headers": {}, "fetched_at": 0, "ttl_hours": 6, "auto_renew": True}

def is_integrity_valid() -> bool:
	try:
		ip = get_integrity_prefs()
		if not ip["headers"] or not ip["headers"].get("Client-Integrity"):
			return False
		age = max(0, _now_ts() - int(ip["fetched_at"]))
		return age < max(1, ip["ttl_hours"]) * 3600
	except Exception:
		return False

def save_integrity_headers(headers: dict):
	try:
		with CONFIG_LOCK:
			PREFERENCES["integrity_headers"] = headers or {}
			PREFERENCES["integrity_fetched_at"] = _now_ts()
			if "integrity_ttl_hours" not in PREFERENCES:
				PREFERENCES["integrity_ttl_hours"] = 6
			threading.Thread(target=lambda: save_preferences(PREFERENCES), daemon=True).start()
	except Exception as e:
		logging.debug(f"Failed saving integrity headers: {e}")

async def fetch_integrity_headers_with_headed(p) -> dict:
	"""Launch a temporary headed context using same user data dir and capture Client-Integrity from gql calls."""
	logging.info("Fetching integrity headers in headed mode…")
	ctx = None
	page = None
	captured = {}
	try:
		ctx = await p.chromium.launch_persistent_context(
			USER_DATA_DIR,
			headless=False,
			channel=BROWSER_CHANNEL,
			slow_mo=50,
			viewport={"width": 1200, "height": 768},
			locale="en-US",
		)
		await apply_stealth_to_context(ctx, profile=("off" if STEALTH_PROFILE == "off" else STEALTH_PROFILE))
		try:
			await apply_additional_stealth(ctx)
		except Exception:
			pass

		page = await ctx.new_page()
		def _maybe_capture_from_headers(h):
			try:
				if not h:
					return False
				lower = {k.lower(): v for k, v in h.items()}
				ci = lower.get("client-integrity")
				if ci:
					captured.update({k: v for k, v in h.items() if k.lower() in (
						"client-integrity", "client-session-id", "x-device-id", "client-id"
					)})
					logging.info("Captured Client-Integrity header")
					return True
				return False
			except Exception:
				return False

		def on_request(req):
			try:
				if "gql.twitch.tv" in req.url:
					if _maybe_capture_from_headers(req.headers):
						return
			except Exception:
				pass

		def on_response(resp):
			try:
				if "gql.twitch.tv" in resp.url:
					# Some frameworks expose request headers via resp.request
					if _maybe_capture_from_headers(getattr(resp.request, 'headers', lambda: {})() if hasattr(resp.request, 'headers') else {}):
						return
					# Also try raw response headers just in case
					_ = resp.headers
			except Exception:
				pass
		page.on("request", on_request)
		page.on("response", on_response)
		await goto_with_exit(page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(page)
		# Provoke some network activity that triggers gql
		try:
			await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
			await page.wait_for_timeout(400)
			await page.evaluate("window.scrollTo(0, 0)")
		except Exception:
			pass
		# Also visit home which triggers many gql calls
		try:
			await goto_with_exit(page, "https://www.twitch.tv/", timeout=60000, wait_until="domcontentloaded")
			await page.wait_for_timeout(800)
		except Exception:
			pass
		# Wait up to ~20s for a gql request with integrity
		for _ in range(40):
			if captured:
				break
			await page.wait_for_timeout(500)
		# Save UA from headed session for reuse in headless
		try:
			ua = await page.evaluate("navigator.userAgent")
			if ua and "HeadlessChrome" not in ua:
				with CONFIG_LOCK:
					PREFERENCES["forced_user_agent"] = ua
				threading.Thread(target=lambda: save_preferences(PREFERENCES), daemon=True).start()
				logging.info("Saved headed user agent for reuse")
		except Exception:
			pass
	finally:
		try:
			if page:
				await page.close()
		except Exception:
			pass
		try:
			if ctx:
				await ctx.close()
		except Exception:
			pass
	return captured

async def apply_integrity_headers_to_context(context):
    # No-op: integrity-based claiming removed per user request
    return
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
		except Exception:
			pass
		
		# Default version if nothing else works
		return "1.0.0"
	except Exception:
		return "1.0.0"

def get_current_commit_hash():
	"""Get the current git commit hash."""
	try:
		import subprocess
		result = subprocess.run(['git', 'rev-parse', 'HEAD'], 
							  capture_output=True, text=True, cwd=BASE_DIR, timeout=5)
		if result.returncode == 0:
			return result.stdout.strip()[:8]  # Return short hash
	except Exception:
		pass
	
	# Fallback: try to get from git log
	try:
		import subprocess
		result = subprocess.run(['git', 'log', '-1', '--format=%H'], 
							  capture_output=True, text=True, cwd=BASE_DIR, timeout=5)
		if result.returncode == 0:
			return result.stdout.strip()[:8]
	except Exception:
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
	except Exception:
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
		except Exception:
			return 0

def update_current_working_item(item_info):
	"""Update the current working item for the web interface."""
	global current_working_item
	
	with current_working_lock:
		current_working_item = item_info
		logging.debug(f"Updated current working item: {item_info}")

def _normalize_match_text(value: str) -> str:
	text = (value or "").strip().lower()
	if not text:
		return ""
	text = text.replace("_", " ").replace("-", " ")
	text = re.sub(r"https?://(www\.)?twitch\.tv/", "", text)
	text = re.sub(r"[^a-z0-9\s]", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text

def _compact_match_text(value: str) -> str:
	return _normalize_match_text(value).replace(" ", "")

def _tokenize_match_text(value: str) -> set[str]:
	return {tok for tok in _normalize_match_text(value).split(" ") if len(tok) >= 3}

def _extract_channel_login(url: str | None) -> str | None:
	return twitch_channel_login_from_url(url)

def _absolutize_twitch_href(href: str | None) -> str:
	link = (href or "").strip()
	if not link:
		return ""
	return f"https://www.twitch.tv{link}" if link.startswith("/") else link

def _contains_variation(title_norm: str, title_compact: str, variation: str) -> bool:
	var_norm = _normalize_match_text(variation)
	if not var_norm:
		return False
	var_compact = var_norm.replace(" ", "")
	if len(var_norm) >= 3 and f" {var_norm} " in f" {title_norm} ":
		return True
	if len(var_norm) >= 4 and var_norm in title_norm:
		return True
	if len(var_compact) >= 4 and var_compact in title_compact:
		return True
	return False

def is_streamer_name_match(streamer_name: str, candidate_name: str, streamer_url: str | None = None) -> bool:
	"""Flexible name matcher for Facepunch/Twitch naming differences."""
	candidate_norm = _normalize_match_text(candidate_name)
	candidate_compact = candidate_norm.replace(" ", "")
	if not candidate_norm:
		return False
	variations = generate_search_variations(streamer_name)
	channel_login = _extract_channel_login(streamer_url)
	if channel_login:
		variations.append(channel_login)
	seen = set()
	for variation in variations:
		key = _normalize_match_text(variation)
		if not key or key in seen:
			continue
		seen.add(key)
		if _contains_variation(candidate_norm, candidate_compact, variation):
			return True
	# Token overlap fallback
	streamer_tokens = set()
	for variation in seen:
		streamer_tokens.update(_tokenize_match_text(variation))
	if channel_login:
		streamer_tokens.update(_tokenize_match_text(channel_login))
	if streamer_tokens and (streamer_tokens & _tokenize_match_text(candidate_name)):
		return True
	return False

def find_recently_claimed_match(streamer_name: str, recently_claimed_items: list[dict] | None, streamer_url: str | None = None) -> dict | None:
	for item in recently_claimed_items or []:
		claimed_name = item.get("name", "")
		if not claimed_name:
			continue
		if is_streamer_name_match(streamer_name, claimed_name, streamer_url=streamer_url):
			return item
	return None

def match_streamer_drop_progress(drop: dict, inventory_progress: dict, used_titles: set[str] | None = None) -> tuple[int | None, str | None, int]:
	"""Score inventory titles using streamer + item (+ channel URL) to avoid collisions."""
	streamer_name = drop.get("streamer", "") if isinstance(drop, dict) else ""
	item_name = drop.get("item", "") if isinstance(drop, dict) else ""
	streamer_url = drop.get("url", "") if isinstance(drop, dict) else ""
	if not streamer_name and not item_name:
		return None, None, 0
	channel_login = _extract_channel_login(streamer_url)
	streamer_variations = generate_search_variations(streamer_name)
	if channel_login:
		streamer_variations.append(channel_login)
	item_variations = generate_search_variations(item_name)
	title_scores: list[tuple[int, str, int]] = []
	used = used_titles or set()
	for title, percent in (inventory_progress or {}).items():
		if not isinstance(title, str) or not isinstance(percent, int):
			continue
		title_norm = _normalize_match_text(title)
		title_compact = title_norm.replace(" ", "")
		score = 0
		# Strong signal: channel login from Facepunch twitch URL
		if channel_login and _contains_variation(title_norm, title_compact, channel_login):
			score += 70
		# Streamer text matching
		for variation in streamer_variations:
			if _contains_variation(title_norm, title_compact, variation):
				score += 42
				break
		streamer_tokens = set()
		for variation in streamer_variations:
			streamer_tokens.update(_tokenize_match_text(variation))
		streamer_overlap = len(streamer_tokens & _tokenize_match_text(title))
		score += min(24, streamer_overlap * 8)
		# Item text matching helps disambiguate multiple drops for one streamer
		for variation in item_variations:
			if _contains_variation(title_norm, title_compact, variation):
				score += 40
				break
		item_overlap = len(_tokenize_match_text(item_name) & _tokenize_match_text(title))
		score += min(24, item_overlap * 8)
		if title in used:
			score -= 20
		if score > 0:
			title_scores.append((score, title, percent))
	if not title_scores:
		return None, None, 0
	title_scores.sort(key=lambda s: (-s[0], s[1]))
	best_score, best_title, best_percent = title_scores[0]
	if best_score < 40:
		return None, None, best_score
	return best_percent, best_title, best_score


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


def intelligent_streamer_matching(streamer_name, inventory_progress):
	"""
	Intelligent matching for streamer names that don't exactly match inventory titles.
	Legacy wrapper for compatibility with existing call sites.
	"""
	progress, title, _ = match_streamer_drop_progress(
		{"streamer": streamer_name, "item": "", "url": ""},
		inventory_progress,
	)
	return progress, title


def update_cached_drops_data(facepunch_data, inventory_progress, recently_claimed_streamers=None, general_progress_map=None):
	"""Update the cached drops data for the web interface."""
	global cached_drops_data
	
	with drops_data_lock:
		# If this is a partial update (only progress data), merge with existing cache
		if facepunch_data is None and inventory_progress:
			# Update existing cache with new progress data
			if cached_drops_data and cached_drops_data.get("in_progress"):
				for drop in cached_drops_data["in_progress"]:
					if drop.get("type") == "streamer":
						match_progress, match_title, _ = match_streamer_drop_progress(drop, inventory_progress)
						if match_progress is not None:
							drop["progress"] = match_progress
							drop["progress_title"] = match_title
						continue
					# Update progress for matching general items
					for title, percent in inventory_progress.items():
						if drop.get("type") == "general" and drop.get("item"):
							item_lower = drop["item"].lower()
							title_lower = title.lower()
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
		used_progress_titles = set()
		for drop in streamer_drops:
			streamer_name = drop.get('streamer', '')
			item_name = drop.get('item', '')
			hours = drop.get('hours', 0)
			is_live = drop.get('is_live', False)
			url = drop.get('url', '')
			
			progress, progress_title, match_score = match_streamer_drop_progress(
				drop,
				inventory_progress,
				used_titles=used_progress_titles
			)
			if progress_title:
				used_progress_titles.add(progress_title)
				logging.info(
					f"[STREAMER-MATCH] '{streamer_name}' / '{item_name}' -> '{progress_title}' "
					f"({progress}%, score={match_score})"
				)
			else:
				logging.info(f"[STREAMER-MATCH] No progress title matched for '{streamer_name}' / '{item_name}'")
			
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
				# Check if this streamer was recently claimed before marking as not_started
				claimed_match = find_recently_claimed_match(streamer_name, recently_claimed_streamers, streamer_url=url)
				streamer_claimed = bool(claimed_match)
				if streamer_claimed:
					logging.info(
						f"[DROPS-CATEGORIZATION] '{streamer_name}' treated as completed from claimed history "
						f"('{claimed_match.get('name')}', {claimed_match.get('days')} day(s) ago)"
					)
				
				if streamer_claimed:
					# Mark as completed since it was recently claimed
					drop_info["ready_to_claim"] = False  # Already claimed
					drops_data["completed"].append(drop_info)
					logging.info(f"[DROPS-CATEGORIZATION] Added '{streamer_name}' to completed section")
				else:
					drops_data["not_started"].append(drop_info)
					logging.info(f"[DROPS-CATEGORIZATION] Added '{streamer_name}' to not_started section")
			elif progress >= 100:
				# Mark as completed but require manual claim
				drop_info["ready_to_claim"] = True
				drops_data["completed"].append(drop_info)
			else:
				drops_data["in_progress"].append(drop_info)
		
		# Process general drops - use general drops area only
		general_drops = facepunch_data.get('general', []) if facepunch_data else []
		
		# Use the provided general progress map, or fall back to empty dict
		general_progress_map = general_progress_map or {}
		if general_drops and general_progress_map:
			logging.info(f"[GENERAL-DROPS-PROCESSING] Using general drops progress map with {len(general_progress_map)} items")
		
		for drop in general_drops:
			item_name = drop.get('item', '')
			hours = drop.get('hours', 0)
			alias = drop.get('alias', '')
			
			# Find matching inventory progress (only in general drops area)
			progress = None
			progress_title = None
			search_terms = [item_name]
			if alias:
				search_terms.append(alias)
			
			for title, percent in general_progress_map.items():
				for term in search_terms:
					# Use word boundaries to avoid partial matches (e.g., "fridge" shouldn't match "Abe Fridge")
					if re.search(r'\b' + re.escape(term.lower()) + r'\b', title.lower()):
						progress = percent
						progress_title = title
						break
				if progress is not None:
					break
			
			# If no exact match found, try a more flexible search
			if progress is None:
				for title, percent in general_progress_map.items():
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
				progress, progress_title = intelligent_item_matching(item_name, general_progress_map)
				if progress is not None:
					logging.info(f"Successfully matched '{item_name}' to '{progress_title}' with {progress}% progress")
			
			# Debug logging for unmatched items
			if progress is None and item_name:
				logging.info(f"Could not find progress for general drop: '{item_name}' (alias: '{alias}')")
				logging.info(f"Available general drops titles: {list(general_progress_map.keys())}")
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
				# Mark as completed but require manual claim
				drop_info["ready_to_claim"] = True
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
		"headless": False, 
		"hide_console": True,
		"test_mode": False,
		"debug_mode": False,
		"enable_web_interface": True,
		"watch_preferences": {"games": {}}
	}
	try:
		if os.path.exists(CONFIG_PATH):
			with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
				data = json.load(f)
				if isinstance(data, dict):
					default_prefs.update(data)
					default_prefs["watch_preferences"] = _sanitize_watch_preferences(default_prefs.get("watch_preferences"))
	except Exception as e:
		logging.debug(f"Could not load preferences: {e}")
	return default_prefs


def load_streamer_mappings():
	"""Load streamer name mappings from the separate mappings file."""
	try:
		if os.path.exists(STREAMER_MAPPINGS_PATH):
			with open(STREAMER_MAPPINGS_PATH, 'r', encoding='utf-8') as f:
				data = json.load(f)
				if isinstance(data, dict):
					return data
	except Exception as e:
		logging.debug(f"Could not load streamer mappings: {e}")
	return {}


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
		pystray.MenuItem("Headless mode", on_toggle_headless, checked=is_headless_checked),
		pystray.MenuItem("Hide console on startup", on_toggle_hide_console, checked=is_hide_console_checked),
		pystray.Menu.SEPARATOR,
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
		watch_prefs = get_watch_preferences_snapshot()
		enabled_games = get_enabled_game_preferences(watch_prefs)
		return jsonify({
			'status': 'running',
			'headless': PREFERENCES.get('headless', DEFAULT_HEADLESS) if PREFERENCES else DEFAULT_HEADLESS,
			'test_mode': PREFERENCES.get('test_mode', False) if PREFERENCES else False,
			'debug_mode': PREFERENCES.get('debug_mode', False) if PREFERENCES else False,
			'enable_web_interface': PREFERENCES.get('enable_web_interface', True) if PREFERENCES else True,
			'integrity_valid': is_integrity_valid(),
			'integrity_age_seconds': max(0, _now_ts() - int(PREFERENCES.get('integrity_fetched_at', 0))) if PREFERENCES else 0,
			'login_state': get_login_status_snapshot().get('state'),
			'logged_in': bool(get_login_status_snapshot().get('logged_in')),
			'watch_selection_active': bool(enabled_games),
			'watch_selected_games_count': len(enabled_games),
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
				'integrity_auto_renew': PREFERENCES.get('integrity_auto_renew', True) if PREFERENCES else True,
				'integrity_ttl_hours': PREFERENCES.get('integrity_ttl_hours', 6) if PREFERENCES else 6,
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
					if 'integrity_auto_renew' in data:
						PREFERENCES['integrity_auto_renew'] = bool(data['integrity_auto_renew'])
					if 'integrity_ttl_hours' in data:
						PREFERENCES['integrity_ttl_hours'] = int(data['integrity_ttl_hours'])
					
					# Save preferences
					save_preferences(PREFERENCES)
					
				return jsonify({'success': True, 'message': 'Settings updated successfully'})
			except Exception as e:
				return jsonify({'success': False, 'message': f'Error updating settings: {str(e)}'}), 500

	@app.route('/api/integrity', methods=['GET'])
	def api_integrity_status():
		ip = get_integrity_prefs()
		return jsonify({
			'valid': is_integrity_valid(),
			'headers_present': bool(ip['headers']),
			'age_seconds': max(0, _now_ts() - int(ip['fetched_at'])),
			'ttl_hours': ip['ttl_hours'],
			'auto_renew': ip['auto_renew']
		})

	@app.route('/api/integrity/renew', methods=['POST'])
	def api_integrity_renew():
		try:
			from playwright.async_api import async_playwright
			async def _renew():
				async with async_playwright() as p:
					captured = await fetch_integrity_headers_with_headed(p)
					if captured:
						save_integrity_headers(captured)
						return True
					return False
			loop = asyncio.new_event_loop()
			ok = loop.run_until_complete(_renew())
			loop.close()
			return jsonify({'success': bool(ok)})
		except Exception as e:
			return jsonify({'success': False, 'message': str(e)}), 500
	
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

	@app.route('/api/games', methods=['GET'])
	def api_games():
		"""Get cached list of drop-enabled games."""
		try:
			return jsonify(get_cached_games_data_snapshot())
		except Exception as e:
			return jsonify({'error': str(e)}), 500

	@app.route('/api/games/streamers', methods=['GET'])
	def api_game_streamers():
		"""Fetch live drop-enabled streamers for a specific game category URL."""
		game_url = (request.args.get("game_url") or "").strip()
		if not game_url:
			return jsonify({'success': False, 'message': 'Missing game_url'}), 400
		try:
			game_url = normalize_app_twitch_game_url(game_url)
		except ValueError as exc:
			return jsonify({'success': False, 'message': str(exc)}), 400
		try:
			async def _refresh():
				return await fetch_game_streamers_public(game_url, limit=100)
			loop = asyncio.new_event_loop()
			try:
				streamers = loop.run_until_complete(_refresh())
			finally:
				loop.close()
			return jsonify({
				"success": True,
				"game_url": game_url,
				"count": len(streamers),
				"streamers": streamers
			})
		except Exception as e:
			return jsonify({'success': False, 'message': str(e)}), 500

	@app.route('/api/games/refresh', methods=['POST'])
	def api_games_refresh():
		"""Refresh drop-enabled games immediately."""
		try:
			async def _refresh():
				return await fetch_drops_enabled_games_public(limit=160)
			loop = asyncio.new_event_loop()
			try:
				games = loop.run_until_complete(_refresh())
			finally:
				loop.close()
			update_cached_games_data(games=games, source="twitch_directory", error=None)
			# Also ask the workflow loop to refresh with the signed-in context later.
			GAMES_REFRESH_REQUESTED.set()
			return jsonify({
				"success": True,
				"count": len(games),
				"games": games,
				"last_updated": datetime.now().isoformat()
			})
		except Exception as e:
			update_cached_games_data(games=None, source="twitch_directory", error=str(e))
			return jsonify({'success': False, 'message': str(e)}), 500

	@app.route('/api/login-status', methods=['GET'])
	def api_login_status():
		"""Expose login-helper status for the dashboard."""
		try:
			data = get_login_status_snapshot()
			data["headless"] = PREFERENCES.get('headless', DEFAULT_HEADLESS) if PREFERENCES else DEFAULT_HEADLESS
			data["test_mode"] = PREFERENCES.get('test_mode', False) if PREFERENCES else False
			data["needs_visible_browser"] = bool(data.get("headless"))
			return jsonify(data)
		except Exception as e:
			return jsonify({'error': str(e)}), 500

	@app.route('/api/login/mode', methods=['POST'])
	def api_login_mode():
		"""Switch between guided-login mode and normal mode."""
		try:
			data = request.get_json(silent=True) or {}
			mode = (data.get('mode') or 'guided').strip().lower()
			with CONFIG_LOCK:
				if mode == 'guided':
					PREFERENCES['headless'] = False
					PREFERENCES['test_mode'] = True
					message = "Guided login mode enabled (visible browser + test mode)."
				elif mode == 'normal':
					PREFERENCES['headless'] = bool(data.get('headless', True))
					PREFERENCES['test_mode'] = bool(data.get('test_mode', False))
					message = "Normal mode settings restored."
				else:
					return jsonify({'success': False, 'message': f"Unsupported mode '{mode}'"}), 400
				save_preferences(PREFERENCES)
			set_login_status(
				state="pending_restart",
				logged_in=False,
				message=f"{message} Click restart to apply."
			)
			return jsonify({
				'success': True,
				'mode': mode,
				'headless': PREFERENCES.get('headless', DEFAULT_HEADLESS),
				'test_mode': PREFERENCES.get('test_mode', False),
				'message': message + " Restart required."
			})
		except Exception as e:
			return jsonify({'success': False, 'message': str(e)}), 500

	@app.route('/api/watch-preferences', methods=['GET', 'POST'])
	def api_watch_preferences():
		"""Get or update selected games and streamer toggles."""
		if request.method == 'GET':
			try:
				return jsonify(get_watch_preferences_snapshot())
			except Exception as e:
				return jsonify({'error': str(e)}), 500
		try:
			data = request.get_json(silent=True) or {}
			updated = update_watch_preferences(data)
			return jsonify({'success': True, 'watch_preferences': updated})
		except Exception as e:
			return jsonify({'success': False, 'message': str(e)}), 500

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

async def apply_additional_stealth(context):
	"""Minimal safe extra evasion to avoid syntax issues."""
	try:
		await context.add_init_script(r"""
		(() => {
		  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
		})();
		""")
	except Exception:
		pass

async def maybe_accept_cookies(page):
	try:
		btn = await page.query_selector('#onetrust-accept-btn-handler')
		if btn:
			await click_ui_element(
				page, 
				'#onetrust-accept-btn-handler', 
				"OneTrust cookie accept button", 
				timeout=2000, 
				wait_after_click=0.5
			)
			logging.info("Accepted OneTrust cookies banner")
	except Exception:
		pass


def _cleanup_stale_browser_profile_locks(user_data_dir: str) -> list[str]:
	"""Best-effort cleanup of Chrome profile locks left by an unclean exit.

	An active Chrome process keeps these files locked on Windows, so failed
	deletions are intentionally ignored. The returned names make the behavior
	easy to verify without coupling callers to filesystem details.
	"""
	removed = []
	try:
		if not os.path.isdir(user_data_dir):
			return removed
		for name in os.listdir(user_data_dir):
			if not name.startswith("Singleton"):
				continue
			path = os.path.join(user_data_dir, name)
			try:
				os.remove(path)
				removed.append(name)
			except OSError as e:
				logging.debug(f"Browser profile lock still active or unavailable ({name}): {e}")
	except OSError as e:
		logging.debug(f"Could not inspect browser profile locks: {e}")
	if removed:
		logging.info(f"Removed stale browser profile locks: {', '.join(sorted(removed))}")
	return removed


async def launch_context(p, compat_mode: bool):
	headless_pref = get_headless_preference()
	_cleanup_stale_browser_profile_locks(USER_DATA_DIR)
    # Integrity capture removed per user request
	args = [
		"--disable-extensions",
		"--disable-features=BlockThirdPartyCookies,CookieDeprecationMessages",
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
		"--disable-background-networking",
		"--force-device-scale-factor=1",
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
		# Human-like behavior arguments
		"--enable-features=NetworkService,NetworkServiceLogging",
		"--disable-dev-shm-usage",
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-default-apps",
		"--disable-extensions-except",
		"--disable-extensions-file-access-check",
		"--disable-extensions-http-throttling",
		"--disable-popup-blocking",
		"--disable-field-trial-config",
		"--disable-back-forward-cache",
		"--disable-background-media-download",
	]

	# Prefer a previously captured headed user agent to avoid "HeadlessChrome" token
	try:
		with CONFIG_LOCK:
			ua_saved = PREFERENCES.get("forced_user_agent")
	except Exception:
		ua_saved = None
	# In headless, enable GPU/WebGL related features and remove conflicting disables
	if headless_pref:
		args = [a for a in args if a not in ("--disable-gpu-sandbox", "--disable-features=VizDisplayCompositor")]
		args.extend([
			"--enable-webgl",
			"--ignore-gpu-blocklist",
			"--use-gl=angle",
			"--use-angle=d3d11",
			"--enable-accelerated-2d-canvas",
			"--canvas-msaa-sample-count=4",
			"--enable-gpu-rasterization",
		])
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
			headless=headless_pref,
			executable_path=chrome_exec,
			slow_mo=50,
			ignore_default_args=ignore_default_args,
			args=args,
			user_agent=(FORCE_USER_AGENT or ua_saved) if (FORCE_USER_AGENT or ua_saved) else None,
			viewport={"width": 1366, "height": 768},
			locale="en-US",
		)
	else:
		# Other platforms: try preferred channel, then fall back to default Chromium
		try:
			context = await p.chromium.launch_persistent_context(
				USER_DATA_DIR,
				headless=headless_pref,
				channel=BROWSER_CHANNEL,
				slow_mo=50,
				ignore_default_args=ignore_default_args,
				args=args,
				user_agent=(FORCE_USER_AGENT or ua_saved) if (FORCE_USER_AGENT or ua_saved) else None,
				viewport={"width": 1366, "height": 768},
				locale="en-US",
			)
		except Exception as e:
			logging.warning(f"Primary browser channel '{BROWSER_CHANNEL}' failed ({e}). Falling back to default Chromium.")
			context = await p.chromium.launch_persistent_context(
				USER_DATA_DIR,
				headless=headless_pref,
				slow_mo=50,
				ignore_default_args=ignore_default_args,
				args=args,
				user_agent=(FORCE_USER_AGENT or ua_saved) if (FORCE_USER_AGENT or ua_saved) else None,
				viewport={"width": 1366, "height": 768},
				locale="en-US",
			)

	await apply_stealth_to_context(context, profile=("off" if compat_mode else STEALTH_PROFILE))
	try:
		await apply_additional_stealth(context)
	except Exception:
		pass
    # Integrity header application disabled per user request

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
		set_login_status("awaiting_login", False, "Waiting for Twitch login in browser")
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
					set_login_status("logged_in", True, "Logged in to Twitch", {"url": poll_page.url})
					return
				logging.info("Not logged in yet; still waiting…")
				set_login_status("awaiting_login", False, "Not logged in yet", {"url": poll_page.url})
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
		set_login_status("starting", False, "Launching browser and checking login")
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
				set_login_status("awaiting_login", False, "Twitch login required", {"url": page.url})
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
					set_login_status("logged_in", True, "Logged in to Twitch", {"url": page.url})
					return context
				else:
					logging.warning("Could not find user avatar with any selector. User may not be logged in.")
					raise Exception("No avatar selectors found")
					
			except Exception:
				logging.warning("Could not find user avatar. User may not be logged in. Waiting for user to complete login.")
				set_login_status("awaiting_login", False, "Waiting for manual Twitch login", {"url": page.url})
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
					set_login_status("logged_in", True, "Logged in to Twitch", {"url": page.url})
					return context
				else:
					raise Exception("Login verification failed - no avatar found after waiting")
		except Exception as e:
			set_login_status("error", False, f"Login flow failed: {e}")
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
		return {
			"general": general,
			"streamer": streamer_specific,
			"not_started": bool(not_started),
			"start_epoch_ms": start_epoch_ms,
			"fetch_failed": False,
		}
	except Exception as e:
		logging.warning(f"Facepunch parsing failed: {e}")
		return {
			"general": [],
			"streamer": [],
			"not_started": False,
			"start_epoch_ms": None,
			"fetch_failed": True,
			"fetch_error": str(e),
		}
	finally:
		try:
			await page.close()
		except Exception:
			pass

async def _extract_drop_games_from_directory_page(page, limit: int = 120) -> list[dict]:
	await goto_with_exit(page, TWITCH_DROPS_ENABLED_DIRECTORY_URL, timeout=120000, wait_until="domcontentloaded")
	await maybe_accept_cookies(page)
	await page.wait_for_timeout(1200)
	raw_rows = await page.evaluate(
		r"""
		(args) => {
		  const maxCards = (args && args.limit) || 120;
		  const rows = [];
		  const cards = Array.from(document.querySelectorAll('article')).slice(0, maxCards);
		  for (const card of cards) {
			const tags = Array.from(card.querySelectorAll('[aria-label^="Tag, "], [data-a-target="tag"], a[href*="/directory/all/tags/"]'))
			  .flatMap(n => [
				(n.textContent || ''),
				(n.getAttribute('aria-label') || '').replace(/^Tag,\s*/i, '')
			  ])
			  .map(value => value.replace(/[^a-z0-9]/gi, '').toLowerCase())
			  .filter(Boolean);
			const hasDrops = tags.some(t => t === 'dropsenabled');
			if (!hasDrops) continue;
			const gameAnchor = card.querySelector('a[data-a-target="preview-card-game-link"], a[href*="/directory/category/"], a[href*="/directory/game/"]');
			const game = (gameAnchor?.textContent || '').trim();
			if (!game) continue;
			const gameHref = (gameAnchor?.getAttribute('href') || '').trim();
			const streamerAnchor = card.querySelector('a[data-a-target="preview-card-title-link"], a[href^="/"]');
			const streamerHref = (streamerAnchor?.getAttribute('href') || '').trim();
			const viewerNode = card.querySelector('[data-a-target="animated-channel-viewers-count"], [data-a-target*="viewers"]');
			const viewersText = (viewerNode?.textContent || '').trim();
			rows.push({
			  game,
			  game_url: gameHref,
			  streamer_url: streamerHref,
			  viewers_text: viewersText
			});
		  }
		  return rows;
		}
		""",
		{"limit": int(limit)}
	)
	aggregated = {}
	for row in raw_rows or []:
		game_name = (row.get("game") or "").strip()
		if not game_name:
			continue
		game_url = _absolutize_twitch_href(row.get("game_url"))
		streamer_login = _extract_channel_login(row.get("streamer_url"))
		key = game_url or _normalize_match_text(game_name)
		if not key:
			continue
		entry = aggregated.setdefault(key, {
			"game": game_name,
			"game_url": game_url,
			"active_channels": 0,
			"sample_streamers": [],
			"viewer_samples": []
		})
		entry["active_channels"] += 1
		if streamer_login and streamer_login not in entry["sample_streamers"] and len(entry["sample_streamers"]) < 4:
			entry["sample_streamers"].append(streamer_login)
		viewer_text = (row.get("viewers_text") or "").strip()
		if viewer_text and viewer_text not in entry["viewer_samples"] and len(entry["viewer_samples"]) < 3:
			entry["viewer_samples"].append(viewer_text)
	games = sorted(
		aggregated.values(),
		key=lambda g: (-(g.get("active_channels") or 0), _normalize_match_text(g.get("game") or ""))
	)
	return games

def _normalize_game_directory_url(game_url: str) -> str:
	url = (game_url or "").strip()
	if not url:
		return ""
	if not url.startswith(("http://", "https://", "/")):
		url = f"/directory/category/{url.strip('/')}"
	try:
		return normalize_app_twitch_game_url(url)
	except ValueError:
		return ""

def _viewer_count_score(viewers_text: str) -> int:
	text = (viewers_text or "").strip().lower().replace(",", "")
	if not text:
		return 0
	try:
		m = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
		if not m:
			return 0
		value = float(m.group(1))
		scale = m.group(2)
		if scale == "k":
			value *= 1000
		elif scale == "m":
			value *= 1000000
		return int(value)
	except Exception:
		return 0

async def _extract_live_drops_streamers_from_game_page(page, game_url: str, limit: int = 80, use_exit_guard: bool = True) -> list[dict]:
	target_url = _normalize_game_directory_url(game_url)
	if not target_url:
		return []
	if use_exit_guard:
		await goto_with_exit(page, target_url, timeout=120000, wait_until="domcontentloaded")
	else:
		await page.goto(target_url, timeout=120000, wait_until="domcontentloaded")
	await maybe_accept_cookies(page)
	await page.wait_for_timeout(2500)
	rows = await page.evaluate(
		r"""
		(args) => {
		  const maxCards = (args && args.limit) || 80;
		  const disallowedLogins = new Set([
			'directory','drops','inventory','downloads','jobs','products','prime','turbo',
			'wallet','settings','search','videos','friends','subscriptions','store','activate','apply'
		  ]);
		  const out = [];
		  const cards = Array.from(document.querySelectorAll('article')).slice(0, maxCards);
		  for (const card of cards) {
			const streamerAnchor = card.querySelector('a[data-a-target="preview-card-title-link"], a[data-a-target="preview-card-image-link"]')
			  || Array.from(card.querySelectorAll('a[href^="/"]')).find(a => {
				const hrefCandidate = (a.getAttribute('href') || '').trim();
				return /^\/[a-z0-9_]+(?:\?|$)/i.test(hrefCandidate);
			  });
			if (!streamerAnchor) continue;
			const href = (streamerAnchor.getAttribute('href') || '').trim();
			let parsed;
			try { parsed = new URL(href, location.origin); }
			catch { continue; }
			if (!/^(www\.)?twitch\.tv$/i.test(parsed.hostname)) continue;
			const parts = parsed.pathname.split('/').filter(Boolean);
			if (parts.length !== 1 || !/^[a-z0-9_]{1,25}$/i.test(parts[0])) continue;
			const login = parts[0].toLowerCase();
			if (disallowedLogins.has(login)) continue;
			const tags = Array.from(card.querySelectorAll('[aria-label^="Tag, "], [data-a-target="tag"], a[href*="/directory/all/tags/"]'))
			  .flatMap(n => [
				(n.textContent || ''),
				(n.getAttribute('aria-label') || '').replace(/^Tag,\s*/i, '')
			  ])
			  .map(value => value.replace(/[^a-z0-9]/gi, '').toLowerCase())
			  .filter(Boolean);
			const hasDrops = tags.some(t => t === 'dropsenabled');
			const viewers = (card.querySelector('[data-a-target="animated-channel-viewers-count"], [data-a-target*="viewers"]')?.textContent || '').trim();
			const game = (card.querySelector('a[data-a-target="preview-card-game-link"]')?.textContent || '').trim();
			out.push({
			  streamer: login,
			  stream_url: `https://www.twitch.tv/${login}`,
			  viewers_text: viewers,
			  game,
			  has_drops: hasDrops
			});
		  }
		  return out;
		}
		""",
		{"limit": int(limit)}
	)
	# Deduplicate by streamer, keep highest viewer score.
	best_by_streamer = {}
	for row in rows or []:
		streamer = (row.get("streamer") or "").strip().lower()
		if not streamer:
			continue
		score = _viewer_count_score(row.get("viewers_text") or "")
		prev = best_by_streamer.get(streamer)
		row_has_drops = bool(row.get("has_drops"))
		prev_has_drops = bool(prev.get("has_drops")) if prev else False
		if (
			not prev
			or (row_has_drops and not prev_has_drops)
			or (row_has_drops == prev_has_drops and score > prev.get("viewer_score", 0))
		):
			item = dict(row)
			item["viewer_score"] = score
			best_by_streamer[streamer] = item
	streamers = sorted(
		best_by_streamer.values(),
		key=lambda s: (0 if s.get("has_drops") else 1, -(s.get("viewer_score") or 0), s.get("streamer") or "")
	)
	return streamers

async def fetch_live_drops_streamers_for_game(context, game_url: str, limit: int = 80) -> list[dict]:
	page = await context.new_page()
	try:
		return await _extract_live_drops_streamers_from_game_page(page, game_url=game_url, limit=limit, use_exit_guard=True)
	finally:
		try:
			await page.close()
		except Exception:
			pass

async def fetch_drops_enabled_games(context, limit: int = 120) -> list[dict]:
	page = await context.new_page()
	try:
		return await _extract_drop_games_from_directory_page(page, limit=limit)
	finally:
		try:
			await page.close()
		except Exception:
			pass

async def fetch_drops_enabled_games_public(limit: int = 120) -> list[dict]:
	"""One-off scrape for the dashboard refresh button."""
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=True)
		page = await browser.new_page(locale="en-US")
		try:
			return await _extract_drop_games_from_directory_page(page, limit=limit)
		finally:
			try:
				await browser.close()
			except Exception:
				pass

async def fetch_game_streamers_public(game_url: str, limit: int = 80) -> list[dict]:
	"""One-off scrape for live drop-enabled streamers in a selected game category."""
	target_url = _normalize_game_directory_url(game_url)
	if not target_url:
		return []
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=True)
		page = await browser.new_page(locale="en-US")
		try:
			return await _extract_live_drops_streamers_from_game_page(
				page,
				game_url=target_url,
				limit=limit,
				use_exit_guard=False
			)
		finally:
			try:
				await browser.close()
			except Exception:
				pass

# ---- Drops workflow helpers ----

async def get_inventory_progress_map(inv_page):
	progress = {}
	try:
		logging.info("[INVENTORY-SCAN] Starting inventory scan...")
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
		logging.info(f"[INVENTORY-SCAN] Found {len(items)} progress bars on inventory page")
		for it in items:
			title = it.get('title')
			percent = it.get('percent')
			if title:
				progress[title] = percent
				logging.info(f"[INVENTORY-SCAN] Item: '{title}' = {percent}%")
			else:
				logging.info(f"[INVENTORY-SCAN] Skipping item with no title: {it}")
		logging.info(f"[INVENTORY-SCAN] Final progress map contains {len(progress)} items")
	except Exception as e:
		logging.warning(f"[INVENTORY-SCAN] Progress map issue: {e}")
	return progress

async def get_general_drops_progress_map(inv_page):
	"""Get progress map for general drops only, excluding streamer-specific drops."""
	progress = {}
	try:
		logging.info("[GENERAL-DROPS-SCAN] Starting general drops area scan...")
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
			  
			  // Look for general drops section - try multiple selectors
			  const generalSections = [
				'[data-test-selector="drops-general-section"]',
				'.drops-general',
				'[aria-label*="general" i]',
				'[aria-label*="General" i]'
			  ];
			  
			  let generalContainer = null;
			  for (const selector of generalSections) {
				generalContainer = document.querySelector(selector);
				if (generalContainer) {
				  console.log('Found general drops section with selector:', selector);
				  break;
				}
			  }
			  
			  // If no specific general section found, look for progress bars that are NOT in streamer sections
			  const allProgressBars = document.querySelectorAll('[role="progressbar"][aria-valuenow]');
			  allProgressBars.forEach(pb => {
				const percent = parseInt(pb.getAttribute('aria-valuenow') || '0', 10);
				const container = pb.parentElement;
				
				// Check if this progress bar is in a streamer-specific section
				const isInStreamerSection = !!pb.closest('[data-test-selector*="streamer"], .streamer-drops, [aria-label*="streamer" i], [aria-label*="Streamer" i]');
				
				// If we found a general container, only include items within it
				// Otherwise, exclude items that are clearly in streamer sections
				let shouldInclude = false;
				if (generalContainer) {
				  shouldInclude = generalContainer.contains(pb);
				} else {
				  shouldInclude = !isInStreamerSection;
				}
				
				if (shouldInclude) {
				  const title = findTitleFrom(container);
				  if (title) {
					// Additional check: exclude titles that clearly contain streamer names
					// (this is a heuristic to avoid streamer-specific items)
					const titleLower = title.toLowerCase();
					const hasStreamerIndicators = /\b(streamer|channel|broadcaster|twitch)\b/.test(titleLower) && 
					  !/\b(general|campaign|event)\b/.test(titleLower);
					
					if (!hasStreamerIndicators) {
					  out.push({ title, percent });
					}
				  }
				}
			  });
			  
			  return out;
			}
			"""
		)
		logging.info(f"[GENERAL-DROPS-SCAN] Found {len(items)} progress bars in general drops area")
		for it in items:
			title = it.get('title')
			percent = it.get('percent')
			if title:
				progress[title] = percent
				logging.info(f"[GENERAL-DROPS-SCAN] General drop: '{title}' = {percent}%")
			else:
				logging.info(f"[GENERAL-DROPS-SCAN] Skipping item with no title: {it}")
		logging.info(f"[GENERAL-DROPS-SCAN] Final general drops progress map contains {len(progress)} items")
	except Exception as e:
		logging.warning(f"[GENERAL-DROPS-SCAN] General drops progress map issue: {e}")
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
	Also includes custom name mappings from config.
	"""
	name = (base_name or "").strip().lower()
	if not name:
		return []
	variations = [name, _normalize_match_text(name)]
	
	# Add custom name mappings from separate mappings file
	try:
		mappings = load_streamer_mappings()
		if name in mappings:
			mapped_name = mappings[name].lower()
			if mapped_name not in variations:
				variations.append(mapped_name)
				variations.append(_normalize_match_text(mapped_name))
				logging.info(f"[NAME-MAPPING] Added mapping for '{name}' -> '{mapped_name}'")
	except Exception as e:
		logging.warning(f"[NAME-MAPPING] Error accessing name mappings: {e}")
	
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
	# Add compact forms so `x choco` also matches `xchoco`
	for existing in list(variations):
		norm = _normalize_match_text(existing)
		compact = norm.replace(" ", "")
		if norm and norm not in variations:
			variations.append(norm)
		if len(compact) >= 4 and compact not in variations:
			variations.append(compact)
	# Deduplicate
	seen = set()
	out = []
	for v in variations:
		key = _normalize_match_text(v) or (v or "").strip().lower()
		if key and key not in seen:
			seen.add(key)
			out.append(key)
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
	return await _get_claimed_days_for_streamer_impl(inv_page, streamer_name, target_lower)

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
				return t.includes('yesterday') || t.includes('ago') || t.includes('last month') || t.includes('months ago') || t.includes('month ago') || t.includes('today');
			  };
			  const toDays = (s) => {
				if (!s) return null;
				const t = s.trim().toLowerCase();
				if (t.includes('today')) return 0;
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


async def _get_claimed_days_for_streamer_impl(inv_page, streamer_name: str, target_lower: str) -> int | None:
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
		for card in cards[:60]:
			try:
				link = await card.query_selector('a[data-a-target="preview-card-title-link"]')
				if not link:
					continue
				href = await link.get_attribute('href')
				if not href:
					continue
				url = 'https://www.twitch.tv' + href if href.startswith('/') else href
				path = href.split('?')[0].strip('/') if href.startswith('/') else url.split('twitch.tv/')[-1]
				has_drops_tag = False
				tag_nodes = await card.query_selector_all('[aria-label^="Tag, "], [data-a-target="tag"], a[href*="/directory/all/tags/"]')
				for t in tag_nodes[:20]:
					txt = re.sub(r'[^a-z0-9]', '', (await t.inner_text()).lower())
					aria_label = (await t.get_attribute('aria-label') or '')
					aria_label = re.sub(r'^tag,\s*', '', aria_label, flags=re.IGNORECASE)
					aria_tag = re.sub(r'[^a-z0-9]', '', aria_label.lower())
					tag_href = (await t.get_attribute('href') or '').lower()
					if (
						txt == 'dropsenabled'
						or aria_tag == 'dropsenabled'
						or tag_href.rstrip('/').endswith('/dropsenabled')
					):
						has_drops_tag = True
						break
				if has_drops_tag and preferred_streamers and path.lower() in preferred_streamers:
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
		return None
	finally:
		await page.close()

async def pick_live_stream_from_enabled_games(context, enabled_games: list[dict]) -> dict | None:
	"""Pick the strongest live drops-enabled stream from selected games/preferences."""
	candidates = []
	for game_entry in enabled_games or []:
		game_name = (game_entry.get("game") or "").strip()
		game_url = (game_entry.get("game_url") or "").strip()
		if not game_url:
			continue
		try:
			streamers = await fetch_live_drops_streamers_for_game(context, game_url, limit=80)
		except Exception as e:
			logging.debug(f"Failed loading streamers for {game_name or game_url}: {e}")
			continue
		for stream in streamers:
			if stream.get("has_drops") is not True:
				continue
			streamer = (stream.get("streamer") or "").strip()
			stream_url = (stream.get("stream_url") or "").strip()
			if not streamer or not stream_url:
				continue
			if not is_streamer_allowed_for_game_preference(game_entry, streamer, streamer_url=stream_url):
				continue
			candidate = {
				"game": game_name or stream.get("game") or game_entry.get("game_key") or "Unknown Game",
				"game_url": game_url,
				"game_key": game_entry.get("game_key") or derive_game_key(game_url, game_name),
				"streamer": streamer,
				"stream_url": stream_url,
				"has_drops": bool(stream.get("has_drops")),
				"viewer_score": int(stream.get("viewer_score") or 0),
				"viewers_text": stream.get("viewers_text") or ""
			}
			candidates.append(candidate)
	if not candidates:
		return None
	candidates.sort(
		key=lambda c: (0 if c.get("has_drops") else 1, -(c.get("viewer_score") or 0), c.get("streamer") or "")
	)
	return candidates[0]


async def selected_stream_matches_target(stream_page, target: dict) -> bool:
	"""Confirm navigation stayed on the selected channel, game, and Drops campaign."""
	try:
		metadata = await read_twitch_channel_metadata(stream_page)
	except Exception as exc:
		logging.debug(f"Failed reading selected stream metadata: {exc}")
		return False
	expected_login = _extract_channel_login(target.get("stream_url"))
	return bool(
		metadata
		and expected_login
		and metadata.get("login") == expected_login
		and metadata.get("drops_enabled") is True
		and twitch_directories_match(
			target.get("game_url"),
			metadata.get("game_url"),
		)
	)

async def watch_selected_games_cycle(context, inv_page, enabled_games: list[dict]) -> bool:
	"""Watch selected non-Rust games with optional streamer filters."""
	target = await pick_live_stream_from_enabled_games(context, enabled_games)
	if not target:
		logging.info("No live drops-enabled stream found for selected games. Retrying shortly.")
		await asyncio.sleep(20)
		return False
	stream_page = await context.new_page()
	try:
		await goto_with_exit(stream_page, target["stream_url"], timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(stream_page)
		update_current_working_item({
			"type": "game",
			"item": target.get("game"),
			"streamer": target.get("streamer"),
			"url": target.get("stream_url"),
			"status": "selected game mode"
		})
		send_notification("Twitch Drops", f"Watching {target.get('streamer')} for {target.get('game')}")
		if (
			not await ensure_stream_playing(stream_page)
			or not await selected_stream_matches_target(stream_page, target)
		):
			logging.warning("Selected stream no longer matches its channel, game, or Drops tag")
			return False
		await set_low_quality(stream_page)
		# Short watch slice; workflow loop will repick based on current live status/preferences.
		for _ in range(4):
			if EXIT_EVENT.is_set():
				return True
			if (
				not await selected_stream_matches_target(stream_page, target)
				or not await ensure_live_video_playing(stream_page)
			):
				logging.warning("Selected stream changed while watching; repicking")
				return False
			try:
				progress_map = await get_inventory_progress_map(inv_page)
				if progress_map:
					update_cached_drops_data(None, progress_map)
			except Exception as e:
				logging.debug(f"Selected-game progress refresh failed: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
		return True
	finally:
		try:
			await stream_page.close()
		except Exception:
			pass
		update_current_working_item(None)

async def ensure_stream_playing(stream_page):
	if not await accept_mature_content_gate(stream_page):
		logging.warning("Twitch content gate could not be cleared; skipping this stream")
		return False
	try:
		await stream_page.wait_for_selector('button[data-a-target="player-play-pause-button"]', timeout=30000)
		label = await stream_page.get_attribute('button[data-a-target="player-play-pause-button"]', 'aria-label')
		if label and 'Play' in label:
			await click_ui_element(
				stream_page, 
				'button[data-a-target="player-play-pause-button"]', 
				"play button", 
				timeout=2000, 
				wait_after_click=1.0
			)
	except Exception as exc:
		logging.debug(f"Play control was unavailable: {exc}")
	try:
		# Ensure muted (only click if currently not muted)
		val = await stream_page.get_attribute('[data-a-target="player-volume-slider"]', 'aria-valuenow')
		if val is None or val != '0':
			await click_ui_element(
				stream_page, 
				'button[data-a-target="player-mute-unmute-button"]', 
				"mute button", 
				timeout=2000, 
				wait_after_click=0.2
			)
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
	try:
		return await ensure_live_video_playing(stream_page)
	except Exception as exc:
		logging.debug(f"Stream playback verification failed: {exc}")
		return False

async def click_ui_element(page, selectors, description="UI element", timeout=2000, wait_after_click=0.3):
    """
    Unified function to click UI elements using simple, direct clicking approach.
    
    This function uses the same simple clicking approach as the original set_low_quality function,
    providing a clean interface for clicking any UI element.
    
    Args:
        page: Playwright page object
        selectors: List of CSS selectors to try, or single selector string
        description: Description for logging purposes
        timeout: Timeout for each click attempt
        wait_after_click: Time to wait after successful click
    
    Returns:
        bool: True if click was successful, False otherwise
    
    Examples:
        # Click a single button
        await click_ui_element(page, 'button[data-a-target="claim-button"]', "claim button")
        
        # Try multiple selectors
        await click_ui_element(page, [
            'button:has-text("Claim")',
            'button[aria-label*="Claim"]',
            '[data-test-selector="claim-button"]'
        ], "claim button")
        
        # Click with custom timeout and wait
        await click_ui_element(page, 'button.settings', "settings button", timeout=5000, wait_after_click=1.0)
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    
    for sel in selectors:
        try:
            # Try to find and click the element directly
            element = await page.query_selector(sel)
            if element:
                await element.click(timeout=timeout)
                logging.info(f"Successfully clicked {description} using selector: {sel}")
                if wait_after_click > 0:
                    await asyncio.sleep(wait_after_click)
                return True
        except Exception:
            continue
    
    logging.debug(f"Failed to click {description} with any of the provided selectors")
    return False


async def set_low_quality(stream_page):
	try:
		# Click settings button using unified function
		settings_clicked = await click_ui_element(
			stream_page, 
			'button[data-a-target="player-settings-button"]', 
			"player settings button", 
			timeout=15000, 
			wait_after_click=0.3
		)
		if not settings_clicked:
			return
			
		# Open Quality submenu using unified function
		quality_selectors = [
			'div[role="menu"] [data-a-target="player-settings-menu-item-quality"]',
			'div[role="menu"] [data-a-target="player-settings-quality"]',
			'div[role="menu"] [role="menuitem"]:has-text("Quality")'
		]
		
		quality_opened = await click_ui_element(
			stream_page, 
			quality_selectors, 
			"quality menu item", 
			timeout=2000, 
			wait_after_click=0.4
		)
		
		if not quality_opened:
			# Close menu and exit
			await click_ui_element(
				stream_page, 
				'button[data-a-target="player-settings-button"]', 
				"player settings button (close)", 
				timeout=2000
			)
			return

		# Click the lowest available quality (Audio Only or lowest p)
		await stream_page.evaluate(
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
		# Close settings menu using unified function
		await click_ui_element(
			stream_page, 
			'button[data-a-target="player-settings-button"]', 
			"player settings button (close)", 
			timeout=2000
		)
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


def _attach_claim_console_logging(inv_page) -> bool:
	"""Attach claim diagnostics once per Playwright page."""
	if getattr(inv_page, "_tda_claim_console_logging", False):
		return False

	def handle_console(msg):
		try:
			sanitized_text = msg.text.encode('ascii', 'ignore').decode('ascii')
			if msg.type == "error":
				logging.error(f"[JS ERROR] {sanitized_text}")
			elif msg.type == "warning":
				logging.warning(f"[JS WARNING] {sanitized_text}")
			elif msg.type == "log":
				logging.info(f"[JS LOG] {sanitized_text}")
			else:
				logging.info(f"[JS {msg.type.upper()}] {sanitized_text}")
		except Exception as e:
			logging.debug(f"Console message handling failed: {e}")

	try:
		setattr(inv_page, "_tda_claim_console_logging", True)
		inv_page.on("console", handle_console)
		return True
	except Exception as e:
		try:
			setattr(inv_page, "_tda_claim_console_logging", False)
		except Exception:
			pass
		logging.debug(f"Could not attach claim console logging: {e}")
		return False


async def claim_available_rewards(inv_page, navigate: bool = True) -> int:
	"""Click all visible 'Claim' buttons on the inventory page.

	Returns the number of claims clicked. When navigate=False, assumes caller is already on the inventory page.
	"""

	_attach_claim_console_logging(inv_page)

	claimed = 0
	try:
		await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
		await maybe_accept_cookies(inv_page)
		await inv_page.wait_for_timeout(500)
		if navigate:
			await goto_with_exit(inv_page, TWITCH_INVENTORY_URL, timeout=120000, wait_until="domcontentloaded")
			await maybe_accept_cookies(inv_page)
			await inv_page.wait_for_timeout(500)
		claim_buttons = await inv_page.query_selector_all('button:has-text("Claim")')
		for btn in claim_buttons:
			try:
				await btn.click(force=True)
				claimed += 1
				logging.info("Claimed a reward")
				await asyncio.sleep(0.5)
			except Exception:
				try:
					await btn.click()
					claimed += 1
					logging.info("Claimed a reward (fallback click)")
					await asyncio.sleep(0.5)
				except Exception:
					continue
	except Exception:
		pass
	return claimed

async def watch_streamer(stream_page, inv_page, streamer_name: str):
	"""Watch a streamer and periodically check inventory for drop progress until 100% or exit."""
	url = stream_page.url
	logging.info(f"Watching stream: {url}")

async def poll_until_reward_complete(context, inv_page, streamer_name: str, item_name: str = "", streamer_url: str | None = None):
	total_wait_seconds = MAX_WATCH_HOURS_PER_REWARD * 3600
	waited = 0
	last_percent = None
	target_drop = {"streamer": streamer_name, "item": item_name, "url": streamer_url or ""}
	logging.info(f"Tracking streamer '{streamer_name}' item '{item_name}' by inventory title match")
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
			await inv_page.wait_for_timeout(600)
			progress_map = await get_inventory_progress_map(inv_page)
			percent, title, score = match_streamer_drop_progress(target_drop, progress_map)
			if percent is not None and title:
				logging.info(f"[{title}] Progress: {percent}% (score={score})")
				last_percent = percent if isinstance(percent, int) else last_percent
				try:
					update_cached_drops_data(None, {title: percent})
				except Exception as e:
					logging.debug(f"Failed to update cache during progress tracking: {e}")
				if isinstance(percent, int) and percent >= 100:
					return True
			else:
				logging.info(f"No inventory entry found for streamer '{streamer_name}' / '{item_name}'.")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
		except Exception as e:
			logging.warning(f"Poll inventory issue: {e}")
			await asyncio.sleep(INVENTORY_POLL_INTERVAL_SECONDS)
			waited += INVENTORY_POLL_INTERVAL_SECONDS
	logging.info(f"Max watch time reached without detecting completion (last percent={last_percent}).")
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
            # Do not auto-claim; user will claim manually
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
			# Restrict general progress search to the general drops area only
			general_map = await get_general_drops_progress_map(inv_page)
			# Find a matching title in the general area
			match_title = None
			match_percent = None
			for title, percent in (general_map or {}).items():
				if isinstance(title, str) and target_lower in (title or '').lower():
					match_title = title
					match_percent = percent if isinstance(percent, int) else None
					break
			if match_title is not None and isinstance(match_percent, int):
				p = match_percent
				logging.info(f"[General] {match_title} Progress: {p}%")
				
				# Update cache with current progress
				try:
					progress_map = {match_title: p}
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
            # Do not auto-claim; user will claim manually
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
			# Restrict general progress search to the general drops area only
			general_map = await get_general_drops_progress_map(inv_page)
			# Find a matching title in the general area
			match_title = None
			match_percent = None
			for title, percent in (general_map or {}).items():
				if isinstance(title, str) and target_lower in (title or '').lower():
					match_title = title
					match_percent = percent if isinstance(percent, int) else None
					break
			if match_title is not None and isinstance(match_percent, int):
				p = match_percent
				logging.info(f"[General] {match_title} Progress: {p}%")
				
				# Update cache with current progress
				try:
					progress_map = {match_title: p}
					update_cached_drops_data(None, progress_map)
				except Exception as e:
					logging.debug(f"Failed to update cache during general progress tracking: {e}")
				
				if p >= 100:
					# Completed; do not auto-claim
					return (True, False)

			# After logging progress, also check if any streamer-specific items need progress
			fp = await fetch_facepunch_drops(context)
			streamer_targets = fp.get('streamer', []) if fp else []
			logging.info(f"[STREAMER-CHECK] Checking {len(streamer_targets)} streamer targets for completion status")
			for st in streamer_targets:
				name = (st.get('streamer') or '').strip()
				if not name:
					logging.info("[STREAMER-CHECK] Skipping streamer: empty name")
					continue
				# Only use Facepunch for live status
				if not bool(st.get('is_live')):
					logging.info(f"[STREAMER-CHECK] Skipping '{name}': not live")
					continue
				name_lower = name.lower()
				if name_lower in completed_streamers:
					logging.info(f"[STREAMER-CHECK] Skipping '{name}': already in completed_streamers set")
					continue
				# If we haven't already completed this streamer drop historically, switch now
				days = await get_claimed_days_for_streamer(inv_page, name)
				if days is not None:
					# Already claimed previously; skip
					logging.info(f"[STREAMER-CHECK] Skipping '{name}': claimed {days} day(s) ago")
					completed_streamers.add(name_lower)
					continue

				logging.info(f"Switching to streamer-specific drop: {name} ({st.get('item')})")
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
			# Refresh drop-enabled games cache periodically for the new dashboard section.
			try:
				force_refresh = GAMES_REFRESH_REQUESTED.is_set()
				if should_refresh_games_cache(force=force_refresh):
					games = await fetch_drops_enabled_games(context, limit=160)
					update_cached_games_data(games=games, source="twitch_directory", error=None)
				if force_refresh:
					GAMES_REFRESH_REQUESTED.clear()
			except Exception as game_err:
				logging.warning(f"Game discovery refresh failed: {game_err}")
				update_cached_games_data(games=None, source="twitch_directory", error=str(game_err))
				GAMES_REFRESH_REQUESTED.clear()

			# Optional watch-target selection mode from dashboard.
			watch_prefs = get_watch_preferences_snapshot()
			enabled_game_prefs = get_enabled_game_preferences(watch_prefs)
			rust_game_pref = next((g for g in enabled_game_prefs if is_rust_game_preference(g)), None)
			non_rust_enabled_games = [g for g in enabled_game_prefs if not is_rust_game_preference(g)]
			if enabled_game_prefs and rust_game_pref is None:
				logging.info("Watch target mode active without Rust selected; using selected games stream cycle.")
				await watch_selected_games_cycle(context, inv_page, enabled_game_prefs)
				continue

			fp = await fetch_facepunch_drops(context)
			if fp.get("fetch_failed"):
				logging.warning(
					"Facepunch data could not be refreshed; preserving current state and retrying."
				)
				await asyncio.sleep(10)
				continue
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
			logging.info(f"[PROGRESS-MAP] Retrieved progress map with {len(progress_map)} items:")
			for title, percent in progress_map.items():
				logging.info(f"[PROGRESS-MAP] '{title}' = {percent}%")
			in_progress_titles = { (t or '').lower() for t in progress_map.keys() }
			logging.info(f"[PROGRESS-MAP] Created in_progress_titles set with {len(in_progress_titles)} lowercase titles")
			
	# Initial claim check removed per user request (user will claim manually)
			
			# Scrape recently claimed items first so we can use them for proper categorization
			recently_claimed = []
			try:
				recently_claimed = await scrape_recent_claimed_items(inv_page)
				logging.info(f"[RECENTLY-CLAIMED] Scraped {len(recently_claimed)} recently claimed items:")
				for i, item in enumerate(recently_claimed):
					name = item.get('name', 'Unknown')
					days = item.get('days', 'Unknown')
					logging.info(f"[RECENTLY-CLAIMED] Item {i+1}: '{name}' (claimed {days} days ago)")
			except Exception as e:
				logging.warning(f"[RECENTLY-CLAIMED] Failed to scrape recently claimed items: {e}")
				recently_claimed = []
			
			# Get general drops progress map if we have general drops
			general_progress_map = {}
			if fp and fp.get('general'):
				general_progress_map = await get_general_drops_progress_map(inv_page)
				logging.info(f"[WORKFLOW] Retrieved general drops progress map with {len(general_progress_map)} items")
			
			# Update cached drops data for web interface with recently claimed data
			update_cached_drops_data(fp, progress_map, recently_claimed, general_progress_map)
			
			# Check for ready-to-claim items and log them
			ready_to_claim_items = []
			if progress_map:
				for title, percent in progress_map.items():
					if isinstance(percent, int) and percent >= 100:
						ready_to_claim_items.append(title)
			
			if ready_to_claim_items:
				logging.info(f"[READY-TO-CLAIM] Found {len(ready_to_claim_items)} items ready to claim: {ready_to_claim_items}")
			else:
				logging.info("[READY-TO-CLAIM] No items ready to claim")

			# Defer general drop decision until after streamer candidates are considered
			longest_general = None

			# Recently claimed items already scraped above

			# Helper to check if a streamer appears in recently_claimed using intelligent matching
			def is_streamer_recently_claimed(streamer: str) -> int | None:
				match = find_recently_claimed_match(streamer, recently_claimed)
				if not match:
					return None
				days = match.get("days")
				try:
					return int(days)
				except Exception:
					return 0

			# Build candidates only for streamer-specific items present in inventory (<100%)
			candidates = []
			live_any = []
			inventory_entries = list((progress_map or {}).items())
			logging.info(f"[INVENTORY-ENTRIES] Processing {len(inventory_entries)} inventory entries:")
			for title, percent in inventory_entries:
				logging.info(f"[INVENTORY-ENTRIES] '{(title or '').lower()}' = {percent}%")
			
			# Debug lists for better visibility
			streamer_drops_with_progress = []
			streamer_drops_no_progress = []
			general_drops_available = []
			
			logging.info(f"[STREAMER-EVAL] Evaluating {len(streamer_targets)} streamer targets for drops")
			for st in streamer_targets:
				name = (st.get('streamer') or '').strip()
				if not name:
					logging.info("[STREAMER-EVAL] Skipping streamer: empty name")
					continue
				if bool(st.get('is_live')):
					live_any.append(st)
				# Only consider live streamers for watching
				if not bool(st.get('is_live')):
					logging.info(f"[STREAMER-EVAL] Skipping '{name}': not live")
					continue
				if rust_game_pref and not is_streamer_allowed_for_game_preference(
					rust_game_pref,
					name,
					streamer_url=st.get('url')
				):
					logging.info(f"[STREAMER-EVAL] Skipping '{name}': not selected in watch preferences")
					continue
				name_lower = name.lower()
				if name_lower in completed_streamers:
					logging.info(f"[STREAMER-EVAL] Skipping '{name}': already in completed_streamers set")
					continue
				# Find matching inventory entry and its percent using streamer+item matching
				match_pct, match_title, match_score = match_streamer_drop_progress(st, progress_map)
				logging.info(
					f"[STREAMER-EVAL] '{name}' best inventory match: title='{match_title}', "
					f"pct={match_pct}, score={match_score}"
				)
				
				emit_debug(f"[candidates] {name}: inventory match_pct={match_pct}")
				
				# If match_pct is None, it means the drop hasn't started yet (not on Twitch inventory)
				# This is exactly what we want to work on!
				# Only skip if it's already 100% complete on Twitch
				if match_pct is not None and match_pct >= 100:
					logging.info(f"[STREAMER-EVAL] Skipping '{name}': already 100% complete ({match_pct}%)")
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
					claimed_match = find_recently_claimed_match(name, recently_claimed, streamer_url=st.get('url'))
					days = claimed_match.get("days") if claimed_match else None
					if days is None:
						days = is_streamer_recently_claimed(name)
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
			logging.info("=== DROP DEBUG INFO ===")
			logging.info(f"Streamer drops with progress: {streamer_drops_with_progress}")
			logging.info(f"Streamer drops with no progress: {streamer_drops_no_progress}")
			logging.info(f"General drops available: {general_drops_available}")
			logging.info(f"Total candidates: {len(candidates)}")
			logging.info("========================")

			# Cache already updated above with recently claimed data

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
				if not await ensure_stream_playing(stream_page):
					logging.info("Skipping gated or unavailable streamer target")
					await stream_page.close()
					continue
				await set_low_quality(stream_page)
				completed = await poll_until_reward_complete(
					context,
					inv_page,
					streamer_name=target_name,
					item_name=target_item,
					streamer_url=target_url
				)
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
			general_drops_complete = await are_all_general_drops_complete(inv_page, fp.get('general') if fp else None)
			if general_drops_complete:
				# Check if there are any ready-to-claim items that need manual claiming
				progress_map = await get_inventory_progress_map(inv_page)
				ready_to_claim_items = []
				if progress_map:
					for title, percent in progress_map.items():
						if isinstance(percent, int) and percent >= 100:
							ready_to_claim_items.append(title)
				
				if ready_to_claim_items:
					logging.info(f"All general drops are complete, but {len(ready_to_claim_items)} items are ready to claim: {ready_to_claim_items}")
					logging.info("Continuing to monitor for manual claiming...")
				else:
					logging.info("All general drops are complete and claimed. Exiting program.")
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
				if not await ensure_stream_playing(stream_page):
					logging.info("Skipping gated or unavailable general-drop stream")
					await stream_page.close()
					continue
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
			if non_rust_enabled_games:
				logging.info("No Rust target available right now; switching to selected non-Rust games.")
				await watch_selected_games_cycle(context, inv_page, non_rust_enabled_games)
				continue
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
			logging.info("[GENERAL-DROPS-CHECK] No general drop data available - completion is unknown")
			return False
		logging.info(f"[GENERAL-DROPS-CHECK] Checking {len(general_list)} general drops for completion")
		# Ensure we are on inventory and scrape current progressbars from general drops area only
		progress_map = await get_general_drops_progress_map(inv_page)
		if progress_map is None:
			progress_map = {}
		logging.info(f"[GENERAL-DROPS-CHECK] Found {len(progress_map)} items in general drops progress map")
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
				logging.info("[GENERAL-DROPS-CHECK] Skipping general drop: no item name or alias")
				continue
			search_terms = [item_name or '', alias or '']
			logging.info(f"[GENERAL-DROPS-CHECK] Checking general drop: '{item_name}' (alias: '{alias}')")
			pct = find_percent_for_any(search_terms)
			if isinstance(pct, int):
				logging.info(f"[GENERAL-DROPS-CHECK] Found progress for '{item_name}': {pct}%")
				if pct < 100:
					logging.info(f"[GENERAL-DROPS-CHECK] General drop '{item_name}' not complete ({pct}%) - returning False")
					return False
				logging.info(f"[GENERAL-DROPS-CHECK] General drop '{item_name}' is complete ({pct}%)")
				continue
			# A missing progress bar is ambiguous: Twitch may still be loading, the
			# campaign may not have started, or selectors may have changed. Only mark
			# it complete when the claimed-items area confirms the reward.
			claimed = await is_general_item_claimed_on_inventory(inv_page, item_name or alias or "")
			if claimed is True:
				logging.info(f"[GENERAL-DROPS-CHECK] Confirmed '{item_name or alias}' in claimed inventory")
				continue
			logging.info(
				f"[GENERAL-DROPS-CHECK] No progress or claimed confirmation for "
				f"'{item_name or alias}' - completion is unknown"
			)
			return False
		logging.info("[GENERAL-DROPS-CHECK] All general drops are complete - returning True")
		return True
	except Exception as e:
		logging.warning(f"[GENERAL-DROPS-CHECK] Error checking general drops: {e}")
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
			subprocess.Popen([preferred, script_path, *sys.argv[1:]], cwd=BASE_DIR, creationflags=flags)
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
