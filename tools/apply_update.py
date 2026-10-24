"""Applies a staged self-update after the main app has exited.

Launched by `src.updater.launch_apply` from a COPY at `state/update/apply_update.py`
(never from `tools/apply_update.py` directly), so it keeps running even while the files
it copies into are being overwritten. Because it runs from a copy, it cannot use its own
file location to find the install root -- `--root` is required.

May run under pythonw.exe on Windows (no console at all; sys.stdout/stderr are None), so
this script never prints -- everything goes to `state/logs/update.log`.

Usage: python apply_update.py --root <install_root> --new <new/ dir> --tag <release tag>

Steps: wait for the app lock to free -> back up every file the update touches -> copy the
new files in -> reinstall pip/playwright deps if their inputs changed -> relaunch the app.
Any failure after the lock is held restores the backup and relaunches the OLD app instead.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOCK_WAIT_TIMEOUT_S = 60.0
LOCK_POLL_INTERVAL_S = 0.5

# Defensive only: `src.updater.extract` already rejects a release containing these, so
# `new/` should never have them -- but this script must never write into a real install's
# state/ or .venv/ regardless, so any such entry (if one ever slipped through) is skipped.
_FORBIDDEN_TOP_DIRS = ("state", ".venv")

log = logging.getLogger("ibbspor.apply_update")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Kurulum kök dizini")
    parser.add_argument("--new", required=True, help="Yeni sürüm dosyalarının dizini")
    parser.add_argument("--tag", required=True, help="Sürüm etiketi (loglama için)")
    return parser.parse_args(argv)


def setup_logging(root: Path) -> None:
    log_dir = root / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "update.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def venv_python(root: Path) -> Path:
    """The venv's console python (used for `pip install` / `playwright install`)."""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _relaunch_target(root: Path) -> Path:
    """The executable the desktop shortcut launches (pythonw on Windows -- no console)."""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "pythonw.exe"
    return root / ".venv" / "bin" / "python"


def wait_for_lock(
    lock, *, timeout_s: float = LOCK_WAIT_TIMEOUT_S, poll_s: float = LOCK_POLL_INTERVAL_S
) -> bool:
    """Poll `lock.acquire()` (non-blocking by design) until it succeeds or `timeout_s`
    elapses. Returns whether the lock is now held."""
    deadline = time.monotonic() + timeout_s
    while True:
        if lock.acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def list_relative_files(new_dir: Path) -> list[Path]:
    """Every file under `new_dir`, as paths relative to it, sorted for deterministic
    copy order. Skips any entry whose top-level component is `state` or `.venv` (see
    `_FORBIDDEN_TOP_DIRS`)."""
    rels = []
    for p in new_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(new_dir)
        if rel.parts[0] in _FORBIDDEN_TOP_DIRS:
            log.warning("new/ içinde beklenmeyen %s girdisi atlandı: %s", rel.parts[0], rel)
            continue
        rels.append(rel)
    return sorted(rels)


def backup(root: Path, rels: list[Path], backup_dir: Path) -> None:
    """Copy every `rel` that currently exists under `root` into a fresh `backup_dir`."""
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True)
    for rel in rels:
        src = root / rel
        if not src.exists():
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_new(new_dir: Path, root: Path, rels: list[Path]) -> None:
    for rel in rels:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new_dir / rel, dst)


def restore_backup(root: Path, backup_dir: Path, rels: list[Path]) -> None:
    """For each rel in rels: copy it back from backup if it exists, else delete it from root.
    Tolerates empty directories created by copy_new."""
    for rel in rels:
        src = backup_dir / rel
        dst = root / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            dst.unlink(missing_ok=True)


def _requirements_bytes(backup_dir: Path, root: Path) -> tuple[bytes, bytes]:
    old = backup_dir / "requirements.txt"
    new = root / "requirements.txt"
    return (old.read_bytes() if old.exists() else b""), (new.read_bytes() if new.exists() else b"")


def requirements_changed(backup_dir: Path, root: Path) -> bool:
    old_bytes, new_bytes = _requirements_bytes(backup_dir, root)
    return old_bytes != new_bytes


def _playwright_line(data: bytes) -> bytes:
    for line in data.splitlines():
        if line.strip().startswith(b"playwright=="):
            return line.strip()
    return b""


def playwright_changed(backup_dir: Path, root: Path) -> bool:
    old_bytes, new_bytes = _requirements_bytes(backup_dir, root)
    return _playwright_line(old_bytes) != _playwright_line(new_bytes)


def _run_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def pip_install(root: Path, *, run=subprocess.run) -> int:
    py = venv_python(root)
    result = run(
        [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt"],
        cwd=str(root), **_run_kwargs(),
    )
    return result.returncode


def playwright_install(root: Path, *, run=subprocess.run) -> int:
    py = venv_python(root)
    result = run(
        [str(py), "-m", "playwright", "install", "chromium"],
        cwd=str(root), **_run_kwargs(),
    )
    return result.returncode


def relaunch(root: Path, *, popen=subprocess.Popen) -> None:
    """Start the app exactly like the desktop shortcut would, detached (outlives us)."""
    exe = _relaunch_target(root)
    if sys.platform == "win32":
        kwargs = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kwargs = {"start_new_session": True}
    popen(
        [str(exe), str(root / "app.pyw")], cwd=str(root),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **kwargs,
    )


def run_apply(
    root: Path, new_dir: Path, tag: str, *,
    run=subprocess.run, popen=subprocess.Popen,
    lock_timeout_s: float = LOCK_WAIT_TIMEOUT_S, lock_poll_s: float = LOCK_POLL_INTERVAL_S,
) -> int:
    """Do the actual update. `--root` must already be on `sys.path` (see `main`) so these
    imports resolve -- done before any copying, per design."""
    from src.notify import notify
    from src.single_instance import SingleInstance

    backup_dir = root / "state" / "update" / "backup"
    lock = SingleInstance(root / "state" / "app.lock")

    if not wait_for_lock(lock, timeout_s=lock_timeout_s, poll_s=lock_poll_s):
        log.error("uygulama kilidi %s saniyede serbest kalmadı; güncelleme uygulanmadı", lock_timeout_s)
        notify("Güncelleme", "Uygulama kapanmadı; güncelleme uygulanamadı.")
        return 1

    failed = False
    try:
        rels = list_relative_files(new_dir)
        backup(root, rels, backup_dir)
        try:
            copy_new(new_dir, root, rels)
        except Exception:
            log.exception("dosya kopyalama hatası; eski sürüm geri yükleniyor")
            restore_backup(root, backup_dir, rels)
            failed = True

        if not failed and requirements_changed(backup_dir, root):
            log.info("requirements.txt değişti; pip install çalıştırılıyor")
            if pip_install(root, run=run) != 0:
                log.error("pip install başarısız; eski sürüm geri yükleniyor")
                restore_backup(root, backup_dir, rels)
                failed = True
            elif playwright_changed(backup_dir, root):
                log.info("playwright sürümü değişti; playwright install çalıştırılıyor")
                if playwright_install(root, run=run) != 0:
                    log.error("playwright install başarısız; eski sürüm geri yükleniyor")
                    restore_backup(root, backup_dir, rels)
                    failed = True

        if not failed:
            shutil.rmtree(new_dir, ignore_errors=True)
    finally:
        lock.release()

    relaunch(root, popen=popen)
    if failed:
        notify("Güncelleme", "Güncelleme uygulanamadı; eski sürüm geri yüklendi.")
        return 1

    log.info("güncelleme uygulandı: %s", tag)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()
    new_dir = Path(args.new).resolve()

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    setup_logging(root)
    log.info("apply_update başladı: tag=%s new=%s", args.tag, new_dir)
    try:
        rc = run_apply(root, new_dir, args.tag)
    except Exception:
        log.exception("apply_update beklenmeyen hatayla durdu")
        rc = 1
    log.info("apply_update bitti: rc=%s", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
