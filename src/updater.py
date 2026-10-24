"""Self-update: check GitHub for a newer release, download and stage it.

Flow: `check_latest()` polls the unauthenticated `releases/latest` GitHub API endpoint
(which already excludes drafts/prereleases) for the newest release's zip asset.
`is_newer()` compares its version against the running one (`src.version.parse_version`).
`stage()` downloads that zip, verifies its sha256 (when GitHub provided one) and
extracts it -- with its single top-level folder stripped -- into `state/update/new/`.
`launch_apply()` then hands off to `tools/apply_update.py`, a separate process that
performs the actual file copy after this app has exited (see that file).

TLS verification uses `certifi`'s CA bundle when importable, the interpreter's default
bundle otherwise -- see `_ssl_context()`.

Never makes a real network call from this module's own code during tests; `check_latest`
and `download` never raise on network/parsing failure (`check_latest` returns None,
logged at INFO).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.version import parse_version

logger = logging.getLogger("ibbspor.updater")

REPO = "furkankeremselimoglu/spor-istanbul-rezervasyon-botu"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

ROOT = Path(__file__).resolve().parent.parent
UPDATE_DIR = ROOT / "state" / "update"

_USER_AGENT = "spor-istanbul-rezervasyon-botu"

# subprocess.DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP only exist on Windows (see
# src/notify.py's CREATE_NO_WINDOW for the same pattern) -- referencing them
# unconditionally would raise AttributeError on macOS/Linux even though the branch
# that uses them never runs there.
try:
    _DETACHED_PROCESS = subprocess.DETACHED_PROCESS
    _CREATE_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
except AttributeError:
    _DETACHED_PROCESS = 0
    _CREATE_NEW_PROCESS_GROUP = 0


class UpdateError(Exception):
    """Raised by `download`/`extract`/`stage` on failure. Message is Turkish, shown to
    the user as-is."""


@dataclass(frozen=True)
class Release:
    version: str  # tag with leading "v" stripped, e.g. "2026.10.23"
    tag: str  # e.g. "v2026.10.23"
    zip_url: str
    sha256: str | None
    size: int


def is_git_checkout(root: Path = ROOT) -> bool:
    """True when `root` is a git checkout (owner updates via `git pull`, not this)."""
    return (Path(root) / ".git").exists()


def _ssl_context() -> ssl.SSLContext:
    """`certifi`'s CA bundle when importable, the interpreter's default otherwise."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def check_latest(api_url: str = API_URL, timeout: float = 10) -> Release | None:
    """Fetch the latest release's zip asset. None on any failure (logged at INFO) --
    missing/unreachable network, malformed JSON, or no `.zip` asset. Never raises."""
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": _USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = json.load(resp)

        tag = data["tag_name"]
        zip_asset = next(
            (
                a
                for a in data.get("assets", [])
                if str(a.get("browser_download_url", "")).endswith(".zip")
            ),
            None,
        )
        if zip_asset is None:
            logger.info("güncelleme kontrolü: %s sürümünde zip eki yok", tag)
            return None

        sha256 = None
        digest = zip_asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            sha256 = digest.split(":", 1)[1]
        else:
            logger.info("güncelleme kontrolü: %s zip ekinde sha256 özeti yok", tag)

        version = tag[1:] if tag.startswith("v") else tag
        return Release(
            version=version,
            tag=tag,
            zip_url=zip_asset["browser_download_url"],
            sha256=sha256,
            size=int(zip_asset.get("size", 0)),
        )
    except Exception as exc:
        logger.info("güncelleme kontrolü başarısız: %s", exc)
        return None


def is_newer(current: str, latest: str) -> bool:
    """True if `latest` parses to a strictly greater version tuple than `current`.
    False (never raises) if either doesn't parse (e.g. the dev fallback string)."""
    current_v = parse_version(current)
    latest_v = parse_version(latest)
    if current_v is None or latest_v is None:
        return False
    return latest_v > current_v


def download(release: Release, dest_dir: Path = UPDATE_DIR) -> Path:
    """Stream `release`'s zip to `dest_dir/<tag>.zip`, verifying its sha256 when given.
    Raises `UpdateError` (Turkish message) on any failure or digest mismatch, leaving no
    partial/final file behind."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{release.tag}.zip"
    tmp = dest_dir / f"{release.tag}.zip.part"

    try:
        req = urllib.request.Request(release.zip_url, headers={"User-Agent": _USER_AGENT})
        hasher = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    f.write(chunk)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Güncelleme indirilemedi: {exc}") from exc

    if release.sha256 and hasher.hexdigest() != release.sha256:
        tmp.unlink(missing_ok=True)
        raise UpdateError("Güncelleme dosyası doğrulanamadı (sha256 uyuşmuyor)")

    os.replace(tmp, dest)
    return dest


def _check_safe_member(name: str) -> None:
    """Reject a zip entry whose name could escape `dest_dir` or clobber a protected
    path component. Raises `UpdateError` (Turkish message)."""
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        raise UpdateError(f"Güncelleme paketi geçersiz: mutlak yol ({name})")
    if "\\" in name:
        raise UpdateError(f"Güncelleme paketi geçersiz: ters eğik çizgi ({name})")
    if ".." in name.split("/"):
        raise UpdateError(f"Güncelleme paketi geçersiz: üst dizin ({name})")


def extract(zip_path: Path, dest_dir: Path) -> Path:
    """Extract `zip_path` into `dest_dir/new/`, stripping its single top-level folder.
    Raises `UpdateError` (Turkish message) for an unsafe member (see
    `_check_safe_member`) or a zip without exactly one top-level folder. Entries under
    `state/` or `.venv/` are skipped (never written) -- the install owns those."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        top_dirs = set()
        for name in names:
            _check_safe_member(name)
            top_dirs.add(name.split("/", 1)[0])
        if len(top_dirs) != 1:
            raise UpdateError("Güncelleme paketi geçersiz: tek üst klasör bekleniyor")

        prefix = next(iter(top_dirs)) + "/"

        target = Path(dest_dir) / "new"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        for info in zf.infolist():
            rel = info.filename[len(prefix):] if info.filename.startswith(prefix) else ""
            # Every release ships state/README.md; state/ and .venv/ belong to the install.
            if not rel or rel.split("/", 1)[0] in ("state", ".venv"):
                continue
            dest_path = target / rel
            if info.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest_path, "wb") as f:
                    f.write(src.read())

    return target


def stage(release: Release) -> Path:
    """Download, verify and extract `release` into `UPDATE_DIR/new/`, and stand up
    `UPDATE_DIR/apply_update.py` for `launch_apply` to run. Returns the `new/` dir.
    Raises `UpdateError` on failure (propagated from `download`/`extract`)."""
    new_dir = UPDATE_DIR / "new"
    if new_dir.exists():
        shutil.rmtree(new_dir)

    zip_path = download(release, UPDATE_DIR)
    try:
        extracted = extract(zip_path, UPDATE_DIR)
    finally:
        zip_path.unlink(missing_ok=True)

    shutil.copy2(extracted / "tools" / "apply_update.py", UPDATE_DIR / "apply_update.py")
    return extracted


def launch_apply(root: Path, new_dir: Path, tag: str) -> None:
    """Hand off to `apply_update.py` in a detached process that outlives this one (the
    caller exits right after this)."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        [
            sys.executable,
            str(UPDATE_DIR / "apply_update.py"),
            "--root",
            str(root),
            "--new",
            str(new_dir),
            "--tag",
            tag,
        ],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
