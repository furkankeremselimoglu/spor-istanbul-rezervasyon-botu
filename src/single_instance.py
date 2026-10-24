"""Cross-platform single-instance lock for app.pyw (state/app.lock).

A non-blocking OS-level advisory lock: `msvcrt.locking` on Windows, `fcntl.flock` on
POSIX (macOS/Linux). Acquiring an already-held lock fails immediately (never blocks,
never raises) rather than waiting for it. Stale-safe by construction -- the lock is tied
to the open file descriptor, so if the holding process dies for any reason (crash,
Task Manager, kill -9) the OS releases it automatically; there is nothing here that can
go stale and nothing to clean up by hand.

Usage (see app.pyw):
    lock = SingleInstance()
    if not lock.acquire():
        messagebox.showinfo("...", "Uygulama zaten açık")
        sys.exit(0)
    try:
        ...
    finally:
        lock.release()

Also usable as a context manager (raises RuntimeError if the lock is already held --
handy in tests, not used by app.pyw itself since it wants the messagebox instead):
    with SingleInstance(path):
        ...
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Derived independently from this file's own location (not imported from src.browser)
# so this module has no dependency on browser.py.
STATE = Path(__file__).resolve().parent.parent / "state"
DEFAULT_LOCK_PATH = STATE / "app.lock"


class SingleInstance:
    def __init__(self, path: Path | str = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._fh = None

    @property
    def locked(self) -> bool:
        """True if THIS object currently holds the lock."""
        return self._fh is not None

    def acquire(self) -> bool:
        """Try to take the lock; never blocks, never raises.

        Returns True if the lock is now held (either freshly taken, or already held by
        this same object), False if another process holds it (or the lock file/its
        directory can't be opened at all, which is treated the same as "can't run" --
        the caller should show the same "already open" message either way).
        """
        if self._fh is not None:
            return True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")
        except OSError:
            return False

        try:
            if not _try_lock(fh):
                fh.close()
                return False
        except Exception:
            fh.close()
            raise

        self._fh = fh
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode("ascii"))
            fh.flush()
        except OSError:
            pass  # best-effort diagnostic only -- the lock itself is already held
        return True

    def release(self) -> None:
        """Release the lock if held; a no-op (never raises) otherwise."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError(f"another instance already holds the lock: {self.path}")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _try_lock(fh) -> bool:
    """Non-blocking exclusive lock attempt on `fh`. Returns False, never raises, if the
    lock is already held by someone else."""
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def _unlock(fh) -> None:
    """Best-effort unlock -- never raises (the fd is about to be closed regardless, which
    would release the lock anyway)."""
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
