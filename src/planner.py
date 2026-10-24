"""Multi-group planner (src/planner.py, pure -- no I/O, no sleeping, no Playwright).

Decides, for each tick of the engine's main loop, which (branch, facility) "group" to
poll next and how long to sleep afterwards. Groups come from `src.targets.Target`s
(with `facility == "*"` expanded to every facility of that branch the catalog has ever
seen); timing comes from `src.config.TimingConfig`. `now` is always injected by the
caller, so this module is fully deterministic given its inputs -- no wall clock, no
`time.sleep`. All datetimes are timezone-aware (`zoneinfo`).

See analysis/final-design-2026-10.md, D6, and the Engine pseudocode (`runner.run`),
which calls `Planner.next(now, booked_dates)` once per loop iteration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from src.catalog import ANY_FACILITY, Catalog
from src.config import TimingConfig
from src.targets import Target, start_allowed, tennis_hot_dual_window, tennis_seviye2

Group = tuple[str, str]  # (branch, facility) -- facility is always concrete here, never "*"


@dataclass(frozen=True)
class Window:
    """One upcoming reservation-opening event: `target_name`'s session at `group`
    starting at `session_start`, whose booking window opens at `open_at`.
    """

    target_name: str
    group: Group
    session_start: datetime  # aware, in tz
    open_at: datetime  # aware, in tz; session_start - open_offset_hours


@dataclass(frozen=True)
class Plan:
    """What `Planner.next()` decided for this tick of the engine loop."""

    group: Group  # which (branch, facility) to poll now
    sleep_s: float  # how long to sleep (stop.wait(sleep_s)) before the next tick
    hot: bool  # True if `group` was chosen from the hot-group set
    next_window: Window | None  # earliest upcoming window overall, for the GUI status line
    hot_groups: tuple[Group, ...]  # all currently-hot groups, best-priority-first


def candidate_starts(target: Target, catalog: Catalog, weekday: int) -> list[str]:
    """Candidate session start times ("HH:MM") for `target` on `weekday` (0=Mon..6=Sun).

    Three tiers, in order:
    1. `target.starts`, if non-empty -- an explicit list overrides everything else.
    2. Otherwise, the UNION of catalog-observed starts for `target`'s facility on this
       weekday AND an hourly fallback "HH:00" from time_from (default "07:00") to
       time_to (default "23:00") inclusive -- the catalog is never allowed to *suppress*
       the fallback (a facility the catalog scanned incompletely must not hide an
       otherwise-valid hourly candidate), then both are filtered to `target`'s
       [time_from, time_to] range (inclusive; None = open on that side).

    `target.facility == "*"` means "any facility of this branch": the catalog part of
    the union covers every facility the catalog knows for `target.branch` (there is no
    single facility to look up); the hourly fallback doesn't depend on facility at all.
    """
    if target.starts:
        return list(target.starts)

    facilities = _target_facilities(target, catalog)
    seen: set[str] = set()
    for facility in facilities:
        seen.update(catalog.starts(target.branch, facility, weekday))
    seen.update(_hourly_fallback(target.time_from, target.time_to))
    return sorted(s for s in seen if start_allowed(target, s))


def windows(
    targets: Iterable[Target],
    catalog: Catalog,
    now: datetime,
    timing: TimingConfig,
    tz: ZoneInfo,
    horizon_days: int = 8,
) -> list[Window]:
    """Upcoming windows for every enabled target, within `horizon_days` of `now`.

    For each enabled target, each calendar day in [now.date(), now.date()+horizon_days]
    whose weekday is one of `target.days`, each candidate start on that day becomes a
    session_start (kept only if strictly after `now`); `open_at = session_start -
    open_offset_hours`. A window is dropped once its open-and-hot period is entirely in
    the past (`open_at + hot_after_s < now`).

    Ekim 2026 duyurusu: a tennis target that qualifies for the dual safety net
    (`src.targets.tennis_hot_dual_window` -- every tennis target except ones whose
    level_prefix explicitly names a Seviye other than 2) gets ONE Window per entry of
    `timing.tennis_open_offsets_hours` (72h AND 66h by default) instead of a single one at
    `open_offset_hours` -- the real rule is unknowable per-member, so the engine stays hot
    at both moments silently; `src.planner.expected_tennis_offset_hours` / `next_window_for_display`
    pick the single predicted one for reminders/dashboard display.

    A `facility == "*"` target produces one Window per concrete facility of its branch
    (the catalog's `facility_names`) for every surviving (day, start) -- "multiplying"
    into several groups, as noted in the design's risk list. A branch the catalog has
    never scanned yields no facilities and so no windows for that target.

    Result is sorted by `open_at` ascending, ties broken by target priority (list order
    in `targets`, lower index = higher priority = sorts first).
    """
    hot_after = timedelta(seconds=timing.hot_after_s)
    today = now.date()

    entries: list[tuple[Window, int]] = []
    for priority, target in enumerate(targets):
        if not target.enabled or not target.days:
            continue
        facilities = _target_facilities(target, catalog)
        if not facilities:
            continue
        offsets = (
            timing.tennis_open_offsets_hours
            if tennis_hot_dual_window(target)
            else (timing.open_offset_hours,)
        )

        for day_offset in range(horizon_days + 1):
            day = today + timedelta(days=day_offset)
            if day.weekday() not in target.days:
                continue

            for start in candidate_starts(target, catalog, day.weekday()):
                hh, mm = map(int, start.split(":"))
                session_start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                if session_start <= now:
                    continue
                for offset_hours in offsets:
                    open_at = session_start - timedelta(hours=offset_hours)
                    if open_at + hot_after < now:
                        continue
                    for facility in facilities:
                        entries.append(
                            (
                                Window(
                                    target_name=target.name,
                                    group=(target.branch, facility),
                                    session_start=session_start,
                                    open_at=open_at,
                                ),
                                priority,
                            )
                        )
                        # Week boundary (analysis/open-rule-2026-10/README.md): the grid
                        # only ever shows the current calendar week (Mon-Sun), and whether
                        # next week's sessions become visible before Monday 00:00 is
                        # UNKNOWN. If this open_at falls in an earlier ISO week than the
                        # session itself, add a SECOND window right at that Monday 00:00
                        # (the week flip) so the engine is also hot there, in case that's
                        # when the grid actually exposes it.
                        if session_start.isocalendar()[:2] != open_at.isocalendar()[:2]:
                            monday = session_start.date() - timedelta(days=session_start.weekday())
                            week_flip_open_at = datetime(
                                monday.year, monday.month, monday.day, 0, 0, tzinfo=tz
                            )
                            if week_flip_open_at > now:
                                entries.append(
                                    (
                                        Window(
                                            target_name=target.name,
                                            group=(target.branch, facility),
                                            session_start=session_start,
                                            open_at=week_flip_open_at,
                                        ),
                                        priority,
                                    )
                                )

    entries.sort(key=lambda e: (e[0].open_at, e[1]))
    return [w for w, _ in entries]


def expand_groups(targets: Iterable[Target], catalog: Catalog) -> list[Group]:
    """Distinct (branch, facility) groups of enabled targets, in priority (first-seen) order.

    `facility == "*"` expands to every facility the catalog has seen for that branch;
    a target whose branch the catalog has never scanned contributes no group at all
    (nothing to log -- it simply has no candidates yet).
    """
    seen: set[Group] = set()
    result: list[Group] = []
    for target in targets:
        if not target.enabled:
            continue
        for facility in _target_facilities(target, catalog):
            key = (target.branch, facility)
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result


class Planner:
    """Round-robin scheduler across groups. Pure, no I/O, no sleeping.

    Keeps two small cursors (hot / idle) across calls to `next()` so repeated calls --
    the engine's main loop -- visit every group fairly instead of always picking the
    same one. `now` is always injected; nothing here reads the wall clock.
    """

    # How far ahead `next()` looks for windows. More than one week on purpose: if this
    # week's occurrence of a target's weekday has already gone stale (its hot window
    # is in the past), the following week's occurrence must still be found so `next()`
    # keeps returning a sensible next_window/hot_groups instead of going quiet.
    _HORIZON_DAYS = 15

    def __init__(self, targets: list[Target], catalog: Catalog, timing: TimingConfig, tz: ZoneInfo) -> None:
        self.targets = list(targets)
        self.catalog = catalog
        self.timing = timing
        self.tz = tz
        if not expand_groups(self.targets, self.catalog):
            # Fail fast at construction (not just inside next()) so a caller's one-time
            # `try/except ValueError` around `Planner(...)` actually catches this -- see
            # runner.py's _engine_run, which also wraps the per-tick `next()` call as a
            # defense-in-depth safety net (item 11).
            raise ValueError("etkin hedef yok")
        self._hot_cursor = 0
        self._idle_cursor = 0

    def next(self, now: datetime, booked_dates: frozenset[date] = frozenset()) -> Plan:
        groups = expand_groups(self.targets, self.catalog)
        if not groups:
            raise ValueError("etkin hedef yok")

        all_windows = windows(
            self.targets, self.catalog, now, self.timing, self.tz, horizon_days=self._HORIZON_DAYS
        )
        live = [w for w in all_windows if w.session_start.date() not in booked_dates]
        next_window = live[0] if live else None

        priority: dict[str, int] = {}
        for i, t in enumerate(self.targets):
            priority.setdefault(t.name, i)
        fallback_priority = len(self.targets)

        hot_before = timedelta(seconds=self.timing.hot_before_s)
        hot_after = timedelta(seconds=self.timing.hot_after_s)

        hot_best: dict[Group, int] = {}
        for w in live:
            if w.open_at - hot_before <= now <= w.open_at + hot_after:
                p = priority.get(w.target_name, fallback_priority)
                if w.group not in hot_best or p < hot_best[w.group]:
                    hot_best[w.group] = p

        if hot_best:
            group_order = {g: i for i, g in enumerate(groups)}
            hot_groups = sorted(
                hot_best, key=lambda g: (hot_best[g], group_order.get(g, len(groups)))
            )
            n = len(hot_groups)
            chosen = hot_groups[self._hot_cursor % n]
            self._hot_cursor += 1
            # Each group's refresh costs 2 requests (Site.refresh() deselects, then
            # re-selects, the facility -- see its docstring on same-value postbacks not
            # re-querying the grid), so the rate math must count 2 per group, not 1.
            sleep_s = max(self.timing.hot_poll_s, n * 2 * 60.0 / self.timing.max_requests_per_min)
            return Plan(
                group=chosen,
                sleep_s=sleep_s,
                hot=True,
                next_window=next_window,
                hot_groups=tuple(hot_groups),
            )

        # Idle: cycle through ALL groups, nearest upcoming open_at first, then priority.
        best_open: dict[Group, datetime] = {}
        best_priority: dict[Group, int] = {}
        for w in live:
            p = priority.get(w.target_name, fallback_priority)
            if w.group not in best_open or w.open_at < best_open[w.group]:
                best_open[w.group] = w.open_at
            if w.group not in best_priority or p < best_priority[w.group]:
                best_priority[w.group] = p

        never = now + timedelta(days=3650)  # groups with no live window sort last
        ordered = sorted(
            groups, key=lambda g: (best_open.get(g, never), best_priority.get(g, fallback_priority))
        )
        n = len(ordered)
        chosen = ordered[self._idle_cursor % n]
        self._idle_cursor += 1

        # A full round-robin cycle takes ~idle_poll_s (so a cancellation at any one
        # group is caught within ~idle_poll_s), while never exceeding the site's rate
        # cap, and never sleeping less than 5s regardless of how many groups there are.
        base = max(self.timing.idle_poll_s / n, 60.0 / self.timing.max_requests_per_min, 5.0)

        upcoming_hot_starts = [w.open_at - hot_before - now for w in live]
        upcoming_hot_starts = [d for d in upcoming_hot_starts if d.total_seconds() > 0]
        if upcoming_hot_starts:
            base = min(base, min(d.total_seconds() for d in upcoming_hot_starts))

        sleep_s = max(0.2, base)
        return Plan(group=chosen, sleep_s=sleep_s, hot=False, next_window=next_window, hot_groups=())


# -- Ekim 2026 duyurusu: predicted single display moment -------------------------------


def expected_tennis_offset_hours(
    last_tennis_session_start: datetime | None, open_at_66: datetime
) -> int:
    """Which of the two tennis hot windows (72h or 66h) the open rule is EXPECTED to use
    this time, for display only (reminders/dashboard) -- `windows()`/`Planner` stay hot at
    BOTH regardless (see `tennis_hot_dual_window`), since this is only a guess.

    Ekim 2026 duyurusu: picking >=1 session within 7 days of the member's last picked
    Seviye-2 tennis session moves them to 66h; 36 picks since the rule began makes 66h
    permanent. Neither "7 days from the session date vs. the moment it was picked" nor
    the lifetime pick count is visible to the bot, so this only approximates the first
    condition -- counting from the session's date (not the pick time, which is unknown)
    -- and never the permanent tier (unknowable without site support). A wrong guess only
    mistimes the reminder/display; polling (`windows()`) still covers both moments.
    """
    if last_tennis_session_start is None:
        return 72
    if open_at_66 - last_tennis_session_start <= timedelta(days=7):
        return 66
    return 72


def next_window_for_display(
    targets: Iterable[Target],
    catalog: Catalog,
    now: datetime,
    timing: TimingConfig,
    tz: ZoneInfo,
    booked_dates: frozenset[date] = frozenset(),
    tennis_last_pick: datetime | None = None,
    horizon_days: int = Planner._HORIZON_DAYS,
) -> Window | None:
    """Earliest live Window, EXCEPT a target with the silent 72h+66h dual safety net
    (`tennis_hot_dual_window`) only ever contributes its single predicted moment here:
    Seviye-2 (`tennis_seviye2`) -> `expected_tennis_offset_hours`; every other dual-window
    target -> the fixed, larger (72h-ish) offset. The member must never be shown/reminded
    of "two openings" for one session -- the pair is a polling-only detail.
    """
    targets = list(targets)
    live = [
        w for w in windows(targets, catalog, now, timing, tz, horizon_days=horizon_days)
        if w.session_start.date() not in booked_dates
    ]
    dual_names = {t.name for t in targets if tennis_hot_dual_window(t)}
    seviye2_names = {t.name for t in targets if tennis_seviye2(t)}

    def _is_display_window(w: Window) -> bool:
        if w.target_name not in dual_names:
            return True
        if w.target_name in seviye2_names:
            open_at_66 = w.session_start - timedelta(hours=66)
            predicted = expected_tennis_offset_hours(tennis_last_pick, open_at_66)
        else:
            predicted = max(timing.tennis_open_offsets_hours)
        return w.open_at == w.session_start - timedelta(hours=predicted)

    display = [w for w in live if _is_display_window(w)]
    return display[0] if display else None


# -- internals -------------------------------------------------------------------------


def _target_facilities(target: Target, catalog: Catalog) -> list[str]:
    if target.facility == ANY_FACILITY:
        return catalog.facility_names(target.branch)
    return [target.facility]


def _hourly_fallback(time_from: str | None, time_to: str | None) -> list[str]:
    lo_h = int((time_from or "07:00").split(":")[0])
    hi_h = int((time_to or "23:00").split(":")[0])
    if hi_h < lo_h:
        return []
    return [f"{h:02d}:00" for h in range(lo_h, hi_h + 1)]
