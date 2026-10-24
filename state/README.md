# state/

Everything the bot **writes** at runtime. Git-ignored except this README.

## What's here

| Path | What | Safe to delete? | Notes |
|---|---|---|---|
| `settings.json` | **GUI-written settings**: membership text, enabled targets, dry_run flag, stop_after_success flag, reminders_enabled flag | ❌ **NO** — contains your targets | JSON; version 1; re-created with defaults if deleted, but you'll lose all configured targets |
| `catalog.json` | **Tesis katalog**: branches → facilities → halls, levels, session slots (scanned from the live site) | ✅ **YES** | Auto-rebuilt next run; used by GUI dropdowns. Safe to delete to force a rescan |
| `app_status.json` | **App status**: PID, running flag, last update time (written by app.pyw when starting/stopping, read by remind.pyw at fire time) | ✅ **YES** | Safe to delete; auto-created. Used to suppress reminder dialogs when the engine is already running |
| `tennis_history.json` | **Latest tennis pick**: naive datetime of the member's most recent tennis session (by date+start), written whenever reservations are fetched (Seanslarım) | ✅ **YES** | Safe to delete; auto-created next fetch. Used by remind.pyw / the dashboard to predict the Ekim 2026 72h/66h tennis open-rule moment (src.planner.expected_tennis_offset_hours); missing/corrupt just means "predict the fixed 72h moment" |
| `cookies.json` | Chromium profile cookies (login state) | ✅ **YES** (usually) | Deleting logs you out; run `python bot.py login` to log in again. Can safely delete if you want a fresh session |
| `profile/` | **Chromium user-data-dir**: persistent browser profile, login cookies, browsing data | ✅ **YES** (usually) | Deleting logs you out; run `python bot.py login` to log in again. Can safely delete if you want a completely fresh browser |
| `reminders/task.xml` | **Windows Task Scheduler XML**: the registered scheduled task (read-only, auto-generated) | ✅ **YES** | Safe to delete; auto-regenerated on next sync. Used by Task Scheduler via `schtasks /Create` |
| `reminders/registered.json` | **Scheduler cache**: slots, Python path, script path (used to avoid re-registering unchanged reminders) | ✅ **YES** | Safe to delete; next start will force a resync with the OS scheduler |
| `logs/bot.log` | **Rotating application log**: debug, info, warning, error messages (5 files × 2 MB each) | ✅ **YES** | Safe to delete; new log starts fresh. Useful for debugging issues |
| `logs/remind.log` | **Reminder log**: messages from `remind.pyw` at fire time (5 files × 512 KB each) | ✅ **YES** | Safe to delete; auto-created on next reminder. Only present when reminders are enabled |
| `logs/remind.launchd.log` | **macOS launchd log**: stdout/stderr from remind.pyw (macOS only) | ✅ **YES** | Safe to delete; auto-created on next reminder. Only present on macOS when reminders are enabled |
| `artifacts/<timestamp>_<step>.png` | **Screenshots**: PNG images saved at critical steps (login, filter changes, card selection, Kaydet, SMS) and errors | ✅ **YES** | Safe to delete; only created when `[safety] screenshot_every_step = true` (default). Useful for investigating bugs. **Auto-pruned**: `Artifacts.prune()` keeps only the most recent 500 (png+html) pairs |
| `artifacts/<timestamp>_<step>.html` | **HTML snapshots**: raw HTML of the page at critical steps and errors | ✅ **YES** | Always saved with the PNG; useful for debugging parsing issues. Same auto-pruning (last 500) as the PNGs |
| `observations.jsonl` | **Session opening observations**: append-only log of status changes per session card (used to learn when tennis slots actually open) | ✅ **YES** | Created by `bot.py watch` or engine runs with observation recording. Safe to delete; append-only so never corrupted |
| `sms_code.txt` | **Temporary file**: SMS code input channel (besides the bot window and terminal) — write digits here, bot reads & deletes within ~1 second | ⚠️ **DO NOT DELETE** while bot is waiting for SMS | Stale file from a prior run is auto-deleted at the start of the next SMS wait. Meant for relaying a code from another phone |
| `update/` | **Self-update staging directory** | ✅ **YES** | Contains: `<tag>.zip` (while downloading), `new/` (staged files), `backup/` (previous versions of replaced files), `apply_update.py` (copy of the updater). Safe to delete while the app is closed; automatically recreated on next update. |
| `app.lock` | **Single-instance lock file**: prevents running the bot twice simultaneously | ✅ **YES** (if bot is stopped) | Tied to the process; auto-released when bot exits. Delete only if you're sure the bot is not running. If bot crashes and lock persists, safe to delete and restart |

## Invariants

1. **`settings.json` is NOT safe to delete casually** — it contains your configured targets. Keep it backed up or recreate your targets after deletion.

2. **`profile/` + `cookies.json` store login state**. Deleting either logs you out. `profile/` also stores extension data and Chrome's built-in settings.

3. **`observations.jsonl` is append-only**. It's never rewritten or corrupted, even if the bot crashes mid-write. Safe to read and delete any time.

4. **`sms_code.txt` is ephemeral**. The bot deletes any stale copy at the start of a fresh SMS wait (so old codes are never reused). A human or relay process may write it, but nothing should assume it persists beyond the current wait.

5. **`artifacts/` is auto-pruned to the most recent 500 screenshot/HTML pairs** (`Artifacts.prune(keep=500)`), so it no longer grows unbounded even with `screenshot_every_step = true` running for hours/days. You can still clean it up manually any time, or disable `screenshot_every_step` in `config.toml` if you'd rather not have screenshots at all.

6. **No code outside the bot reads state files except**:
   - Chromium reads `profile/` (own internal data)
   - Bot code reads `observations.jsonl` (append-only, for the `watch` command)
   - Bot code reads `sms_code.txt` (and deletes immediately)
   - GUI reads `settings.json` (and writes atomically)
   - Bot reads `catalog.json` (and writes atomically)

## Safe cleanup

If your `state/` directory grows too large:

```bash
# Delete old logs (keep the latest one):
rm state/logs/bot.log.*

# Delete old artifacts:
rm -r state/artifacts/

# Delete catalog (will rebuild on next run):
rm state/catalog.json

# Delete cookies (next run will ask you to log in):
rm state/cookies.json
rm -rf state/profile/

# Do NOT delete settings.json (your targets are there!)
```

After cleanup, start the bot normally — missing files are auto-created.

## Diagnostics ZIP (`src/diagnostics.py`, `bot.py diagnostics`)

A support bundle built from this directory, for sending to the bot's author when
something goes wrong: the web UI's "Tanı paketi" button (Hesap › Yardım) and
`bot.py diagnostics [--dest DIR]` (default: Desktop) both call
`create_diagnostics_zip()`, which produces `ibbspor-tani-<timestamp>.zip`
containing:

- `logs/` — all of `logs/bot.log` and its rotations (`bot.log.1` .. `bot.log.5`), plus `logs/remind.log*` (when present)
- `artifacts/` — the most recent 40 artifact **HTML** snapshots (never the PNGs —
  screenshots can show personal account pages as pixels and can't be redacted)
- `settings.json` — in full
- `catalog_summary.txt` — a size/branch/facility-count summary of `catalog.json`,
  **not** its full content
- `environment.txt` — app version (`src.version.get_version()`), OS, Python and key
  package versions
- `README.txt` — explains the above to whoever receives the ZIP

**Never included**: `cookies.json`, `profile/` (the live login session), or anything
from the OS keychain (TC/pasaport no, password) — none of these ever leave this
machine via a diagnostics ZIP.

**Redacted on the fly** in every included file (the files on disk are never modified):
11-digit numbers (TC kimlik no) → `***TC***`; Turkish phone numbers → `***TEL***`; the
member's full name (found via an "AdSoyad" element in the collected HTML, then
replaced everywhere) → `***AD SOYAD***`; `X-Token`/`?t=` tokens and cookie values →
`***TOKEN***`/`***COOKIE***`.

---

**Note:** This directory is Git-ignored. Your local settings, tokens, and session data never leave your machine.
