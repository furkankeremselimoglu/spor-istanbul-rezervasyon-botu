"""Parse the member's existing reservations from Seanslarım's "detay" view (pure, no I/O).

On /uyespor, the membership row's lbtnDetayGoster postback expands a child table:
    <tr><td>2 - 25.09.2026 Cuma</td><td>16:00 - 19:00</td><td>İBB RECEP TÜRKOĞLU SPOR KOMPLEKSİ</td>
        <td>FİTNESS KADIN</td><td>[<i class="fa fa-check-circle">] (attended)</td><td>Satış Yapıldı</td>
        <td><a id="pageContent_rptListe_rptChild_0_lbtnSecimIptal_1" href="javascript:__doPostBack(...)">İptal Et</a></td></tr>
Only rows that can still be cancelled carry the İptal Et link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup

_DATE_CELL = re.compile(r"^\s*\d+\s*-\s*(\d{2}\.\d{2}\.\d{4})")
_TIME_CELL = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")
_SAFE_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*")


@dataclass(frozen=True)
class Reservation:
    date: date
    start: str       # "HH:MM"
    end: str
    facility: str
    hall: str
    attended: bool
    status: str      # e.g. "Satış Yapıldı"
    cancel_id: str | None  # id of the İptal Et link, None if not cancellable

    def describe(self) -> str:
        cancel = "iptal edilebilir" if self.cancel_id else "iptal edilemez"
        return (f"{self.date.strftime('%d.%m.%Y')} {self.start}-{self.end} {self.facility} / "
                f"{self.hall} — {self.status} ({cancel})")


def parse_reservations(html: str) -> list[Reservation]:
    """All reservation rows found on the page, in page order. Unparseable rows are skipped."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[Reservation] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            continue
        m = _DATE_CELL.match(tds[0].get_text(" ", strip=True))
        t = _TIME_CELL.search(tds[1].get_text(" ", strip=True))
        if not m or not t:
            continue
        link = tr.find("a", id=re.compile("lbtnSecimIptal"))
        raw_cancel_id = link.get("id") if link else None
        cancel_id = raw_cancel_id if raw_cancel_id and _SAFE_ID_RE.fullmatch(raw_cancel_id) else None
        out.append(Reservation(
            date=datetime.strptime(m.group(1), "%d.%m.%Y").date(),
            start=t.group(1),
            end=t.group(2),
            facility=tds[2].get_text(" ", strip=True),
            hall=tds[3].get_text(" ", strip=True),
            attended=tds[4].find("i") is not None,
            status=tds[5].get_text(" ", strip=True),
            cancel_id=cancel_id,
        ))
    return out


def find_reservation(items: list[Reservation], day: date, start: str) -> Reservation | None:
    return next((r for r in items if r.date == day and r.start == start), None)


def last_tennis_pick(items: Iterable) -> datetime | None:
    """Most recent tennis session (by date+start) among `items`, matched on `hall` (the
    Seanslarım 4th column, Salon Adı -- e.g. "KAPALI TENİS KORTU" -- never .lower()/
    .upper() -- Turkish İ/ı casing); any status counts as a prior pick (attended-past or
    upcoming are both prior picks). Used by the Ekim 2026 72h/66h open-rule prediction
    (src.planner.expected_tennis_offset_hours). Naive datetime (site-local, like
    `Reservation.date`/`start` -- no tz attached here). Defensive against non-Reservation
    items -- never raises."""
    best: datetime | None = None
    for r in items:
        hall = getattr(r, "hall", None)
        if not isinstance(hall, str) or "TENİS" not in hall or "AYAK TENİSİ" in hall:
            continue
        try:
            hh, mm = map(int, r.start.split(":"))
            dt = datetime(r.date.year, r.date.month, r.date.day, hh, mm)
        except (AttributeError, ValueError):
            continue
        if best is None or dt > best:
            best = dt
    return best
