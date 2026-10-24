"""Root entry point: starts the engine + the local web UI backend (`src/webapp.py`) and
shows it in a native window (pywebview) or, if that fails, the system default browser.

Double-clicked on Windows via a `pythonw.exe app.pyw` shortcut (install.bat) or on macOS
via `Rezervasyon Botu.command` (`.venv/bin/python app.pyw`) -- see
analysis/web-ui-api.md and analysis/final-design-2026-10.md. Also runnable directly:
`python app.pyw` (normal run), `python app.pyw --smoke` (CI: real WebServer + a stub
engine, no browser/window, no state/ writes, no lock), or `python app.pyw --demo [...]`
(visual-review / screenshot mode -- see below).

IMPORTANT: this runs under pythonw.exe on Windows, which has NO console at all --
sys.stdout and sys.stderr are both None. Never `print()` here (see `_safe_print`, used
only where a real console is expected: --smoke/--demo diagnostics); real logging goes
through `src.browser.setup_logging`, which already skips the stdout handler when
sys.stdout is None.

`--demo [--theme light|dark] [--port N] [--no-window] [--seconds N]`: a fake engine (see
`_FakeEngine`) + a scratch settings/catalog dir (never touches state/settings.json or
state/catalog.json) seeded with realistic sample data, and credentials faked as present
(src.credentials is monkeypatched for this process only -- the real OS keychain is never
touched). `--no-window` just serves and prints the URL (for tools/ci_smoke.py's
Playwright screenshots); otherwise opens the real pywebview window. `--seconds N` closes
the window / stops serving automatically after N seconds (for scripted use); omitted, it
runs until the window is closed (or Ctrl+C in `--no-window` mode). No lock, no real
EngineThread, no browser/Playwright -- exactly as safe to run alongside a real instance
as --smoke.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import events as ev  # noqa: E402
from src import reminder_sched  # noqa: E402
from src import updater  # noqa: E402
from src.browser import setup_logging  # noqa: E402
from src.config import ConfigError, load_config  # noqa: E402
from src.single_instance import SingleInstance  # noqa: E402
from src.webapp import WebServer  # noqa: E402
from src.worker import EngineThread, QueueLogHandler  # noqa: E402

APP_TITLE = "Spor İstanbul Rezervasyon Botu"
log = logging.getLogger("ibbspor.app")


def _safe_print(message: str) -> None:
    """print(), but a no-op if there's no attached stream (pythonw.exe). Only used on
    the --smoke/--demo paths, which are only ever invoked from a console."""
    stream = sys.stderr
    if stream is not None:
        try:
            print(message, file=stream)
        except Exception:
            pass


def _set_windows_dpi_awareness() -> None:
    """High-DPI opt-in on Windows so the window isn't blurry/mis-scaled on a scaled
    display. No-op (and never raises) on any other platform or if the call fails."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _show_message(title: str, message: str, *, is_error: bool = False) -> None:
    """A tiny native dialog -- MessageBoxW on Windows, an AppleScript `display dialog`
    on macOS -- falling back to stderr anywhere else (or if the native call fails).
    Used for "already running" / startup-error messages, i.e. before (or instead of)
    any window exists."""
    if sys.platform == "win32":
        try:
            import ctypes

            icon = 0x10 if is_error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(0, message, title, icon)
            return
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            script = (
                f'display dialog "{esc(message)}" buttons {{"OK"}} default button "OK" '
                f'with title "{esc(title)}"'
            )
            subprocess.run(["osascript", "-e", script], timeout=15, check=False)
            return
        except Exception:
            pass
    _safe_print(f"{title}: {message}")


def _clear_on_top(window: "object") -> None:
    try:
        window.on_top = False
    except Exception:
        pass


def _flash_windows_taskbar(title: str) -> None:
    """Windows: flash the taskbar button via FlashWindowEx (fix list item 1). Finds the
    HWND by window title (pywebview doesn't expose the native handle directly). No-op
    (never raises) on any failure, including "window not found yet"."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        FLASHW_ALL = 0x00000003
        FLASHW_TIMERNOFG = 0x0000000C
        info = FLASHWINFO()
        info.cbSize = ctypes.sizeof(FLASHWINFO)
        info.hwnd = hwnd
        info.dwFlags = FLASHW_ALL | FLASHW_TIMERNOFG
        info.uCount = 5
        info.dwTimeout = 0
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        log.exception("app: görev çubuğu simgesi yanıp söndürülemedi")


def _request_attention_macos() -> None:
    """macOS: bounce the Dock icon / request attention via AppKit (fix list item 1).
    Guarded on pyobjc being installed -- silently does nothing otherwise."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().requestUserAttention_(0)  # NSInformationalRequest
    except Exception:
        log.exception("app: macOS dikkat isteği başarısız (pyobjc eksik olabilir)")


def _window_attention(window: "object", reason: str) -> None:
    """`WebServer(on_attention=...)` callback for the pywebview-window path (fix list
    item 1, P0): restore if minimized, show, briefly force on-top, and flash/bounce via
    the OS. Runs on a background thread (see WebServer._trigger_attention) -- every step
    is independently guarded so one failing call never skips the rest."""
    log.info("app: dikkat isteniyor (%s)", reason)
    try:
        window.restore()
    except Exception:
        pass
    try:
        window.show()
    except Exception:
        pass
    try:
        window.on_top = True
        timer = threading.Timer(2.0, _clear_on_top, args=(window,))
        timer.daemon = True
        timer.start()
    except Exception:
        pass
    _flash_windows_taskbar(APP_TITLE)
    _request_attention_macos()


def _start_watchdog(timeout_s: float) -> threading.Timer:
    """Daemon timer that hard-exits the process (`os._exit`, skipping cleanup/atexit --
    deliberate) if it's still alive `timeout_s` from now. Armed by --smoke/--demo as a
    backstop against any hang, regardless of cause. Exit code 3 distinguishes a hung run
    from a real failure."""
    import os

    def _fire() -> None:
        # Leave evidence of *where* it hung before exiting (CI shows stderr).
        if sys.stderr is not None:
            import faulthandler

            print(f"watchdog: {timeout_s:.0f}s doldu, süreç takıldı; iş parçacığı yığınları:", file=sys.stderr)
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
        os._exit(3)

    timer = threading.Timer(timeout_s, _fire)
    timer.daemon = True
    timer.start()
    return timer


# ---------------------------------------------------------------------- real app

def _run_app(
    cfg: "object", engine: "EngineThread", events: "queue.Queue"
) -> tuple[int, tuple[Path, str] | None]:
    quit_event = threading.Event()
    state: dict[str, object] = {"window": None, "shutdown_done": False}

    def _shutdown_once() -> None:
        if state["shutdown_done"]:
            return
        state["shutdown_done"] = True
        try:
            engine.shutdown()
        except Exception:
            log.exception("app: engine.shutdown() hata verdi")

    def _do_quit() -> None:
        _shutdown_once()
        quit_event.set()
        window = state["window"]
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _sync_reminders(slots, enabled, force=False):
        return reminder_sched.sync(slots, enabled=enabled, force=force)

    server = WebServer(cfg, engine, events, mode="window", on_quit=_do_quit,
                        reminder_sync=_sync_reminders,
                        update_check=None if updater.is_git_checkout(ROOT) else updater.check_latest)
    server.start()

    try:
        import webview
    except Exception as e:
        log.warning("pywebview yüklenemedi (%s); varsayılan tarayıcıda açılıyor", e)
        return _run_browser_fallback(server, quit_event, _shutdown_once)

    try:
        window = webview.create_window(
            APP_TITLE, server.launch_url(), width=1180, height=800, min_size=(900, 620),
        )
        state["window"] = window
        server.on_attention = lambda reason: _window_attention(window, reason)
        webview.start(private_mode=False, gui=None)
    except Exception as e:
        log.warning("pywebview başlatılamadı (%s); varsayılan tarayıcıda açılıyor", e)
        return _run_browser_fallback(server, quit_event, _shutdown_once)
    finally:
        _shutdown_once()
        server.stop()

    return 0, server.update_ready


def _run_browser_fallback(
    server: WebServer, quit_event: threading.Event, shutdown_once
) -> tuple[int, tuple[Path, str] | None]:
    server.mode = "browser"
    import webbrowser

    # Fix list item 1: in browser-fallback mode there's no window to raise, so the best
    # we can do is re-open the URL -- most browsers focus the existing tab instead of
    # opening a new one. Each open mints its own fresh nonce (F6) -- a spent one would
    # serve an empty token.
    server.on_attention = lambda reason: webbrowser.open(server.launch_url())
    webbrowser.open(server.launch_url())
    try:
        quit_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_once()
        server.stop()
    return 0, server.update_ready


# ---------------------------------------------------------------------- fake engine (--demo/--smoke)

_DEMO_TC = "12345678901"
_DEMO_PASSWORD = "demo-not-a-real-password"  # never saved anywhere; see _patch_demo_modules


class _FakeEngine:
    """Stub engine for --demo and --smoke. `send()` records every command and
    synthesizes a plausible src.events stream on the SAME queue WebServer drains --
    never touches Playwright, the network, or state/."""

    def __init__(self, events: "queue.Queue") -> None:
        self.events = events
        self.sent: list[object] = []
        self._lock = threading.Lock()
        self._running = False

    def send(self, cmd: object) -> None:
        self.sent.append(cmd)
        if isinstance(cmd, ev.Start):
            self._start_run(cmd.dry_run)
        elif isinstance(cmd, ev.Stop):
            self._stop_run()
        elif isinstance(cmd, ev.SmsCode):
            self._complete_sms(cmd.code)
        elif isinstance(cmd, ev.ScanCatalog):
            self._run_scan(cmd.depth)
        elif isinstance(cmd, ev.CheckLogin):
            self.events.put(ev.LoginResult(ok=True, message="Giriş başarılı.", attempts_left=3))
        elif isinstance(cmd, ev.ManualLogin):
            self.events.put(ev.LoginResult(ok=True, message="Giriş algılandı."))
        elif isinstance(cmd, ev.ListMemberships):
            self.events.put(ev.Memberships(("Çoklu Tesis Tüm Branşlar 12 KULLANIM",)))
        elif isinstance(cmd, ev.ListReservations):
            self.events.put(ev.Reservations(_demo_reservations()))
        elif isinstance(cmd, ev.CancelReservation):
            self.events.put(ev.Status("idle", "İptal edildi"))
        # ShowBrowser / HideBrowser / Quit: nothing to show in demo mode -- no-op.

    def demo_sms(self) -> None:
        """Demo-only hook for `POST /api/command {"type": "demo_sms"}` (see
        tools/ci_smoke.py --ui-screenshots): shows the SMS modal on demand, regardless
        of whether a run is active."""
        self.events.put(ev.Status("hot", "Açılış penceresi — hızlı kontrol"))
        self.events.put(ev.Found("Cuma akşam tenis", "Cum 16.10 18:00-19:00 Kort 1"))
        self.events.put(ev.SmsNeeded("Cum 16.10 18:00-19:00 Kort 1", 240.0))

    def start(self) -> None:
        pass

    def shutdown(self, timeout: float = 10) -> None:
        self._stop_run()

    def _start_run(self, dry_run: bool) -> None:
        with self._lock:
            if self._running:
                self.events.put(ev.Error("Önce durdurun"))
                return
            self._running = True
        self.events.put(ev.Status("starting", "Başlatılıyor"))

        def _worker() -> None:
            time.sleep(0.3)
            if not self._running:
                return
            self.events.put(ev.Status("monitoring", "İzleniyor"))
            open_at = datetime.now() + timedelta(days=2, hours=1)
            session_start = open_at + timedelta(hours=1)
            self.events.put(ev.NextWindow(
                "Cuma akşam tenis", ("TENİS", "İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ"),
                session_start, open_at,
            ))
            n = 0
            while self._running:
                time.sleep(2.0)
                if not self._running:
                    break
                n += 1
                self.events.put(ev.Log("INFO", f"Hedefler taranıyor (tur {n})"))

        threading.Thread(target=_worker, name="FakeEngineRun", daemon=True).start()

    def _stop_run(self) -> None:
        with self._lock:
            was_running, self._running = self._running, False
        if was_running:
            self.events.put(ev.Stopped(130))
            self.events.put(ev.Status("idle"))

    def _complete_sms(self, code: str) -> None:
        self.events.put(ev.SmsResult(True, "SMS kodu doğrulandı."))
        self.events.put(ev.Booked("Cum 16.10 18:00-19:00 Kort 1", "Rezervasyon tamam"))
        with self._lock:
            self._running = False
        self.events.put(ev.Stopped(0))
        self.events.put(ev.Status("idle"))

    def _run_scan(self, depth: str) -> None:
        def _worker() -> None:
            total = 5
            for i in range(1, total + 1):
                time.sleep(0.2)
                self.events.put(ev.CatalogProgress(
                    i, total, f"TENİS · TENİS MALTEPE SPOR KOMPLEKSİ ({i}/{total})"
                ))
            self.events.put(ev.CatalogUpdated())

        threading.Thread(target=_worker, name="FakeEngineScan", daemon=True).start()


def _demo_reservations() -> tuple:
    from src.reservations import Reservation

    today = datetime.now().date()
    return (
        Reservation(
            date=today + timedelta(days=2), start="18:00", end="19:00",
            facility="İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ", hall="Kort 1",
            attended=False, status="Satış Yapıldı", cancel_id="r1",
        ),
        Reservation(
            date=today + timedelta(days=9), start="19:00", end="20:00",
            facility="TENİS MALTEPE SPOR KOMPLEKSİ", hall="TENIS KORTU 1",
            attended=False, status="Seçim İptal Edildi", cancel_id=None,
        ),
    )


def _demo_config():
    """A minimal valid Config for --demo/--smoke -- real config.toml values aren't
    needed since the fake engine never runs the runner loop."""
    from src.config import parse_config

    return parse_config({
        "site": {"base_url": "https://example.invalid", "timezone": "Europe/Istanbul"},
        "timing": {
            "open_offset_hours": 1, "hot_before_s": 60, "hot_after_s": 60,
            "idle_poll_s": 30, "hot_poll_s": 5, "max_requests_per_min": 20,
        },
        "safety": {"dry_run": True, "stop_after_success": True, "screenshot_every_step": False},
    })


def _patch_demo_modules(tmp_dir: Path) -> None:
    """Redirect settings/catalog I/O at a scratch dir and fake "credentials saved" --
    NEVER touches state/settings.json, state/catalog.json, or the real OS keychain.
    Patches the module objects in place (not a `from x import y` alias), so every other
    module that does `from src import credentials as credentials_mod` and calls
    `credentials_mod.load()` (e.g. src/webapp.py) picks this up too."""
    from src import catalog as catalog_mod
    from src import credentials as credentials_mod
    from src import reminders as reminders_mod
    from src import settings as settings_mod

    settings_mod.SETTINGS_PATH = tmp_dir / "settings.json"
    catalog_mod.CATALOG_PATH = tmp_dir / "catalog.json"
    # Reminders (2026-10-18): never write the real state/app_status.json, and never let
    # src.reminders fall back to the real settings/catalog/config paths either, even
    # though webapp.py itself never calls those -- belt and suspenders, see
    # analysis/reminders-design-2026-10.md.
    reminders_mod.APP_STATUS_PATH = tmp_dir / "app_status.json"
    reminders_mod.SETTINGS_PATH = settings_mod.SETTINGS_PATH
    reminders_mod.CATALOG_PATH = catalog_mod.CATALOG_PATH
    reminders_mod.CONFIG_PATH = tmp_dir / "config.toml"

    credentials_mod.ACCOUNTS_PATH = tmp_dir / "accounts.json"
    credentials_mod.load = lambda: (_DEMO_TC, _DEMO_PASSWORD)
    credentials_mod.save = lambda tc, password: None
    credentials_mod.clear = lambda: None
    # Multiple accounts (2026-10-18): src.webapp also calls these -- never the real
    # keychain/state/accounts.json in --demo/--smoke either.
    credentials_mod.list_accounts = lambda: [
        credentials_mod.Account(
            id="demo", name="Demo Üye", tc_masked=credentials_mod.mask_tc(_DEMO_TC), active=True,
        )
    ]
    credentials_mod.remove = lambda account_id: None
    credentials_mod.set_active = lambda account_id: None
    credentials_mod.set_name = lambda account_id, name: None


def _seed_demo_settings_and_catalog() -> None:
    """Realistic sample data: 3 targets (2 enabled, 1 disabled/any-facility) and a
    2-branch catalog with a few facilities/halls/levels/slots. Call AFTER
    `_patch_demo_modules` -- paths are read from the (now redirected) module globals."""
    from src import catalog as catalog_mod
    from src import settings as settings_mod
    from src.grid import Grid, SessionCard, Status
    from src.targets import Target

    targets = (
        Target(
            id="demo1", name="Cuma akşam tenis", enabled=True,
            branch="TENİS", facility="İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ",
            halls=(), level_prefix="", days=(4,), time_from="18:00", time_to="21:00", starts=(),
        ),
        Target(
            id="demo2", name="Çarşamba Maltepe", enabled=True,
            branch="TENİS", facility="TENİS MALTEPE SPOR KOMPLEKSİ",
            halls=("TENIS KORTU 1",), level_prefix="", days=(2,),
            time_from=None, time_to=None, starts=("19:00",),
        ),
        Target(
            id="demo3", name="Fitness (herhangi tesis)", enabled=False,
            branch="FİTNESS", facility="*",
            halls=(), level_prefix="", days=(0, 1, 2, 3, 4), time_from=None, time_to=None, starts=(),
        ),
    )
    settings = settings_mod.Settings(
        version=1, membership_text="", dry_run=False, stop_after_success=True, targets=targets,
    )
    settings_mod.save_settings(settings, path=settings_mod.SETTINGS_PATH)

    now = datetime.now()
    cat = catalog_mod.Catalog(branches={}, branches_listed_at=None)
    cat.observe_branches(["TENİS", "FİTNESS"], now)
    cat.observe_facilities(
        "TENİS", ["İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ", "TENİS MALTEPE SPOR KOMPLEKSİ"], now
    )
    cat.observe_facilities("FİTNESS", ["İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ"], now)

    grid1 = Grid(
        dates=(), has_confirm=False, has_save=False,
        branch_selected="TENİS", facility_selected="İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ",
        cards=(
            SessionCard(card_id="c1", date=now.date(), start="18:00", end="19:00",
                        level="TENİS 1.SEVİYE", court="Kort 1", status=Status.OPEN,
                        color="#08f51a", quota="4", checkbox_id=None, checkbox_checked=False),
            SessionCard(card_id="c2", date=now.date(), start="19:00", end="20:00",
                        level="TENİS 2.SEVİYE", court="Kort 2", status=Status.OPEN,
                        color="#08f51a", quota="2", checkbox_id=None, checkbox_checked=False),
        ),
    )
    cat.observe("TENİS", "İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ", grid1, now)

    grid2 = Grid(
        dates=(), has_confirm=False, has_save=False,
        branch_selected="TENİS", facility_selected="TENİS MALTEPE SPOR KOMPLEKSİ",
        cards=(
            SessionCard(card_id="c3", date=now.date(), start="19:00", end="20:00",
                        level="TENİS 1.SEVİYE", court="TENIS KORTU 1", status=Status.OPEN,
                        color="#08f51a", quota="4", checkbox_id=None, checkbox_checked=False),
        ),
    )
    cat.observe("TENİS", "TENİS MALTEPE SPOR KOMPLEKSİ", grid2, now)
    cat.save(catalog_mod.CATALOG_PATH)


# ---------------------------------------------------------------------- --smoke

def _probe_server(server: WebServer) -> bool:
    import urllib.error
    import urllib.request

    base = f"http://{server.host}:{server.port}"
    # Never route 127.0.0.1 through a system proxy (macOS urllib reads the system proxy
    # config; a proxied loopback request can hang on CI machines).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{base}/", timeout=10) as resp:
            if resp.status != 200:
                _safe_print(f"smoke: GET / -> {resp.status}")
                return False
    except urllib.error.URLError as e:
        _safe_print(f"smoke: GET / hata: {e}")
        return False

    try:
        req = urllib.request.Request(
            f"{base}/api/state?since=-1", headers={"X-Token": server.token}
        )
        with opener.open(req, timeout=10) as resp:
            if resp.status != 200:
                _safe_print(f"smoke: GET /api/state -> {resp.status}")
                return False
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        _safe_print(f"smoke: GET /api/state hata: {e}")
        return False

    for key in ("version", "now", "engine", "settings", "account", "log"):
        if key not in data:
            _safe_print(f"smoke: /api/state yanıtında eksik alan: {key!r}")
            return False
    return True


def _run_smoke(argv: list[str]) -> int:
    """Real WebServer + a stub engine, no window, no state/ writes, no lock. See
    tools/ci_smoke.py (--gui)."""
    import shutil
    import tempfile

    watchdog = _start_watchdog(30.0)
    tmp_dir = Path(tempfile.mkdtemp(prefix="ibbspor-smoke-"))
    server: WebServer | None = None
    try:
        _patch_demo_modules(tmp_dir)
        cfg = _demo_config()
        events: "queue.Queue" = queue.Queue()
        engine = _FakeEngine(events)
        server = WebServer(cfg, engine, events, mode="window", port=0)
        server.start()
        ok = _probe_server(server)
        return 0 if ok else 1
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        watchdog.cancel()


# ---------------------------------------------------------------------- --demo

def _arg_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _run_demo(argv: list[str]) -> int:
    import shutil
    import tempfile

    theme = _arg_value(argv, "--theme")
    port_raw = _arg_value(argv, "--port")
    port = int(port_raw) if port_raw else 0
    no_window = "--no-window" in argv
    seconds_raw = _arg_value(argv, "--seconds")
    seconds = float(seconds_raw) if seconds_raw else None

    watchdog = _start_watchdog((seconds + 30.0) if seconds is not None else 600.0)
    tmp_dir = Path(tempfile.mkdtemp(prefix="ibbspor-demo-"))
    server: WebServer | None = None
    try:
        _patch_demo_modules(tmp_dir)
        _seed_demo_settings_and_catalog()

        cfg = _demo_config()
        events: "queue.Queue" = queue.Queue()
        engine = _FakeEngine(events)

        quit_event = threading.Event()
        state: dict[str, object] = {"window": None}

        def _do_quit() -> None:
            quit_event.set()
            window = state["window"]
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    pass

        server = WebServer(cfg, engine, events, mode="window", port=port, on_quit=_do_quit)
        server.start()

        url = server.url
        if theme:
            url = f"{url}&theme={theme}"

        if no_window:
            server.mode = "browser"
            _safe_print(url)
            try:
                quit_event.wait(timeout=seconds)
            except KeyboardInterrupt:
                pass
            return 0

        try:
            import webview
        except Exception as e:
            _safe_print(f"demo: pywebview yüklenemedi ({e}); --no-window ile deneyin")
            return 0

        window = webview.create_window(
            f"{APP_TITLE} (demo)", url, width=1180, height=800, min_size=(900, 620),
        )
        state["window"] = window

        if seconds is not None:
            def _auto_close() -> None:
                w = state["window"]
                if w is not None:
                    try:
                        w.destroy()
                    except Exception:
                        pass

            timer = threading.Timer(seconds, _auto_close)
            timer.daemon = True
            timer.start()

        webview.start(private_mode=False, gui=None)
        return 0
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        watchdog.cancel()


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--smoke" in argv:
        return _run_smoke(argv)
    if "--demo" in argv:
        return _run_demo(argv)

    _set_windows_dpi_awareness()

    lock = SingleInstance()
    if not lock.acquire():
        _show_message(APP_TITLE, "Uygulama zaten açık")
        return 0

    update_ready: tuple[Path, str] | None = None
    try:
        try:
            cfg = load_config(ROOT / "config.toml")
        except ConfigError as e:
            _show_message(APP_TITLE, f"Ayarlar okunamadı: {e}", is_error=True)
            return 1

        events: "queue.Queue" = queue.Queue()
        setup_logging(gui_handler=QueueLogHandler(events))

        engine = EngineThread(cfg, events)
        engine.start()

        rc, update_ready = _run_app(cfg, engine, events)
        return rc
    finally:
        lock.release()
        if update_ready is not None:
            new_dir, tag = update_ready
            updater.launch_apply(ROOT, new_dir, tag)


if __name__ == "__main__":
    sys.exit(main())
