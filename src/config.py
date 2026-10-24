"""config.toml -> frozen dataclasses, with validation."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(ValueError):
    """Invalid or missing config value. Message names the offending field."""


@dataclass(frozen=True)
class SiteConfig:
    base_url: str
    timezone: str
    # Optional since D3 (state/settings.json / src.targets.Target carry these per-target
    # now); kept here only so an old config.toml (pre-GUI) can still be read for migration.
    membership_text: str | None = None
    branch_text: str | None = None
    facility_text: str | None = None


@dataclass(frozen=True)
class Pref:
    weekday: int  # 0=Mon .. 6=Sun (datetime.weekday())
    start: str    # "HH:MM"

    def label(self) -> str:
        return f"{WEEKDAYS[self.weekday]} {self.start}"


@dataclass(frozen=True)
class TargetsConfig:
    """Legacy single-preference-list config. Optional as of D3/D4: superseded by
    state/settings.json + src.targets.Target, kept only to migrate an old config.toml.
    """

    prefs: tuple[Pref, ...] = ()
    level_prefix: str = ""


@dataclass(frozen=True)
class TimingConfig:
    open_offset_hours: float
    hot_before_s: float
    hot_after_s: float
    idle_poll_s: float
    hot_poll_s: float
    max_requests_per_min: int
    # Ekim 2026 duyurusu (Seviye-2 tenis): bir seans hem 72h hem 66h öncesinde açılabilir
    # (hangisi olduğu önceden bilinemez) -- src.planner.windows() tenis/Seviye-2 hedefler
    # için HER İKİ an için de sessizce sıcak yoklama penceresi açar; bildirim/ekran tahmini
    # tek anı gösterir (src.planner.expected_tennis_offset_hours).
    tennis_open_offsets_hours: tuple[float, ...] = (72.0, 66.0)


@dataclass(frozen=True)
class SafetyConfig:
    dry_run: bool
    stop_after_success: bool
    screenshot_every_step: bool
    max_save_attempts: int  # hard cap on Kaydet clicks per run; each click can send an SMS


@dataclass(frozen=True)
class SmsConfig:
    timeout_s: float    # how long to wait for the human to supply the SMS code
    alert_every_s: float  # repeat the alert sound this often while waiting


@dataclass(frozen=True)
class SessionConfig:
    # Force a fresh login (logout + auto_login) right as the hot polling window starts,
    # when credentials are saved (src/credentials.py). See runner.run().
    relogin_before_open: bool


@dataclass(frozen=True)
class Config:
    site: SiteConfig
    targets: TargetsConfig
    timing: TimingConfig
    safety: SafetyConfig
    sms: SmsConfig
    session: SessionConfig

    @property
    def has_legacy_targets(self) -> bool:
        """True if this config still carries the old single-target-list fields.

        Used by src.settings.load_settings: when state/settings.json doesn't exist yet,
        a config.toml with these fields filled in is migrated into one Target per pref.
        """
        return bool(self.site.branch_text and self.site.facility_text and self.targets.prefs)


def _get(section: dict, name: str, typ, field: str):
    if name not in section:
        raise ConfigError(f"missing field: {field}")
    val = section[name]
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if not isinstance(val, typ) or (typ is not bool and isinstance(val, bool)):
        raise ConfigError(f"{field}: expected {typ.__name__}, got {val!r}")
    if typ is str and not val.strip():
        raise ConfigError(f"{field}: must not be empty")
    return val


def _positive(val: float, field: str) -> float:
    if val <= 0:
        raise ConfigError(f"{field}: must be > 0, got {val!r}")
    return val


def _get_optional(section: dict, name: str, typ, field: str, default):
    """Like _get, but returns `default` when the field (or the whole section) is absent.

    Lets old config.toml files without [sms] or safety.max_save_attempts keep loading.
    """
    if name not in section:
        return default
    val = section[name]
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if not isinstance(val, typ) or (typ is not bool and isinstance(val, bool)):
        raise ConfigError(f"{field}: expected {typ.__name__}, got {val!r}")
    return val


def parse_config(data: dict) -> Config:
    def section(name: str) -> dict:
        s = data.get(name)
        if not isinstance(s, dict):
            raise ConfigError(f"missing section: [{name}]")
        return s

    s = section("site")
    site = SiteConfig(
        base_url=_get(s, "base_url", str, "site.base_url").rstrip("/"),
        timezone=_get(s, "timezone", str, "site.timezone"),
        membership_text=_get_optional(s, "membership_text", str, "site.membership_text", None),
        branch_text=_get_optional(s, "branch_text", str, "site.branch_text", None),
        facility_text=_get_optional(s, "facility_text", str, "site.facility_text", None),
    )
    try:
        ZoneInfo(site.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"site.timezone: unknown zone {site.timezone!r}") from e

    t = data.get("targets", {})
    if not isinstance(t, dict):
        raise ConfigError("targets: expected a table [targets]")
    raw_prefs = t.get("prefs", [])
    if not isinstance(raw_prefs, list):
        raise ConfigError("targets.prefs: must be a list")
    prefs = []
    for i, p in enumerate(raw_prefs):
        field = f"targets.prefs[{i}]"
        if not isinstance(p, dict):
            raise ConfigError(f"{field}: expected table {{weekday, start}}")
        wd = _get(p, "weekday", str, f"{field}.weekday")
        if wd not in WEEKDAYS:
            raise ConfigError(f"{field}.weekday: must be one of {', '.join(WEEKDAYS)}, got {wd!r}")
        start = _get(p, "start", str, f"{field}.start")
        if not _HHMM.match(start):
            raise ConfigError(f"{field}.start: must be HH:MM, got {start!r}")
        prefs.append(Pref(weekday=WEEKDAYS.index(wd), start=start))
    level_prefix = t.get("level_prefix", "")  # "" = any level (FİTNESS cards have no level text)
    if not isinstance(level_prefix, str):
        raise ConfigError(f"targets.level_prefix: expected str, got {level_prefix!r}")
    targets = TargetsConfig(prefs=tuple(prefs), level_prefix=level_prefix)

    tm = section("timing")
    raw_tennis_offsets = tm.get("tennis_open_offsets_hours")
    if raw_tennis_offsets is None:
        tennis_open_offsets_hours = (72.0, 66.0)
    else:
        if not isinstance(raw_tennis_offsets, list) or not raw_tennis_offsets:
            raise ConfigError(
                f"timing.tennis_open_offsets_hours: expected a non-empty list, got {raw_tennis_offsets!r}"
            )
        tennis_open_offsets_hours = tuple(dict.fromkeys(
            _positive(float(v), "timing.tennis_open_offsets_hours") for v in raw_tennis_offsets
        ))

    timing = TimingConfig(
        open_offset_hours=_positive(_get(tm, "open_offset_hours", float, "timing.open_offset_hours"), "timing.open_offset_hours"),
        hot_before_s=_positive(_get(tm, "hot_before_s", float, "timing.hot_before_s"), "timing.hot_before_s"),
        hot_after_s=_positive(_get(tm, "hot_after_s", float, "timing.hot_after_s"), "timing.hot_after_s"),
        idle_poll_s=_positive(_get(tm, "idle_poll_s", float, "timing.idle_poll_s"), "timing.idle_poll_s"),
        hot_poll_s=_positive(_get(tm, "hot_poll_s", float, "timing.hot_poll_s"), "timing.hot_poll_s"),
        max_requests_per_min=int(_positive(_get(tm, "max_requests_per_min", int, "timing.max_requests_per_min"), "timing.max_requests_per_min")),
        tennis_open_offsets_hours=tennis_open_offsets_hours,
    )

    sf = section("safety")
    max_save_attempts = _get_optional(sf, "max_save_attempts", int, "safety.max_save_attempts", 1)
    if max_save_attempts < 1:
        raise ConfigError(f"safety.max_save_attempts: must be >= 1, got {max_save_attempts!r}")
    safety = SafetyConfig(
        dry_run=_get(sf, "dry_run", bool, "safety.dry_run"),
        stop_after_success=_get(sf, "stop_after_success", bool, "safety.stop_after_success"),
        screenshot_every_step=_get(sf, "screenshot_every_step", bool, "safety.screenshot_every_step"),
        max_save_attempts=max_save_attempts,
    )

    sm = data.get("sms", {})
    if not isinstance(sm, dict):
        raise ConfigError("sms: expected a table [sms]")
    sms = SmsConfig(
        timeout_s=_positive(
            _get_optional(sm, "timeout_s", float, "sms.timeout_s", 300.0), "sms.timeout_s"
        ),
        alert_every_s=_positive(
            _get_optional(sm, "alert_every_s", float, "sms.alert_every_s", 15.0), "sms.alert_every_s"
        ),
    )

    se = data.get("session", {})
    if not isinstance(se, dict):
        raise ConfigError("session: expected a table [session]")
    session = SessionConfig(
        relogin_before_open=_get_optional(
            se, "relogin_before_open", bool, "session.relogin_before_open", True
        ),
    )

    return Config(site=site, targets=targets, timing=timing, safety=safety, sms=sms, session=session)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    return parse_config(data)
