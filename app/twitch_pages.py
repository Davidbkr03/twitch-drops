"""Shared Twitch page parsing and interaction helpers."""

import asyncio
import re
from collections.abc import Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


TWITCH_ORIGIN = "https://www.twitch.tv"
MATURE_GATE_SELECTOR = (
    '[data-a-target="content-classification-gate-overlay"], '
    '[data-a-target="player-overlay-content-gate"]'
)
MATURE_ACCEPT_SELECTORS = (
    '[data-a-target="player-overlay-mature-accept"]',
    '[data-a-target="content-classification-gate-overlay"] '
    'button:has-text("Continue Watching")',
    '[data-a-target="content-classification-gate-overlay"] '
    'button:has-text("Start Watching")',
    '[data-a-target="player-overlay-content-gate"] '
    'button:has-text("Continue Watching")',
    '[data-a-target="player-overlay-content-gate"] '
    'button:has-text("Start Watching")',
)

CHANNEL_METADATA_JS = r"""
() => {
    const current = new URL(location.href);
    const parts = current.pathname.split('/').filter(Boolean);
    const login = parts.length === 1 && /^[a-z0-9_]{1,25}$/i.test(parts[0])
        ? parts[0].toLowerCase() : '';
    const root = document.querySelector('main') || document;
    const heading = login
        ? root.querySelector(`a[href="/${CSS.escape(login)}"] h1`) : null;
    let scope = null;
    for (let node = heading; node && node !== document.body; node = node.parentElement) {
        if (node === root) break;
        if (node.querySelector(
            'a[data-a-target="stream-game-link"], '
            + 'a[href*="/directory/category/"], a[href*="/directory/game/"]'
        )) {
            scope = node;
            break;
        }
    }
    scope = scope
        || root.querySelector('[data-a-target="stream-info-card-component"]')
        || root.querySelector('[data-a-target="channel-header"]');
    const gameLink = scope?.querySelector(
        'a[data-a-target="stream-game-link"], '
        'a[href*="/directory/category/"], a[href*="/directory/game/"]'
    );
    const gameUrl = gameLink
        ? new URL(gameLink.getAttribute('href') || '', location.origin).href : '';
    const tagNodes = Array.from(scope?.querySelectorAll(
        '[aria-label^="Tag, "], [data-a-target="tag"], '
        'a[href*="/directory/all/tags/"]'
    ) || []);
    const dropsEnabled = tagNodes.some(node => {
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
    return {
        url: current.href,
        login,
        displayName: (heading?.textContent || login).trim(),
        gameName: (gameLink?.textContent || '').trim(),
        gameUrl,
        dropsEnabled,
    };
}
"""

_CHANNEL_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_RESERVED_TWITCH_PATHS = {
    "activate",
    "directory",
    "downloads",
    "drops",
    "friends",
    "inventory",
    "jobs",
    "login",
    "payments",
    "prime",
    "products",
    "search",
    "settings",
    "signup",
    "store",
    "subscriptions",
    "turbo",
    "videos",
    "wallet",
}
_GAME_DIRECTORY_RE = re.compile(
    r"^/directory/(?:category|game)/[^/]+/?$",
    flags=re.IGNORECASE,
)


def normalize_twitch_game_url(value: str) -> str:
    """Validate and normalize a Twitch game or DropsEnabled directory URL."""
    invalid_url = "game_url must be an HTTPS Twitch directory URL"
    if not isinstance(value, str):
        raise ValueError("game_url must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError("game_url required")

    try:
        parsed = urlsplit(urljoin(TWITCH_ORIGIN, raw))
        port = parsed.port
    except ValueError as exc:
        raise ValueError(invalid_url) from exc

    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"twitch.tv", "www.twitch.tv"}
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or "\\" in decoded_path
        or unquote(decoded_path) != decoded_path
    ):
        raise ValueError(invalid_url)

    normalized_path = decoded_path.rstrip("/")
    is_drops_directory = (
        normalized_path.casefold() == "/directory/all/tags/dropsenabled"
    )
    is_game_directory = bool(_GAME_DIRECTORY_RE.fullmatch(decoded_path))
    if not is_drops_directory and not is_game_directory:
        raise ValueError("game_url must point to a Twitch game directory")

    if is_game_directory:
        slug = normalized_path.rsplit("/", 1)[-1]
        if slug in {".", ".."} or not slug.strip():
            raise ValueError("game_url must point to a Twitch game directory")

    return urlunsplit(
        ("https", "www.twitch.tv", parsed.path.rstrip("/"), parsed.query, "")
    )


def normalize_twitch_channel_login(value: str) -> str:
    """Return a canonical Twitch login from a login or channel URL."""
    if not isinstance(value, str):
        raise ValueError("streamer must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError("streamer required")

    if "://" in raw or raw.startswith("/"):
        login = twitch_channel_login_from_url(raw)
    else:
        login = raw
    if (
        not login
        or not _CHANNEL_LOGIN_RE.fullmatch(login)
        or login.casefold() in _RESERVED_TWITCH_PATHS
    ):
        raise ValueError("streamer must be a valid Twitch channel login")
    return login.casefold()


def twitch_channel_login_from_url(value: str | None) -> str | None:
    """Extract a login only from an exact, first-level Twitch channel URL."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(urljoin(TWITCH_ORIGIN, raw))
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"twitch.tv", "www.twitch.tv"}
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        len(parts) != 1
        or not _CHANNEL_LOGIN_RE.fullmatch(parts[0])
        or parts[0].casefold() in _RESERVED_TWITCH_PATHS
    ):
        return None
    return parts[0].casefold()


async def read_twitch_channel_metadata(page) -> dict | None:
    """Read current-channel identity, category, and Drops eligibility."""
    metadata = await page.evaluate(CHANNEL_METADATA_JS)
    if not isinstance(metadata, dict):
        return None

    login = twitch_channel_login_from_url(metadata.get("url") or page.url)
    if not login:
        return None
    game_url = metadata.get("gameUrl") or ""
    try:
        game_url = normalize_twitch_game_url(game_url) if game_url else ""
    except ValueError:
        game_url = ""
    return {
        "login": login,
        "display_name": (metadata.get("displayName") or login).strip(),
        "url": f"{TWITCH_ORIGIN}/{login}",
        "game_name": (metadata.get("gameName") or "").strip(),
        "game_url": game_url,
        "drops_enabled": metadata.get("dropsEnabled") is True,
    }


def twitch_directory_path(value: str | None) -> str | None:
    """Return a normalized Twitch directory path, or ``None`` for invalid input."""
    try:
        path = urlsplit(normalize_twitch_game_url(value or "")).path
        return unquote(path).rstrip("/").casefold()
    except ValueError:
        return None


def twitch_directories_match(expected: str | None, actual: str | None) -> bool:
    """Match canonical and legacy Twitch directory URLs for the same game."""
    expected_path = twitch_directory_path(expected)
    actual_path = twitch_directory_path(actual)
    if not expected_path or not actual_path:
        return False
    if expected_path == "/directory/all/tags/dropsenabled":
        return True
    if expected_path == actual_path:
        return True
    expected_parts = expected_path.split("/")
    actual_parts = actual_path.split("/")
    if not (
        len(expected_parts) == 4
        and len(actual_parts) == 4
        and expected_parts[1] == actual_parts[1] == "directory"
        and expected_parts[2] in {"category", "game"}
        and actual_parts[2] in {"category", "game"}
    ):
        return False

    def slug(parts: list[str]) -> str:
        value = parts[3]
        if parts[2] == "game":
            value = re.sub(r"\s+", "-", value.strip())
        return value

    return slug(expected_parts) == slug(actual_parts)


async def accept_mature_content_gate(page, *, timeout: int = 5000) -> bool:
    """Accept Twitch's current or legacy mature gate and confirm it cleared."""
    gate = await page.query_selector(MATURE_GATE_SELECTOR)
    if not gate:
        return True
    try:
        if not await gate.is_visible():
            return True
    except Exception:
        # Some test doubles and older browser handles do not expose visibility.
        # In that case, retain the conservative click-and-confirm behavior.
        pass

    for selector in MATURE_ACCEPT_SELECTORS:
        button = await page.query_selector(selector)
        if not button:
            continue
        try:
            await button.click()
            try:
                await page.wait_for_selector(
                    MATURE_GATE_SELECTOR,
                    state="hidden",
                    timeout=timeout,
                )
            except Exception:
                # Some Twitch builds remove and replace the overlay without
                # satisfying Playwright's original wait handle.
                await asyncio.sleep(0.5)
            remaining = await page.query_selector(MATURE_GATE_SELECTOR)
            if not remaining:
                return True
            try:
                return not await remaining.is_visible()
            except Exception:
                return False
        except Exception:
            continue
    return False


async def ensure_live_video_playing(
    page,
    *,
    readiness_attempts: int = 8,
    readiness_delay: float = 0.5,
) -> bool:
    """Wait briefly for Twitch's live video to load and ensure it is playing."""
    for _ in range(max(1, readiness_attempts)):
        try:
            video = await page.query_selector("video")
        except Exception:
            video = None
        if not video:
            await asyncio.sleep(readiness_delay)
            continue
        try:
            state = await video.evaluate(
                "video => ({ended: video.ended, paused: video.paused, "
                "readyState: video.readyState, error: Boolean(video.error)})"
            )
        except Exception:
            await asyncio.sleep(readiness_delay)
            continue
        if (
            not isinstance(state, dict)
            or state.get("ended") is True
            or state.get("error") is True
        ):
            return False
        if int(state.get("readyState") or 0) < 2:
            await asyncio.sleep(readiness_delay)
            continue
        if state.get("paused") is not True:
            return True
        try:
            resumed = await video.evaluate(
                "video => { video.muted = true; return video.play()"
                ".then(() => true).catch(() => false); }"
            )
        except Exception:
            return False
        if resumed is not True:
            return False
        await asyncio.sleep(readiness_delay)

    return False


async def collect_virtualized_cards(
    page,
    extractor: str,
    *,
    key: Callable[[dict], str | None],
    max_scrolls: int,
    scroll_delay: float = 1.0,
) -> list[dict]:
    """Accumulate card data while Twitch recycles DOM nodes during scrolling."""
    items: dict[str, dict] = {}
    previous_height: int | float | None = None
    stable_rounds = 0

    for index in range(max_scrolls + 1):
        batch = await page.evaluate(extractor)
        before = len(items)
        for item in batch or []:
            if not isinstance(item, dict):
                continue
            item_key = key(item)
            if item_key:
                current = items.get(item_key)
                if current is None or (
                    item.get("drops") is True and current.get("drops") is not True
                ):
                    items[item_key] = item

        height = await page.evaluate("document.body.scrollHeight")
        if len(items) == before and height == previous_height:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if index >= max_scrolls or stable_rounds >= 2:
            break

        previous_height = height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(scroll_delay)

    return list(items.values())
