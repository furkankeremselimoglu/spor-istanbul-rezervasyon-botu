"""Local web UI backend: a stdlib `ThreadingHTTPServer` + a pump thread that drains the
engine's events queue into `src.controller.Controller`, serving the JSON API and the
static frontend (`src/web/`, owned by a separate engineer) described in
`analysis/web-ui-api.md` -- THAT FILE IS THE CONTRACT. Read it first; this module
implements every endpoint, security rule and JSON shape it specifies.

`app.pyw` owns the process: it builds the `EngineThread` (or a demo `_FakeEngine`), a
`WebServer`, starts both, then shows the URL in a pywebview window (or a browser tab).
No Tk anywhere in this module.

Security (see analysis/web-ui-api.md, "Security"):
    - Bind 127.0.0.1 only; a random per-process token must be sent as `X-Token` on every
      `/api/*` call (the frontend reads it from the `?t=` query string on the page URL).
    - `Host` header must name this exact host:port (blocks DNS rebinding).
    - Static files are served only from `static_dir`, with directory-traversal blocked
      even through percent-encoded paths.
    - The password from `POST /api/credentials` is used once (via `src.credentials.save`)
      and never logged or echoed back.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import re
import secrets
import sys
import threading
import time
from collections import deque
from datetime import date, datetime
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from src import catalog as catalog_mod
from src import credentials as credentials_mod
from src import events as ev
from src import notify as notify_mod
from src import reminders as reminders_mod
from src import settings as settings_mod
from src import updater as updater_mod
from src.controller import Controller

log = logging.getLogger("ibbspor.webapp")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATIC_DIR = ROOT / "src" / "web"

_LONG_POLL_TIMEOUT_S = 10.0

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

_BUSY_ERROR = "Önce durdurun"

# Any single /api/* request body larger than this is rejected with 413 before it is even
# read into memory (see fix list item 9 -- security hygiene).
_MAX_BODY_BYTES = 64 * 1024

# How often (wall-clock seconds, checked from the pump thread's otherwise-idle poll tick)
# to stat() state/catalog.json for a change the engine made without emitting CatalogUpdated
# (it only saves periodically during a long scan -- see fix list item 8).
_CATALOG_MTIME_POLL_INTERVAL_S = 5.0

# Self-update background check (see src/updater.py, decision 8 in the design doc): first
# check this long after start(), then every this often. Module constants so tests can
# monkeypatch them to something fast instead of waiting hours.
_UPDATE_CHECK_INITIAL_DELAY_S = 5.0
_UPDATE_CHECK_INTERVAL_S = 6 * 60 * 60

_UPDATE_BUSY_ERROR = "Bot çalışırken güncellenemez; önce durdurun"

# Turkish text shown instead of a banner that looks like a raw Python/Playwright
# exception (fix list item 10). The raw text always stays in the log (see _append_log
# calls in _handle_event), only the *banner* is replaced.
_GENERIC_ERROR_BANNER_TR = (
    "Beklenmeyen bir hata oluştu; bot kendini toparlamaya çalışıyor. "
    "Ayrıntı: Hesap › Tanı paketi."
)

_TURKISH_LETTERS = set("ışğüöçİŞĞÜÖÇ")
_RAW_EXCEPTION_RE = re.compile(r"Traceback \(most recent call last\)|Error:|\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b")


def _looks_like_raw_exception(text: str) -> bool:
    """Best-effort heuristic: does `text` look like a raw Python/Playwright exception
    (rather than one of this app's own Turkish messages)? Used only to decide whether to
    swap the *banner* text for a generic Turkish one -- the raw text is always kept in
    the log. See fix list item 10."""
    if not text:
        return False
    if _RAW_EXCEPTION_RE.search(text):
        return True
    if len(text) > 20 and not any(ch in _TURKISH_LETTERS for ch in text):
        ascii_count = sum(1 for ch in text if ord(ch) < 128)
        if ascii_count / len(text) > 0.98:
            return True
    return False


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_client_log_text(text: str) -> str:
    """Strip newlines/control chars from UI-reported diagnostic text before it is ever
    written to state/logs/bot.log -- a misbehaving/compromised page must not be able to
    forge extra log lines or inject terminal control sequences. See fix list item 9."""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _CONTROL_CHARS_RE.sub("", text)


def _content_type(path: Path) -> str:
    ctype = _CONTENT_TYPES.get(path.suffix.lower())
    if ctype:
        return ctype
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _check_keyring() -> bool:
    """Best-effort: is a real OS keychain backend available (not keyring's no-op "fail"
    backend)? Never raises. Mirrors the Tk GUI's own `_check_keyring`."""
    try:
        import keyring

        backend = keyring.get_keyring()
        if backend is None:
            return False
        return backend.__class__.__module__ != "keyring.backends.fail"
    except Exception:
        return False


def _make_handler(server: "WebServer") -> type[BaseHTTPRequestHandler]:
    """A `BaseHTTPRequestHandler` subclass bound to `server` via closure (stdlib's
    `HTTPServer` only ever constructs the handler with `(request, client_address,
    server)`, so there is no other clean way to hand it our `WebServer` instance)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "IBBSporWeb/1"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            server._handle(self, "GET")

        def do_POST(self) -> None:  # noqa: N802
            server._handle(self, "POST")

        def do_PUT(self) -> None:  # noqa: N802
            server._handle(self, "PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            server._handle(self, "DELETE")

    return Handler


class _PollActivityHandler(logging.Handler):
    """Attached directly to the `ibbspor.site` logger (not the `ibbspor` root -- see
    src.browser.setup_logging, which owns and periodically replaces the root logger's
    handlers) so the dashboard's "Son kontrol" indicator (fix list item 5) has a signal
    for "the engine just actually talked to the site", independent of how/whether that
    line is also relayed to the GUI as an `ev.Log` event. Never raises from `emit`."""

    def __init__(self, on_activity: Callable[[], None]) -> None:
        super().__init__()
        self._on_activity = on_activity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if msg.startswith("refresh done") or msg.startswith("switch_to"):
                self._on_activity()
        except Exception:
            self.handleError(record)


class _LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind.

    The stdlib's HTTPServer.server_bind calls socket.getfqdn(host); on machines with
    slow/unreachable DNS (seen on a macOS CI runner) that blocks app startup for 30 s+.
    We only ever bind 127.0.0.1, so the name is not needed.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class WebServer:
    """Owns the HTTP server, the event pump, and the (single) `Controller` for the
    whole app life. One instance per process; `app.pyw` is the only caller."""

    def __init__(
        self,
        cfg: object,
        engine: object,
        events_queue: "queue.Queue",
        *,
        mode: str = "window",
        host: str = "127.0.0.1",
        port: int = 0,
        static_dir: Path = DEFAULT_STATIC_DIR,
        on_quit: Callable[[], None] | None = None,
        on_attention: Callable[[str], None] | None = None,
        reminder_sync: Callable[..., object] | None = None,
        update_check: Callable[[], updater_mod.Release | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.events_queue = events_queue
        self.mode = mode
        self.host = host
        self.requested_port = port
        self.port = port
        self.static_dir = Path(static_dir).resolve()
        self.on_quit = on_quit
        self.on_attention = on_attention
        self.reminder_sync = reminder_sync
        self.update_check = update_check
        self.token = secrets.token_urlsafe(32)
        # F6: one-time nonces minted by launch_url() -- consumed (and replaced with the
        # real token) the moment index.html is served, so the real token never has to
        # appear in a URL a window/webview history or OS process list could retain.
        self._launch_nonces: set[str] = set()

        try:
            self._tz = ZoneInfo(cfg.site.timezone)
        except Exception:
            self._tz = ZoneInfo("Europe/Istanbul")

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._version = 0

        self.controller = Controller(send=self.engine.send)
        self._settings = settings_mod.load_settings(
            path=settings_mod.SETTINGS_PATH, legacy_cfg=cfg
        )
        self._catalog = catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH)
        self._catalog_version = 0
        try:
            self._catalog_mtime: float | None = catalog_mod.CATALOG_PATH.stat().st_mtime
        except OSError:
            self._catalog_mtime = None
        self._last_catalog_mtime_check = 0.0

        self._log_seq = 0
        self._log: "deque[dict]" = deque(maxlen=300)

        self._next_window: ev.NextWindow | None = None
        self._sms_error: str | None = None
        self._ever_logged_in = False
        self._attempts_left: int | None = None
        self._keyring_ok = _check_keyring()
        self._has_creds = False
        self._tc_masked: str | None = None
        # Multiple accounts (2026-10-18): self._account_name is the live member name of
        # the currently logged-in session (set by AccountInfo events) -- distinct from
        # each account's STORED name (self._accounts, from accounts.json), which only
        # catches up once _on_account_info persists it via credentials_mod.set_name.
        self._accounts: list = []
        self._active_account_id: str | None = None
        self._account_name: str | None = None
        self._refresh_credentials_cache()

        self._last_poll: float | None = None  # fix list item 5: "Son kontrol"
        self._poll_activity_handler = _PollActivityHandler(self._on_poll_activity)
        self._app_version = self._load_app_version()

        # Self-update (src/updater.py): self._update is the "update" key of /api/state
        # (None | {"version", "status", "error"}); self._update_release is the matching
        # Release the "update" command stages. self.update_ready is read by app.pyw
        # after stop() to hand off to updater.launch_apply(). update_check is None for
        # a git checkout, tests, --smoke and --demo (see app.pyw) -- no check thread.
        self._update: dict | None = None
        self._update_release: updater_mod.Release | None = None
        self._notified_update_versions: set[str] = set()
        self.update_ready: tuple[Path, str] | None = None

        # Reminder notifications (2026-10-18): see analysis/reminders-design-2026-10.md,
        # "webapp / app integration (Task C)". `reminder_sync` is None for tests/--smoke/
        # --demo (no OS calls at all). `_reminder_sync_busy`/`_reminder_sync_rerun*` are
        # the minimal "coalesce while in flight" scheme: a request made while a sync is
        # already running on its own thread just arms one more run right after, instead
        # of spawning a second thread.
        self._reminders_status: dict = {"registered": False, "error": None, "synced_at": None}
        self._reminder_sync_busy = False
        self._reminder_sync_rerun = False
        self._reminder_sync_rerun_force = False
        self._app_status_running: bool | None = None

        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()

    @staticmethod
    def _load_app_version() -> str:
        """`src.version.get_version()` may not exist yet (packaging engineer's file,
        landing separately) -- never let its absence or failure break the UI."""
        try:
            from src.version import get_version

            return str(get_version())
        except ImportError:
            return "—"
        except Exception:
            log.exception("webapp: sürüm bilgisi okunamadı")
            return "—"

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        handler_cls = _make_handler(self)
        self._server = _LocalHTTPServer((self.host, self.requested_port), handler_cls)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="WebServerHTTP", daemon=True
        )
        self._server_thread.start()

        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="WebServerPump", daemon=True
        )
        self._pump_thread.start()

        logging.getLogger("ibbspor.site").addHandler(self._poll_activity_handler)

        self._app_status_running = False
        reminders_mod.write_app_status(False)
        self._request_reminder_sync(force=True)

        if self.update_check is not None:
            threading.Thread(
                target=self._update_check_loop, name="WebServerUpdateCheck", daemon=True
            ).start()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?t={self.token}"

    def launch_url(self) -> str:
        """A fresh one-time nonce URL for opening the UI window (fix list item F6):
        unlike `url`, this never carries the real token -- _serve_static spends the
        nonce (and injects the real token into index.html's meta tag) the moment the
        page is served."""
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            self._launch_nonces.add(nonce)
        return f"http://{self.host}:{self.port}/?t={nonce}"

    def stop(self) -> None:
        self._pump_stop.set()
        reminders_mod.clear_app_status()

        try:
            logging.getLogger("ibbspor.site").removeHandler(self._poll_activity_handler)
        except Exception:
            pass

        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                log.exception("webapp: sunucu kapatılırken hata")
            try:
                server.server_close()
            except Exception:
                pass

        thread, self._server_thread = self._server_thread, None
        if thread is not None:
            thread.join(timeout=5)

        pump, self._pump_thread = self._pump_thread, None
        if pump is not None:
            pump.join(timeout=2)

    # ---------------------------------------------------------------- event pump

    def _pump_loop(self) -> None:
        while not self._pump_stop.is_set():
            try:
                event = self.events_queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_reload_catalog_from_disk()
                continue
            with self._lock:
                try:
                    self._handle_event(event)
                except Exception:
                    log.exception("webapp: olay işlenirken hata: %r", event)
                self._sync_app_status()
                self._version += 1
                self._cond.notify_all()
            if isinstance(event, ev.CatalogUpdated):
                self._request_reminder_sync()

    def _maybe_reload_catalog_from_disk(self) -> None:
        """Fix list item 8: the engine saves state/catalog.json periodically (every ~5
        min) during a long scan without necessarily emitting `CatalogUpdated` for every
        save. Poll its mtime from here (the pump thread's otherwise-idle tick) so the UI
        picks up those saves too, not only the explicit event."""
        now = time.monotonic()
        if now - self._last_catalog_mtime_check < _CATALOG_MTIME_POLL_INTERVAL_S:
            return
        self._last_catalog_mtime_check = now
        try:
            mtime = catalog_mod.CATALOG_PATH.stat().st_mtime
        except OSError:
            return
        if mtime == self._catalog_mtime:
            return
        with self._lock:
            self._catalog = catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH)
            self._catalog_version += 1
            self._catalog_mtime = mtime
            self._bump_version()
        self._request_reminder_sync()

    def _on_poll_activity(self) -> None:
        """Called (from `_PollActivityHandler`, i.e. from whatever thread is logging --
        normally the engine thread) whenever the site logger reports a completed
        refresh/switch_to. See fix list item 5."""
        with self._lock:
            self._last_poll = time.time()
            self._bump_version()

    def _update_check_loop(self) -> None:
        """Daemon thread, only started when `update_check` is set (see `start()`):
        checks `_UPDATE_CHECK_INITIAL_DELAY_S` after start, then every
        `_UPDATE_CHECK_INTERVAL_S`, until `stop()` sets `self._pump_stop`."""
        if self._pump_stop.wait(timeout=_UPDATE_CHECK_INITIAL_DELAY_S):
            return
        while True:
            self._run_update_check()
            if self._pump_stop.wait(timeout=_UPDATE_CHECK_INTERVAL_S):
                return

    def _run_update_check(self) -> None:
        """Runs `self.update_check()` off the lock (it makes a real network call);
        never raises. On a release newer than the running app, records it as
        `"available"` and fires an OS notification -- once per version (see
        `self._notified_update_versions`, decision 10 in the design doc)."""
        try:
            release = self.update_check()
        except Exception:
            log.exception("webapp: güncelleme kontrolü başarısız")
            return
        if release is None or not updater_mod.is_newer(self._app_version, release.version):
            return

        notify_needed = False
        with self._lock:
            if self._update is not None and self._update["status"] == "downloading":
                return  # a download is already in flight -- don't clobber it
            self._update_release = release
            self._update = {"version": release.version, "status": "available", "error": None}
            if release.version not in self._notified_update_versions:
                self._notified_update_versions.add(release.version)
                notify_needed = True
            self._bump_version()

        if notify_needed:
            notify_mod.notify(
                "Güncelleme mevcut",
                f"Uygulamaya güncelleme geldi (v{release.version}), lütfen güncelleyiniz.",
            )

    def _run_update_stage(self, release: updater_mod.Release) -> None:
        """Daemon thread started by the `"update"` command: downloads+extracts
        `release` off the lock (real I/O). Success -> `self.update_ready` and quit;
        failure -> `status: "error"` (the UI's "Tekrar dene" just resends `"update"`)."""
        try:
            new_dir = updater_mod.stage(release)
        except updater_mod.UpdateError as e:
            with self._lock:
                self._update = {"version": release.version, "status": "error", "error": str(e)}
                self._bump_version()
            return
        except Exception as e:
            log.exception("webapp: güncelleme hazırlanamadı")
            with self._lock:
                self._update = {"version": release.version, "status": "error", "error": str(e)}
                self._bump_version()
            return

        with self._lock:
            self.update_ready = (new_dir, release.tag)
            self._bump_version()
        self._trigger_quit()

    def _sync_app_status(self) -> None:
        """Must be called with `self._lock` held. Writes `state/app_status.json` iff the
        controller's `running` flag changed since the last check (see analysis/
        reminders-design-2026-10.md -- the OS-scheduler fire-time program uses this file
        to tell "app open & engine running" apart from "app open & idle")."""
        running = self.controller.state.running
        if running != self._app_status_running:
            self._app_status_running = running
            reminders_mod.write_app_status(running)

    def _request_reminder_sync(self, force: bool = False) -> None:
        """Recompute the weekly reminder slots from the current settings/catalog and
        push them to the OS scheduler via `self.reminder_sync`, on a daemon thread --
        never while holding `self._lock` (it may shell out to schtasks/launchctl).
        `self.reminder_sync is None` (tests, --smoke, --demo) -> no-op. A request made
        while a sync is already in flight doesn't spawn a second thread; it just arms one
        more run right after the current one finishes (settings/catalog can be saved
        repeatedly in quick succession)."""
        if self.reminder_sync is None:
            return
        with self._lock:
            if self._reminder_sync_busy:
                self._reminder_sync_rerun = True
                self._reminder_sync_rerun_force = self._reminder_sync_rerun_force or force
                return
            self._reminder_sync_busy = True
            settings, catalog = self._settings, self._catalog
        threading.Thread(
            target=self._run_reminder_sync, args=(settings, catalog, force),
            name="WebServerReminderSync", daemon=True,
        ).start()

    def _run_reminder_sync(self, settings: object, catalog: object, force: bool) -> None:
        while True:
            try:
                now = datetime.now(self._tz)
                slots = reminders_mod.weekly_slots(
                    reminders_mod.upcoming(
                        settings, catalog, self.cfg.timing, self._tz, now,
                        tennis_last_pick=self._tennis_last_pick(),
                    )
                )
                result = self.reminder_sync(slots, settings.reminders_enabled, force)
            except Exception:
                log.exception("webapp: hatırlatma zamanlayıcısına kaydedilemedi")
                result = None
            with self._lock:
                if result is not None:
                    self._reminders_status = {
                        "registered": result.registered,
                        "error": result.error,
                        "synced_at": datetime.now(self._tz).isoformat(),
                    }
                self._bump_version()
                if self._reminder_sync_rerun:
                    self._reminder_sync_rerun = False
                    force = self._reminder_sync_rerun_force
                    self._reminder_sync_rerun_force = False
                    settings, catalog = self._settings, self._catalog
                    continue
                self._reminder_sync_busy = False
                return

    def _trigger_attention(self, reason: str) -> None:
        """Fix list item 1 (P0 SMS attention): ask the app window to come to the front.
        Runs the callback on its own thread -- it may make blocking OS/window calls
        (pywebview, ctypes, AppKit) and must never hold up the pump thread's lock."""
        cb = self.on_attention
        if cb is None:
            return

        def _run() -> None:
            try:
                cb(reason)
            except Exception:
                log.exception("webapp: on_attention çağrısı hata verdi (%s)", reason)

        threading.Thread(target=_run, name="WebServerAttention", daemon=True).start()

    def _handle_event(self, event: object) -> None:
        """Must be called with `self._lock` held."""
        if isinstance(event, ev.AccountInfo):
            # Handled entirely here, never forwarded to self.controller (which has no
            # notion of accounts and would otherwise log it via its "unknown event"
            # fallback -- the member's full name must NEVER be logged).
            self._on_account_info(event)
            return

        if isinstance(event, ev.Log):
            self._append_log(event.level, event.line)
        elif isinstance(event, ev.Found):
            self._append_log("INFO", f"Bulundu: {event.target_name} · {event.card_text}")
        elif isinstance(event, ev.Booked):
            self._append_log("INFO", event.message or f"Rezervasyon tamam: {event.card_text}")
        elif isinstance(event, ev.Failed):
            self._append_log("WARNING", event.message)
        elif isinstance(event, ev.SmsResult):
            self._append_log("INFO" if event.ok else "WARNING", event.message)
            self._sms_error = None if event.ok else event.message
        elif isinstance(event, ev.LoggedOut):
            self._append_log("WARNING", "Oturum kapandı.")
        elif isinstance(event, ev.Error):
            self._append_log("ERROR" if event.fatal else "WARNING", event.message)

        self.controller.handle(event)

        if isinstance(event, ev.NextWindow):
            self._next_window = event
        elif isinstance(event, ev.SmsNeeded):
            self._sms_error = None
        elif isinstance(event, ev.CatalogUpdated):
            self._catalog = catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH)
            self._catalog_version += 1
            try:
                self._catalog_mtime = catalog_mod.CATALOG_PATH.stat().st_mtime
            except OSError:
                pass
        elif isinstance(event, ev.LoginResult):
            self._attempts_left = event.attempts_left
            self._refresh_credentials_cache()
            if event.ok:
                self._ever_logged_in = True
        elif isinstance(event, ev.LoggedOut):
            # Fix list item 4: must reset so the sidebar/start-gate reflect reality again
            # (re-set on the next successful LoginResult or monitoring/hot Status, below).
            self._ever_logged_in = False
        elif isinstance(event, (ev.Found, ev.Booked, ev.Memberships, ev.Reservations)):
            self._ever_logged_in = True
        elif isinstance(event, ev.Status) and event.state in ("monitoring", "hot"):
            self._ever_logged_in = True

        # Fix list item 1 (P0 SMS attention): raise the app window on an SMS prompt or a
        # run-ending result, independent of the branches above.
        if isinstance(event, ev.SmsNeeded):
            self._trigger_attention("sms")
        elif isinstance(event, (ev.Booked, ev.Failed)):
            self._trigger_attention("result")

    def _append_log(self, level: str, text: str) -> None:
        self._log_seq += 1
        self._log.append(
            {"seq": self._log_seq, "ts": datetime.now(self._tz).isoformat(), "level": level, "text": text}
        )

    def _on_account_info(self, event: ev.AccountInfo) -> None:
        """Must be called with `self._lock` held. NEVER logs event.name."""
        self._account_name = event.name
        account_id = self._active_account_id
        if not account_id or not event.name:
            return
        current = next((a for a in self._accounts if a.id == account_id), None)
        if current is not None and current.name != event.name:
            try:
                credentials_mod.set_name(account_id, event.name)
            except Exception:
                log.exception("webapp: hesap adı güncellenemedi")
            self._refresh_credentials_cache()

    def _refresh_credentials_cache(self) -> None:
        creds = credentials_mod.load()
        self._has_creds = creds is not None
        self._tc_masked = credentials_mod.mask_tc(creds[0]) if creds else None
        try:
            self._accounts = credentials_mod.list_accounts()
        except Exception:
            log.exception("webapp: hesap listesi okunamadı")
            self._accounts = []
        active = next((a for a in self._accounts if a.active), None)
        new_active_id = active.id if active else None
        if new_active_id != self._active_account_id:
            # Switched (or first-ever) active account: forget the previous session's
            # live name until its own AccountInfo arrives; seed from the stored name
            # (may be "" until the first successful login -- see src.credentials).
            self._account_name = active.name if (active and active.name) else None
        self._active_account_id = new_active_id

    # ---------------------------------------------------------------- state -> JSON

    def _iso_dt(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt.isoformat()

    @staticmethod
    def _iso_date(d: date | None) -> str | None:
        return d.isoformat() if d is not None else None

    def _reservation_dict(self, r: object) -> dict:
        return {
            "date": self._iso_date(r.date),
            "start": r.start,
            "end": r.end,
            "facility": r.facility,
            "hall": r.hall,
            "status": r.status,
            "cancellable": r.cancel_id is not None,
        }

    def _start_block_reason(self, st) -> str | None:
        if st.running:
            return "Bot zaten çalışıyor"
        if st.busy:
            return "Başka bir işlem sürüyor"
        if not any(t.enabled for t in self._settings.targets):
            return "Önce Hedefler'den bir hedef ekleyin"
        if not (self._has_creds or self._ever_logged_in):
            return "Hesap bölümünden giriş bilgilerinizi kaydedin"
        return None

    def _tennis_last_pick(self) -> datetime | None:
        """Ekim 2026 duyurusu: the member's latest tennis pick, from the cache written
        wherever reservations are fetched (`src.controller._on_reservations`). Never
        raises (`read_tennis_history` doesn't)."""
        pick = reminders_mod.read_tennis_history()
        return pick.replace(tzinfo=self._tz) if pick is not None else None

    def _build_reminders_dict(self) -> dict:
        """Must be called with `self._lock` held. Never raises: `upcoming` is `[]` both
        when reminders are disabled and when computing it throws (odd settings/catalog)
        -- see analysis/reminders-design-2026-10.md, API contract."""
        enabled = self._settings.reminders_enabled
        upcoming: list[dict] = []
        if enabled:
            try:
                now = datetime.now(self._tz)
                for r in reminders_mod.upcoming(
                    self._settings, self._catalog, self.cfg.timing, self._tz, now,
                    tennis_last_pick=self._tennis_last_pick(),
                )[:5]:
                    upcoming.append({
                        "remind_at": self._iso_dt(r.remind_at),
                        "open_at": self._iso_dt(r.open_at),
                        "session_start": self._iso_dt(r.session_start),
                        "target_name": r.target_name,
                    })
            except Exception:
                log.exception("webapp: hatırlatmalar hesaplanamadı")
                upcoming = []
        status = self._reminders_status
        return {
            "enabled": enabled,
            "supported": sys.platform in ("win32", "darwin"),
            "registered": status["registered"],
            "error": status["error"],
            "synced_at": status["synced_at"],
            "upcoming": upcoming,
        }

    def _build_state_dict(self) -> dict:
        """Must be called with `self._lock` held."""
        st = self.controller.state
        reason = self._start_block_reason(st)

        next_window = None
        nw = self._next_window
        if nw is not None:
            next_window = {
                "target_name": nw.target_name,
                "branch": nw.group[0],
                "facility": nw.group[1],
                "session_start": self._iso_dt(nw.session_start),
                "open_at": self._iso_dt(nw.open_at),
            }

        sms = None
        if st.sms_visible:
            deadline = (
                self._iso_dt(datetime.fromtimestamp(st.sms_deadline, tz=self._tz))
                if st.sms_deadline is not None
                else None
            )
            sms = {"card": st.sms_card, "deadline": deadline, "error": self._sms_error}

        banner = None
        if st.banner:
            kind = st.banner_kind or "error"
            text = st.banner
            if kind == "error" and _looks_like_raw_exception(text):
                # Fix list item 10: the raw text is already in self._log (appended by
                # _handle_event's Error/Failed/Stopped-error branches); only the banner
                # itself is swapped for something a non-developer can act on.
                text = _GENERIC_ERROR_BANNER_TR
            banner = {"kind": kind, "text": text}

        catalog_progress = None
        if st.catalog_progress:
            done, total, label = st.catalog_progress
            catalog_progress = {"done": done, "total": total, "label": label}

        return {
            "version": self._version,
            "now": datetime.now(self._tz).isoformat(),
            "engine": {
                "state": st.engine_state,
                "status_text": st.status_text,
                "running": st.running,
                "busy": st.busy,
                "can_start": st.can_start and reason is None,
                "can_stop": st.can_stop,
                "start_block_reason": reason,
                "dry_run": bool(getattr(self, "_run_dry_run", False)) if st.running else None,
            },
            "next_window": next_window,
            "reminders": self._build_reminders_dict(),
            "sms": sms,
            "banner": banner,
            "log": list(self._log),
            "catalog_progress": catalog_progress,
            "catalog_version": self._catalog_version,
            "account": {
                "has_credentials": self._has_creds,
                "tc_masked": self._tc_masked,
                "keyring_ok": self._keyring_ok,
                "login_text": st.login_text,
                "attempts_left": self._attempts_left,
                "logged_in": self._has_creds or self._ever_logged_in,
                "name": self._account_name,
                "active_id": self._active_account_id,
                "accounts": [
                    {"id": a.id, "name": a.name, "tc_masked": a.tc_masked, "active": a.active}
                    for a in self._accounts
                ],
            },
            "memberships": list(st.memberships),
            "reservations": [self._reservation_dict(r) for r in st.reservations],
            "settings": settings_mod.settings_to_dict(self._settings),
            "targets_summary": [
                {"id": t.id, "describe": t.describe()} for t in self._settings.targets
            ],
            "dry_run_ui": self._settings.dry_run,
            "mode": self.mode,
            "update": dict(self._update) if self._update is not None else None,
            "last_poll": (
                self._iso_dt(datetime.fromtimestamp(self._last_poll, tz=self._tz))
                if self._last_poll is not None
                else None
            ),
            # NOTE: named "app_version" (not "version") because the top-level "version"
            # key is already the long-poll state counter the frontend's pollLoop depends
            # on for `since=` -- see analysis/web-ui-api.md.
            "app_version": self._app_version,
        }

    # ---------------------------------------------------------------- command dispatch

    def _dispatch_command(self, body: dict) -> tuple[int, dict]:
        """Returns (http_status, response_dict). Must be called with `self._lock` held."""
        ctype = body.get("type")
        st = self.controller.state

        if ctype == "start":
            if st.running:
                return 200, {"ok": False, "error": "Bot zaten çalışıyor"}
            if self._update is not None and self._update["status"] == "downloading":
                return 200, {"ok": False, "error": "Güncelleme indiriliyor; lütfen bekleyin"}
            self._run_dry_run = bool(body.get("dry_run", False))
            log.info("başlatılıyor: %s", "DENEME MODU (Kaydet'e basılmaz)" if self._run_dry_run
                     else "GERÇEK MOD (uygun seans bulunursa rezervasyon yapılır)")
            self.controller.start(dry_run=self._run_dry_run)
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "stop":
            self.controller.stop()
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "sms":
            err = self.controller.submit_sms(str(body.get("code", "")))
            if err:
                return 200, {"ok": False, "error": err}
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "scan":
            depth = body.get("depth")
            if depth not in ("branches", "facilities", "grids"):
                return 400, {"ok": False, "error": "Geçersiz tarama derinliği"}
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            branches = body.get("branches")
            if branches is not None and not isinstance(branches, list):
                return 400, {"ok": False, "error": "branches bir liste olmalı"}
            self.controller.scan(depth, branches)
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "check_login":
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            self.controller.check_login()
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "manual_login":
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            self.controller.manual_login()
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "show_browser":
            self.controller.show_browser()
            return 200, {"ok": True}

        if ctype == "hide_browser":
            self.controller.hide_browser()
            return 200, {"ok": True}

        if ctype == "list_reservations":
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            self.controller.list_reservations()
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "list_memberships":
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            self.controller.list_memberships()
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "cancel":
            try:
                day = date.fromisoformat(str(body.get("date")))
            except (TypeError, ValueError):
                return 400, {"ok": False, "error": "Geçersiz tarih"}
            start = body.get("start")
            if not isinstance(start, str) or not start:
                return 400, {"ok": False, "error": "Geçersiz saat"}
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            self.controller.cancel(day, start)
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "switch_account":
            if st.running or st.busy:
                return 200, {"ok": False, "error": _BUSY_ERROR}
            account_id = body.get("id")
            if not isinstance(account_id, str) or not account_id:
                return 400, {"ok": False, "error": "Geçersiz hesap kimliği"}
            self.engine.send(ev.SwitchAccount(account_id=account_id))
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "dismiss_banner":
            self.controller.state.banner = None
            self.controller.state.banner_kind = None
            self._bump_version()
            return 200, {"ok": True}

        if ctype == "update":
            if st.running or st.busy or st.sms_visible or self._update is None:
                return 200, {"ok": False, "error": _UPDATE_BUSY_ERROR}
            if self._update["status"] == "downloading":
                return 200, {"ok": False, "error": _UPDATE_BUSY_ERROR}
            release = self._update_release
            self._update = {"version": release.version, "status": "downloading", "error": None}
            self._bump_version()
            threading.Thread(
                target=self._run_update_stage, args=(release,),
                name="WebServerUpdateStage", daemon=True,
            ).start()
            return 200, {"ok": True}

        if ctype == "quit":
            return 200, {"ok": True}

        if ctype == "demo_sms":
            # Demo-only hook (see app.pyw's --demo FakeEngine) used by
            # tools/ci_smoke.py to screenshot the SMS modal; a no-op/unsupported
            # error against the real EngineThread (which has no such method).
            fn = getattr(self.engine, "demo_sms", None)
            if callable(fn):
                fn()
                return 200, {"ok": True}
            return 400, {"ok": False, "error": "desteklenmiyor"}

        return 400, {"ok": False, "error": f"Bilinmeyen komut: {ctype!r}"}

    def _dispatch_export_diagnostics(self) -> tuple[int, dict]:
        """Fix list item 7. Deliberately NOT routed through `_dispatch_command` (so it
        never runs under `self._lock`): this does disk I/O (reading state/, zipping)
        that must not block /api/state long-polling for everyone else while it runs. It
        is still "in a thread" in the sense that matters -- ThreadingHTTPServer already
        gives every request its own thread, so this one simply does its work on that
        thread instead of the shared pump/lock."""
        try:
            from src.diagnostics import create_diagnostics_zip
        except ImportError:
            return 400, {"ok": False, "error": "Tanı paketi özelliği bu sürümde yok"}
        dest_dir = Path.home() / "Desktop"
        try:
            path = create_diagnostics_zip(dest_dir)
        except Exception as e:
            log.exception("webapp: tanı paketi oluşturulamadı")
            return 500, {"ok": False, "error": f"Tanı paketi oluşturulamadı: {e}"}
        return 200, {"ok": True, "path": str(path)}

    def _bump_version(self) -> None:
        """Must be called with `self._lock` held."""
        self._version += 1
        self._cond.notify_all()

    def _trigger_quit(self) -> None:
        if self.on_quit is not None:
            threading.Thread(target=self.on_quit, name="WebServerQuit", daemon=True).start()

    # ---------------------------------------------------------------- settings / credentials

    def _dispatch_settings_put(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "Ayarlar bir nesne (obje) olmalı"}
        with self._lock:
            if self.controller.state.running:
                return 409, {"ok": False, "error": "Bot çalışırken ayarlar değiştirilemez"}
            try:
                parsed = settings_mod.settings_from_dict(body)
                settings_mod.validate(parsed)
            except settings_mod.SettingsError as e:
                return 400, {"ok": False, "error": str(e)}
            try:
                settings_mod.save_settings(parsed, path=settings_mod.SETTINGS_PATH)
            except OSError as e:
                return 500, {"ok": False, "error": f"Kaydedilemedi: {e}"}
            self._settings = parsed
            self._bump_version()
            return 200, {"ok": True, "settings": settings_mod.settings_to_dict(parsed)}

    def _dispatch_catalog_get(self) -> tuple[int, dict]:
        with self._lock:
            return 200, {"version": self._catalog_version, "catalog": self._catalog.to_dict()}

    def _dispatch_credentials_post(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "Geçersiz istek"}
        tc = body.get("tc")
        password = body.get("password")
        with self._lock:
            st = self.controller.state
            if st.running or st.busy:
                return 409, {"ok": False, "error": "Bot çalışırken hesap değiştirilemez"}
            before = self._active_account_id
            try:
                credentials_mod.save(tc, password)
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}
            except Exception as e:
                log.exception("kimlik bilgileri kaydedilemedi")
                return 400, {"ok": False, "error": f"Anahtarlığa kaydedilemedi: {e}"}
            self._refresh_credentials_cache()
            if self._active_account_id != before:
                # F1: the active account changed -- drop the live site session (both
                # layers) so the previous member's cookies can never serve this one.
                self.engine.send(ev.ResetSession())
            self._bump_version()
            return 200, {"ok": True, "tc_masked": self._tc_masked}

    def _dispatch_credentials_delete(self, body: object = None) -> tuple[int, dict]:
        account_id = body.get("id") if isinstance(body, dict) else None
        with self._lock:
            st = self.controller.state
            if st.running or st.busy:
                return 409, {"ok": False, "error": "Bot çalışırken hesap değiştirilemez"}
            before = self._active_account_id
            if account_id:
                credentials_mod.remove(account_id)
            else:
                credentials_mod.clear()
            self._refresh_credentials_cache()
            if self._active_account_id != before:
                self.engine.send(ev.ResetSession())
            self._bump_version()
            return 200, {"ok": True}

    def _dispatch_state_get(self, since: int) -> dict:
        deadline = time.monotonic() + _LONG_POLL_TIMEOUT_S
        with self._cond:
            while self._version == since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(remaining, 1.0))
            return self._build_state_dict()

    # ---------------------------------------------------------------- HTTP plumbing

    def _check_host(self, h: BaseHTTPRequestHandler) -> bool:
        host = h.headers.get("Host", "")
        return host in (f"{self.host}:{self.port}", f"localhost:{self.port}")

    def _check_token(self, h: BaseHTTPRequestHandler) -> bool:
        token = h.headers.get("X-Token", "")
        return secrets.compare_digest(token, self.token)

    @staticmethod
    def _read_json_body(h: BaseHTTPRequestHandler) -> dict | None:
        try:
            length = int(h.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw = h.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _send_json(h: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json; charset=utf-8")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Cache-Control", "no-store")
            h.end_headers()
            h.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_static(self, h: BaseHTTPRequestHandler, path: str, query: dict) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if not rel:
            rel = "index.html"
        try:
            target = (self.static_dir / rel).resolve()
            target.relative_to(self.static_dir)
        except (ValueError, OSError):
            self._send_json(h, 403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(h, 404, {"error": "not found"})
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._send_json(h, 404, {"error": "not found"})
            return
        if rel == "index.html":
            # F6: the launch URL carries a one-time nonce, not the real token -- swap it
            # in here, the only place __TOKEN__ is ever filled in.
            t = query.get("t", [""])[0]
            with self._lock:
                if t in self._launch_nonces:
                    self._launch_nonces.discard(t)
                    token_bytes = self.token.encode("utf-8")
                elif secrets.compare_digest(t.encode("utf-8"), self.token.encode("utf-8")):
                    token_bytes = self.token.encode("utf-8")
                else:
                    token_bytes = b""
            data = data.replace(b"__TOKEN__", token_bytes)
        try:
            h.send_response(200)
            h.send_header("Content-Type", _content_type(target))
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Cache-Control", "no-store")
            h.end_headers()
            h.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_api(self, h: BaseHTTPRequestHandler, method: str, path: str, query: dict) -> None:
        if path == "/api/state" and method == "GET":
            since_raw = query.get("since", ["-1"])[0]
            try:
                since = int(since_raw)
            except ValueError:
                since = -1
            payload = self._dispatch_state_get(since)
            self._send_json(h, 200, payload)
            return

        if path == "/api/command" and method == "POST":
            body = self._read_json_body(h)
            if body is None:
                self._send_json(h, 400, {"ok": False, "error": "Geçersiz JSON"})
                return
            ctype = body.get("type")
            if ctype == "export_diagnostics":
                status, payload = self._dispatch_export_diagnostics()
                if payload.get("ok"):
                    log.info("arayüz komutu: %s", ctype)
                else:
                    log.info("arayüz komutu reddedildi: %s (%s)", ctype, payload.get("error"))
                self._send_json(h, status, payload)
                return
            with self._lock:
                status, payload = self._dispatch_command(body)
                self._sync_app_status()
            if ctype != "sms":  # never log codes
                if payload.get("ok"):
                    log.info("arayüz komutu: %s", ctype)
                else:
                    log.info("arayüz komutu reddedildi: %s (%s)", ctype, payload.get("error"))
            self._send_json(h, status, payload)
            if body.get("type") == "quit" and payload.get("ok"):
                self._trigger_quit()
            return

        if path == "/api/client_log" and method == "POST":
            # Diagnostics from the UI (JS errors, failed requests, button clicks), so a
            # problem inside the embedded web view shows up in state/logs/bot.log.
            # Text is sanitized (fix list item 9): a misbehaving/compromised page must
            # not be able to forge multi-line log entries or inject control sequences.
            body = self._read_json_body(h) or {}
            level = str(body.get("level", "info")).lower()
            text = _sanitize_client_log_text(str(body.get("text", "")))[:500]
            (log.warning if level in ("warn", "warning", "error") else log.info)("arayüz: %s", text)
            self._send_json(h, 200, {"ok": True})
            return

        if path == "/api/catalog" and method == "GET":
            status, payload = self._dispatch_catalog_get()
            self._send_json(h, status, payload)
            return

        if path == "/api/settings" and method == "PUT":
            body = self._read_json_body(h)
            if body is None:
                self._send_json(h, 400, {"ok": False, "error": "Geçersiz JSON"})
                return
            status, payload = self._dispatch_settings_put(body)
            self._send_json(h, status, payload)
            if status == 200:
                self._request_reminder_sync()
            return

        if path == "/api/credentials" and method == "POST":
            body = self._read_json_body(h)
            if body is None:
                self._send_json(h, 400, {"ok": False, "error": "Geçersiz JSON"})
                return
            status, payload = self._dispatch_credentials_post(body)
            self._send_json(h, status, payload)
            return

        if path == "/api/credentials" and method == "DELETE":
            body = self._read_json_body(h)
            if body is None:
                self._send_json(h, 400, {"ok": False, "error": "Geçersiz JSON"})
                return
            status, payload = self._dispatch_credentials_delete(body)
            self._send_json(h, status, payload)
            return

        self._send_json(h, 404, {"error": "not found"})

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        try:
            parsed = urlsplit(h.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)

            if not self._check_host(h):
                self._send_json(h, 403, {"error": "forbidden host"})
                return

            try:
                content_length = int(h.headers.get("Content-Length", 0) or 0)
            except ValueError:
                content_length = 0
            if content_length > _MAX_BODY_BYTES:
                # Fix list item 9: refuse oversized bodies before reading them into
                # memory at all. We never read (and thus never consume) the body, so the
                # connection can't safely be reused for a next request on the same
                # socket -- force it closed instead of leaving stale body bytes for the
                # next request's parser to choke on.
                h.close_connection = True
                self._send_json(h, 413, {"ok": False, "error": "İstek gövdesi çok büyük"})
                return

            if path.startswith("/api/"):
                if not self._check_token(h):
                    self._send_json(h, 403, {"error": "forbidden"})
                    return
                self._handle_api(h, method, path, query)
                return

            if method != "GET":
                self._send_json(h, 405, {"error": "method not allowed"})
                return
            self._serve_static(h, path, query)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("webapp: %s %s işlenirken beklenmeyen hata", method, h.path)
            try:
                self._send_json(h, 500, {"error": "internal error"})
            except Exception:
                pass
