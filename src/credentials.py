"""OS keychain storage for site login credentials (TC Kimlik No + şifre), multi-account.

Secrets stay ONLY in the OS keychain (service name below); `state/accounts.json` (see
`ACCOUNTS_PATH`) holds no secret at all -- just id/name/masked-TC metadata plus which
account is active. Thin wrapper over the `keyring` library (Windows Kimlik Bilgisi
Yöneticisi / macOS Anahtar Zinciri / Linux Secret Service -- whatever backend `keyring`
finds); each account gets its own pair of keychain usernames, `"<id>:tc"` / `"<id>:password"`.

A legacy single-account install (usernames `"tc"` / `"password"`) is migrated into one
account, lazily, the first time any function in this module runs -- see `_migrate_legacy`.
Migration is idempotent: the legacy keychain entries are deleted only once the new ones
are safely written, so a crash mid-migration never loses the credentials, and once the
legacy entries are gone there is nothing left to migrate on later calls.

Safety rules, load-bearing for the rest of the bot:
    - The password is NEVER logged, anywhere, under any log level.
    - The TC/pasaport no is only ever logged masked (see `mask_tc`).
    - The member's full NAME (stored per-account in accounts.json) is never logged either.
    - `load()` never raises -- a missing/broken keychain backend, or a corrupt/missing
      accounts.json, is treated the same as "no credentials saved" (returns None), so
      callers can always fall back to manual login without a try/except of their own.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import keyring
import keyring.errors

SERVICE_NAME = "ibbspor-rezervasyon-bot"
_TC_USERNAME = "tc"              # legacy single-account keychain username
_PASSWORD_USERNAME = "password"  # legacy single-account keychain username

# state/accounts.json -- derived the same way src/settings.py derives SETTINGS_PATH (a
# module attribute, not a function, so tests can monkeypatch it to tmp_path).
STATE = Path(__file__).resolve().parent.parent / "state"
ACCOUNTS_PATH = STATE / "accounts.json"
ACCOUNTS_VERSION = 1

_TC_11_DIGITS = re.compile(r"^\d{11}$")
_TC_ALNUM_5_20 = re.compile(r"^[A-Za-z0-9]{5,20}$")

log = logging.getLogger("ibbspor.credentials")


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    tc_masked: str
    active: bool


def mask_tc(tc: str) -> str:
    """Mask a TC/pasaport no for logging, e.g. "12*******89" (first 2 + stars + last 2)."""
    if len(tc) <= 4:
        return "*" * len(tc)
    return f"{tc[:2]}{'*' * (len(tc) - 4)}{tc[-2:]}"


def validate_tc(tc: str) -> None:
    """Raise ValueError if `tc` isn't a plausible TC Kimlik No / pasaport no.

    The standard case is exactly 11 digits. Passport numbers are also accepted (5-20
    alphanumeric characters), but logged with a warning since it's not the expected form.
    """
    if not isinstance(tc, str) or not tc:
        raise ValueError("TC/pasaport no boş olamaz")
    if _TC_11_DIGITS.match(tc):
        return
    if _TC_ALNUM_5_20.match(tc):
        log.warning(
            "TC kimlik no 11 haneli değil (girilen: %s); pasaport no olarak kabul ediliyor",
            mask_tc(tc),
        )
        return
    raise ValueError("TC/pasaport no 5-20 alfanümerik karakter olmalı (TC için 11 haneli rakam)")


# --------------------------------------------------------------------------
# low-level keyring access -- never raises NoKeyringError, logs once if it happens
# --------------------------------------------------------------------------

def _try_get_password(username: str) -> str | None:
    """keyring.get_password, with a missing backend treated as "nothing stored"."""
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except keyring.errors.NoKeyringError:
        log.warning(
            "kullanılabilir bir işletim sistemi anahtarlığı bulunamadı; "
            "otomatik giriş devre dışı (elle giriş kullanılacak)"
        )
        return None
    except Exception:
        log.exception("kayıtlı kimlik bilgileri okunamadı; elle giriş kullanılacak")
        return None


def _delete_password(username: str) -> None:
    """keyring.delete_password, tolerant of "nothing stored" and a missing backend."""
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing was stored under this username
    except keyring.errors.NoKeyringError:
        pass
    except Exception:
        log.exception("kayıtlı kimlik bilgisi silinemedi")


def _forget_browser_session() -> None:
    """Drop the persisted browser session (state/cookies.json) when the active account
    changes, so the previous member's cookies can never serve the new active account --
    once they're gone, auto_login() has to actually authenticate as whoever is active now.
    Duplicates the path src.browser.COOKIES_FILE derives (not imported -- this module
    must stay importable without pulling in Playwright). Best-effort: a missing file is
    a no-op, and any other OSError is silently ignored."""
    cookies_file = STATE / "cookies.json"
    with contextlib.suppress(OSError):
        cookies_file.unlink(missing_ok=True)


def _new_account_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_credentials(account_id: str) -> tuple[str, str] | None:
    tc = _try_get_password(f"{account_id}:tc")
    password = _try_get_password(f"{account_id}:password")
    if not tc or not password:
        return None
    return tc, password


# --------------------------------------------------------------------------
# state/accounts.json -- tolerant load, atomic+owner-only save
# --------------------------------------------------------------------------

def _empty_accounts_data() -> dict:
    return {"version": ACCOUNTS_VERSION, "active": None, "accounts": []}


def _load_accounts_file() -> dict:
    """Tolerant load: missing file -> empty data; corrupt/unreadable -> empty data + a
    warning (never raises)."""
    path = ACCOUNTS_PATH
    if not path.exists():
        return _empty_accounts_data()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("hesaplar dosyası okunamadı veya bozuk (%s): %s; boş liste ile devam ediliyor",
                    path, e)
        return _empty_accounts_data()
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        log.warning("hesaplar dosyası beklenmeyen biçimde (%s); boş liste ile devam ediliyor", path)
        return _empty_accounts_data()
    data.setdefault("active", None)
    data.setdefault("version", ACCOUNTS_VERSION)
    return data


def _save_accounts_file(data: dict) -> None:
    """Atomic UTF-8 JSON save (temp file + os.replace), owner-only permissions.

    Mirrors src.settings.save_settings's atomic-write pattern. chmod is best-effort --
    it has limited meaning on Windows, so failures there are silently ignored.
    """
    path = ACCOUNTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)

    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _migrate_legacy(data: dict) -> bool:
    """Migrate the legacy single-account keychain entries (usernames "tc"/"password")
    into one account on `data`, in place. Returns True if it migrated anything (the
    caller is then responsible for persisting `data`).

    Idempotent: the legacy entries are deleted only after the new ones are written, and
    once they're gone, later calls find nothing to migrate.
    """
    legacy_tc = _try_get_password(_TC_USERNAME)
    legacy_password = _try_get_password(_PASSWORD_USERNAME)
    if not legacy_tc or not legacy_password:
        return False

    account_id = _new_account_id()
    try:
        keyring.set_password(SERVICE_NAME, f"{account_id}:tc", legacy_tc)
        keyring.set_password(SERVICE_NAME, f"{account_id}:password", legacy_password)
    except Exception:
        log.exception("eski kimlik bilgileri yeni hesap biçimine taşınamadı")
        return False

    data["accounts"].append({
        "id": account_id,
        "name": "",
        "tc_masked": mask_tc(legacy_tc),
        "added_at": _now_iso(),
        "last_login": None,
    })
    data["active"] = account_id

    # Only delete the legacy entries now that the new ones are safely written -- never
    # risk losing the credentials if something above had failed.
    _delete_password(_TC_USERNAME)
    _delete_password(_PASSWORD_USERNAME)
    log.info("eski tek hesap kaydı yeni hesap biçimine taşındı (TC: %s)", mask_tc(legacy_tc))
    return True


def _load_and_migrate() -> dict:
    data = _load_accounts_file()
    try:
        if _migrate_legacy(data):
            _save_accounts_file(data)
    except Exception:
        log.exception("eski kimlik bilgisi taşınırken hata")
    return data


# --------------------------------------------------------------------------
# multi-account API
# --------------------------------------------------------------------------

def list_accounts() -> list[Account]:
    """Every saved account, in storage order. Never raises."""
    data = _load_and_migrate()
    active_id = data.get("active")
    return [
        Account(
            id=a["id"],
            name=a.get("name") or "",
            tc_masked=a.get("tc_masked") or "",
            active=(a["id"] == active_id),
        )
        for a in data.get("accounts", [])
        if isinstance(a, dict) and a.get("id")
    ]


def add_or_update(tc: str, password: str) -> Account:
    """Save (tc, password) in the keychain and make that account active.

    If an already-saved account has this exact TC, its password is updated (id and name
    are kept) -- otherwise a new account is created. Raises ValueError if `tc` fails
    validation or `password` is empty, or a keyring.errors.KeyringError if no usable
    backend is available.
    """
    validate_tc(tc)
    if not password:
        raise ValueError("şifre boş olamaz")

    data = _load_and_migrate()
    old_active = data.get("active")
    existing = None
    for a in data["accounts"]:
        if _try_get_password(f"{a['id']}:tc") == tc:
            existing = a
            break

    if existing is not None:
        account_id = existing["id"]
        keyring.set_password(SERVICE_NAME, f"{account_id}:password", password)
        existing["tc_masked"] = mask_tc(tc)
    else:
        account_id = _new_account_id()
        keyring.set_password(SERVICE_NAME, f"{account_id}:tc", tc)
        keyring.set_password(SERVICE_NAME, f"{account_id}:password", password)
        existing = {
            "id": account_id, "name": "", "tc_masked": mask_tc(tc),
            "added_at": _now_iso(), "last_login": None,
        }
        data["accounts"].append(existing)

    data["active"] = account_id
    _save_accounts_file(data)
    if data["active"] != old_active:
        _forget_browser_session()
    log.info("kimlik bilgileri anahtarlığa kaydedildi (TC: %s)", mask_tc(tc))
    return Account(id=account_id, name=existing.get("name") or "", tc_masked=existing["tc_masked"], active=True)


def set_active(account_id: str) -> None:
    """Make `account_id` the active account. Raises ValueError if it doesn't exist."""
    data = _load_and_migrate()
    if not any(a.get("id") == account_id for a in data["accounts"]):
        raise ValueError("hesap bulunamadı")
    old_active = data.get("active")
    data["active"] = account_id
    _save_accounts_file(data)
    if data["active"] != old_active:
        _forget_browser_session()


def remove(account_id: str | None) -> None:
    """Delete an account's keychain entries and its accounts.json record.

    No-op if `account_id` is None or unknown (idempotent, like the old single-account
    `clear()`). If the removed account was active, the new active account is the first
    remaining one, or None if there are no accounts left.
    """
    if not account_id:
        return
    data = _load_and_migrate()
    if not any(a.get("id") == account_id for a in data["accounts"]):
        return
    old_active = data.get("active")
    data["accounts"] = [a for a in data["accounts"] if a.get("id") != account_id]
    _delete_password(f"{account_id}:tc")
    _delete_password(f"{account_id}:password")
    if data.get("active") == account_id:
        data["active"] = data["accounts"][0]["id"] if data["accounts"] else None
    _save_accounts_file(data)
    if data["active"] != old_active:
        _forget_browser_session()
    log.info("hesap silindi")


def set_name(account_id: str, name: str) -> None:
    """Store the member's display name for `account_id` (never logged). No-op if the
    account doesn't exist -- a stale id from an in-flight event must not raise."""
    data = _load_and_migrate()
    for a in data["accounts"]:
        if a.get("id") == account_id:
            a["name"] = name or ""
            _save_accounts_file(data)
            return


# --------------------------------------------------------------------------
# backward-compatible single-account API (acts on the ACTIVE account)
# --------------------------------------------------------------------------

def save(tc: str, password: str) -> None:
    """Validate and store (tc, password) as the active account. See `add_or_update`."""
    add_or_update(tc, password)


def load() -> tuple[str, str] | None:
    """Return (tc, password) of the ACTIVE account, or None if absent/unavailable.

    Never raises: a missing keyring backend, a corrupt accounts.json, or any other error
    reading the store is logged and treated the same as "no credentials saved" -- the
    caller falls back to manual login.
    """
    try:
        data = _load_and_migrate()
    except Exception:
        log.exception("kayıtlı kimlik bilgileri okunamadı; elle giriş kullanılacak")
        return None
    active_id = data.get("active")
    if not active_id:
        return None
    return _account_credentials(active_id)


def clear() -> None:
    """Remove the active account. Never raises -- missing entries/backend are no-ops."""
    try:
        data = _load_and_migrate()
        remove(data.get("active"))
    except Exception:
        log.exception("kayıtlı kimlik bilgileri silinemedi")
        return
    log.info("kayıtlı kimlik bilgileri silindi")
