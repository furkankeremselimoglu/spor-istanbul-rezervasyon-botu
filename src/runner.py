"""The multi-target engine loop, and the observation-diffing shared by `run` and `watch`.

`run()` drives an already-open `Site`: it cycles the planner's chosen (branch, facility)
group, looks for a matching target's open card, books it, and stops (or keeps hunting,
per `settings.stop_after_success`). `diff_observations` is a pure function (no I/O) so it
can be unit-tested without a browser or the filesystem; `record_observations` wraps it
with the append-to-jsonl side effect that both `run` and the `watch` subcommand in bot.py
share.

Two call shapes share the name `run` (see analysis/final-design-2026-10.md, "Engine"):
  - the NEW api, `run(cfg, settings, site, io, *, catalog=None, dry_run=False, once=False)`,
    used by src/worker.py (EngineThread): the caller already owns an open Playwright
    browser/Site and a RunIO (stop event, sms code queue, event emitter).
  - the OLD api, `run(cfg, dry_run=..., once=...)`, still used by bot.py's `cmd_run`
    (unedited -- see T9). When `site`/`io` are omitted, `run` delegates to `run_cli`,
    which opens the browser itself and builds a throwaway RunIO (stdin-fed code queue,
    a no-op emit).

HARD SAFETY RULES (see incident notes: 14 Kaydet clicks = 14 SMS):
  - `Site.click_save` is the ONLY place that clicks Kaydet, and it is only ever called
    here, guarded by `SaveBudget(cfg.safety.max_save_attempts)` -- at most that many
    clicks per run.
  - Kaydet is never clicked while an SMS confirmation is pending (`_complete_sms` only
    calls `submit_sms`/`check_booked`, never `click_save` again).
  - A "failed" save outcome stops the run (return 1) rather than retrying.
  - Only a `SiteError` from `select_card` (someone else took the card) continues hunting;
    any other error falls through to the generic backoff handler.
  - `dry_run` selects, then unselects, and returns 0 -- Kaydet is never clicked.
"""
from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src import events
from src.browser import STATE, BrowserClosed, LoggedOut, SiteError, SiteUnavailable, Stopped
from src.catalog import Catalog
from src.config import Config
from src.grid import Grid, SessionCard, find_card
from src.grid import Status as CardStatus
from src.notify import alert, keep_awake, notify
from src.planner import Planner, next_window_for_display
from src.reminders import read_tennis_history, write_tennis_history
from src.reservations import last_tennis_pick
from src.targets import booked_dates_in, find_target_multi

log = logging.getLogger("ibbspor.runner")

SELECT_COOLDOWN_S = 300.0  # skip a card whose selection just failed for this long (idle)
SELECT_COOLDOWN_HOT_S = 10.0  # ... much shorter while hot, so a genuine miss is retried fast
SITE_UNAVAILABLE_WAIT_S = 30.0  # Cloudflare/maintenance: fixed, stop-aware wait before retry
RELOGIN_VERIFIED_FRESH_S = 600.0  # skip the hot-window relogin check if verified this recently
ERROR_BACKOFF_EVERY_N = 5  # emit a non-fatal events.Error every this many consecutive errors
OBSERVATIONS_PATH = STATE / "observations.jsonl"
SMS_CODE_FILE = STATE / "sms_code.txt"
CATALOG_SAVE_INTERVAL_S = 300.0
ARTIFACTS_PRUNE_EVERY_N_POLLS = 200
ARTIFACTS_PRUNE_KEEP = 500
_CODE_RE = re.compile(r"^\d{4,8}$")
_DAILY_LIMIT_TOKENS = ("günde", "aynı gün")
_SMS_SEND_FAILURE_TOKENS = ("gönderme", "başarısız")
_WRONG_CODE_TOKENS = ("kod", "hatalı", "geçersiz")


# --------------------------------------------------------------------------
# RunIO -- the GUI/CLI <-> engine contract (see analysis/final-design-2026-10.md)
# --------------------------------------------------------------------------

@dataclass
class RunIO:
    """What the caller (src/worker.py's EngineThread, or run_cli below) hands the engine.

    `stop`: every wait inside the engine is `stop.wait(...)`, so Stop is honoured promptly.
    `code_queue`: SMS codes (from the GUI's SMS box, or a CLI stdin/file reader) arrive here.
    `emit`: receives src.events dataclasses; the engine always calls it via `_emit` below,
        which never lets an exception from a misbehaving handler reach engine logic.
    `gui`: True means a Tk GUI owns this run -- the engine itself never starts the stdin
        SMS reader (that's CLI-only, see run_cli) regardless of this flag, but callers use
        it to decide whether to start one themselves.
    """

    stop: threading.Event
    code_queue: "queue.Queue[str]"
    emit: Callable[[object], None]
    gui: bool = False


def _emit(io: "RunIO", event: object) -> None:
    """io.emit(event), never letting an exception from the handler reach the engine."""
    try:
        io.emit(event)
    except Exception:
        log.exception("emit failed for event %r", event)


# --------------------------------------------------------------------------
# Observations (pure diff + jsonl side effect)
# --------------------------------------------------------------------------

def _card_key(card: SessionCard) -> tuple:
    return (card.date, card.start, card.level, card.court)


def _status_value(status) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def diff_observations(prev: Grid | None, cur: Grid) -> list[dict]:
    """Pure: status-change events between two grids. No I/O.

    Key = (date, start, level, court). On the first grid (prev is None), every card is
    reported once with old=None. Otherwise only cards whose status changed (or are new)
    are reported. Keys are exactly {ts, date, start, end, level, old, new, quota} -- see
    record_observations for how branch/facility are attached on top, without changing
    this function's return shape.
    """
    prev_status: dict[tuple, object] = {}
    if prev is not None:
        for card in prev.cards:
            prev_status[_card_key(card)] = card.status

    out: list[dict] = []
    now_iso = datetime.now().astimezone().isoformat()
    for card in cur.cards:
        key = _card_key(card)
        if prev is None:
            old = None
            changed = True
        else:
            old = prev_status.get(key)
            changed = key not in prev_status or old != card.status
        if not changed:
            continue
        out.append(
            {
                "ts": now_iso,
                "date": str(card.date),
                "start": card.start,
                "end": card.end,
                "level": card.level,
                "old": _status_value(old),
                "new": _status_value(card.status),
                "quota": card.quota,
            }
        )
    return out


def record_observations(
    prev: Grid | None,
    cur: Grid,
    path: Path = OBSERVATIONS_PATH,
    branch: str | None = None,
    facility: str | None = None,
) -> list[dict]:
    """diff_observations + append each event as a JSON line to `path`.

    `branch`/`facility`, when given, are merged into each written line (the multi-target
    engine polls several groups, so the jsonl needs to say which one each event is about)
    -- but never into the dicts this function *returns*, so diff_observations' pure
    contract (and its existing tests) stay unchanged.
    """
    events_ = diff_observations(prev, cur)
    if events_:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for event in events_:
                record = dict(event)
                if branch is not None:
                    record["branch"] = branch
                if facility is not None:
                    record["facility"] = facility
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return events_


# --------------------------------------------------------------------------
# SaveBudget -- the fix for the 14-Kaydet-clicks incident
# --------------------------------------------------------------------------

class SaveBudget:
    """Hard cap on Kaydet clicks in a single run (see incident notes on Site.click_save).

    Call take() right before every Site.click_save; if it returns False the budget is
    exhausted and click_save must NOT be called -- the caller should stop the run instead.
    """

    def __init__(self, max_attempts: int) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
        self.max_attempts = max_attempts
        self._used = 0

    def take(self) -> bool:
        """Consume one attempt if the budget allows it. Returns whether it was granted."""
        if self._used >= self.max_attempts:
            return False
        self._used += 1
        return True

    @property
    def used(self) -> int:
        return self._used


# --------------------------------------------------------------------------
# SMS code helpers
# --------------------------------------------------------------------------

def normalize_code(raw: str) -> str | None:
    """Strip whitespace; return the code if it's 4-8 digits, else None (letters/wrong length)."""
    code = re.sub(r"\s+", "", raw or "")
    return code if _CODE_RE.match(code) else None


def take_file_code(path: Path) -> str | None:
    """Read+delete `path` if present, returning its normalized code (or None if invalid).

    The file is deleted as soon as it's read (even if the content turns out invalid) so a
    stale or already-used code can never be picked up twice. Never raises: a missing file,
    a race with a concurrent delete, or unreadable content all just return None.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("take_file_code: failed to read %s", path)
        return None
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
    return normalize_code(raw)


def _sms_stdin_reader(q: "queue.Queue[str]", input_fn=input) -> None:
    """Daemon thread body: read lines from stdin, push valid 4-8 digit codes onto `q`.

    Exits quietly on EOFError -- stdin is not a TTY or has been closed (e.g. the bot is
    run in the background/detached), which is an expected, non-fatal condition. Used only
    by run_cli (the CLI shim) -- a GUI run supplies codes to the same `io.code_queue` via
    its own command channel instead, and must never start this thread.
    """
    while True:
        try:
            raw = input_fn()
        except EOFError:
            return
        except Exception:
            log.exception("sms stdin reader: unexpected error, exiting")
            return
        code = normalize_code(raw)
        if code is None:
            print("Geçersiz kod (4-8 rakam olmalı); tekrar deneyin.", flush=True)
            continue
        q.put(code)


# --------------------------------------------------------------------------
# SMS confirmation wait
# --------------------------------------------------------------------------

def _confirm_outcome(site, group: tuple[str, str], card: SessionCard, outcome, stop: threading.Event):
    """Double-check a non-booked result after the SMS code was submitted.

    The post-verification page is unknown (it may redirect away from the grid, or still be
    loading), so before reporting failure re-open the TARGET's own (branch, facility)
    group via `site.ensure_ready(*group)` (no Kaydet) and look at the card's status.
    `group` should be `site.current` as captured right before click_save -- the group the
    card actually belongs to, not whatever group the engine happens to be polling next.

    Never navigates while an SMS confirmation might still be pending (`site.sms_pending()`
    truthy, or erroring): navigating away mid-verification could abandon a confirmation
    that was still in flight.
    """
    if outcome.kind != "failed":
        return outcome
    stop.wait(2)
    try:
        if site.sms_pending():
            return outcome
    except (SiteError, PlaywrightError):
        return outcome
    try:
        grid = site.ensure_ready(*group)
    except (SiteError, PlaywrightError) as e:
        log.warning("SMS sonrası doğrulama için tablo açılamadı: %s", e)
        return outcome
    again = next(
        (c for c in grid.cards if c.date == card.date and c.start == card.start
         and c.court == card.court and c.status == CardStatus.BOOKED),
        None,
    )
    if again is not None:
        log.info("SMS sonrası tablo kontrolü: seans rezerve görünüyor")
        from src.site import SaveOutcome

        return SaveOutcome(kind="booked", message=outcome.message, card=again, dialogs=outcome.dialogs)
    return outcome


def _is_wrong_code_outcome(outcome) -> bool:
    """True if a "failed" submit_sms outcome actually looks like a wrong/expired code
    (the save didn't go through -- our card is still SELECTED -- or the message itself
    says so), as opposed to a genuine failure. See _complete_sms."""
    if outcome.card is not None and outcome.card.status == CardStatus.SELECTED:
        return True
    message = (outcome.message or "").lower()
    return any(tok in message for tok in _WRONG_CODE_TOKENS)


def _is_sms_send_failure_outcome(outcome) -> bool:
    """True if a "failed" outcome's message is the site's own "couldn't send the SMS"
    text (observed live: "SMS gönderme işlemi başarısız. Lütfen tekrar deneyiniz.")."""
    message = (outcome.message or "").lower()
    return all(tok in message for tok in _SMS_SEND_FAILURE_TOKENS)


def _complete_sms(site, group: tuple[str, str], card: SessionCard, cfg: Config, io: "RunIO"):
    """Wait for a human to supply the SMS code (GUI/CLI box, terminal stdin, or
    STATE/sms_code.txt -- e.g. relayed from a friend's phone) and resolve the result.

    Never clicks Kaydet again -- only submit_sms (for a code from io.code_queue/file) or a
    no-navigation re-parse (check_booked, for a code the human entered in the browser
    themselves). Polls roughly once a second for up to cfg.sms.timeout_s; re-notifies
    (notify + alert) every cfg.sms.alert_every_s. A wrong/expired code just keeps the wait
    going (emits SmsResult(False, ...)); the site's own "couldn't send the SMS" message
    instead stops the run with a Failed outcome (the save budget was already spent -- the
    cap stays in place, no retry). Honours io.stop throughout -- raises Stopped (never a
    "failed" outcome) so a user-requested Stop during the SMS wait is never reported as a
    booking failure. While the human may be mid-interaction with the browser, polls are
    wrapped against PlaywrightError so a page mid-navigation never crashes the wait.
    """
    from src.site import SaveOutcome

    with contextlib.suppress(OSError):
        SMS_CODE_FILE.unlink()  # a code left over from an earlier run must never be used

    with contextlib.suppress(Exception):
        site.page.bring_to_front()

    notify("SMS kodu gerekli", card.describe())
    with contextlib.suppress(Exception):
        alert(repeat=3)

    deadline = time.monotonic() + cfg.sms.timeout_s
    last_alert = time.monotonic()

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if io.stop.is_set():
            raise Stopped("SMS beklerken durduruldu")

        try:
            # Capped at `remaining` too, so a short cfg.sms.timeout_s (tests) doesn't
            # overshoot by up to a full second waiting on an empty queue.
            code = io.code_queue.get(timeout=min(1.0, max(0.01, remaining)))
        except queue.Empty:
            code = None

        if code is None:
            code = take_file_code(SMS_CODE_FILE)

        if code is not None:
            log.info("SMS kodu alındı, doğrulanıyor")
            try:
                outcome = site.submit_sms(code, card)
            except PlaywrightError:
                log.exception("submit_sms failed (page busy?); will retry")
                continue
            if outcome.kind == "sms":
                _emit(io, events.SmsResult(False, "Kod yanlış ya da süresi dolmuş görünüyor"))
                continue
            if outcome.kind == "failed":
                if _is_sms_send_failure_outcome(outcome):
                    log.warning("SMS gönderilemedi, durduruluyor: %s", outcome.message)
                    return SaveOutcome.failed(
                        "Site SMS gönderemedi. Birkaç dakika sonra Başlat'a tekrar basın.",
                        card=outcome.card,
                    )
                if _is_wrong_code_outcome(outcome):
                    _emit(io, events.SmsResult(False, "Kod hatalı, tekrar deneyin"))
                    continue
            return _confirm_outcome(site, group, card, outcome, io.stop)

        try:
            still_pending = site.sms_pending()
        except PlaywrightError:
            still_pending = True  # page mid-navigation; don't misread as resolved

        if not still_pending:
            # The human submitted in the browser (or the page is mid-navigation): let it
            # settle, and require the SMS field to still be gone before judging.
            if io.stop.wait(1.5):
                raise Stopped("SMS beklerken durduruldu")
            with contextlib.suppress(PlaywrightError, PlaywrightTimeoutError):
                site.page.wait_for_load_state("domcontentloaded", timeout=10_000)
            try:
                if site.sms_pending():
                    continue
                outcome = site.check_booked(card)
            except PlaywrightError:
                log.warning("check_booked failed while page was mid-navigation; retrying")
                continue
            if outcome.kind == "sms":
                continue  # false alarm: still on the SMS step
            return _confirm_outcome(site, group, card, outcome, io.stop)

        now = time.monotonic()
        if now - last_alert >= cfg.sms.alert_every_s:
            with contextlib.suppress(Exception):
                notify("SMS kodu gerekli", card.describe())
            with contextlib.suppress(Exception):
                alert(repeat=1)
            last_alert = now

    return SaveOutcome.failed("SMS kodu zamanında girilmedi")


def _has_credentials() -> bool:
    """Whether OS-keychain credentials are saved (src.credentials.load() never raises --
    a missing/broken keyring backend is just treated as "no credentials")."""
    from src.credentials import load as load_credentials

    try:
        return load_credentials() is not None
    except Exception:
        log.exception("_has_credentials: credentials.load() başarısız, kimlik bilgisi yok sayılıyor")
        return False


# --------------------------------------------------------------------------
# relogin-at-hot-start (idle -> hot transition)
# --------------------------------------------------------------------------

def _relogin_at_hot_start(
    site, cfg: Config, last_verified_at: float | None
) -> tuple[bool, float | None]:
    """Verify (and, only if needed, refresh) the session right as a hot window starts.

    No logout, ever: just `site.check_logged_in()`; only if that's False does it try
    `site.auto_login()` (a FAILED auto_login attempt counts toward the lockout limiter --
    a successful one never does, see Site.auto_login). Skipped entirely if the session
    was verified less than RELOGIN_VERIFIED_FRESH_S ago, and the caller only invokes this
    once per hot-window transition (on idle->hot, never again until the next one) --
    so this never runs more than once per window. Every step here is a single bounded
    Playwright call (check_logged_in/auto_login each navigate once, with the normal
    navigation timeout) -- never the old 600s manual-login wait.

    Returns (relogin_happened, new_last_verified_at) -- the caller resets its diff/group
    baseline only when relogin_happened is True.
    """
    if not cfg.session.relogin_before_open:
        return False, last_verified_at
    now_m = time.monotonic()
    if last_verified_at is not None and now_m - last_verified_at < RELOGIN_VERIFIED_FRESH_S:
        return False, last_verified_at

    log.info("Sıcak pencere başlıyor: oturum kontrol ediliyor")
    try:
        if site.check_logged_in():
            return False, time.monotonic()
    except Exception:
        log.exception("hot pencere başlangıcı: oturum kontrolü başarısız")

    try:
        if site.auto_login():
            log.info("hot pencere başlangıcı: oturum yenilendi")
            return True, time.monotonic()
        log.warning("hot pencere başlangıcı: otomatik giriş başarısız, mevcut oturumla devam")
    except Exception:
        log.exception("hot pencere başlangıcı: beklenmeyen hata, mevcut oturumla devam")
    return False, last_verified_at


# --------------------------------------------------------------------------
# booked_dates bootstrap (existing "Satış Yapıldı" reservations)
# --------------------------------------------------------------------------

def _initial_booked_dates(site, tz: ZoneInfo) -> frozenset:
    """dates() of site.open_reservations() items already sold, today or later.

    Wrapped: any failure just logs and starts with an empty set (a one-session-per-day
    dedupe that's briefly incomplete is far safer than crashing the whole run over a
    read-only listing call). A Stop request propagates so the caller can unwind promptly.
    """
    try:
        items = site.open_reservations()
    except Stopped:
        raise
    except Exception:
        log.exception("open_reservations başarısız; booked_dates boş başlıyor")
        return frozenset()
    write_tennis_history(last_tennis_pick(items))  # keeps the 66h/72h prediction fresh
    today = datetime.now(tz).date()
    return frozenset(r.date for r in items if r.status == "Satış Yapıldı" and r.date >= today)


# --------------------------------------------------------------------------
# targets-for-group helper (facility "*" matches any facility of the branch)
# --------------------------------------------------------------------------

def _targets_for_group(targets, group: tuple[str, str]) -> list:
    branch, facility = group
    return [t for t in targets if t.branch == branch and (t.facility == facility or t.facility == "*")]


# --------------------------------------------------------------------------
# The engine loop (new API)
# --------------------------------------------------------------------------

def _prune_artifacts(site) -> None:
    """Best-effort: prune state/artifacts/ via the site's Artifacts helper, if it has one
    (FakeSite stand-ins in tests may not). Never raises."""
    artifacts = getattr(site, "artifacts", None)
    if artifacts is None:
        return
    with contextlib.suppress(Exception):
        artifacts.prune(ARTIFACTS_PRUNE_KEEP)


def _status_text(plan) -> str:
    if plan.next_window is None:
        return ""
    w = plan.next_window
    return f"{w.target_name} @ {w.open_at.strftime('%d.%m %H:%M')}"


def _engine_run(
    cfg: Config,
    settings,
    site,
    io: "RunIO",
    *,
    catalog: Catalog | None,
    dry_run: bool,
    once: bool,
) -> int:
    tz = ZoneInfo(cfg.site.timezone)
    cat = catalog if catalog is not None else Catalog.load()

    try:
        with keep_awake():
            _emit(io, events.Status("starting"))

            try:
                if not site.login_if_needed(stop=io.stop):
                    _emit(io, events.LoggedOut())
                    _emit(io, events.Stopped(2))
                    return 2
            except Stopped:
                _emit(io, events.Stopped(130))
                return 130
            last_verified_at: float | None = time.monotonic()

            booked_dates = _initial_booked_dates(site, tz)
            # Ekim 2026 duyurusu (tenis 72h/66h açılış kuralı): the member's latest
            # tennis pick, written wherever reservations are fetched (just above, and
            # src.controller._on_reservations) -- read_tennis_history never raises, so a
            # missing/corrupt cache just means "predict the fixed 72h moment" (None).
            tennis_last_pick = read_tennis_history()
            if tennis_last_pick is not None:
                tennis_last_pick = tennis_last_pick.replace(tzinfo=tz)

            enabled_targets = [t for t in settings.targets if t.enabled]
            try:
                planner = Planner(enabled_targets, cat, cfg.timing, tz)
            except ValueError:
                _emit(io, events.Error("Etkin hedef bulunamadı", fatal=True))
                _emit(io, events.Stopped(1))
                return 1

            budget = SaveBudget(cfg.safety.max_save_attempts)
            any_booked = False  # at least one successful Kaydet this run (see SaveBudget note)
            # Cards whose selection just failed (taken by someone else, or the site's
            # one-session-per-day rule) are skipped for a while instead of re-clicked
            # on every poll.
            select_cooldown: dict[tuple, float] = {}
            prev_grid: Grid | None = None
            prev_window_key: tuple | None = None
            last_hot: bool | None = None
            last_catalog_save = time.monotonic()
            consecutive_errors = 0
            poll_count = 0
            # Dates for which we've already logged the week-boundary ("tabloda henüz
            # görünmüyor") message once -- never spammed every poll for the same date.
            week_boundary_logged: set = set()

            def _save_catalog() -> None:
                with contextlib.suppress(Exception):
                    cat.save()

            _prune_artifacts(site)

            try:
                while not io.stop.is_set():
                    try:
                        poll_count += 1
                        if poll_count % ARTIFACTS_PRUNE_EVERY_N_POLLS == 0:
                            _prune_artifacts(site)

                        now = datetime.now(tz)
                        try:
                            plan = planner.next(now, booked_dates)
                        except ValueError:
                            log.error("Planner.next: etkin hedef bulunamadı")
                            _emit(io, events.Error("Etkin hedef bulunamadı", fatal=True))
                            _emit(io, events.Stopped(1))
                            return 1

                        # Ekim 2026 duyurusu: the dashboard/reminder must never show a
                        # tennis session's silent 72h+66h dual hot-polling pair as "two
                        # openings" -- recompute the single predicted moment separately
                        # from `plan.next_window` (which `events.NextWindow` used to be
                        # built from directly) for display only; hot-polling itself
                        # (`plan.hot`, below) is unaffected and still covers both. Falls
                        # back to the raw `plan.next_window` if `cat` can't support a
                        # real `windows()` call (test doubles) -- the real Catalog always
                        # can.
                        try:
                            display_window = next_window_for_display(
                                enabled_targets, cat, now, cfg.timing, tz, booked_dates, tennis_last_pick
                            )
                        except Exception:
                            display_window = plan.next_window
                        if display_window is not None:
                            key = (display_window.target_name, display_window.open_at)
                            if key != prev_window_key:
                                _emit(
                                    io,
                                    events.NextWindow(
                                        display_window.target_name,
                                        display_window.group,
                                        display_window.session_start,
                                        display_window.open_at,
                                    ),
                                )
                                prev_window_key = key

                        if plan.hot != last_hot:
                            _emit(io, events.Status("hot" if plan.hot else "monitoring", _status_text(plan)))
                            if plan.hot and not last_hot:
                                relogin_happened, last_verified_at = _relogin_at_hot_start(
                                    site, cfg, last_verified_at
                                )
                                if relogin_happened:
                                    prev_grid = None
                                    _emit(io, events.AccountInfo(name=site.last_member_name))
                            last_hot = plan.hot

                        grid = site.switch_to(*plan.group)

                        if (
                            plan.hot
                            and plan.next_window is not None
                            and plan.next_window.group == plan.group
                        ):
                            session_date = plan.next_window.session_start.date()
                            if session_date not in grid.dates and session_date not in week_boundary_logged:
                                log.info(
                                    "Seans tarihi tabloda henüz görünmüyor (hafta sınırı): %s",
                                    session_date.strftime("%d.%m.%Y"),
                                )
                                week_boundary_logged.add(session_date)

                        with contextlib.suppress(Exception):
                            cat.observe(plan.group[0], plan.group[1], grid, now)
                        if time.monotonic() - last_catalog_save >= CATALOG_SAVE_INTERVAL_S:
                            _save_catalog()
                            last_catalog_save = time.monotonic()

                        record_observations(prev_grid, grid, branch=plan.group[0], facility=plan.group[1])
                        prev_grid = grid

                        booked_dates |= booked_dates_in(grid)

                        targets_for_group = _targets_for_group(enabled_targets, plan.group)
                        now_m = time.monotonic()
                        select_cooldown = {k: t for k, t in select_cooldown.items() if t > now_m}
                        candidates = replace(grid, cards=tuple(
                            c for c in grid.cards if _card_key(c) not in select_cooldown))
                        hit = find_target_multi(candidates, targets_for_group, booked_dates)

                        if hit:
                            target, card = hit
                            _emit(io, events.Found(target.name, card.describe()))
                            _emit(io, events.Status("found"))

                            try:
                                grid = site.select_card(card)
                            except BrowserClosed:
                                raise
                            except SiteError as e:
                                log.info("Seçim başarısız; ~1sn sonra tekrar kontrol ediliyor: %s", e)
                                if io.stop.wait(1.0):
                                    _emit(io, events.Stopped(130))
                                    return 130

                                if any(tok in str(e) for tok in _DAILY_LIMIT_TOKENS):
                                    booked_dates = booked_dates | {card.date}

                                again_status = None
                                reparsed = None
                                try:
                                    reparsed = site.grid()
                                    again = find_card(reparsed, card.date, card.start, card.court)
                                    again_status = again.status if again is not None else None
                                except Exception:
                                    log.exception("Seçim sonrası tablo tekrar okunamadı")

                                if again_status == CardStatus.SELECTED and reparsed is not None:
                                    log.info(
                                        "Kart aslında seçilmiş görünüyor (yarış); Kaydet'e devam ediliyor: %s",
                                        card.describe(),
                                    )
                                    grid = reparsed
                                else:
                                    if again_status in (CardStatus.FULL, CardStatus.CLOSED):
                                        log.info(
                                            "Kart dolu/kapalı görünüyor; bekleme uygulanmadan devam: %s",
                                            card.describe(),
                                        )
                                    else:
                                        cooldown = SELECT_COOLDOWN_HOT_S if plan.hot else SELECT_COOLDOWN_S
                                        select_cooldown[_card_key(card)] = time.monotonic() + cooldown
                                    _emit(
                                        io,
                                        events.Status(
                                            "hot" if plan.hot else "monitoring", _status_text(plan)
                                        ),
                                    )
                                    continue

                            if dry_run:
                                site.unselect_card(card)
                                _emit(io, events.Stopped(0, f"DRY-RUN: {card.describe()} seçildi; Kaydet'e basılmadı"))
                                return 0

                            if not budget.take():
                                if any_booked:
                                    log.info(
                                        "Kaydet sınırına ulaşıldı ama en az bir rezervasyon tamam; duruluyor."
                                    )
                                    _emit(
                                        io,
                                        events.Stopped(
                                            0, "Rezervasyon tamam; Kaydet sınırına ulaşıldı"
                                        ),
                                    )
                                    return 0
                                notify("Kaydet deneme sınırına ulaşıldı", card.describe())
                                log.error(
                                    "Kaydet deneme sınırına ulaşıldı (max_save_attempts=%d)",
                                    cfg.safety.max_save_attempts,
                                )
                                _emit(io, events.Failed("Kaydet deneme sınırına ulaşıldı"))
                                _emit(io, events.Stopped(1))
                                return 1

                            group = site.current
                            outcome = site.click_save(card)
                            if outcome.kind == "sms":
                                _emit(io, events.SmsNeeded(card.describe(), cfg.sms.timeout_s))
                                _emit(io, events.Status("sms_wait"))
                                # The bot's own Chromium window is never raised here -- the
                                # UI (web app) raises ITS OWN window on SmsNeeded instead.
                                notify("SMS kodu gerekli", card.describe())
                                with contextlib.suppress(Exception):
                                    alert(repeat=1)
                                outcome = _complete_sms(site, group, card, cfg, io)

                            if outcome.kind == "booked":
                                notify("REZERVASYON TAMAM ✅", f"{card.describe()} rezerve edildi")
                                log.info("Rezervasyon tamam: %s", card.describe())
                                any_booked = True
                                _emit(io, events.Booked(card.describe(), outcome.message))
                                _emit(io, events.Status("booked"))
                                if getattr(site, "window", None) is not None:
                                    site.window.minimize()
                                if settings.stop_after_success:
                                    _emit(io, events.Stopped(0))
                                    return 0
                                booked_dates = booked_dates | {card.date}
                                continue
                            else:
                                notify("Rezervasyon başarısız", outcome.message)
                                log.warning("Kaydetme başarısız: %s", outcome.message)
                                _emit(io, events.Failed(outcome.message))
                                _emit(io, events.Stopped(1))
                                return 1

                        if once:
                            _emit(io, events.Stopped(0))
                            return 0

                        consecutive_errors = 0
                        if io.stop.wait(plan.sleep_s):
                            _emit(io, events.Stopped(130))
                            return 130

                    except LoggedOut:
                        log.warning("Oturum kapanmış; yeniden giriş deneniyor / bekleniyor.")
                        _emit(io, events.LoggedOut())
                        has_creds = _has_credentials()

                        try:
                            if has_creds:
                                # Never fall into a manual wait-then-exit while we have
                                # credentials: keep trying auto_login forever (P0 --
                                # unattended must never exit here). A failed attempt still
                                # counts toward Site's lockout limiter; the limiter itself
                                # is what makes "forever" safe.
                                _emit(io, events.Status("monitoring", "Oturum yenileniyor…"))
                                warned_rejected = False
                                while True:
                                    if io.stop.is_set():
                                        _emit(io, events.Stopped(130))
                                        return 130
                                    if site.auto_login():
                                        break
                                    if getattr(type(site), "credentials_rejected_fp", None):
                                        # Saved credentials refused repeatedly: stop retrying
                                        # them (account lockout risk) and wait for a human --
                                        # either a manual login or newly saved credentials.
                                        if not warned_rejected:
                                            _emit(io, events.Error(
                                                "Kayıtlı TC/şifre ile giriş yapılamadı. Hesap bölümünden "
                                                "bilgileri kontrol edin ya da 'Tarayıcıda elle giriş' yapın."))
                                            warned_rejected = True
                                        if site.wait_for_login(timeout_s=120, stop=io.stop):
                                            break
                                        continue
                                    if io.stop.wait(60):
                                        _emit(io, events.Stopped(130))
                                        return 130
                            else:
                                # No saved credentials: wait for a human to log in by hand,
                                # but keep retrying after a timeout instead of giving up --
                                # emit LoggedOut again each round so the UI keeps showing it.
                                while True:
                                    ok = site.login_if_needed(stop=io.stop)
                                    if ok:
                                        break
                                    if io.stop.is_set():
                                        _emit(io, events.Stopped(130))
                                        return 130
                                    _emit(io, events.LoggedOut())
                        except Stopped:
                            _emit(io, events.Stopped(130))
                            return 130

                        last_verified_at = time.monotonic()
                        prev_grid = None
                        site.current = None
                        # Logged in again mid-run: tell the UI whose session this is.
                        _emit(io, events.AccountInfo(name=site.last_member_name))
                        continue

                    except Stopped:
                        _emit(io, events.Stopped(130))
                        return 130

                    except BrowserClosed:
                        raise  # the owner (worker/CLI) relaunches the browser; retrying here can't help

                    except SiteUnavailable as e:
                        # Cloudflare challenge / maintenance page: wait and retry. Never
                        # counts toward the consecutive-error backoff/budget below -- this
                        # is not "our" error, it's the site being temporarily unreachable.
                        log.warning("Site kullanılamıyor görünüyor: %s", e)
                        site.current = None
                        if io.stop.wait(SITE_UNAVAILABLE_WAIT_S):
                            _emit(io, events.Stopped(130))
                            return 130

                    except (SiteError, PlaywrightTimeoutError, PlaywrightError) as e:
                        log.warning("Hata: %s", e)
                        consecutive_errors += 1
                        if consecutive_errors % ERROR_BACKOFF_EVERY_N == 0:
                            _emit(
                                io,
                                events.Error(
                                    f"Siteye ulaşılamıyor, tekrar deneniyor ({consecutive_errors}. deneme)",
                                    fatal=False,
                                ),
                            )
                        backoff = min(10 * 2 ** (consecutive_errors - 1), 300)
                        log.warning("Backoff: %ds (hata #%d) -- çıkılmıyor, tekrar denenecek", backoff, consecutive_errors)
                        site.current = None
                        if io.stop.wait(backoff):
                            _emit(io, events.Stopped(130))
                            return 130

                # io.stop was set between iterations (e.g. right after a `continue`).
                _emit(io, events.Stopped(130))
                return 130
            finally:
                _save_catalog()
    except Stopped:
        _emit(io, events.Stopped(130))
        return 130


# --------------------------------------------------------------------------
# Public API: run (new, multi-arg) with a backwards-compatible CLI shim
# --------------------------------------------------------------------------

def run(
    cfg: Config,
    settings=None,
    site=None,
    io: "RunIO | None" = None,
    *,
    catalog: Catalog | None = None,
    dry_run: bool = False,
    once: bool = False,
) -> int:
    """The engine loop.

    New API (src/worker.py's EngineThread): `run(cfg, settings, site, io, catalog=...,
    dry_run=..., once=...)` -- the browser/site are already open and owned by the caller.
    Returns 0 booked/dry-run/once, 1 error/failed save, 2 logged out (couldn't re-login),
    130 stopped via io.stop.

    Backwards-compat (bot.py's `cmd_run`, unedited -- see T9): when `site`/`io` are
    omitted (bot.py calls `run(cfg, dry_run=dry_run, once=args.once)`), delegates to
    `run_cli`, which opens its own browser and builds a throwaway RunIO.
    """
    if site is None or io is None:
        return run_cli(cfg, dry_run=dry_run, once=once)
    if settings is None:
        from src.settings import load_settings

        settings = load_settings(legacy_cfg=cfg)
    return _engine_run(cfg, settings, site, io, catalog=catalog, dry_run=dry_run, once=once)


def run_cli(cfg: Config, dry_run: bool = False, once: bool = False) -> int:
    """CLI entry point (bot.py `run` subcommand): opens the browser itself, feeds SMS
    codes from stdin into a fresh RunIO, discards emitted events, and runs the engine."""
    from src.browser import Artifacts, RateLimiter, open_browser
    from src.settings import load_settings
    from src.site import Site

    settings = load_settings(legacy_cfg=cfg)
    stop = threading.Event()
    code_queue: "queue.Queue[str]" = queue.Queue()
    io = RunIO(stop=stop, code_queue=code_queue, emit=lambda e: None, gui=False)

    reader_thread = threading.Thread(
        target=_sms_stdin_reader, args=(code_queue,), daemon=True, name="sms-stdin-reader"
    )
    reader_thread.start()

    try:
        with open_browser(headless=False, minimized=False) as bs:
            limiter = RateLimiter(cfg.timing.max_requests_per_min, sleep=stop.wait)
            artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
            site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
            return run(cfg, settings, site, io, dry_run=dry_run, once=once)
    except KeyboardInterrupt:
        stop.set()
        return 130
