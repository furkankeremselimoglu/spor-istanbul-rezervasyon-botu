"""Pure GUI view-model: turns src.events.Event history into a ViewState, and turns user
intents into src.events Commands. No Tk import -- unit-testable without a display.

See analysis/final-design-2026-10.md, "GUI ⇄ worker protocol". `src/gui.py` (T8) is the
only thing that imports Tk; it owns a `Controller`, calls its intent methods from widget
callbacks, feeds every event pumped off the engine's queue to `handle()`, and renders the
returned `ViewState`.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from src.events import (
    Booked,
    CancelReservation,
    CatalogProgress,
    CatalogUpdated,
    CheckLogin,
    Error,
    Failed,
    Found,
    HideBrowser,
    ListMemberships,
    ListReservations,
    Log,
    LoggedOut,
    LoginResult,
    ManualLogin,
    Memberships,
    NextWindow,
    Reservations,
    ScanCatalog,
    ShowBrowser,
    SmsCode,
    SmsNeeded,
    SmsResult,
    Start,
    Status,
    Stop,
    Stopped,
)

_SMS_CODE_RE = re.compile(r"^\d{4,8}$")

_WD_TR_SHORT = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")

# Canonical Turkish status text per engine_state; Status.text overrides this when non-empty
# (e.g. a specific "İptal edildi: ..." message), so this is only the fallback/default.
_STATUS_TEXT: dict[str, str] = {
    "idle": "Hazır",
    "starting": "Başlatılıyor",
    "monitoring": "İzleniyor",
    "hot": "Açılış penceresi — hızlı kontrol",
    "found": "Seans bulundu, seçiliyor…",
    "sms_wait": "SMS kodu bekleniyor",
    "booked": "Rezervasyon tamam ✅",
    "stopped": "Durduruldu",
    "error": "Hata",
}

# engine_state values that mean "a Start run is actively in progress".
_RUNNING_STATES = frozenset({"starting", "monitoring", "hot", "found", "sms_wait", "booked"})
_NOT_RUNNING_STATES = frozenset({"idle", "stopped", "error"})


def _fmt_dt(dt) -> str:
    return f"{_WD_TR_SHORT[dt.weekday()]} {dt:%d.%m %H:%M}"


@dataclass
class ViewState:
    engine_state: str = "idle"
    status_text: str = "Hazır"
    next_window_text: str = ""
    running: bool = False
    busy: bool = False                         # a non-run op (scan/login/list/cancel) is in progress
    sms_visible: bool = False
    sms_card: str = ""
    sms_deadline: float | None = None
    banner: str | None = None
    banner_kind: str | None = None              # "success" | "error" | None
    log_lines: "deque[str]" = field(default_factory=lambda: deque(maxlen=2000))
    catalog_progress: tuple[int, int, str] | None = None
    login_text: str = ""
    reservations: tuple = ()
    memberships: tuple[str, ...] = ()
    can_start: bool = True
    can_stop: bool = False


class Controller:
    """One instance per GUI session. `send` is normally `EngineThread.send`."""

    def __init__(self, send: Callable[[object], None], clock: Callable[[], float] = time.time) -> None:
        self._send = send
        self._clock = clock
        self.state = ViewState()

    # ---------------------------------------------------------------- event -> state

    def handle(self, event: object) -> ViewState:
        if isinstance(event, Status):
            self._on_status(event)
        elif isinstance(event, Log):
            self._on_log(event)
        elif isinstance(event, NextWindow):
            self._on_next_window(event)
        elif isinstance(event, Found):
            self._on_found(event)
        elif isinstance(event, SmsNeeded):
            self._on_sms_needed(event)
        elif isinstance(event, SmsResult):
            self._on_sms_result(event)
        elif isinstance(event, Booked):
            self._on_booked(event)
        elif isinstance(event, Failed):
            self._on_failed(event)
        elif isinstance(event, LoggedOut):
            self._on_logged_out()
        elif isinstance(event, LoginResult):
            self._on_login_result(event)
        elif isinstance(event, CatalogProgress):
            self._on_catalog_progress(event)
        elif isinstance(event, CatalogUpdated):
            self._on_catalog_updated()
        elif isinstance(event, Memberships):
            self._on_memberships(event)
        elif isinstance(event, Reservations):
            self._on_reservations(event)
        elif isinstance(event, Stopped):
            self._on_stopped(event)
        elif isinstance(event, Error):
            self._on_error(event)
        else:
            self._append_log(f"bilinmeyen olay: {event!r}")
        self._recompute_flags()
        return self.state

    def _on_status(self, event: Status) -> None:
        self.state.engine_state = event.state
        self.state.status_text = event.text or _STATUS_TEXT.get(event.state, event.state)
        if event.state in _RUNNING_STATES:
            self.state.running = True
        elif event.state in _NOT_RUNNING_STATES:
            self.state.running = False

    def _on_log(self, event: Log) -> None:
        self._append_log(f"{event.level}: {event.line}")

    def _on_next_window(self, event: NextWindow) -> None:
        self.state.next_window_text = (
            f"Sıradaki açılış: {event.target_name} · {_fmt_dt(event.open_at)} "
            f"(seans {_fmt_dt(event.session_start)})"
        )

    def _on_found(self, event: Found) -> None:
        self.state.engine_state = "found"
        self.state.status_text = _STATUS_TEXT["found"]
        self._append_log(f"Bulundu: {event.target_name} · {event.card_text}")

    def _on_sms_needed(self, event: SmsNeeded) -> None:
        self.state.sms_visible = True
        self.state.sms_card = event.card_text
        self.state.sms_deadline = self._clock() + event.deadline_s
        self.state.engine_state = "sms_wait"
        self.state.status_text = _STATUS_TEXT["sms_wait"]

    def _on_sms_result(self, event: SmsResult) -> None:
        self._append_log(f"SMS sonucu: {event.message}")

    def _on_booked(self, event: Booked) -> None:
        self.state.sms_visible = False
        self.state.sms_card = ""
        self.state.sms_deadline = None
        self.state.engine_state = "booked"
        self.state.status_text = _STATUS_TEXT["booked"]
        self.state.banner = event.message or f"Rezervasyon tamam: {event.card_text}"
        self.state.banner_kind = "success"

    def _on_failed(self, event: Failed) -> None:
        self.state.sms_visible = False
        self.state.sms_card = ""
        self.state.sms_deadline = None
        self.state.banner = event.message
        self.state.banner_kind = "error"

    def _on_logged_out(self) -> None:
        self.state.login_text = "Oturum kapandı; yeniden giriş gerekiyor."
        self._append_log("Oturum kapandı.")

    def _on_login_result(self, event: LoginResult) -> None:
        self.state.busy = False
        text = event.message
        if not event.ok and event.attempts_left is not None:
            text = f"{text} (kalan deneme: {event.attempts_left})"
        self.state.login_text = text

    def _on_catalog_progress(self, event: CatalogProgress) -> None:
        self.state.catalog_progress = (event.done, event.total, event.label)

    def _on_catalog_updated(self) -> None:
        self.state.busy = False
        self.state.catalog_progress = None
        self._append_log("Katalog güncellendi.")

    def _on_memberships(self, event: Memberships) -> None:
        self.state.busy = False
        self.state.memberships = tuple(event.items)

    def _on_reservations(self, event: Reservations) -> None:
        self.state.busy = False
        self.state.reservations = tuple(event.items)
        # Ekim 2026 duyurusu (tenis 72h/66h açılış kuralı): reservations are already
        # fetched here (list/cancel, both GUIs) -- persist the member's most recent
        # tennis pick so remind.pyw, which never touches the network, can predict the
        # moment too (src.planner.expected_tennis_offset_hours). Never raises.
        from src.reminders import write_tennis_history
        from src.reservations import last_tennis_pick

        write_tennis_history(last_tennis_pick(event.items))

    def _on_stopped(self, event: Stopped) -> None:
        self.state.running = False
        self.state.sms_visible = False
        self.state.sms_card = ""
        self.state.sms_deadline = None
        if event.exit_code == 0:
            # Booked/dry-run/clean stop. If Booked already set a success banner, leave it
            # (and the "booked" status) alone instead of overwriting it with "Hazır".
            if self.state.banner_kind != "success":
                self.state.engine_state = "idle"
                self.state.status_text = _STATUS_TEXT["idle"]
        elif event.exit_code == 130:
            self.state.engine_state = "stopped"
            self.state.status_text = _STATUS_TEXT["stopped"]
        elif event.exit_code == 2:
            self.state.engine_state = "error"
            self.state.status_text = "Oturum kapalı"
            self.state.banner = event.message or "Oturum kapandı"
            self.state.banner_kind = "error"
        else:
            self.state.engine_state = "error"
            self.state.status_text = _STATUS_TEXT["error"]
            if event.message:
                self.state.banner = event.message
                self.state.banner_kind = "error"

    def _on_error(self, event: Error) -> None:
        self.state.banner = event.message
        self.state.banner_kind = "error"
        # Whatever op this Error ends (a Start/Scan/login/list/cancel attempt that failed,
        # or the rejection of a command sent while another was busy) is no longer pending.
        self.state.busy = False
        if event.fatal:
            self.state.running = False
            self.state.sms_visible = False
            self.state.sms_card = ""
            self.state.sms_deadline = None

    def _append_log(self, line: str) -> None:
        self.state.log_lines.append(line)

    def _recompute_flags(self) -> None:
        self.state.can_start = not self.state.running and not self.state.busy
        self.state.can_stop = self.state.running or self.state.busy

    # ---------------------------------------------------------------- user intents -> commands

    def start(self, dry_run: bool) -> None:
        self._send(Start(dry_run=dry_run))
        self.state.running = True
        self.state.banner = None
        self.state.banner_kind = None
        self.state.sms_visible = False
        self.state.sms_card = ""
        self.state.sms_deadline = None
        self.state.engine_state = "starting"
        self.state.status_text = _STATUS_TEXT["starting"]
        self._recompute_flags()

    def stop(self) -> None:
        self._send(Stop())

    def submit_sms(self, code: str) -> str | None:
        """Validate+send an SMS code; returns a Turkish error message, or None if sent."""
        cleaned = re.sub(r"\s+", "", code or "")
        if not _SMS_CODE_RE.match(cleaned):
            return "Kod 4-8 rakam olmalı."
        self._send(SmsCode(code=cleaned))
        return None

    def scan(self, depth: str, branches=None) -> None:
        self._send(ScanCatalog(depth=depth, branches=tuple(branches) if branches else None))
        self.state.busy = True
        self.state.catalog_progress = None
        self._recompute_flags()

    def check_login(self) -> None:
        self._send(CheckLogin())
        self.state.busy = True
        self._recompute_flags()

    def manual_login(self) -> None:
        self._send(ManualLogin())
        self.state.busy = True
        self.state.login_text = "Elle giriş bekleniyor… tarayıcı penceresinde oturum açın."
        self._recompute_flags()

    def show_browser(self) -> None:
        self._send(ShowBrowser())

    def hide_browser(self) -> None:
        self._send(HideBrowser())

    def list_reservations(self) -> None:
        self._send(ListReservations())
        self.state.busy = True
        self._recompute_flags()

    def cancel(self, date: date, start: str) -> None:
        self._send(CancelReservation(date=date, start=start))
        self.state.busy = True
        self._recompute_flags()

    def list_memberships(self) -> None:
        self._send(ListMemberships())
        self.state.busy = True
        self._recompute_flags()

    # ---------------------------------------------------------------- misc

    def target_summary(self, settings) -> list[str]:
        """Turkish one-liners for the GUI's target list (Target.describe())."""
        return [t.describe() for t in settings.targets]
