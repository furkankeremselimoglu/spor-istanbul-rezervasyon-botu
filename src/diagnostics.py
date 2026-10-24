"""Build a redacted diagnostics ZIP a non-technical user can send to the bot's author
for troubleshooting, without handing over their live session or personal data.

Entry point: `create_diagnostics_zip(dest_dir)` (also wired up as `bot.py diagnostics`
and the web UI's "Tanı paketi" button, Hesap > Yardım).

**What's included** (redacted on the fly -- see `redact()`):
- `state/logs/*` -- all rotations (`bot.log`, `bot.log.1`, ...)
- the last 40 `state/artifacts/*.html` snapshots (HTML only, see below)
- `state/settings.json`, in full
- a size/summary of `state/catalog.json` (branch/facility counts), not its full content
- this app's version (`src.version.get_version()`), OS, Python and key package versions
- a `README.txt` explaining what's in the zip

**What's never included**:
- `state/artifacts/*.png` screenshots -- they can show the member's account pages
  (name, membership, reservation history) as pixels, which can't be redacted the way
  text can
- `state/cookies.json`, `state/profile/` -- the live login session
- anything from the OS keychain (TC/pasaport no, password)
- `state/accounts.json` -- it names every saved account (see src.credentials); every
  stored account name found there is still redacted (like the live member's name,
  below) from every OTHER file that's bundled

**Redaction** (`redact()`, applied to every text blob before it's written -- the files
on disk are never modified):
- 11-digit numbers (TC kimlik no) -> `***TC***`
- Turkish phone numbers (`05xxxxxxxxx`, `5xxxxxxxxx`, `+90 5xx ...`) -> `***TEL***`
- the member's full name, discovered from an "AdSoyad" element in the collected
  artifact HTML (the site's own field name for this -- see
  `tests/fixtures/*_real_*.html`) -- every occurrence of that exact string, in every
  file, is replaced with `***AD SOYAD***`
- `X-Token:` headers and `?t=`/`&t=` query tokens -> `***TOKEN***`
- `cookie: ...` / `cookie = ...` values -> `***COOKIE***`

Never raises for a missing file -- a half-broken or empty `state/` directory still
produces a best-effort zip (with whatever pieces exist).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import zipfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ibbspor.diagnostics")

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
LOGS_DIR = STATE / "logs"
ARTIFACTS_DIR = STATE / "artifacts"
SETTINGS_PATH = STATE / "settings.json"
CATALOG_PATH = STATE / "catalog.json"
# NEVER bundled (see module docstring) -- read only to find account names to redact.
ACCOUNTS_PATH = STATE / "accounts.json"

MAX_ARTIFACT_HTML = 40

# Packages worth knowing the version of when debugging (see requirements.txt).
_PACKAGES = ("playwright", "beautifulsoup4", "pytest", "keyring", "tzdata", "pywebview")

# -- redaction ----------------------------------------------------------------------

# Turkish phone numbers: optional "+90" or leading "0", then 5XX XXX XX XX (10 digits),
# with optional space/dot/dash separators between groups.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+90[\s.\-]?)?0?5\d{2}[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}(?!\d)"
)
# Any other standalone 11-digit run (TC kimlik no). Applied AFTER the phone regex so a
# phone number already masked as ***TEL*** doesn't also get caught here.
_TC_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")
_TOKEN_QUERY_RE = re.compile(r"([?&][tT]=)[A-Za-z0-9_\-]+")
_X_TOKEN_HEADER_RE = re.compile(r"(?im)^(X-Token:\s*)\S+$")
_COOKIE_RE = re.compile(r"""(?i)(cookie["']?\s*[:=]\s*["']?)([^;\s"'<>]+)""")

# The site's own "AdSoyad" (ad soyad = full name) field, e.g.
# <span id="lblAdSoyad">TEST KULLANICI</span> -- also used for the header user-menu
# text, which is the same element. Backreference to the opening tag name so we don't
# accidentally swallow unrelated following markup.
_AD_SOYAD_RE = re.compile(
    r'(?is)<(\w+)[^>]*\b(?:id|class)=["\']([^"\']*AdSoyad[^"\']*)["\'][^>]*>(.*?)</\1>'
)


def redact(text: str, *, name: str | None = None, names: "list[str] | None" = None) -> str:
    """Apply every redaction rule to `text` and return the result. Never raises; a
    regex that doesn't match simply leaves the text unchanged.

    `name` is the live member's name (from an "AdSoyad" element); `names` is every
    OTHER name worth scrubbing (e.g. every saved account's name, from accounts.json --
    see `_account_names`). Both, if given, are replaced with "***AD SOYAD***".
    """
    text = _PHONE_RE.sub("***TEL***", text)
    text = _TC_RE.sub("***TC***", text)
    text = _TOKEN_QUERY_RE.sub(r"\1***TOKEN***", text)
    text = _X_TOKEN_HEADER_RE.sub(r"\1***TOKEN***", text)
    text = _COOKIE_RE.sub(r"\1***COOKIE***", text)
    if name:
        # shortcut: re.IGNORECASE does not fold Turkish İ/ı; upgrade with casefold-based matching if a real log shows a missed name
        text = re.sub(re.escape(name), "***AD SOYAD***", text, flags=re.IGNORECASE)
    for other in names or ():
        if other:
            # shortcut: re.IGNORECASE does not fold Turkish İ/ı; upgrade with casefold-based matching if a real log shows a missed name
            text = re.sub(re.escape(other), "***AD SOYAD***", text, flags=re.IGNORECASE)
    return text


def find_member_name(html_texts: list[str]) -> str | None:
    """Best-effort: the member's full name, read from the first HTML blob with an
    "AdSoyad" element. Returns None (never raises) if nothing matches."""
    for html in html_texts:
        m = _AD_SOYAD_RE.search(html)
        if not m:
            continue
        name = re.sub(r"<[^>]+>", " ", m.group(3))
        name = " ".join(name.split()).strip()
        if name:
            return name
    return None


def _account_names() -> list[str]:
    """Every stored account name in state/accounts.json (see src.credentials),
    best-effort -- never raises, and this file itself is never bundled into the zip."""
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names = []
    for a in data.get("accounts", []):
        if isinstance(a, dict):
            name = a.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


# -- collection -----------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    """Read `path` as UTF-8 text, or None (never raises) if it's missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _log_sources() -> list[tuple[str, Path]]:
    try:
        files = sorted(LOGS_DIR.glob("bot.log*")) + sorted(LOGS_DIR.glob("remind.log*"))
    except OSError:
        return []
    return [(f"logs/{f.name}", f) for f in files]


def _artifact_html_sources() -> list[tuple[str, Path]]:
    """The most recent `MAX_ARTIFACT_HTML` artifact HTML snapshots (never the PNGs).
    Filenames are `<timestamp>_<step>.html`, zero-padded so a plain reverse name sort
    is a recency sort."""
    try:
        files = sorted(ARTIFACTS_DIR.glob("*.html"), reverse=True)[:MAX_ARTIFACT_HTML]
    except OSError:
        return []
    return [(f"artifacts/{f.name}", f) for f in files]


def _package_version(pkg: str) -> str:
    try:
        import importlib.metadata as im

        return im.version(pkg)
    except Exception:
        return "bulunamadı"


def _app_version() -> str:
    try:
        from src.version import get_version

        return get_version()
    except Exception as e:  # pragma: no cover - defensive only
        return f"bilinmiyor ({e})"


def _environment_text() -> str:
    lines = [
        f"uygulama sürümü: {_app_version()}",
        f"işletim sistemi: {platform.platform()}",
        f"python: {sys.version.split()[0]}",
        "paketler:",
    ]
    for pkg in _PACKAGES:
        lines.append(f"  {pkg}: {_package_version(pkg)}")
    return "\n".join(lines) + "\n"


def _catalog_summary_text() -> str:
    try:
        size = CATALOG_PATH.stat().st_size
    except OSError:
        return "catalog.json bulunamadı.\n"

    try:
        from src.catalog import Catalog

        cat = Catalog.load(CATALOG_PATH)
        branch_count = len(cat.branches)
        facility_count = sum(len(b.facilities) for b in cat.branches.values())
        return (
            f"catalog.json boyutu: {size} bayt\n"
            f"şube sayısı: {branch_count}\n"
            f"tesis sayısı (toplam): {facility_count}\n"
        )
    except Exception as e:  # pragma: no cover - defensive only
        return f"catalog.json boyutu: {size} bayt (özet okunamadı: {e})\n"


_README_TEXT = """Bu ZIP, İBB Spor rezervasyon botu için bir tanı (diagnostics) paketidir.

İçindekiler:
- logs/           bot.log ve dönen (rotated) eski günlük dosyaları
- artifacts/      son en fazla {max_html} adet sayfa anlık görüntüsü (yalnızca HTML;
                   kişisel veri içerebilecek PNG ekran görüntüleri HİÇBİR ZAMAN dahil
                   edilmez)
- settings.json   kayıtlı hedefleriniz ve ayarlarınız
- catalog_summary.txt   katalog dosyasının özeti (tam içeriği değil)
- environment.txt sürüm bilgisi: uygulama, işletim sistemi, Python, paketler

Neler dahil DEĞİL:
- Giriş oturumunuz (cookies.json, profile/) -- hesabınıza erişim bu pakette yoktur
- TC kimlik no / şifre (işletim sistemi anahtarlığında saklanır, bu pakete hiç girmez)
- Ekran görüntüleri (PNG) -- yalnızca HTML sayfa kaynakları dahildir

Gizlilik için aşağıdakiler otomatik olarak maskelenmiştir (metin dosyalarının hepsinde):
- TC kimlik no benzeri 11 haneli sayılar -> ***TC***
- Telefon numaraları -> ***TEL***
- Ad soyad -> ***AD SOYAD***
- Oturum belirteçleri (token) ve çerez değerleri -> ***TOKEN*** / ***COOKIE***

Bu paketi yalnızca güvendiğiniz birine (botun yazarına) gönderin.
"""


def _readme_text() -> str:
    return _README_TEXT.format(max_html=MAX_ARTIFACT_HTML)


# -- entry point ----------------------------------------------------------------------

def create_diagnostics_zip(dest_dir: Path) -> Path:
    """Build `ibbspor-tani-<timestamp>.zip` in `dest_dir` (created if missing) and
    return its path. Never raises for a missing/unreadable source file -- worst case,
    that piece is simply absent from the zip."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = dest_dir / f"ibbspor-tani-{ts}.zip"

    sources = _log_sources() + _artifact_html_sources()

    contents: dict[str, str] = {}
    for arcname, path in sources:
        text = _read_text(path)
        if text is not None:
            contents[arcname] = text

    settings_text = _read_text(SETTINGS_PATH)
    if settings_text is not None:
        contents["settings.json"] = settings_text

    html_blobs = [text for name, text in contents.items() if name.startswith("artifacts/")]
    member_name = find_member_name(html_blobs)
    account_names = _account_names()  # state/accounts.json is read, never bundled

    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, text in contents.items():
                zf.writestr(arcname, redact(text, name=member_name, names=account_names))
            zf.writestr(
                "catalog_summary.txt",
                redact(_catalog_summary_text(), name=member_name, names=account_names),
            )
            zf.writestr(
                "environment.txt",
                redact(_environment_text(), name=member_name, names=account_names),
            )
            zf.writestr("README.txt", _readme_text())
    except Exception:
        log.exception("tanı paketi oluşturulurken hata")
        raise

    with suppress(OSError):
        os.chmod(out_path, 0o600)

    return out_path
