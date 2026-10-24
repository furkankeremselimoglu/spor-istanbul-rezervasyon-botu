"""EngineThread: the single thread that owns Playwright for the GUI's whole lifetime.

See analysis/final-design-2026-10.md, D2 and "GUI ⇄ worker protocol". The GUI (main/Tk
thread) talks to this thread through two queues: it calls `EngineThread.send(cmd)` with a
`src.events` Command, and pumps `events` (the queue given to `__init__`, usually via
`root.after(100, ...)`) for `src.events` Event objects to render.

Long operations (Start, ScanCatalog, CheckLogin, ManualLogin, ListMemberships,
ListReservations, CancelReservation) run ON THIS THREAD, one at a time, because the
Playwright sync API requires every call on a given browser/page to happen on the thread
that created it. `send()` is still safe to call from the GUI thread at any moment:

- `Stop`, `SmsCode` are "immediate" commands: `send()` acts on them directly (setting the
  current operation's `threading.Event`, or putting the code on the current run's
  `code_queue`) without going through the command queue, so they take effect even while
  the engine thread is blocked deep inside a long operation.
- `ShowBrowser`/`HideBrowser` are also accepted immediately by `send()`, but actually
  touching the browser window (CDP calls) can only happen on this thread. `send()` just
  records the latest requested action; the engine thread applies it at its next safe
  point -- between two commands when idle, and (best-effort, for responsiveness) between
  events emitted by an in-progress Start, since the runner's own `io.emit` calls happen on
  this thread too. KNOWN LIMITATION: if a long non-Start operation (e.g. a catalog scan)
  runs for a while without yielding, a Show/Hide request sent during it is only applied
  once that operation finishes. The runner is separately responsible for showing the
  window itself when it needs the human (manual login, SMS) -- that is not routed through
  this mechanism at all.
- `Start`/`ScanCatalog`/`CheckLogin`/`ManualLogin`/`ListMemberships`/`ListReservations`/
  `CancelReservation` sent while another such operation is already running are rejected
  immediately with `Error("Önce durdurun")` -- they are never queued up behind the busy
  operation.

Errors: a `LoggedOut` raised by any operation emits a `LoggedOut` event; `browser.Stopped`
(the rate limiter noticing a stop mid-wait) emits `Stopped(130)`; any other exception
emits `Error(str(exc), fatal=False)` and is logged. The thread itself never dies from an
operation's exception -- only `Quit` (via `shutdown()`) ends it.
"""
from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

from src import catalog as catalog_mod
from src import events as ev
from src import settings as settings_mod
from src.browser import Artifacts, BrowserClosed, LoggedOut, RateLimiter, Stopped, open_browser
from src.credentials import load as load_credentials
from src.reservations import find_reservation
from src.site import Site

if TYPE_CHECKING:
    from src.config import Config

log = logging.getLogger("ibbspor.worker")

# Commands that start a long, exclusive operation on the engine thread (as opposed to the
# "immediate" commands -- Stop/SmsCode/ShowBrowser/HideBrowser -- handled directly by send()).
_EXCLUSIVE_COMMANDS = (
    ev.Start, ev.ScanCatalog, ev.CheckLogin, ev.ManualLogin,
    ev.ListMemberships, ev.ListReservations, ev.CancelReservation, ev.SwitchAccount,
)


def _unexpected_error_message(e: BaseException) -> str:
    """Turkish fallback text for an unexpected exception, instead of the raw (often
    English) str(e) -- the full exception is still in the log (see the log.exception
    call right before this is used)."""
    return f"Beklenmeyen hata: {type(e).__name__}; ayrıntı logda"


def _lazy_runner_run(cfg, settings, site, io, *, catalog=None, dry_run=False, once=False):
    """Default `runner_run`: imports src.runner lazily, only once an actual Start runs.

    Keeps importing src.worker independent of src.runner being importable (useful while
    it's mid-edit elsewhere) -- tests inject a fake `runner_run` and never hit this path.
    """
    from src.runner import run as _run

    return _run(cfg, settings, site, io, catalog=catalog, dry_run=dry_run, once=once)


class QueueLogHandler(logging.Handler):
    """logging.Handler that turns every record into a `src.events.Log` on `events`.

    Wire it up with `src.browser.setup_logging(gui_handler=QueueLogHandler(events))` so
    GUI-mode log lines reach the Tk window instead of (or alongside) stdout/file. Never
    raises from `emit` -- a formatting hiccup must not break logging itself.
    """

    def __init__(self, events: "queue.Queue") -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self.events.put(ev.Log(level=record.levelname, line=line))
        except Exception:
            self.handleError(record)


class EngineThread(threading.Thread):
    """Owns Playwright for its whole life; runs every long site operation serially."""

    def __init__(
        self,
        cfg: "Config",
        events: "queue.Queue",
        *,
        browser_factory: Callable = open_browser,
        site_factory: Callable = Site,
        runner_run: Callable | None = None,
        headless: bool = False,
    ) -> None:
        super().__init__(daemon=True, name="EngineThread")
        self.cfg = cfg
        self.events = events
        self._browser_factory = browser_factory
        self._site_factory = site_factory
        self._runner_run = runner_run or _lazy_runner_run
        self.headless = headless

        self._cmds: "queue.Queue" = queue.Queue()
        self._busy_lock = threading.Lock()
        self._busy = False

        # The CURRENTLY RUNNING operation's interrupt event / SMS code sink, if any. Set
        # right before that operation starts, cleared right after -- send() reads these
        # directly (thread-safe: plain attribute read/write) so Stop/SmsCode reach
        # whichever operation is active without waiting for it to finish.
        self._current_stop: "threading.Event | None" = None
        self._current_code_queue: "queue.Queue[str] | None" = None

        # Latest requested window action not yet applied ("show" / "hide" / None). Only
        # ever read/applied from this thread; only ever written from send() (any thread).
        self._pending_window_action: str | None = None

        self._stack = ExitStack()
        self._session = None  # browser.BrowserSession, once open_browser() has run
        self._limiter: RateLimiter | None = None
        self._artifacts: Artifacts | None = None
        self._site: Site | None = None

        self._quit = False

    # ---------------------------------------------------------------- GUI-thread API

    def send(self, cmd: object) -> None:
        """Thread-safe; callable from the GUI thread at any time, busy or not."""
        if isinstance(cmd, ev.Stop):
            stop = self._current_stop
            if stop is not None:
                stop.set()
            else:
                log.debug("Stop alındı ama aktif bir işlem yok; yok sayıldı")
            return
        if isinstance(cmd, ev.SmsCode):
            code_queue = self._current_code_queue
            if code_queue is not None:
                code_queue.put(cmd.code)
            else:
                log.warning("SmsCode alındı ama aktif bir run yok; yok sayıldı")
            return
        if isinstance(cmd, ev.ShowBrowser):
            self._pending_window_action = "show"
            return
        if isinstance(cmd, ev.HideBrowser):
            self._pending_window_action = "hide"
            return

        if isinstance(cmd, _EXCLUSIVE_COMMANDS):
            with self._busy_lock:
                busy = self._busy
            if busy:
                self.events.put(ev.Error("Önce durdurun"))
                return

        self._cmds.put(cmd)

    def shutdown(self, timeout: float = 10) -> None:
        """Interrupt whatever is running, ask the thread to quit, and join it."""
        self.send(ev.Stop())
        self.send(ev.Quit())
        self.join(timeout=timeout)

    # ---------------------------------------------------------------- thread body

    def run(self) -> None:
        try:
            while not self._quit:
                try:
                    cmd = self._cmds.get(timeout=0.2)
                except queue.Empty:
                    self._apply_pending_window_action()
                    continue
                self._apply_pending_window_action()
                self._dispatch(cmd)
        finally:
            try:
                self._stack.close()
            except Exception:
                log.exception("EngineThread: tarayıcı kapatılırken hata")

    def _dispatch(self, cmd: object) -> None:
        if isinstance(cmd, ev.Quit):
            self._quit = True
            return

        # Create & publish the stop event as the FIRST thing for every op (not just
        # Start) -- so Stop (and the rate limiter's stop-aware wait, which reads
        # self._current_stop) works for every operation, including the setup/browser-
        # launch phase before the op's own body even starts (see send()'s Stop handling
        # and _limiter_sleep).
        stop = threading.Event()
        self._current_stop = stop

        with self._busy_lock:
            self._busy = True
        t0 = time.monotonic()
        log.info("işlem başladı: %s", type(cmd).__name__)
        try:
            try:
                self._run_command(cmd, stop)
            except BrowserClosed:
                # The user closed the bot browser (Windows closes the whole browser with
                # its last window). Relaunch it and try the operation once more.
                log.warning("Bot tarayıcısı kapanmış; yeniden başlatılıp işlem tekrar deneniyor")
                self.events.put(ev.Log("WARNING", "Bot tarayıcısı kapatılmış; yeniden açılıyor"))
                self._reset_browser()
                self._run_command(cmd, stop)
        except LoggedOut:
            self.events.put(ev.LoggedOut())
        except Stopped:
            self.events.put(ev.Stopped(130))
        except Exception as e:
            log.exception("işlem sırasında beklenmeyen hata: %r", cmd)
            self.events.put(ev.Error(_unexpected_error_message(e), fatal=False))
        finally:
            log.info("işlem bitti: %s (%.1f sn)", type(cmd).__name__, time.monotonic() - t0)
            self._current_stop = None
            self._current_code_queue = None
            with self._busy_lock:
                self._busy = False

    def _run_command(self, cmd: object, stop: threading.Event) -> None:
        if isinstance(cmd, ev.Start):
            self._do_start(cmd, stop)
        elif isinstance(cmd, ev.ScanCatalog):
            self._do_scan(cmd, stop)
        elif isinstance(cmd, ev.CheckLogin):
            self._do_check_login(stop)
        elif isinstance(cmd, ev.ManualLogin):
            self._do_manual_login(stop)
        elif isinstance(cmd, ev.ListMemberships):
            self._do_list_memberships(stop)
        elif isinstance(cmd, ev.ListReservations):
            self._do_list_reservations(stop)
        elif isinstance(cmd, ev.CancelReservation):
            self._do_cancel(cmd, stop)
        elif isinstance(cmd, ev.SwitchAccount):
            self._do_switch_account(cmd, stop)
        elif isinstance(cmd, ev.ResetSession):
            self._do_reset_session()
        else:
            log.warning("bilinmeyen komut: %r", cmd)

    # ---------------------------------------------------------------- browser lifecycle

    def _limiter_sleep(self, seconds: float) -> bool:
        """RateLimiter's `sleep`: waits on the CURRENTLY ACTIVE operation's stop event,
        so Stop interrupts whichever operation is in flight, not a fixed event."""
        stop = self._current_stop
        if stop is None:
            time.sleep(seconds)
            return False
        return stop.wait(seconds)

    def _reset_browser(self) -> None:
        """Forget the (dead) browser so the next _ensure_browser() launches a new one."""
        try:
            self._stack.close()
        except Exception:
            log.debug("closing the dead browser failed", exc_info=True)
        self._stack = ExitStack()
        self._session = None
        self._site = None

    def _ensure_browser(self) -> Site:
        """Open the browser + build the shared Site on first use; reuse afterwards.

        A closed tab is replaced in place (Site.ensure_page); a browser that is gone
        altogether is relaunched.
        """
        if self._site is not None:
            try:
                self._site.ensure_page()
                return self._site
            except BrowserClosed:
                log.warning("Bot tarayıcısı kapanmış; yeniden başlatılıyor")
                self._reset_browser()

        session = self._stack.enter_context(
            self._browser_factory(headless=self.headless, minimized=True)
        )
        self._session = session
        self._limiter = RateLimiter(self.cfg.timing.max_requests_per_min, sleep=self._limiter_sleep)
        self._artifacts = Artifacts(enabled=self.cfg.safety.screenshot_every_step, window=session.window)

        membership_text = None
        try:
            settings = settings_mod.load_settings(path=settings_mod.SETTINGS_PATH, legacy_cfg=self.cfg)
            membership_text = settings.membership_text
        except settings_mod.SettingsError:
            log.exception("_ensure_browser: ayarlar okunamadı, membership_text boş bırakıldı")

        self._site = self._site_factory(
            session.page, self.cfg, self._artifacts, self._limiter,
            membership_text=membership_text, window=session.window,
        )
        return self._site

    def _apply_pending_window_action(self) -> None:
        action = self._pending_window_action
        if action is None or self._session is None:
            return
        self._pending_window_action = None
        if action == "show":
            self._session.window.show()
        elif action == "hide":
            self._session.window.minimize()

    # ---------------------------------------------------------------- operations

    def _do_start(self, cmd: ev.Start, stop: threading.Event) -> None:
        try:
            settings = settings_mod.load_settings(path=settings_mod.SETTINGS_PATH, legacy_cfg=self.cfg)
        except settings_mod.SettingsError as e:
            self.events.put(ev.Error(f"Ayarlar okunamadı: {e}"))
            return

        if not any(t.enabled for t in settings.targets):
            self.events.put(ev.Error("Çalıştırmak için en az bir etkin hedef gerekli"))
            return

        try:
            site = self._ensure_browser()
        except Exception as e:
            log.exception("Start: tarayıcı açılamadı")
            self.events.put(ev.Error(f"Tarayıcı açılamadı: {e}"))
            return
        site.membership_text = settings.membership_text

        has_session = False
        try:
            has_session = site.check_logged_in()
        except BrowserClosed:
            raise  # _dispatch relaunches the browser and retries Start once
        except Exception:
            log.exception("Start: oturum kontrolü başarısız")
        if has_session:
            # "at Start (once logged in)" -- the runner's own later login attempts (if
            # the session turns out to be stale) are not observed here; this covers the
            # common case where Start finds an already-live session right away.
            self.events.put(ev.AccountInfo(name=site.last_member_name))
        if not has_session and load_credentials() is None:
            self.events.put(
                ev.Error("Giriş yapılmamış ve kayıtlı kimlik bilgisi yok; önce Hesap sekmesinden giriş yapın")
            )
            return

        # `stop` was already created and published (as self._current_stop) by _dispatch,
        # before even the settings/browser setup above -- so a Stop sent during that setup
        # is honoured too, not just once the runner loop itself starts.
        code_queue: "queue.Queue[str]" = queue.Queue()
        self._current_code_queue = code_queue
        stopped_seen = False

        def _emit(event: object) -> None:
            nonlocal stopped_seen
            if isinstance(event, ev.Stopped):
                stopped_seen = True
            self._apply_pending_window_action()
            self.events.put(event)

        io = SimpleNamespace(stop=stop, code_queue=code_queue, emit=_emit, gui=True)

        self.events.put(ev.Status("starting", "Başlatılıyor"))
        try:
            exit_code = self._runner_run(
                self.cfg, settings, site, io,
                catalog=catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH),
                dry_run=cmd.dry_run, once=False,
            )
        except LoggedOut:
            log.warning("Start: oturum kapalı")
            self.events.put(ev.LoggedOut())
            if not stopped_seen:
                self.events.put(ev.Stopped(2))
        except Stopped:
            if not stopped_seen:
                self.events.put(ev.Stopped(130))
        except BrowserClosed:
            raise  # _dispatch relaunches the browser and retries Start once
        except Exception as e:
            log.exception("Start: runner.run patladı")
            self.events.put(ev.Error(_unexpected_error_message(e), fatal=False))
            if not stopped_seen:
                self.events.put(ev.Stopped(1))
        else:
            if not stopped_seen:
                self.events.put(ev.Stopped(exit_code))
        finally:
            self._current_stop = None
            self._current_code_queue = None
            self.events.put(ev.Status("idle"))

    def _do_scan(self, cmd: ev.ScanCatalog, stop: threading.Event) -> None:
        site = self._ensure_browser()

        if not site.login_if_needed(stop=stop):
            self.events.put(ev.Error("Giriş yapılamadı; tarama durduruldu"))
            return

        branches = list(cmd.branches) if cmd.branches else None
        catalog = catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH)
        seen = 0
        for item in site.iter_scan(branches, cmd.depth, stop=stop):
            self._apply_pending_window_action()
            if stop.is_set():
                break
            now = datetime.now()
            if item.kind == "branches":
                catalog.observe_branches(item.names, now)
                label = "Branşlar"
            elif item.kind == "facilities":
                catalog.observe_facilities(item.branch, item.names, now)
                label = str(item.branch)
            else:  # "grid"
                catalog.observe(item.branch, item.facility, item.grid, now)
                label = f"{item.branch} · {item.facility}"
            self.events.put(ev.CatalogProgress(item.done, item.total, label))
            seen += 1
            if seen % 20 == 0:
                catalog.save(catalog_mod.CATALOG_PATH)

        catalog.save(catalog_mod.CATALOG_PATH)
        self.events.put(ev.CatalogUpdated())

    def _do_check_login(self, stop: threading.Event) -> None:
        site = self._ensure_browser()
        ok = site.check_logged_in()
        if not ok:
            ok = site.auto_login()
        self.events.put(
            ev.LoginResult(
                ok=ok,
                message="Giriş başarılı." if ok else "Giriş başarısız.",
                attempts_left=self._auto_login_attempts_left(),
            )
        )
        self.events.put(ev.AccountInfo(name=site.last_member_name if ok else None))

    def _do_manual_login(self, stop: threading.Event) -> None:
        site = self._ensure_browser()
        ok = site.wait_for_login(stop=stop)
        if ok and self._session is not None:
            # A human just typed their credentials in by hand -- save cookies right away
            # (same as Site.auto_login's success path) rather than waiting for a clean
            # shutdown to persist the session.
            with contextlib.suppress(Exception):
                from src.browser import save_cookies_now

                save_cookies_now(self._session.context)
        self.events.put(
            ev.LoginResult(
                ok=ok,
                message="Giriş algılandı." if ok else "Giriş algılanamadı (zaman aşımı veya durduruldu).",
                attempts_left=None,
            )
        )
        self.events.put(ev.AccountInfo(name=site.last_member_name if ok else None))

    def _do_list_memberships(self, stop: threading.Event) -> None:
        site = self._ensure_browser()
        if not site.login_if_needed(stop=stop):
            self.events.put(ev.Error("Giriş yapılamadı"))
            return
        self.events.put(ev.Memberships(tuple(site.list_memberships())))

    def _do_list_reservations(self, stop: threading.Event) -> None:
        site = self._ensure_browser()
        if not site.login_if_needed(stop=stop):
            self.events.put(ev.Error("Giriş yapılamadı"))
            return
        self.events.put(ev.Reservations(tuple(site.open_reservations())))

    def _do_cancel(self, cmd: ev.CancelReservation, stop: threading.Event) -> None:
        site = self._ensure_browser()
        if not site.login_if_needed(stop=stop):
            self.events.put(ev.Error("Giriş yapılamadı"))
            return

        items = site.open_reservations()
        res = find_reservation(items, cmd.date, cmd.start)
        if res is None:
            self.events.put(ev.Error("Rezervasyon bulunamadı (zaten iptal edilmiş olabilir)"))
            return

        ok, message = site.cancel_reservation(res, stop=stop)
        if ok:
            refreshed = site.open_reservations()
            self.events.put(ev.Reservations(tuple(refreshed)))
            self.events.put(ev.Status("idle", f"İptal edildi: {message}" if message else "İptal edildi"))
        else:
            self.events.put(ev.Error(message or "İptal edilemedi"))

    def _reset_session(self, site: Site) -> None:
        """Drop the live site session at both layers: logout() the Site, then clear the
        browser context's cookies (in-memory) and persist that empty state to
        state/cookies.json -- so the previous member's cookies can never carry over to
        whoever becomes active next (F1)."""
        site.logout()
        ctx = self._session.context
        ctx.clear_cookies()
        from src.browser import save_cookies_now

        save_cookies_now(ctx)

    def _do_reset_session(self) -> None:
        if self._site is None:
            return
        self._reset_session(self._site)
        self.events.put(ev.LoggedOut())

    def _do_switch_account(self, cmd: ev.SwitchAccount, stop: threading.Event) -> None:
        from src.credentials import load as load_credentials
        from src.credentials import set_active as set_active_account
        from src.site import Site, _credentials_fingerprint

        try:
            set_active_account(cmd.account_id)
        except ValueError as e:
            self.events.put(ev.Error(str(e)))
            return

        # The new account's credentials fingerprint naturally differs from whatever was
        # previously rejected -- when it does, lift the "stop retrying" lockout guard so
        # auto_login() actually tries the new credentials instead of short-circuiting.
        creds = load_credentials()
        if creds is not None:
            new_fp = _credentials_fingerprint(*creds)
            if new_fp != Site.credentials_rejected_fp:
                Site.credentials_rejected_fp = None
                Site._credential_rejections = 0

        site = self._ensure_browser()
        self._reset_session(site)
        ok = site.auto_login()
        self.events.put(
            ev.LoginResult(
                ok=ok,
                message="Giriş başarılı." if ok else "Giriş başarısız.",
                attempts_left=self._auto_login_attempts_left(),
            )
        )
        self.events.put(ev.AccountInfo(name=site.last_member_name if ok else None))

    # ---------------------------------------------------------------- small helpers

    @staticmethod
    def _auto_login_attempts_left() -> int | None:
        """Remaining Site.auto_login attempts in the current rolling window, or None if
        the limiter's internals aren't the expected shape (best-effort, never raises)."""
        limiter = getattr(Site, "_auto_login_limiter", None)
        if limiter is None:
            return None
        try:
            used = len(limiter._timestamps)
            return max(0, limiter.max_attempts - used)
        except Exception:
            return None
