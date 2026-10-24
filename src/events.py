"""Messages between the GUI (main thread) and the engine thread (owns Playwright).

Commands flow GUI -> engine through one queue, events flow engine -> GUI through another
(see analysis/final-design-2026-10.md, "GUI ⇄ worker protocol"). Plain frozen dataclasses,
no behaviour, so both sides and the runner can import them without cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

# ---------------------------------------------------------------- commands (GUI -> engine)


@dataclass(frozen=True)
class Start:
    dry_run: bool = False          # settings are re-read from state/settings.json at start


@dataclass(frozen=True)
class Stop:
    pass


@dataclass(frozen=True)
class SmsCode:
    code: str


@dataclass(frozen=True)
class ScanCatalog:
    depth: Literal["branches", "facilities", "grids"]
    branches: tuple[str, ...] | None = None   # None = all known branches


@dataclass(frozen=True)
class CheckLogin:
    pass


@dataclass(frozen=True)
class ManualLogin:
    pass


@dataclass(frozen=True)
class ShowBrowser:
    pass


@dataclass(frozen=True)
class HideBrowser:
    pass


@dataclass(frozen=True)
class ListMemberships:
    pass


@dataclass(frozen=True)
class ListReservations:
    pass


@dataclass(frozen=True)
class CancelReservation:
    date: date
    start: str


@dataclass(frozen=True)
class Quit:
    pass


@dataclass(frozen=True)
class SwitchAccount:
    """Make `account_id` (src.credentials) the active account, then log it in: logout()
    + auto_login() on the engine's Site -- rejected while running/busy like the other
    exclusive commands. Emits LoginResult + AccountInfo."""
    account_id: str


@dataclass(frozen=True)
class ResetSession:
    """active account changed via /api/credentials; drop the live site session (logout +
    clear_cookies) so the previous member's cookies can never serve the new active
    account."""


Command = (Start | Stop | SmsCode | ScanCatalog | CheckLogin | ManualLogin | ShowBrowser
           | HideBrowser | ListMemberships | ListReservations | CancelReservation | Quit
           | SwitchAccount | ResetSession)

# ---------------------------------------------------------------- events (engine -> GUI)

EngineState = Literal["idle", "starting", "monitoring", "hot", "found", "sms_wait",
                      "booked", "stopped", "error"]


@dataclass(frozen=True)
class Status:
    state: EngineState
    text: str = ""


@dataclass(frozen=True)
class Log:
    level: str           # "INFO", "WARNING", ...
    line: str


@dataclass(frozen=True)
class NextWindow:
    target_name: str
    group: tuple[str, str]           # (branch, facility)
    session_start: datetime
    open_at: datetime


@dataclass(frozen=True)
class Found:
    target_name: str
    card_text: str


@dataclass(frozen=True)
class SmsNeeded:
    card_text: str
    deadline_s: float                # seconds the engine will wait for the code


@dataclass(frozen=True)
class SmsResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class Booked:
    card_text: str
    message: str


@dataclass(frozen=True)
class Failed:
    message: str


@dataclass(frozen=True)
class LoggedOut:
    pass


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    message: str
    attempts_left: int | None = None


@dataclass(frozen=True)
class AccountInfo:
    """The logged-in member's full name (src.site.Site.last_member_name), or None if not
    known/not logged in. Emitted after CheckLogin, ManualLogin, SwitchAccount, and at
    Start (once logged in). NEVER log the name -- see src.credentials."""
    name: str | None


@dataclass(frozen=True)
class CatalogProgress:
    done: int
    total: int
    label: str


@dataclass(frozen=True)
class CatalogUpdated:
    pass                              # GUI re-reads Catalog.load()


@dataclass(frozen=True)
class Memberships:
    items: tuple[str, ...]


@dataclass(frozen=True)
class Reservations:
    items: tuple = field(default_factory=tuple)   # tuple[src.reservations.Reservation, ...]


@dataclass(frozen=True)
class Stopped:
    exit_code: int                    # 0 ok/booked/dry-run, 1 error, 2 logged out, 130 stopped by user
    message: str = ""


@dataclass(frozen=True)
class Error:
    message: str
    fatal: bool = False


Event = (Status | Log | NextWindow | Found | SmsNeeded | SmsResult | Booked | Failed | LoggedOut
         | LoginResult | AccountInfo | CatalogProgress | CatalogUpdated | Memberships | Reservations
         | Stopped | Error)
