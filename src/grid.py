"""Parse the /uyeseanssecim ("Uye Seans Secim") grid page into structured data.

Turns the ASP.NET-rendered HTML into a `Grid` of `SessionCard`s so the rest of
the bot can reason about which sessions are open, closed, full, etc. without
touching markup directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from src.config import Pref


class Status(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    FULL = "full"
    SELECTED = "selected"
    BOOKED = "booked"
    UNKNOWN = "unknown"


COLOR_STATUS: dict[str, Status] = {
    "#08f51a": Status.OPEN,
    "#808080": Status.CLOSED,
    "#e81f69": Status.FULL,
    "#e8fd2f": Status.SELECTED,
    "#3ed1ff": Status.BOOKED,
}

_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
_TIME_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")
_COLOR_RE = re.compile(r"border-color\s*:\s*(#[0-9a-fA-F]{6})")
_SAFE_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*")
_WEEKDAY_TR = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


@dataclass(frozen=True)
class SessionCard:
    card_id: str
    date: date
    start: str
    end: str
    level: str
    court: str
    status: Status
    color: str
    quota: str | None
    checkbox_id: str | None
    checkbox_checked: bool

    def start_dt(self, tz) -> datetime:
        hh, mm = self.start.split(":")
        return datetime.combine(self.date, time(int(hh), int(mm)), tzinfo=tz)

    def describe(self) -> str:
        parts = [
            f"{_WEEKDAY_TR[self.date.weekday()]} {self.date:%d.%m}",
            f"{self.start}-{self.end}",
            f"[{self.status.value}]",
            self.level,
        ]
        if self.court:
            parts.append(f"({self.court})")
        parts.append(f"kontenjan={self.quota if self.quota is not None else '?'}")
        return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class Grid:
    dates: tuple[date, ...]
    cards: tuple[SessionCard, ...]
    branch_selected: str | None
    facility_selected: str | None
    has_confirm: bool
    has_save: bool


def _parse_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None


def _find_date_headings(root: Tag) -> list[tuple[Tag, date]]:
    """Date-bearing panel-title tags within root; falls back to panel-heading."""
    titles = [t for t in root.find_all(True, class_=True) if "panel-title" in (t.get("class") or [])]
    tags = titles if titles else [t for t in root.find_all(True, class_=True) if "panel-heading" in (t.get("class") or [])]
    result = []
    for t in tags:
        d = _parse_date(t.get_text())
        if d:
            result.append((t, d))
    return result


def _resolve_card_date(card: Tag, order: dict[int, int], global_headings: list[tuple[Tag, date]]) -> date | None:
    for ancestor in card.parents:
        if not isinstance(ancestor, Tag):
            continue
        found = {d for _, d in _find_date_headings(ancestor)}
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            break  # ancestor spans multiple days; fall back to document order
    idx = order.get(id(card))
    if idx is None:
        return None
    best: tuple[int, date] | None = None
    for tag, d in global_headings:
        i = order.get(id(tag))
        if i is not None and i < idx and (best is None or i > best[0]):
            best = (i, d)
    return best[1] if best else None


def _parse_card(card: Tag, order: dict[int, int], global_headings: list[tuple[Tag, date]]) -> SessionCard | None:
    d = _resolve_card_date(card, order, global_headings)
    if d is None:
        return None

    time_tag = card.find(id=re.compile(r"lblSeansSaat", re.I))
    time_text = time_tag.get_text() if time_tag else card.get_text(" ")
    m = _TIME_RE.search(time_text)
    if not m:
        return None
    start, end = m.group(1), m.group(2)

    level_tag = card.find(id=re.compile(r"lblSeviye", re.I))
    level = " ".join(level_tag.get_text().split()) if level_tag else ""

    court = ""
    for lbl in card.find_all("label"):
        classes = lbl.get("class") or []
        if "cinsiyet" in classes:
            continue
        text = lbl.get_text(strip=True)
        if text:
            court = text
            break

    style = card.get("style", "") or ""
    cm = _COLOR_RE.search(style)
    color = cm.group(1).lower() if cm else ""
    status = COLOR_STATUS.get(color, Status.UNKNOWN)

    # "Kalan Kontenjan" badge: green label-success when seats remain, grey label-default at 0
    quota_tag = card.find(attrs={"title": "Kalan Kontenjan"}) or card.find(
        class_=lambda c: c and "label-default" in c
    )
    quota = quota_tag.get_text(strip=True) if quota_tag else None

    cb = card.find("input", attrs={"type": re.compile("^checkbox$", re.I)})
    raw_id = cb.get("id") if cb else None
    checkbox_id = raw_id if raw_id and _SAFE_ID_RE.fullmatch(raw_id) else None
    checkbox_checked = bool(cb and cb.has_attr("checked"))

    return SessionCard(
        card_id=card.get("id", "") or "",
        date=d,
        start=start,
        end=end,
        level=level,
        court=court,
        status=status,
        color=color,
        quota=quota,
        checkbox_id=checkbox_id,
        checkbox_checked=checkbox_checked,
    )


def _selected_option_text(soup: BeautifulSoup, select_id: str) -> str | None:
    select = soup.find(id=select_id)
    if not select:
        return None
    opt = select.find("option", selected=True)
    if not opt:
        return None
    value = (opt.get("value") or "").strip()
    text = " ".join(opt.get_text().split())
    if not value or not text:
        return None
    return text


def parse_grid(html: str) -> Grid:
    soup = BeautifulSoup(html, "html.parser")

    order: dict[int, int] = {}
    for i, el in enumerate(soup.descendants):
        order[id(el)] = i

    global_headings = _find_date_headings(soup)
    global_headings.sort(key=lambda t: order.get(id(t[0]), 0))

    seen: dict[date, None] = {}
    for _, d in global_headings:
        seen.setdefault(d, None)
    dates = tuple(seen.keys())

    cards: list[SessionCard] = []
    for card in soup.find_all(id=re.compile(r"dvSeans")):
        parsed = _parse_card(card, order, global_headings)
        if parsed is not None:
            cards.append(parsed)

    return Grid(
        dates=dates,
        cards=tuple(cards),
        branch_selected=_selected_option_text(soup, "ddlBransFiltre"),
        facility_selected=_selected_option_text(soup, "ddlTesisFiltre"),
        has_confirm=soup.find(id="pageContent_cboxOnay") is not None,
        has_save=soup.find(id="lbtnKaydet") is not None,
    )


def find_card(grid: Grid, date_: date, start: str, court: str | None = None) -> SessionCard | None:
    for c in grid.cards:
        if c.date == date_ and c.start == start and (court is None or c.court == court):
            return c
    return None


def find_target(grid: Grid, prefs: Iterable[Pref], level_prefix: str) -> tuple[Pref, SessionCard] | None:
    for pref in prefs:
        candidates = [
            c
            for c in grid.cards
            if c.status == Status.OPEN
            and c.date.weekday() == pref.weekday
            and c.start == pref.start
            and c.level.startswith(level_prefix)
            and c.checkbox_id is not None
        ]
        if candidates:
            best = min(candidates, key=lambda c: c.date)
            return pref, best
    return None


def is_selection_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.find(id="ddlBransFiltre") is not None
