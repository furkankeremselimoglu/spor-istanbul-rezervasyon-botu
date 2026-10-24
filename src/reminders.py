"""Reminder computation + fire-time program (src/reminders.py, Task A).

Pure computation (`upcoming`, `weekly_slots`, `due_now`, `starts_for`) + a tiny
app-status file + the fire-time entry point (`fire`/`main`), run by `remind.pyw` --
itself invoked by the OS scheduler (Windows Task Scheduler / macOS launchd, see
src/reminder_sched.py, Task B) once per weekly (weekday, HH:MM) trigger, completely
independent of whether the GUI app is running. See analysis/reminders-design-2026-10.md.

Cross-platform (Windows + macOS); `main()` may run under pythonw.exe on Windows, which
has no console at all -- never print() here, only logging.handlers.RotatingFileHandler
(see `main`). Everything here that touches the filesystem/OS is written to never raise:
a bad/missing/corrupt settings.json, catalog.json or config.toml, or a failed dialog,
must result in "show nothing, log why" rather than an unhandled crash in a
scheduler-spawned background process.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src import notify, single_instance
from src.catalog import ANY_FACILITY, Catalog
from src.catalog import CATALOG_PATH as _CATALOG_PATH
from src.config import WEEKDAYS, TimingConfig, load_config
from src.planner import expected_tennis_offset_hours
from src.settings import SETTINGS_PATH as _SETTINGS_PATH
from src.settings import Settings, load_settings
from src.targets import TR_DAY, Target, is_tennis_branch, start_allowed, tennis_seviye2

log = logging.getLogger("ibbspor.reminders")

LEAD = timedelta(minutes=15)
# Catalog slots not seen on the site for this long are dropped (session removed/moved).
SLOT_MAX_AGE = timedelta(days=14)
APP_TITLE = "Spor İstanbul Rezervasyon Botu"  # must equal app.pyw's APP_TITLE (Windows FindWindowW)

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CONFIG_PATH = ROOT / "config.toml"
SETTINGS_PATH = _SETTINGS_PATH
CATALOG_PATH = _CATALOG_PATH
APP_STATUS_PATH = STATE / "app_status.json"
# Most recent tennis session (by date+start) the member has picked -- Ekim 2026 duyurusu
# 72h/66h open-rule prediction (src.planner.expected_tennis_offset_hours). Written
# wherever reservations are already fetched (src.controller._on_reservations) so
# remind.pyw -- which never touches the network -- can use it too.
TENNIS_HISTORY_PATH = STATE / "tennis_history.json"

# Scheduler jitter / wake-from-sleep slack applied to due_now()'s lower bound.
_DUE_SLACK = timedelta(minutes=2)


@dataclass(frozen=True)
class Reminder:
    remind_at: datetime      # aware (site tz) = open_at - LEAD
    open_at: datetime        # aware (site tz) = session_start - timing.open_offset_hours
    session_start: datetime  # aware (site tz)
    target_name: str


# --------------------------------------------------------------------------
# Pure computation
# --------------------------------------------------------------------------

def starts_for(target: Target, catalog: Catalog, weekday: int,
               seen_since: datetime | None = None) -> list[str]:
    """Session starts ("HH:MM") for `target` on `weekday` (0=Mon..6=Sun) to remind about.

    Only real starts -- never a guess (no planner hourly fallback, no `time_from`
    estimate): the user rejected reminders for sessions that don't exist or that come an
    hour early (a 19:00 guess for a 20:00 session). The engine adds every session it sees
    to the catalog while searching, so a new session gets its reminder once observed.
    1. `target.starts`, if non-empty.
    2. Otherwise, the union of catalog-observed starts for `target`'s facilities on this
       weekday (facility "*" -> every facility of `target.branch` the catalog has seen,
       mirroring src.planner.candidate_starts), filtered by `start_allowed`, sorted;
       only slots last seen at/after `seen_since` (upcoming() passes now - SLOT_MAX_AGE,
       so the reminders follow the latest weeks' real schedule).
    """
    if target.starts:
        return list(target.starts)

    if target.facility == ANY_FACILITY:
        facilities = catalog.facility_names(target.branch)
    else:
        facilities = [target.facility]

    seen: set[str] = set()
    for facility in facilities:
        seen.update(catalog.starts(target.branch, facility, weekday, seen_since))
    return sorted(s for s in seen if start_allowed(target, s))


def upcoming(
    settings: Settings,
    catalog: Catalog,
    timing: TimingConfig,
    tz: ZoneInfo,
    now: datetime,
    *,
    horizon_days: int | None = None,
    tennis_last_pick: datetime | None = None,
) -> list[Reminder]:
    """Upcoming reminders for every ENABLED target, within `horizon_days` of `now`.

    Default horizon: open_offset (rounded up to days) + 8, so every weekly slot appears
    once even when this week's opening just passed -- with 72h, next Wednesday's session
    is 10 days away when its Sunday opening is still ahead (weekly_slots relies on this).

    Ignores `settings.reminders_enabled` entirely -- that's the caller's decision
    (`fire`), not this function's. For each enabled target, each calendar day in
    [now.date(), now.date()+horizon_days] whose weekday is one of `target.days`, each
    candidate start (`starts_for`) becomes a `session_start`; a reminder is dropped once
    its open-and-hot period is entirely in the past (`open_at + hot_after_s < now`).
    Deduped on (target_name, session_start); sorted by (remind_at, target priority --
    list order in `settings.targets`). Never includes the planner's week-flip (Monday
    00:00) pseudo-window -- that's an engine polling hint, not a real opening.

    Ekim 2026 duyurusu: unlike the planner (which silently polls BOTH 72h and 66h for
    tennis -- see src.planner.windows), exactly ONE reminder is produced per session --
    a Seviye-2 target (`src.targets.tennis_seviye2`) predicts 66h vs 72h from
    `tennis_last_pick` (`src.planner.expected_tennis_offset_hours`); every other tennis
    target always uses the fixed, earlier 72h; non-tennis targets are unaffected
    (`timing.open_offset_hours`, as before).
    """
    hot_after = timedelta(seconds=timing.hot_after_s)
    today = now.date()
    if horizon_days is None:
        max_offset_h = max(timing.open_offset_hours, 72)
        horizon_days = -(-timedelta(hours=max_offset_h) // timedelta(days=1)) + 8

    seen: set[tuple[str, datetime]] = set()
    entries: list[tuple[Reminder, int]] = []
    for priority, target in enumerate(settings.targets):
        if not target.enabled or not target.days:
            continue

        for day_offset in range(horizon_days + 1):
            day = today + timedelta(days=day_offset)
            if day.weekday() not in target.days:
                continue

            for start in starts_for(target, catalog, day.weekday(), now - SLOT_MAX_AGE):
                hh, mm = map(int, start.split(":"))
                session_start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                if is_tennis_branch(target.branch):
                    if tennis_seviye2(target):
                        open_at_66 = session_start - timedelta(hours=66)
                        offset_h = expected_tennis_offset_hours(tennis_last_pick, open_at_66)
                    else:
                        offset_h = 72
                else:
                    offset_h = timing.open_offset_hours
                open_at = session_start - timedelta(hours=offset_h)
                if open_at + hot_after < now:
                    continue

                key = (target.name, session_start)
                if key in seen:
                    continue
                seen.add(key)

                entries.append((
                    Reminder(
                        remind_at=open_at - LEAD,
                        open_at=open_at,
                        session_start=session_start,
                        target_name=target.name,
                    ),
                    priority,
                ))

    entries.sort(key=lambda e: (e[0].remind_at, e[1]))
    return [r for r, _ in entries]


def weekly_slots(
    reminders: Iterable[Reminder], local_tz: tzinfo | None = None
) -> list[tuple[int, int, int]]:
    """Distinct (weekday 0=Mon..6=Sun, hour, minute) of each reminder's `remind_at`,
    converted to `local_tz` (None = system local, same as `datetime.astimezone(None)`).
    Sorted; this is what src.reminder_sched.sync() registers as OS scheduler triggers.
    """
    seen: set[tuple[int, int, int]] = set()
    for r in reminders:
        local = r.remind_at.astimezone(local_tz)
        seen.add((local.weekday(), local.hour, local.minute))
    return sorted(seen)


def due_now(reminders: Iterable[Reminder], now: datetime, hot_after: timedelta) -> list[Reminder]:
    """Reminders whose dialog should be showing right now: `remind_at` - 2 min slack
    (scheduler jitter / wake-from-sleep delay) <= `now` <= `open_at` + `hot_after`."""
    return [r for r in reminders if r.remind_at - _DUE_SLACK <= now <= r.open_at + hot_after]


# --------------------------------------------------------------------------
# App status (state/app_status.json)
# --------------------------------------------------------------------------

def write_app_status(running: bool, path: Path | None = None) -> None:
    """Atomic write of `{"pid", "running", "updated_at"}` to `path`. Never raises."""
    p = Path(path) if path is not None else APP_STATUS_PATH
    data = {"pid": os.getpid(), "running": running, "updated_at": datetime.now().isoformat()}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".{p.name}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError:
        log.exception("app_status yazılamadı: %s", p)


def read_app_status(path: Path | None = None) -> dict | None:
    """Parsed `app_status.json`, or None if missing/corrupt (never raises)."""
    p = Path(path) if path is not None else APP_STATUS_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def clear_app_status(path: Path | None = None) -> None:
    """Remove `app_status.json` (e.g. on app shutdown). Never raises."""
    p = Path(path) if path is not None else APP_STATUS_PATH
    try:
        p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Tennis pick history (state/tennis_history.json) -- Ekim 2026 duyurusu
# --------------------------------------------------------------------------

def write_tennis_history(last_pick: datetime | None, path: Path | None = None) -> None:
    """Atomic write of `{"last_tennis_session_start": <naive ISO or null>}`: the member's
    most recent tennis pick (`src.reservations.last_tennis_pick`), naive/site-local like
    that function's result -- no tz attached here. Never raises."""
    p = Path(path) if path is not None else TENNIS_HISTORY_PATH
    data = {"last_tennis_session_start": last_pick.isoformat() if last_pick else None}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".{p.name}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError:
        log.exception("tenis geçmişi yazılamadı: %s", p)


def read_tennis_history(path: Path | None = None) -> datetime | None:
    """Parsed `tennis_history.json`'s naive datetime (caller attaches the site tz), or
    None if missing/corrupt/never written (never raises)."""
    p = Path(path) if path is not None else TENNIS_HISTORY_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("last_tennis_session_start") if isinstance(data, dict) else None
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------

def app_is_open(lock_path: Path | None = None) -> bool:
    """Whether the app (or `bot.py`) currently holds the single-instance lock."""
    p = Path(lock_path) if lock_path is not None else single_instance.DEFAULT_LOCK_PATH
    lock = single_instance.SingleInstance(p)
    if lock.acquire():
        lock.release()
        return False
    return True


def launch_app(root: Path, *, popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
    """Start `app.pyw` as a detached process (never a child of this scheduler-spawned
    one -- see analysis/reminders-design-2026-10.md §0.7). Never raises.

    Windows: `pythonw.exe` next to `sys.executable` if present (no console flash), else
    `sys.executable`; spawned with CREATE_BREAKAWAY_FROM_JOB|DETACHED_PROCESS|
    CREATE_NEW_PROCESS_GROUP so Task Scheduler's job object doesn't kill it when this
    process exits, retried without the breakaway flag if that raises (e.g. the task was
    started without the right to break away). POSIX: `start_new_session=True` so
    launchd's AbandonProcessGroup-handled process group doesn't take it down either.
    """
    root = Path(root)
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    interpreter = pythonw if sys.platform == "win32" and pythonw.exists() else python
    argv = [str(interpreter), str(root / "app.pyw")]
    kwargs: dict = {"cwd": str(root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    if sys.platform == "win32":
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        try:
            popen(argv, creationflags=CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, **kwargs)
        except OSError:
            try:
                popen(argv, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, **kwargs)
            except OSError:
                log.exception("app.pyw başlatılamadı")
    else:
        try:
            popen(argv, start_new_session=True, **kwargs)
        except OSError:
            log.exception("app.pyw başlatılamadı")


def raise_app(pid: int | None) -> None:
    """Best-effort bring the running app's window to the front. Never raises.

    Windows: FindWindowW(None, APP_TITLE) -> ShowWindow(SW_RESTORE)+SetForegroundWindow.
    macOS: AppKit NSRunningApplication.activateWithOptions_ for `pid` (guarded import --
    silently does nothing if pyobjc isn't installed). Other platforms: no-op.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            SW_RESTORE = 9
            hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        elif sys.platform == "darwin" and pid is not None:
            try:
                from AppKit import NSRunningApplication
            except ImportError:
                return
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
    except Exception:
        log.exception("uygulama penceresi öne getirilemedi")


# --------------------------------------------------------------------------
# Fire-time behaviour
# --------------------------------------------------------------------------

def _weekday_tr(dt: datetime) -> str:
    return TR_DAY[WEEKDAYS[dt.weekday()]]


def _reminder_line(r: Reminder) -> str:
    return (
        f"• {_weekday_tr(r.session_start)} {r.session_start.strftime('%H:%M')} seansı — "
        f"açılış {_weekday_tr(r.open_at)} {r.open_at.strftime('%H:%M')} ({r.target_name})"
    )


def fire(
    now: datetime | None = None,
    *,
    ask: Callable[..., bool | None] = notify.ask,
    alert: Callable[[int], None] = notify.alert,
    app_open: Callable[[], bool] = app_is_open,
    launch: Callable[[Path], None] = launch_app,
    raise_window: Callable[[int | None], None] = raise_app,
    status: Callable[[], dict | None] = read_app_status,
    tennis_history: Callable[[], datetime | None] = read_tennis_history,
) -> int:
    """Recompute everything from the current settings/catalog/config and act. Always
    returns 0; never raises (a scheduler-spawned, console-less process must not crash).

    Nothing due -> nothing shown. Due, and the app is open with the engine running ->
    suppressed (it's already handling it). Due, app open & idle -> raise the window and
    show a one-button "Tamam" dialog. Due, app closed -> ask whether to launch it.
    """
    try:
        cfg = load_config(CONFIG_PATH)
        settings = load_settings(SETTINGS_PATH)
        catalog = Catalog.load(CATALOG_PATH)
    except Exception as e:
        log.error("ayarlar/katalog okunamadı, hatırlatma atlandı: %s", e)
        return 0

    tz = ZoneInfo(cfg.site.timezone)
    now = now if now is not None else datetime.now(tz)

    if not settings.reminders_enabled:
        log.info("hatırlatma kapalı, atlandı")
        return 0

    tennis_last_pick = tennis_history()
    if tennis_last_pick is not None:
        tennis_last_pick = tennis_last_pick.replace(tzinfo=tz)
    all_upcoming = upcoming(settings, catalog, cfg.timing, tz, now, tennis_last_pick=tennis_last_pick)
    hot_after = timedelta(seconds=cfg.timing.hot_after_s)
    due = due_now(all_upcoming, now, hot_after)
    if not due:
        log.info("hatırlatma: şu an yaklaşan açılış yok")
        return 0

    open_ = app_open()
    st = status()
    if open_ and st and st.get("running"):
        log.info("bot zaten çalışıyor, hatırlatma gösterilmedi (%d açılış)", len(due))
        return 0

    alert(2)
    text = "Rezervasyon açılışı yaklaşıyor:\n" + "\n".join(_reminder_line(r) for r in due)

    due_keys = {(r.target_name, r.session_start) for r in due}
    not_due = [r for r in all_upcoming if (r.target_name, r.session_start) not in due_keys]
    deadline = min(r.open_at + hot_after for r in due)
    if not_due:
        deadline = min(deadline, min(r.remind_at for r in not_due))
    timeout_s = max(30.0, min(1800.0, (deadline - now).total_seconds()))

    if open_:
        raise_window(st.get("pid") if st else None)
        ask(
            APP_TITLE, text + "\nUygulama açık — Başlat'a basın.",
            yes_label="Tamam", no_label=None, timeout_s=timeout_s,
        )
        log.info("hatırlatma: uygulama açık, bildirim gösterildi (%d açılış)", len(due))
    else:
        answer = ask(
            APP_TITLE, text + "\nUygulama şimdi açılsın mı?",
            yes_label="Uygulamayı aç", no_label="Kapat", timeout_s=timeout_s,
        )
        if answer is True:
            launch(ROOT)
            log.info("hatırlatma: uygulama kapalı, kullanıcı uygulamayı açtı (%d açılış)", len(due))
        else:
            log.info("hatırlatma: uygulama kapalı, bildirim gösterildi (%d açılış)", len(due))

    return 0


def _arg_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str]) -> int:
    """Entry point for `remind.pyw`. Sets up `state/logs/remind.log` (RotatingFileHandler,
    512 KB x 1), parses an optional `--at <ISO datetime>` override (for manual/test runs
    -- the OS scheduler never passes it), and runs `fire()`."""
    log_dir = STATE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_dir / "remind.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root_logger = logging.getLogger("ibbspor")
    root_logger.setLevel(logging.INFO)
    for old_handler in list(root_logger.handlers):
        try:
            old_handler.close()
        except Exception:
            pass
    root_logger.handlers.clear()
    root_logger.propagate = False
    root_logger.addHandler(handler)

    now = None
    at = _arg_value(argv, "--at")
    if at is not None:
        now = datetime.fromisoformat(at)

    return fire(now=now)
