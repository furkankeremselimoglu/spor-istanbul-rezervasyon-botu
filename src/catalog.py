"""Catalog of branches/facilities/halls/levels/session-slots SEEN on the live site.

Feeds the GUI dropdowns (branş, tesis, salon/kort, seviye, saat). The site only ever
shows ~72 h of sessions at a time, so this accrues knowledge over many polls instead of
reflecting any single page. Everything here is pure (no network); the one bit of I/O is
JSON load/save to `state/catalog.json`, done atomically so a crash mid-write never
corrupts the file.

Names are stored and returned exactly as the site renders them (Turkish casing matters,
so nothing here ever calls `.lower()`/`.upper()` on a name).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from src.grid import Grid

log = logging.getLogger("ibbspor.catalog")

CATALOG_PATH = Path(__file__).resolve().parent.parent / "state" / "catalog.json"

# Mon=0 .. Sun=6, matching datetime.date.weekday() -- used as the weekday token in slot keys.
_WEEKDAY_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

ANY_FACILITY = "*"


@dataclass
class FacilityInfo:
    halls: dict[str, str]  # court/hall name -> last_seen ISO timestamp
    levels: dict[str, str]  # level text -> last_seen ISO timestamp
    slots: dict[str, str]  # "Wd|HH:MM|HH:MM" -> last_seen ISO timestamp
    last_scanned: str | None  # ISO timestamp of the last time a grid for this facility was observed


@dataclass
class BranchInfo:
    facilities: dict[str, FacilityInfo]
    facilities_listed_at: str | None  # ISO timestamp of the last observe_facilities() call


@dataclass
class Catalog:
    branches: dict[str, BranchInfo]
    branches_listed_at: str | None  # ISO timestamp of the last observe_branches() call

    # -- observation (never deletes anything; last_seen/listed_at only ever move forward) --

    def observe_branches(self, names: list[str], now: datetime) -> None:
        """Record that `names` is the current branch list. Old branches are kept."""
        for name in names:
            self._ensure_branch(name)
        self.branches_listed_at = now.isoformat()

    def observe_facilities(self, branch: str, names: list[str], now: datetime) -> None:
        """Record that `names` is the current facility list for `branch`. Old ones are kept."""
        info = self._ensure_branch(branch)
        for name in names:
            if name == ANY_FACILITY:
                continue
            self._ensure_facility(branch, name)
        info.facilities_listed_at = now.isoformat()

    def observe(self, branch: str, facility: str, grid: Grid, now: datetime) -> None:
        """Union-merge halls/levels/slots seen in `grid` into (branch, facility)."""
        if facility == ANY_FACILITY:
            return
        info = self._ensure_facility(branch, facility)
        now_iso = now.isoformat()
        for card in grid.cards:
            if card.court:
                info.halls[card.court] = now_iso
            if card.level:
                info.levels[card.level] = now_iso
            key = f"{_WEEKDAY_EN[card.date.weekday()]}|{card.start}|{card.end}"
            info.slots[key] = now_iso
        info.last_scanned = now_iso

    # -- accessors (page/insertion order for names; never lower()/upper() anything) --

    def branch_names(self) -> list[str]:
        return list(self.branches.keys())

    def facility_names(self, branch: str) -> list[str]:
        info = self.branches.get(branch)
        return list(info.facilities.keys()) if info else []

    def halls(self, branch: str, facility: str) -> list[str]:
        info = self._facility_or_none(branch, facility)
        return list(info.halls.keys()) if info else []

    def levels(self, branch: str, facility: str) -> list[str]:
        info = self._facility_or_none(branch, facility)
        return list(info.levels.keys()) if info else []

    def slots(self, branch: str, facility: str, weekday: int | None = None,
              seen_since: datetime | None = None) -> list[tuple[int, str, str]]:
        """Seen (weekday, start, end) tuples, sorted by (weekday, start).

        `seen_since`: only slots last seen at or after this moment (aware datetime)."""
        info = self._facility_or_none(branch, facility)
        if info is None:
            return []
        result: list[tuple[int, str, str]] = []
        for key, last_seen in info.slots.items():
            parsed = _parse_slot_key(key)
            if parsed is None:
                continue
            wd, start, end = parsed
            if weekday is not None and wd != weekday:
                continue
            if seen_since is not None and not _seen_at_or_after(last_seen, seen_since):
                continue
            result.append((wd, start, end))
        result.sort(key=lambda t: (t[0], t[1]))
        return result

    def starts(self, branch: str, facility: str, weekday: int,
               seen_since: datetime | None = None) -> list[str]:
        """Distinct session start times seen for this (branch, facility, weekday), sorted."""
        return sorted({start for _, start, _ in self.slots(branch, facility, weekday, seen_since)})

    # -- persistence --

    def to_dict(self) -> dict:
        return {
            "branches_listed_at": self.branches_listed_at,
            "branches": {
                name: {
                    "facilities_listed_at": b.facilities_listed_at,
                    "facilities": {
                        fname: {
                            "halls": dict(f.halls),
                            "levels": dict(f.levels),
                            "slots": dict(f.slots),
                            "last_scanned": f.last_scanned,
                        }
                        for fname, f in b.facilities.items()
                    },
                }
                for name, b in self.branches.items()
            },
        }

    @classmethod
    def from_dict(cls, d: object) -> "Catalog":
        """Tolerant of missing keys or outright garbage -- never raises, worst case empty."""
        if not isinstance(d, dict):
            return cls(branches={}, branches_listed_at=None)

        branches_listed_at = _clean_str_or_none(d.get("branches_listed_at"))

        branches: dict[str, BranchInfo] = {}
        raw_branches = d.get("branches")
        if isinstance(raw_branches, dict):
            for name, raw_b in raw_branches.items():
                if not isinstance(name, str) or not isinstance(raw_b, dict):
                    continue
                facilities_listed_at = _clean_str_or_none(raw_b.get("facilities_listed_at"))

                facilities: dict[str, FacilityInfo] = {}
                raw_facilities = raw_b.get("facilities")
                if isinstance(raw_facilities, dict):
                    for fname, raw_f in raw_facilities.items():
                        if not isinstance(fname, str) or not isinstance(raw_f, dict):
                            continue
                        facilities[fname] = FacilityInfo(
                            halls=_clean_str_map(raw_f.get("halls")),
                            levels=_clean_str_map(raw_f.get("levels")),
                            slots=_clean_str_map(raw_f.get("slots")),
                            last_scanned=_clean_str_or_none(raw_f.get("last_scanned")),
                        )
                branches[name] = BranchInfo(facilities=facilities, facilities_listed_at=facilities_listed_at)

        return cls(branches=branches, branches_listed_at=branches_listed_at)

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> "Catalog":
        """Load from `path`. Missing or corrupt file -> empty Catalog. Never raises."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return cls(branches={}, branches_listed_at=None)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("catalog bozuk, bos katalogla basliyor: %s", path)
            return cls(branches={}, branches_listed_at=None)
        return cls.from_dict(data)

    def save(self, path: Path = CATALOG_PATH) -> None:
        """Atomic save: write to a tmp file in the same directory, then os.replace()."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    # -- internals --

    def _ensure_branch(self, name: str) -> BranchInfo:
        info = self.branches.get(name)
        if info is None:
            info = BranchInfo(facilities={}, facilities_listed_at=None)
            self.branches[name] = info
        return info

    def _ensure_facility(self, branch: str, facility: str) -> FacilityInfo:
        branch_info = self._ensure_branch(branch)
        info = branch_info.facilities.get(facility)
        if info is None:
            info = FacilityInfo(halls={}, levels={}, slots={}, last_scanned=None)
            branch_info.facilities[facility] = info
        return info

    def _facility_or_none(self, branch: str, facility: str) -> FacilityInfo | None:
        branch_info = self.branches.get(branch)
        if branch_info is None:
            return None
        return branch_info.facilities.get(facility)


def _seen_at_or_after(last_seen: str, since: datetime) -> bool:
    try:
        seen = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return False
    if seen.tzinfo is None:  # older/naive stamps: assume the caller's zone
        seen = seen.replace(tzinfo=since.tzinfo)
    return seen >= since


def _parse_slot_key(key: str) -> tuple[int, str, str] | None:
    parts = key.split("|")
    if len(parts) != 3:
        return None
    wd_text, start, end = parts
    try:
        wd = _WEEKDAY_EN.index(wd_text)
    except ValueError:
        return None
    return wd, start, end


def _clean_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _clean_str_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def parse_options(html: str, select_id: str) -> list[str]:
    """Option texts of `<select id=select_id>`, in page order.

    Skips the empty-value entry and the "-- Seçiniz --" placeholder (or any option whose
    text contains "Seçiniz"). Text is whitespace-normalized; never case-folded.
    """
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id=select_id)
    if select is None:
        return []
    result: list[str] = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        text = " ".join(opt.get_text().split())
        if not value or "Seçiniz" in text:
            continue
        result.append(text)
    return result
