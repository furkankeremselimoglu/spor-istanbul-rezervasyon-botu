"""Multi-target model (src/targets.py, pure -- no I/O).

Replaces the old single-preference-list model (src.config.Pref/TargetsConfig) with a list
of `Target`s, each describing its own branch/facility/hall/level/day/time window. Priority
is simply list order: `find_target_multi` walks targets in order and returns the first one
with any open, matching card.

See analysis/final-design-2026-10.md, D4.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from src.config import WEEKDAYS
from src.grid import Grid, SessionCard, Status

TENNIS_BRANCH_PREFIX = "TENİS"  # "AYAK TENİSİ" doesn't start with this -- not tennis
_SEVIYE_RE = re.compile(r"Seviye\s*-?\s*(\d+)")

TR_DAY: dict[str, str] = {
    "Mon": "Pazartesi",
    "Tue": "Salı",
    "Wed": "Çarşamba",
    "Thu": "Perşembe",
    "Fri": "Cuma",
    "Sat": "Cumartesi",
    "Sun": "Pazar",
}


def new_target_id() -> str:
    """A short, practically-unique id for a new Target (settings.json "id" field)."""
    return uuid.uuid4().hex[:8]


def _short_facility(facility: str) -> str:
    if facility == "*":
        return "tüm tesisler"
    if len(facility) <= 15:
        return facility
    words = facility.split()
    return " ".join(words[:2]) + "…"


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    enabled: bool
    branch: str
    facility: str  # "*" = any facility of branch
    halls: tuple[str, ...]  # empty = any
    level_prefix: str  # "" = any
    days: tuple[int, ...]  # 0=Mon..6=Sun; empty is invalid (see src.settings.validate)
    time_from: str | None  # "HH:MM" inclusive bound on session START; None = unbounded
    time_to: str | None
    starts: tuple[str, ...]  # non-empty overrides the time_from/time_to range

    def describe(self) -> str:
        """Turkish one-liner, e.g. "TENİS · İBB RECEP… · Cuma 18:00–21:00 · tüm salonlar"."""
        if self.days:
            days_str = ", ".join(TR_DAY[WEEKDAYS[d]] for d in sorted(self.days))
        else:
            days_str = "?"

        if self.starts:
            time_str = ", ".join(self.starts)
        elif self.time_from and self.time_to:
            time_str = f"{self.time_from}–{self.time_to}"
        elif self.time_from:
            time_str = f"{self.time_from}+"
        elif self.time_to:
            time_str = f"–{self.time_to}"
        else:
            time_str = "her saat"

        halls_str = "tüm salonlar" if not self.halls else ", ".join(self.halls)
        facility_str = _short_facility(self.facility)

        return f"{self.branch} · {facility_str} · {days_str} {time_str} · {halls_str}"


def start_allowed(target: Target, start: str) -> bool:
    """Whether `start` ("HH:MM") satisfies target's starts/time_from/time_to.

    `starts`, when non-empty, overrides the range entirely (exact membership). Otherwise
    time_from/time_to are inclusive bounds on the session start; either (or both) may be
    None, meaning unbounded on that side. HH:MM strings compare correctly lexicographically
    since they're always zero-padded.
    """
    if target.starts:
        return start in target.starts
    if target.time_from is not None and start < target.time_from:
        return False
    if target.time_to is not None and start > target.time_to:
        return False
    return True


def is_tennis_branch(branch: str) -> bool:
    """Whether `branch` is under the Ekim 2026 72h/66h open-rule change (prefix match,
    never .lower()/.upper() -- Turkish İ/ı casing). "AYAK TENİSİ" does not start with
    "TENİS" so it's correctly excluded."""
    return branch.startswith(TENNIS_BRANCH_PREFIX)


def tennis_seviye_number(level_prefix: str) -> int | None:
    """The Seviye number named in `level_prefix` (e.g. 2 for "TENİS Seviye-2 (...)" or
    "TENİS Seviye 2"), or None if no "Seviye" number appears at all (bare "TENİS"/"" --
    matches any level, including Seviye-2 cards). The exact Seviye-2/3 label text isn't
    documented (only "TENİS Seviye-1" and "...Seviye-1 (Başlangıç Seviyesi Oyuncu
    Eğitimi)" are confirmed), so this matches the number tolerantly rather than an exact
    string. Plain regex, never .lower()/.upper() (Turkish İ/ı casing)."""
    m = _SEVIYE_RE.search(level_prefix)
    return int(m.group(1)) if m else None


def tennis_hot_dual_window(target: Target) -> bool:
    """Whether `target` gets the SILENT 72h+66h hot-polling safety net (see
    src.planner.windows): every tennis target except ones whose level_prefix explicitly
    names a Seviye other than 2. A level_prefix naming no Seviye at all (bare "TENİS"/"")
    still doubles -- it can still match a Seviye-2 card."""
    if not is_tennis_branch(target.branch):
        return False
    n = tennis_seviye_number(target.level_prefix)
    return n is None or n == 2


def tennis_seviye2(target: Target) -> bool:
    """Whether `target`'s level_prefix explicitly names Seviye-2 -- the open rule's
    USER-VISIBLE prediction (reminder/dashboard, see src.planner.expected_tennis_offset_hours)
    only applies there; every other target (tennis or not) always shows/reminds at the
    fixed, earlier 72h moment."""
    return is_tennis_branch(target.branch) and tennis_seviye_number(target.level_prefix) == 2


def matches(target: Target, card: SessionCard, booked_dates: frozenset[date] = frozenset()) -> bool:
    """Whether `card` is a live, open candidate for `target`.

    Branch/facility are not checked here -- the grid passed in is already scoped to a
    (branch, facility) group (see `groups` / the planner), so a card carries no branch or
    facility of its own. Never use .lower()/.upper() when comparing Turkish text (İ/ı).
    """
    if card.status != Status.OPEN:
        return False
    if card.checkbox_id is None:
        return False
    if card.date.weekday() not in target.days:
        return False
    if not start_allowed(target, card.start):
        return False
    if target.halls and card.court not in target.halls:
        return False
    if not card.level.startswith(target.level_prefix):
        return False
    if card.date in booked_dates:
        return False
    return True


def find_target_multi(
    grid: Grid, targets: Iterable[Target], booked_dates: frozenset[date] = frozenset()
) -> tuple[Target, SessionCard] | None:
    """First enabled target (priority = list order) with any matching card; among that
    target's matches, the earliest by (date, start). Disabled targets are skipped entirely.
    """
    for target in targets:
        if not target.enabled:
            continue
        candidates = [c for c in grid.cards if matches(target, c, booked_dates)]
        if candidates:
            best = min(candidates, key=lambda c: (c.date, c.start))
            return target, best
    return None


def booked_dates_in(grid: Grid) -> set[date]:
    """Dates of BOOKED cards visible in `grid` (one-session-per-day bookkeeping)."""
    return {c.date for c in grid.cards if c.status == Status.BOOKED}


def groups(targets: Iterable[Target]) -> list[tuple[str, str]]:
    """Distinct (branch, facility) pairs of enabled targets, first-seen (priority) order."""
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for t in targets:
        if not t.enabled:
            continue
        key = (t.branch, t.facility)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
