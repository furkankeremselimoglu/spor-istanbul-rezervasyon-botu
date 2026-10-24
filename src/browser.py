"""Playwright session lifecycle, on-disk artifacts, rate limiting, and logging.

Root-anchor: PROJECT_ROOT is derived from this file's own location, so this module
must never move relative to the repo root (see state/README.md for the state layout).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

if TYPE_CHECKING:
    from src.config import Config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE = PROJECT_ROOT / "state"
PROFILE_DIR = STATE / "profile"
ARTIFACTS_DIR = STATE / "artifacts"
LOG_DIR = STATE / "logs"
# Session cookies (login) die with the browser; we persist them ourselves. Owner-only perms.
COOKIES_FILE = STATE / "cookies.json"
COOKIE_DOMAIN = "spor.istanbul"

DEFAULT_NAV_TIMEOUT_MS = 30_000

# Keep the renderer fully active while the window is minimized/occluded, so a hot poll
# or a click isn't throttled/paused by Chromium's background-tab power-saving heuristics.
LAUNCH_ARGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]

logger = logging.getLogger("ibbspor.browser")


def _private_dir(path: Path) -> None:
    """Create a directory with owner-only (0o700) permissions, idempotent.
    
    Never raises on chmod failure (permission already set or unsupported platform).
    """
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)


class LoggedOut(Exception):
    """The Playwright session is not authenticated on the site."""


class SiteError(Exception):
    """Unexpected site state that is not a login issue (e.g. missing membership row)."""


class BrowserClosed(SiteError):
    """The bot's browser (context/process) is gone -- e.g. the user closed its last
    window on Windows. Callers that own the browser must relaunch it."""


class SiteUnavailable(SiteError):
    """The site itself looks unavailable (a Cloudflare challenge page, or a maintenance
    page), as opposed to a login or application-level error. Callers (runner.py) should
    wait and retry -- this never counts toward any fatal error budget."""


class Stopped(Exception):
    """Raised by RateLimiter.acquire() when its injected stop-aware sleep signals a stop.

    `RateLimiter(sleep=...)` accepts any callable(seconds) -> truthy-if-stopped, e.g.
    `threading.Event.wait` (which returns True once the event is set). When that happens
    mid-wait, acquire() raises this immediately instead of finishing the rate-limit wait,
    so callers (the engine loop, Site methods) should let it propagate to unwind the
    current run rather than catching it like a normal error.
    """


class BrowserWindow:
    """Controls the OS-level window (minimize/restore) of a page via its CDP session.

    Thin wrapper around `context.new_cdp_session(page)` plus the CDP Browser domain:
    `Browser.getWindowForTarget` (page -> windowId), `Browser.setWindowBounds`
    (windowId + bounds.windowState -> minimize/restore) and `Browser.getWindowBounds`
    (windowId -> current windowState).

    Never raises: any failure (unsupported platform, closed page, stale session) is
    logged as a warning and surfaced as `False` (or `None` from `state()`) -- window
    control is best-effort UX and must never break a run. The CDP session is lazily
    (re)created whenever `.page` differs from the page the cached session was built
    for, or that page has since closed, so reassigning `.page` after construction
    (e.g. the engine switches to a different tab) is safe.
    """

    def __init__(self, context: BrowserContext, page: Page) -> None:
        self.context = context
        self.page = page
        self._cdp = None
        self._cdp_page: Page | None = None
        self._window_id = None

    def _invalidate(self) -> None:
        self._cdp = None
        self._cdp_page = None
        self._window_id = None

    def _ensure_session(self) -> bool:
        """(Re)create the CDP session + windowId if the page changed/closed/never set up."""
        if self._cdp is not None and self._cdp_page is self.page:
            try:
                if self.page.is_closed():
                    self._invalidate()
            except Exception:
                self._invalidate()
        if self._cdp is not None and self._cdp_page is self.page:
            return True
        self._invalidate()
        try:
            if self.page.is_closed():
                return False
            cdp = self.context.new_cdp_session(self.page)
            result = cdp.send("Browser.getWindowForTarget")
            self._cdp = cdp
            self._cdp_page = self.page
            self._window_id = result["windowId"]
            return True
        except Exception:
            logger.warning("BrowserWindow: could not create CDP session", exc_info=True)
            self._invalidate()
            return False

    def minimize(self) -> bool:
        """Minimize the page's OS window. Never raises."""
        if not self._ensure_session():
            return False
        try:
            self._cdp.send(
                "Browser.setWindowBounds",
                {"windowId": self._window_id, "bounds": {"windowState": "minimized"}},
            )
            return True
        except Exception:
            logger.warning("BrowserWindow.minimize failed", exc_info=True)
            self._invalidate()
            return False

    def show(self) -> bool:
        """Restore the OS window to "normal" state and bring the page to front. Never raises."""
        ok = True
        if self._ensure_session():
            try:
                self._cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": self._window_id, "bounds": {"windowState": "normal"}},
                )
            except Exception:
                logger.warning("BrowserWindow.show: setWindowBounds failed", exc_info=True)
                self._invalidate()
                ok = False
        else:
            ok = False
        try:
            self.page.bring_to_front()
        except Exception:
            logger.warning("BrowserWindow.show: bring_to_front failed", exc_info=True)
            ok = False
        return ok

    def state(self) -> str | None:
        """Return the current windowState ("normal", "minimized", ...), or None on failure."""
        if not self._ensure_session():
            return None
        try:
            result = self._cdp.send("Browser.getWindowBounds", {"windowId": self._window_id})
            return result.get("bounds", {}).get("windowState")
        except Exception:
            logger.warning("BrowserWindow.state failed", exc_info=True)
            self._invalidate()
            return None


@dataclass
class BrowserSession:
    """A live persistent-context Playwright session with a single reused page."""

    context: BrowserContext
    page: Page
    window: BrowserWindow


@contextmanager
def open_browser(
    headless: bool = False,
    minimized: bool = False,
    profile_dir: Path = PROFILE_DIR,
) -> Iterator[BrowserSession]:
    """Launch (or reuse) the bot's persistent Chromium profile.

    Context manager: yields a BrowserSession exposing .page/.context/.window, and closes
    the browser and stops the Playwright driver on exit (including on exception).

    `profile_dir` defaults to the bot's real profile (state/profile) but can be pointed
    at a throwaway directory (tests, manual live checks) without touching the real one.

    `minimized` (ignored when `headless`, since there is no OS window to hide) minimizes
    the window right after launch via the returned session's `.window`; if that fails
    (platform/CDP hiccup), per BrowserWindow it never raises -- the window is simply
    left visible.
    """
    profile_dir = Path(profile_dir)
    _private_dir(profile_dir)
    pw = sync_playwright().start()
    context: BrowserContext | None = None
    try:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1400, "height": 900},
            args=list(LAUNCH_ARGS),
        )
        context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
        context.set_default_timeout(DEFAULT_NAV_TIMEOUT_MS)
        cookies_file = COOKIES_FILE if Path(profile_dir) == PROFILE_DIR else Path(profile_dir) / "cookies.json"
        _restore_cookies(context, cookies_file)
        page = context.pages[0] if context.pages else context.new_page()
        window = BrowserWindow(context, page)
        if minimized and not headless:
            window.minimize()
        yield BrowserSession(context=context, page=page, window=window)
    finally:
        if context is not None:
            _save_cookies(context, cookies_file)
            try:
                context.close()
            except Exception:
                logger.exception("error closing browser context")
        try:
            pw.stop()
        except Exception:
            logger.exception("error stopping playwright")


def _restore_cookies(context: BrowserContext, path: Path = COOKIES_FILE) -> None:
    """Re-add the site cookies saved at the last close (session cookies included)."""
    try:
        if path.exists():
            context.add_cookies(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        logger.exception("could not restore cookies from %s", path)


def _save_cookies(context: BrowserContext, path: Path = COOKIES_FILE) -> None:
    """Persist the site's cookies so the login survives a browser restart."""
    try:
        cookies = [c for c in context.cookies() if COOKIE_DOMAIN in c["domain"]]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps(cookies), encoding="utf-8")
    except Exception as e:
        # Browser already gone (the user quit it) or mid-shutdown (CDP answers
        # "Browser context management is not supported"); the previous file is kept.
        if "has been closed" in str(e) or "context management is not supported" in str(e):
            logger.warning("çerezler kaydedilemedi: bot tarayıcısı zaten kapanmış")
        else:
            logger.exception("could not save cookies to %s", path)


def save_cookies_now(context: BrowserContext, path: Path = COOKIES_FILE) -> None:
    """Public wrapper over `_save_cookies`: persist the session's cookies right now,
    instead of waiting for the browser to close -- e.g. right after a successful login
    (src.site.Site.auto_login), so a crash/kill right afterward doesn't lose the session.
    Never raises (see `_save_cookies`)."""
    _save_cookies(context, path)


class Artifacts:
    """Saves screenshot + HTML snapshots of the page for debugging and audit."""

    def __init__(self, enabled: bool = True, directory: Path = ARTIFACTS_DIR,
                 window: "BrowserWindow | None" = None) -> None:
        self.enabled = enabled
        self.directory = Path(directory)
        # When the browser is minimized a screenshot cannot be captured and only times out
        # (5 s, seen live on macOS) -- that would stall every booking step. Skip it then.
        self.window = window

    def save(self, page: Page, step: str, force: bool = False) -> None:
        """Write <ts>_<step>.png (full page) and .html. Never raises; logs failures.

        The screenshot (5s timeout -- a minimized/occluded window can otherwise hang
        or time out at Playwright's much longer default) and the HTML dump are
        independent: if one fails, the other is still attempted and written.
        """
        if not (self.enabled or force):
            return
        try:
            _private_dir(self.directory)
        except Exception:
            logger.exception("failed to create artifacts directory %s", self.directory)
            return

        ts = datetime.now().strftime("%Y%m%d-%H%M%S.%f")[:-3]
        safe_step = re.sub(r"[^A-Za-z0-9_-]+", "_", step).strip("_") or "step"
        base_name = f"{ts}_{safe_step}"

        minimized = self.window is not None and self.window.state() == "minimized"
        if not minimized:
            try:
                page.screenshot(
                    path=str(self.directory / f"{base_name}.png"), full_page=True, timeout=5000
                )
            except Exception:
                logger.exception("failed to save screenshot for step %r", step)

        try:
            (self.directory / f"{base_name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            logger.exception("failed to save html for step %r", step)

    def prune(self, keep: int = 500) -> None:
        """Delete all but the `keep` most recent artifact pairs. Never raises."""
        try:
            if not self.directory.exists():
                return
            stems = sorted(
                {p.stem for p in self.directory.glob("*.png")}
                | {p.stem for p in self.directory.glob("*.html")},
                reverse=True,
            )
            for stem in stems[keep:]:
                for ext in (".png", ".html"):
                    f = self.directory / f"{stem}{ext}"
                    if f.exists():
                        f.unlink()
        except Exception:
            logger.exception("failed to prune artifacts")


class RateLimiter:
    """Bounds calls to at most `max_per_min` in any rolling 60-second window.

    `sleep` is injectable so the engine can pass a stop-aware wait (e.g. a
    `threading.Event`'s `.wait`) instead of a plain `time.sleep`: it accepts any
    callable(seconds), and if it returns a truthy value (Event.wait returns True once
    the event is set) acquire() raises `Stopped` right away instead of finishing the
    rate-limit wait, so a Stop request unwinds promptly instead of only after the full
    rolling-window wait.
    """

    _WINDOW_S = 60.0

    def __init__(self, max_per_min: int, sleep: Callable[[float], object] = time.sleep) -> None:
        if max_per_min <= 0:
            raise ValueError("max_per_min must be > 0")
        self.max_per_min = max_per_min
        self._sleep = sleep
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        """Wait (via the injected `sleep`) until issuing another call stays in budget.

        Raises `Stopped` immediately if `sleep` signals a stop (truthy return) while
        waiting.
        """
        self._evict()
        if len(self._timestamps) >= self.max_per_min:
            sleep_for = self._WINDOW_S - (time.monotonic() - self._timestamps[0])
            if sleep_for > 0:
                if self._sleep(sleep_for):
                    raise Stopped("stop requested while waiting for rate limit")
            self._evict()
        self._timestamps.append(time.monotonic())

    def _evict(self) -> None:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] >= self._WINDOW_S:
            self._timestamps.popleft()


class AttemptLimiter:
    """Caps events to at most `max_attempts` in any rolling `window_s` window.

    Used to guard Site.auto_login against account lockout from repeated attempts.
    Pure/testable: `clock` is a zero-arg callable returning monotonic seconds (defaults to
    time.monotonic), injectable so tests don't need to sleep for real.
    """

    def __init__(self, max_attempts: int, window_s: float, clock=time.monotonic) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._clock = clock
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        """Record and allow one more attempt if under budget; otherwise refuse (no record).

        Kept for callers where every call is inherently "an attempt" worth counting.
        Site.auto_login instead uses `blocked()` (a non-consuming peek) before trying,
        and `record()` only when that attempt actually failed -- see its docstring.
        """
        self._evict()
        if len(self._timestamps) >= self.max_attempts:
            return False
        self._timestamps.append(self._clock())
        return True

    def blocked(self) -> bool:
        """True if already at the attempt cap. A non-consuming peek -- never records."""
        self._evict()
        return len(self._timestamps) >= self.max_attempts

    def record(self) -> None:
        """Record one more consumed attempt (e.g. a failed login), regardless of capacity."""
        self._evict()
        self._timestamps.append(self._clock())

    def _evict(self) -> None:
        now = self._clock()
        while self._timestamps and now - self._timestamps[0] >= self.window_s:
            self._timestamps.popleft()


def setup_logging(
    verbose: bool = False, gui_handler: logging.Handler | None = None
) -> logging.Logger:
    """Configure the "ibbspor" logger tree: stdout + rotating file (5 x 2MB), UTF-8.

    Idempotent: safe to call repeatedly (e.g. every GUI Start) -- any handlers from a
    previous call are closed and replaced rather than stacked, so the logger never
    ends up with duplicate/leaked handlers.

    Skips the stdout StreamHandler when `sys.stdout is None` (pythonw.exe on Windows
    runs with no console/stdout at all; attaching a handler to it would raise on the
    first emit). The rotating file handler is unaffected and always added.

    If `gui_handler` is given (e.g. a handler that enqueues `Log` events for a Tk
    GUI), it is attached as well, so GUI mode gets log lines without a stdout handler.
    """
    _private_dir(LOG_DIR)
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger("ibbspor")
    root.setLevel(level)
    for old_handler in list(root.handlers):
        try:
            old_handler.close()
        except Exception:
            pass
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(level)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if gui_handler is not None:
        root.addHandler(gui_handler)

    return root


def run_login(cfg: "Config", timeout_s: float = 600) -> bool:
    """Open the bot browser and wait (no terminal input) until the user has logged in."""
    from src.site import Site  # local import: site.py depends on this module

    with open_browser(headless=False) as session:
        site = Site(session.page, cfg, Artifacts(enabled=False), RateLimiter(cfg.timing.max_requests_per_min))
        if site.wait_for_login(timeout_s):
            print("Giriş başarılı, oturum kaydedildi.")
            return True
        print("Süre doldu, giriş algılanamadı. `python bot.py login` ile tekrar deneyin.")
        return False
