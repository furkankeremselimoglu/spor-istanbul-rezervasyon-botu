"""Root entry point run by the OS scheduler (Windows Task Scheduler / macOS launchd --
see src/reminder_sched.py, Task B) once per registered weekly (weekday, HH:MM) trigger.
ROOT-ANCHOR: derives the project root from this file's own location, like app.pyw --
never move it.

Runs under pythonw.exe on Windows, which has no console at all -- never print() here;
all output goes to state/logs/remind.log via src.reminders.main's logging setup.

Manual/test run: `python remind.pyw --at 2030-01-01T00:00:00+03:00` (overrides "now").
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.reminders import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
