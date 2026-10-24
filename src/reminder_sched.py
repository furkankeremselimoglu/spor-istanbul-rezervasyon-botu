"""OS scheduler registration for reminder notifications. See
analysis/reminders-design-2026-10.md, SS0 items 3/7 and SS1 "OS registration" (Task B).

Registers/unregisters ONE OS scheduler entry that runs `remind.pyw` at a set of weekly
(weekday, hour, minute) slots: Windows Task Scheduler (one task, one <CalendarTrigger>
per distinct HH:MM, via `schtasks /Create ... /XML`), macOS launchd (one LaunchAgent
plist with a `StartCalendarInterval` array). `sync()` is the only entry point the rest
of the app calls; it is idempotent (skips the OS call when nothing changed) and never
raises -- failures come back as `SyncResult.error` (Turkish).

Cross-platform (Windows + macOS): no shell=True, paths always passed as argv elements;
`run` (the subprocess invoker) is injectable so tests never spawn schtasks/launchctl for
real. `os.getuid()` only exists on POSIX -- it must never be reached on the Windows path.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger("ibbspor.reminder_sched")

TASK_NAME = "IBBSporRezervasyonHatirlatma"
LAUNCHD_LABEL = "com.ibbspor.rezervasyon.hatirlatma"

ROOT = Path(__file__).resolve().parent.parent
REG_DIR = ROOT / "state" / "reminders"

_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_WEEKDAY_TAGS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

Slot = tuple[int, int, int]  # (weekday 0=Mon, hour, minute)
Runner = Callable[[list[str]], "subprocess.CompletedProcess"]

_sync_lock = threading.Lock()


class SchedulerError(RuntimeError):
    """Registering/unregistering with the OS scheduler failed. Message is Turkish."""


@dataclass(frozen=True)
class SyncResult:
    slots: tuple[Slot, ...]
    registered: bool
    error: str | None
    changed: bool


# --------------------------------------------------------------------------
# Pure builders
# --------------------------------------------------------------------------

def windows_task_xml(slots: Sequence[Slot], *, command: str, arguments: str, workdir: str,
                      today: date) -> str:
    """Build the Task Scheduler XML body (no `<?xml ...?>` prolog -- that is added at
    write time, since an encoding-declared prolog can't be parsed back from a Python str).

    One <CalendarTrigger> per distinct (hour, minute); its DaysOfWeek is the union of
    weekdays that use that time, and its StartBoundary is the first date >= `today` whose
    weekday is one of them.
    """
    ET.register_namespace("", _TASK_NS)

    def tag(name: str) -> str:
        return f"{{{_TASK_NS}}}{name}"

    by_time: dict[tuple[int, int], set[int]] = {}
    for weekday, hour, minute in slots:
        by_time.setdefault((hour, minute), set()).add(weekday)

    root = ET.Element(tag("Task"), {"version": "1.2"})

    triggers = ET.SubElement(root, tag("Triggers"))
    for (hour, minute), weekdays in sorted(by_time.items()):
        start = _first_date_on_or_after(today, weekdays)
        trigger = ET.SubElement(triggers, tag("CalendarTrigger"))
        ET.SubElement(trigger, tag("StartBoundary")).text = (
            f"{start.isoformat()}T{hour:02d}:{minute:02d}:00"
        )
        ET.SubElement(trigger, tag("Enabled")).text = "true"
        schedule = ET.SubElement(trigger, tag("ScheduleByWeek"))
        days_el = ET.SubElement(schedule, tag("DaysOfWeek"))
        for weekday in sorted(weekdays):
            ET.SubElement(days_el, tag(_WEEKDAY_TAGS[weekday]))
        ET.SubElement(schedule, tag("WeeksInterval")).text = "1"

    principals = ET.SubElement(root, tag("Principals"))
    principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"

    settings = ET.SubElement(root, tag("Settings"))
    ET.SubElement(settings, tag("MultipleInstancesPolicy")).text = "Parallel"
    ET.SubElement(settings, tag("DisallowStartIfOnBatteries")).text = "false"
    ET.SubElement(settings, tag("StopIfGoingOnBatteries")).text = "false"
    ET.SubElement(settings, tag("StartWhenAvailable")).text = "true"
    ET.SubElement(settings, tag("ExecutionTimeLimit")).text = "PT0S"
    ET.SubElement(settings, tag("WakeToRun")).text = "false"

    actions = ET.SubElement(root, tag("Actions"), {"Context": "Author"})
    exec_el = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(exec_el, tag("Command")).text = command
    ET.SubElement(exec_el, tag("Arguments")).text = arguments
    ET.SubElement(exec_el, tag("WorkingDirectory")).text = workdir

    return ET.tostring(root, encoding="unicode")


def _first_date_on_or_after(today: date, weekdays: set[int]) -> date:
    """First date >= `today` whose `date.weekday()` (0=Mon) is in `weekdays`."""
    from datetime import timedelta

    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            return candidate
    raise AssertionError("unreachable: weekdays is non-empty")  # pragma: no cover


def launchd_plist(slots: Sequence[Slot], *, program_args: list[str], workdir: str,
                   log_path: str) -> dict:
    """Pure: a plistlib-ready dict for the LaunchAgent. `slots` must be non-empty -- an
    empty `StartCalendarInterval` array is unregister's job, not register's."""
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(program_args),
        "WorkingDirectory": workdir,
        "StartCalendarInterval": [
            {"Weekday": (weekday + 1) % 7, "Hour": hour, "Minute": minute}
            for weekday, hour, minute in slots
        ],
        "RunAtLoad": False,
        "AbandonProcessGroup": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


# --------------------------------------------------------------------------
# register / unregister
# --------------------------------------------------------------------------

def register(slots: Sequence[Slot], *, root: Path, python: Path, run: Runner, home: Path,
             platform: str, reg_dir: Path, task_name: str = TASK_NAME) -> None:
    """Write the scheduler definition for `slots` (non-empty) and register it with the OS.
    Raises SchedulerError on failure. `platform` is passed explicitly (not read from
    sys.platform) so callers/tests can exercise either OS from either OS."""
    if platform == "win32":
        _register_windows(slots, root=root, python=python, run=run, reg_dir=reg_dir,
                           task_name=task_name)
    elif platform == "darwin":
        _register_macos(slots, root=root, python=python, run=run, home=home)
    else:
        raise SchedulerError(f"desteklenmeyen platform: {platform}")


def unregister(*, run: Runner, home: Path, platform: str, reg_dir: Path,
                task_name: str = TASK_NAME) -> None:
    """Idempotent: a missing task/agent ("not found") is not an error. `reg_dir` is accepted
    for signature symmetry with register()/sync() (the cache file it holds is sync's job,
    not this function's)."""
    del reg_dir
    if platform == "win32":
        try:
            run(["schtasks", "/Delete", "/TN", task_name, "/F"])
        except Exception:
            pass  # non-zero/exception on delete is tolerated -- "not found" is fine
    elif platform == "darwin":
        uid = os.getuid()
        try:
            run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        except Exception:
            pass
        plist_path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        plist_path.unlink(missing_ok=True)


def _register_windows(slots: Sequence[Slot], *, root: Path, python: Path, run: Runner,
                       reg_dir: Path, task_name: str) -> None:
    pythonw = python.with_name("pythonw.exe")
    command = str(pythonw) if pythonw.exists() else str(python)
    script = root / "remind.pyw"
    arguments = f'"{script}"'
    workdir = str(root)

    body = windows_task_xml(slots, command=command, arguments=arguments, workdir=workdir,
                             today=date.today())

    reg_dir.mkdir(parents=True, exist_ok=True)
    xml_path = reg_dir / "task.xml"
    # encoding="utf-16" writes a BOM, required for Task Scheduler to accept non-ASCII
    # paths (e.g. a Turkish username) in the XML.
    xml_path.write_text('<?xml version="1.0" encoding="UTF-16"?>\r\n' + body, encoding="utf-16")

    result = run(["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"])
    if result.returncode != 0:
        raise SchedulerError(f"Windows görev zamanlayıcısına kaydedilemedi: {_tail(result)}")


def _register_macos(slots: Sequence[Slot], *, root: Path, python: Path, run: Runner,
                     home: Path) -> None:
    plist_path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    log_path = str(root / "state" / "logs" / "remind.launchd.log")
    plist = launchd_plist(
        slots,
        program_args=[str(python), str(root / "remind.pyw")],
        workdir=str(root),
        log_path=log_path,
    )
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(plist))

    uid = os.getuid()
    try:
        # Unload any previously-loaded copy first; launchd refuses to bootstrap a label
        # that's already loaded. Not loaded yet (first registration) -> non-zero, ignored.
        run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
    except Exception:
        pass
    result = run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result.returncode != 0:
        raise SchedulerError(f"launchd'ye kaydedilemedi: {_tail(result)}")


def _tail(result: "subprocess.CompletedProcess", n: int = 500) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-n:]


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def _default_run(args: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        args, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def sync(slots: Sequence[Slot], *, enabled: bool, root: Path = ROOT, python: Path | None = None,
          run: Runner = _default_run, home: Path | None = None, platform: str | None = None,
          reg_dir: Path = REG_DIR, force: bool = False) -> SyncResult:
    """Register/unregister the OS scheduler entry so it matches `slots` (if `enabled`) or
    nothing (if not). Never raises. Skips the OS call entirely when the last successful
    sync already matches (unless `force`) -- callers may call this on every settings/
    catalog save without churning schtasks/launchd."""
    slots = tuple(slots)
    python = Path(sys.executable) if python is None else python
    home = Path.home() if home is None else home
    platform = sys.platform if platform is None else platform

    with _sync_lock:
        if platform not in ("win32", "darwin"):
            return SyncResult(slots=slots, registered=False, error=None, changed=False)

        effective = slots if enabled else ()
        script = root / "remind.pyw"
        cache_path = reg_dir / "registered.json"
        cached = _read_cache(cache_path)
        unchanged = (
            cached is not None
            and tuple(tuple(s) for s in cached.get("slots", [])) == effective
            and cached.get("python") == str(python)
            and cached.get("script") == str(script)
        )
        if unchanged and not force:
            return SyncResult(slots=slots, registered=bool(effective), error=None, changed=False)

        try:
            if effective:
                register(effective, root=root, python=python, run=run, home=home,
                          platform=platform, reg_dir=reg_dir)
                _write_cache(cache_path, effective, python, script)
            else:
                unregister(run=run, home=home, platform=platform, reg_dir=reg_dir)
                cache_path.unlink(missing_ok=True)
            return SyncResult(slots=slots, registered=bool(effective), error=None, changed=True)
        except SchedulerError as e:
            log.error("reminder_sched: kayit basarisiz: %s", e)
            return SyncResult(slots=slots, registered=False, error=str(e), changed=True)
        except Exception as e:
            log.exception("reminder_sched: beklenmeyen hata")
            return SyncResult(
                slots=slots, registered=False,
                error=f"Hatırlatma zamanlayıcısına kaydedilemedi: {e}", changed=True,
            )


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, slots: tuple[Slot, ...], python: Path, script: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"slots": [list(s) for s in slots], "python": str(python), "script": str(script)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
