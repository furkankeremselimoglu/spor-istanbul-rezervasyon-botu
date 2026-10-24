# src/

Library modules imported by `app.pyw` and `bot.py` (the only entry points). Flat directory for Python modules — no subfolders (the one exception, `src/web/`, holds the static frontend assets — HTML/CSS/JS, not Python — served as-is by `webapp.py`). Modules are grouped here by role in prose rather than in the directory layout, so files never have to move (and their import paths never churn) when a module's role gets reclassified.

## Pure logic

No network, no Playwright/browser; safe to unit-test without a browser. Most of these read/write nothing beyond `config.toml` and `state/settings.json`/`state/catalog.json` (atomic saves); `version.py` additionally shells out to `git` (subprocess, 2s timeout, never raises) and `diagnostics.py` reads the rest of `state/` to build a support ZIP — see each entry below.

- **`config.py`** — Parses and validates `config.toml` into frozen dataclasses (`Config`, `SiteConfig`, `TimingConfig`, `SafetyConfig`, `SmsConfig`, `SessionConfig`, `TargetsConfig`, `Pref`). Entry point: `load_config(path)`. Raises `ConfigError` on invalid input. Also defines `WEEKDAYS = ("Mon", "Tue", ..., "Sun")` and `has_legacy_targets` property (true when migration should be attempted).

- **`settings.py`** — Owns `state/settings.json`: the GUI-written settings (membership text, targets, dry_run, stop_after_success). Pure functions `settings_from_dict`, `settings_to_dict`, and `validate(s)` handle round-trips and validation (Turkish error messages). `load_settings()` loads the JSON file; if absent, migrates from old `config.toml`'s single-target list (when `legacy_cfg.has_legacy_targets`). `save_settings()` does atomic file save (temp + os.replace, never leaves a half-written file). Kept pure except load/save, which do plain UTF-8 file I/O — no Playwright, no network.

- **`targets.py`** — The `Target` model (id, name, enabled, branch, facility, halls, level_prefix, days, time_from, time_to, starts) replacing the old single-preference-list (config.Pref). Pure functions: `find_target_multi(grid, targets, booked_dates)` (walk targets in order, return first with an open matching card) and `matches(target, card)` (OPEN ∧ checkbox ∧ weekday∈days ∧ time ∧ hall ∧ level ∧ not booked). Also `new_target_id()` (generate UUIDs) and `tr_day(weekday_int)` (English weekday name → Turkish name). Ekim 2026 duyurusu (tenis 72h/66h open rule): `is_tennis_branch(branch)`, `tennis_seviye_number(level_prefix)`, `tennis_hot_dual_window(target)` (72h+66h silent hot-polling eligibility), `tennis_seviye2(target)` (predicted single display moment eligibility).

- **`catalog.py`** — Pure catalog model: `Catalog{scanned_at, branches:{name: Branch{facilities:{name: Facility{halls:{name:last_seen}, levels:{name:last_seen}, slots:{(weekday,start,end):last_seen}, last_scanned}}}}}`. `observe(branch, facility, grid, now)` union-merges a new grid's cards into the catalog. JSON load/save to `state/catalog.json` is atomic. Names are stored exactly as the site renders them (Turkish casing matters). Entry points: `load()`, `save()`, `observe()`.

- **`grid.py`** — Parses the "/uyeseanssecim" (Uye Seans Secim) grid page HTML into structured `Grid` and `SessionCard` objects. Entry points: `parse_grid(html, membership_text, now)` (returns `Grid` or raises `ParseError`) and `find_card(grid, target)` (find a card matching a target, raises `SiteError` if none found). Also defines `Status` enum (OPEN, CLOSED, FULL, BOOKED), `SessionCard` dataclass, and `is_selection_page(html)` check.

- **`schedule.py`** — Session timing math: `session_starts(pref, now, tz, count)` (next N weekly occurrences), `next_open_window(pref, now, tz, offset_hours)` (when the reservation window opens), `poll_interval(...)` (how long to sleep between polls given timing config). All pure, deterministic given inputs (no wall clock, all datetimes timezone-aware via `zoneinfo`).

- **`planner.py`** — Pure multi-group planner. Given targets, catalog, timing config, and current time, `Planner.next(now, booked_dates, ...)` returns a `Plan` (which group to poll, how long to sleep, whether we're in a "hot" window near an opening). Groups = distinct (branch, facility) of enabled targets; facility `"*"` is expanded to every concrete facility the catalog has seen. Handles hot vs. idle polling with round-robin and rate limiting. Ekim 2026 duyurusu: `windows()` emits a Window per entry of `timing.tennis_open_offsets_hours` (72h+66h) for tennis targets eligible for the dual hot-polling safety net (`src.targets.tennis_hot_dual_window`); `expected_tennis_offset_hours(last_tennis_session_start, open_at_66)` and `next_window_for_display(...)` pick the single predicted moment for reminders/dashboard display.

- **`reservations.py`** — Parses the member's existing reservations from the Seanslarım page's detail table. `parse_reservations(html)` returns list of `Reservation` objects (date, start, end, facility, hall, attended, cancellable). Used by `bot.py reservations|cancel`. `last_tennis_pick(items)` (Ekim 2026 duyurusu): most recent tennis session by date+start, for the 72h/66h open-rule prediction.

- **`credentials.py`** — OS keychain storage for TC/pasaport no + şifre (Windows Kimlik Bilgisi Yöneticisi / macOS Keychain / Linux Secret Service via the `keyring` library). Functions: `save(tc, password)`, `load()` (returns `(tc, password)` or `None`), `clear()`. Safety rules: password never logged, TC only logged masked (`mask_tc`); `load()` never raises (missing/broken backend = `None`, allowing caller to fall back to manual login).

- **`version.py`** — `get_version()`: the app version shown in the web UI sidebar and in diagnostics bundles. Three sources in order: git-archive export-subst (`.gitattributes` marks this file `export-subst`, so any ZIP built via `git archive` — `tools/make_release.py` or GitHub's "Download ZIP" — carries a baked-in tag-based version like "2026.10.23"), else `git describe --tags --always --dirty` against a live checkout, else a "geliştirme sürümü" fallback. New function `parse_version(s)` parses the numeric `X.Y.Z[.N]` prefix into a tuple for version comparison; returns None if `s` doesn't parse (e.g., the fallback or an untagged commit). All git calls are subprocess, 2s timeout, never raise.

- **`diagnostics.py`** — `create_diagnostics_zip(dest_dir)`: builds a redacted support ZIP (`ibbspor-tani-<timestamp>.zip`) from `state/logs/*`, the most recent 40 `state/artifacts/*.html` (never the PNGs), `state/settings.json`, a `catalog.json` size/summary, and version/OS/Python/package info. `redact()` masks TC kimlik no, phone numbers, the member's full name (found via an "AdSoyad" element in the collected HTML), and tokens/cookies in every file before it's written. Never includes `cookies.json`/`profile/`/OS-keychain credentials; never raises for a missing source file. Used by `bot.py diagnostics` and the web UI's "Tanı paketi" button (Hesap › Yardım).

- **`reminders.py`** — Pure reminder computation: given settings, catalog, timing config, and current time, computes upcoming reminders (15 minutes before each opening). Dataclass `Reminder` (remind_at, open_at, session_start, target_name) with functions `upcoming(settings, catalog, timing, tz, now, tennis_last_pick=None)` (Ekim 2026 duyurusu: exactly one reminder per tennis session, predicted 66h/72h for Seviye-2 targets via `src.planner.expected_tennis_offset_hours`) and `weekly_slots(reminders)` (extract distinct reminder times for OS scheduler). Also app-status helpers: `write_app_status(running)` / `read_app_status()` (atomic file I/O to `state/app_status.json`), tennis-pick-history helpers `write_tennis_history(last_pick)` / `read_tennis_history()` (atomic file I/O to `state/tennis_history.json`), `app_is_open()` (checks single-instance lock), `fire(now)` (fire-time behavior when the scheduler runs `remind.pyw`), `main(argv)` (entry point for the reminder process, sets up logging).

- **`updater.py`** — Self-update: check GitHub for a newer release, download and stage it. `check_latest(api_url, timeout)` polls the unauthenticated `releases/latest` endpoint for the newest release's zip asset. `is_newer(current, latest)` compares versions via `parse_version` from `version.py`. `stage(release)` downloads the zip, verifies sha256 (when provided), extracts it, and stands up `apply_update.py`. `launch_apply(root, new_dir, tag)` spawns the apply script as a detached process. Uses certifi's CA bundle for TLS when available, the interpreter's default otherwise. Never makes real network calls during tests; `check_latest` returns None on any failure (logged at INFO, never raises). Dataclass `Release` and custom `UpdateError` exception (Turkish message).

## Browser / site I/O

Drive Playwright against online.spor.istanbul; handle session, rate limiting, screenshots.

- **`browser.py`** — Playwright session lifecycle: `open_browser(headless)` (creates a persistent browser + page, lazy loading), `run_login(cfg)` (manual login flow), logging setup (`setup_logging`, skips stdout when `sys.stdout is None` — running under `pythonw`). Also `RateLimiter` (class-level per Site), `AttemptLimiter` (rolling window for auto-login attempts, avoid lockout), `Artifacts` (screenshots + HTML capture with timeout), and `LoggedOut` / `SiteError` exceptions. ⚠️ **ROOT-ANCHOR**: `PROJECT_ROOT` is derived from this module's own location (`Path(__file__).parent.parent`), so `browser.py` must never move deeper than `src/`, or every path under `state/` silently shifts.

- **`site.py`** — High-level page-object layer for online.spor.istanbul (ASP.NET WebForms postbacks). `Site` wraps a Playwright `page` and exposes: `switch_to(branch, facility)` and `refresh()` (navigate to /uyeseanssecim with filters), `select_card(card)` (click the seans checkbox), `confirm_and_save()` (click Kaydet), `unselect_card()` (uncheck, used in dry-run). Session helpers: `check_logged_in()`, `auto_login()` (attempts auto-login with credentials if saved, respects rate limit), `wait_for_login(timeout)` (manual), `login_if_needed()` (chain check → auto → manual), `ensure_ready_or_login(branch, facility)` (switch + ensure logged in), `open_membership()` and `apply_filters(...)` (form submission). Rate limiting via `RateLimiter` before navigation-causing actions. `_check_session()` raises `LoggedOut` on login-page signs (after each navigation).

- **`notify.py`** — Cross-platform user notifications: macOS (via `osascript`), Windows (via Windows API), Linux (fallback to stdout). `notify(title, message, sound=True)` for bell/chime. `ask(title, message, *, yes_label, no_label, timeout_s)` for modal dialogs: Windows `MessageBoxTimeoutW`, macOS `osascript display dialog`, Linux/fallback → None. Both safe: never raise, return None on unsupported/failure. Also `keep_awake()` context manager: Windows (no-op; system handles it), macOS (wraps `caffeinate -i`).

## Orchestration / UI

Ties the above together into the behaviors `app.pyw` and `bot.py` expose.

- **`runner.py`** — The multi-target engine loop, shared by both CLI and GUI. Two API shapes: **new** `run(cfg, settings, site, io, *, catalog=None, dry_run=False, once=False)` (used by `worker.py`; caller owns the browser/Site and RunIO), and **old** `run(cfg, dry_run=..., once=...)` (used by `bot.py`; opens browser itself). Core loop: poll groups in order, find a matching target's open card, attempt save (guarded by `SaveBudget` budget), handle SMS, book (or unbook in dry-run), return on success (or iterate per `stop_after_success`). Also `diff_observations` (pure, used by `run` and the `watch` command) and `record_observations` (appends to `state/observations.jsonl`). **Hard safety rule**: `Site.click_save` is the ONLY place that clicks Kaydet, guarded by `SaveBudget`.

- **`events.py`** — Frozen dataclasses for GUI ⇄ engine protocol. Commands (GUI → engine): `Start`, `Stop`, `SmsCode`, `ScanCatalog`, `TestLogin`, `ManualLogin`, `ShowBrowser`, `HideBrowser`, `ListReservations`, `CancelReservation`, `Quit`. Events (engine → GUI): `Status`, `Log`, `NextWindow`, `Found`, `SmsNeeded`, `SmsResult`, `Booked`, `Failed`, `LoggedOut`, `LoginResult`, `CatalogProgress`, `CatalogUpdated`, `Reservations`, `Stopped`, `Error`. Plain dataclasses, no behaviour, so both sides can import them without cycles.

- **`worker.py`** — `EngineThread`: the one thread that owns Playwright for the app's entire lifetime. Receives commands via `send()`, runs long operations (Start, ScanCatalog, etc.) on this thread (Playwright sync API requires all calls on the owning thread). `webapp.py`'s pump thread drains its events queue and feeds them to `Controller`/the web UI (no Tk, no `root.after` — that was the old `src/gui.py`, removed). Stop/SmsCode are "immediate" (acted on directly by `send()`, not queued). ShowBrowser/HideBrowser are recorded; the thread applies them at safe points.

- **`controller.py`** — Pure view-model: turns `events.Event` history into a `ViewState` (what the UI should display). Implements the state machine (idle → starting → monitoring → hot → found → sms_wait → booked/stopped/error) and user intents (start, stop, sms code, scan, test login, etc.). No UI-toolkit import, no event queueing — unit-testable without a display. Used by `webapp.py` to turn engine events into the JSON the web frontend polls.

- **`single_instance.py`** — Cross-platform single-instance lock for `app.pyw` (`state/app.lock`). OS-level advisory lock: `msvcrt.locking` on Windows, `fcntl.flock` on POSIX. Non-blocking: acquiring an already-held lock fails immediately (never blocks, never raises). Stale-safe by construction (lock tied to open file descriptor; releasing is automatic when the process dies). `bot.py` takes the same lock for every subcommand that drives a real browser (login/probe/watch/run/scan/reservations/cancel/catalog), so the CLI and a running `app.pyw` can never touch `state/profile/` at once.

- **`webapp.py`** — Local web UI backend: a stdlib `ThreadingHTTPServer` + a pump thread that drains the engine's events queue into `Controller`, serving the JSON API and the static frontend (`src/web/`). Security: binds 127.0.0.1 only, requires a per-process `X-Token` (read from the page's `?t=` query string) on every `/api/*` call, checks the `Host` header, blocks directory traversal. See `analysis/web-ui-api.md` for the full API contract. Also manages reminder sync: `_request_reminder_sync()` computes reminder slots and calls the scheduler (if provided) to register them with the OS.

- **`reminder_sched.py`** — OS scheduler registration: Windows Task Scheduler XML (via `schtasks`) or macOS LaunchAgent plist (via `launchctl`). Dataclass `SyncResult` (slots, registered, error, changed). Functions: `register(slots, ...)` (Windows: write XML, run schtasks; macOS: write plist, run launchctl), `unregister(...)` (cleanup), `sync(slots, enabled, ...)` (idempotent; serialized by lock; caches registered state in `state/reminders/registered.json` to avoid churning OS calls on every catalog save). `SchedulerError` raised on OS command failure; `sync` catches and returns as error field. Platform detection: no calls on Linux or unsupported OS.

**`src/web/`** — the static frontend: `index.html`, `app.js`, `app.css`. Plain HTML/CSS/JS, no build step; served by `webapp.py` and polls its JSON API (`/api/state?since=...`). Replaces the old Tk GUI (`src/gui.py`, removed) entirely — there is no Tk anywhere in this project anymore. Also includes the reminders UI: toggle + card showing upcoming reminders and sync status.

---

## Root entry points

- **`remind.pyw`** — Fire-time entry point run by the OS scheduler (Windows Task Scheduler, macOS launchd). ~10 lines: adds project root to sys.path, imports and calls `src.reminders.main(sys.argv[1:])`. **ROOT-ANCHOR**: derives project root from `__file__`; must stay at project root or all paths shift.

---

## Root-anchor warning

**`browser.py` and `settings.py`** both derive the project root from their own location:
- `browser.py`: `PROJECT_ROOT = Path(__file__).resolve().parent.parent` → used for `STATE`, `PROFILE_DIR`, `ARTIFACTS_DIR`, `LOG_DIR`
- `settings.py`: `STATE = Path(__file__).resolve().parent.parent / "state"` → same path

**Do not move these modules deeper into a subdirectory**, or `state/` and all relative paths will silently shift, causing the app to read/write in the wrong place with all tests passing. Similar root-anchor risk exists in any module that derives paths from `__file__`. Before any refactor that touches these files, add a test asserting that the project root resolves correctly and that the state directory exists.

---

## Testing

All modules are imported by tests without a browser or network. See `tests/README.md` for how to run tests.
