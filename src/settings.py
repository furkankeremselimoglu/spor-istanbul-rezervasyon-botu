"""state/settings.json <-> dataclasses: the GUI-owned settings (membership text, targets,
dry_run, stop_after_success). See analysis/final-design-2026-10.md, D3 and the
"settings.json" section.

config.toml (src/config.py) keeps tunables only (timing/safety/sms/session + base_url);
this module owns everything the GUI itself writes. Kept pure except load/save, which do
plain, explicit-encoding file I/O -- no Playwright, no network.

Cross-platform (Windows + macOS): pathlib throughout, UTF-8 explicit (Turkish text),
atomic save via a temp file + os.replace (never leaves a half-written settings.json).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.config import Config, WEEKDAYS
from src.targets import Target, new_target_id

log = logging.getLogger("ibbspor.settings")

# src.browser.STATE is NOT importable here (browser.py is being edited concurrently by
# another task) -- derive the same path independently: <project root>/state.
STATE = Path(__file__).resolve().parent.parent / "state"
SETTINGS_PATH = STATE / "settings.json"

CURRENT_VERSION = 1

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    """Invalid settings.json content. Message names the offending field, in Turkish."""


@dataclass(frozen=True)
class Settings:
    version: int
    membership_text: str
    dry_run: bool
    stop_after_success: bool
    targets: tuple[Target, ...]
    reminders_enabled: bool = True


# --------------------------------------------------------------------------
# dict <-> dataclass
# --------------------------------------------------------------------------

def _target_from_dict(d: dict) -> Target:
    days_raw = d.get("days") or []
    days = tuple(WEEKDAYS.index(w) for w in days_raw if w in WEEKDAYS)
    halls_raw = d.get("halls") or []
    starts_raw = d.get("starts") or []
    return Target(
        id=d.get("id") or new_target_id(),
        name=d.get("name") or "",
        enabled=bool(d.get("enabled", True)),
        branch=d.get("branch") or "",
        facility=d.get("facility") or "",
        halls=tuple(halls_raw),
        level_prefix=d.get("level_prefix") or "",
        days=days,
        time_from=d.get("time_from") or None,
        time_to=d.get("time_to") or None,
        starts=tuple(starts_raw),
    )


def _target_to_dict(t: Target) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "enabled": t.enabled,
        "branch": t.branch,
        "facility": t.facility,
        "halls": list(t.halls),
        "level_prefix": t.level_prefix,
        "days": [WEEKDAYS[d] for d in t.days],
        "time_from": t.time_from,
        "time_to": t.time_to,
        "starts": list(t.starts),
    }


def settings_from_dict(d: dict) -> Settings:
    """Parse a settings.json-shaped dict. Unknown top-level/target keys are tolerated."""
    targets_raw = d.get("targets") or []
    targets = tuple(_target_from_dict(t) for t in targets_raw)
    return Settings(
        version=int(d.get("version", CURRENT_VERSION)),
        membership_text=d.get("membership_text") or "",
        dry_run=bool(d.get("dry_run", False)),
        stop_after_success=bool(d.get("stop_after_success", True)),
        targets=targets,
        reminders_enabled=bool(d.get("reminders_enabled", True)),
    )


def settings_to_dict(s: Settings) -> dict:
    """JSON-ready dict, exact shape from the design doc (days as "Mon".. strings, lists)."""
    return {
        "version": s.version,
        "membership_text": s.membership_text,
        "dry_run": s.dry_run,
        "stop_after_success": s.stop_after_success,
        "targets": [_target_to_dict(t) for t in s.targets],
        "reminders_enabled": s.reminders_enabled,
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _target_label(i: int, t: Target) -> str:
    return f"Hedef {i} ({t.name or t.id or '?'})"


def validate(s: Settings) -> None:
    """Raise SettingsError (Turkish, names the field) on any invalid target.

    Checks: non-empty name/branch/facility, non-empty days (valid weekday ints),
    HH:MM formats for time_from/time_to/starts, time_from <= time_to when both are set,
    non-empty unique ids.
    """
    seen_ids: set[str] = set()
    for i, t in enumerate(s.targets, start=1):
        label = _target_label(i, t)

        if not t.id:
            raise SettingsError(f"{label}: 'id' boş olamaz")
        if t.id in seen_ids:
            raise SettingsError(f"{label}: 'id' tekrarlı: {t.id!r}")
        seen_ids.add(t.id)

        if not t.name.strip():
            raise SettingsError(f"{label}: 'name' boş olamaz")
        if not t.branch.strip():
            raise SettingsError(f"{label}: 'branch' boş olamaz")
        if not t.facility.strip():
            raise SettingsError(f"{label}: 'facility' boş olamaz")

        if not t.days:
            raise SettingsError(f"{label}: 'days' boş olamaz")
        for wd in t.days:
            if not isinstance(wd, int) or wd < 0 or wd > 6:
                raise SettingsError(f"{label}: 'days' geçersiz gün değeri: {wd!r}")

        if t.time_from is not None and not _HHMM.match(t.time_from):
            raise SettingsError(f"{label}: 'time_from' HH:MM biçiminde olmalı, geldi: {t.time_from!r}")
        if t.time_to is not None and not _HHMM.match(t.time_to):
            raise SettingsError(f"{label}: 'time_to' HH:MM biçiminde olmalı, geldi: {t.time_to!r}")
        if t.time_from is not None and t.time_to is not None and t.time_from > t.time_to:
            raise SettingsError(
                f"{label}: 'time_from' 'time_to' değerinden sonra olamaz ({t.time_from} > {t.time_to})"
            )
        for st in t.starts:
            if not _HHMM.match(st):
                raise SettingsError(f"{label}: 'starts' içinde geçersiz saat: {st!r}")


# --------------------------------------------------------------------------
# Migration (old config.toml single-target-list -> one Target per Pref)
# --------------------------------------------------------------------------

def _migrate(cfg: Config) -> Settings:
    """One Target per old Pref (never a cross-product of days x starts): name "Fri 20:00",
    days=(weekday,), starts=(start,), branch/facility/level_prefix shared from [site]/
    [targets].
    """
    targets = []
    for pref in cfg.targets.prefs:
        wd_name = WEEKDAYS[pref.weekday]
        targets.append(
            Target(
                id=new_target_id(),
                name=f"{wd_name} {pref.start}",
                enabled=True,
                branch=cfg.site.branch_text or "",
                facility=cfg.site.facility_text or "",
                halls=(),
                level_prefix=cfg.targets.level_prefix,
                days=(pref.weekday,),
                time_from=None,
                time_to=None,
                starts=(pref.start,),
            )
        )
    return Settings(
        version=CURRENT_VERSION,
        membership_text=cfg.site.membership_text or "",
        dry_run=cfg.safety.dry_run,
        stop_after_success=cfg.safety.stop_after_success,
        targets=tuple(targets),
    )


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------

def load_settings(path: Path = SETTINGS_PATH, legacy_cfg: Config | None = None) -> Settings:
    """Load state/settings.json; if it doesn't exist, migrate an old config.toml's single
    target list (when `legacy_cfg.has_legacy_targets`) or fall back to empty settings
    (no targets -- first GUI run before the user has configured anything).
    """
    path = Path(path)

    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise SettingsError(f"settings dosyası okunamadı: {path}: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SettingsError(f"settings dosyası geçersiz JSON: {path}: {e}") from e
        if not isinstance(data, dict):
            raise SettingsError(f"settings dosyası: üst seviye bir nesne (obje) olmalı: {path}")
        settings = settings_from_dict(data)
        validate(settings)
        return settings

    if legacy_cfg is not None and legacy_cfg.has_legacy_targets:
        settings = _migrate(legacy_cfg)
        validate(settings)
        save_settings(settings, path)
        log.info(
            "config.toml'daki eski hedef listesi state/settings.json'a taşındı (%d hedef)",
            len(settings.targets),
        )
        return settings

    return Settings(version=CURRENT_VERSION, membership_text="", dry_run=False,
                     stop_after_success=True, targets=())


def save_settings(s: Settings, path: Path = SETTINGS_PATH) -> None:
    """Atomic UTF-8 JSON save: write to a sibling temp file, then os.replace over `path`.

    os.replace is atomic on both Windows and macOS/POSIX and (unlike os.rename on
    Windows) overwrites an existing destination, so a reader never observes a partially
    written file, and a failure anywhere before the final replace leaves the previous
    settings.json (if any) untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(settings_to_dict(s), ensure_ascii=False, indent=2)

    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
