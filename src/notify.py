"""Cross-platform user notifications: macOS, Windows, Linux."""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator
from xml.sax.saxutils import escape

logger = logging.getLogger("ibbspor.notify")

# subprocess.CREATE_NO_WINDOW is only available on Windows
try:
    CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:
    CREATE_NO_WINDOW = 0

# macOS: osascript with "on run argv" keeps title/message/sound out of the script source
# entirely: they arrive as positional arguments, so no string-building/escaping is needed
# and no value the caller passes can be interpreted as AppleScript.
_MACOS_NOTIFY_SCRIPT = """
on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    set wantsSound to item 3 of argv
    if wantsSound is "1" then
        display notification theMessage with title theTitle sound name "Glass"
    else
        display notification theMessage with title theTitle
    end if
end run
"""

# Windows PowerShell script: reads title/message from environment variables and
# injects them into XML. Values must be XML-escaped to prevent XML/toast injection.
_WINDOWS_NOTIFY_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.UI.Notifications.ToastTemplate, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null

$APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
$template = @"
<toast>
    <visual>
        <binding template="ToastText02">
            <text id="1" placement="attribution">$([Environment]::GetEnvironmentVariable('IBB_TITLE'))</text>
            <text id="2">$([Environment]::GetEnvironmentVariable('IBB_MESSAGE'))</text>
        </binding>
    </visual>
</toast>
"@

try {
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($APP_ID).Show($toast)
} catch {
    # Silent fallback: the Windows.UI.Notifications API might not be available
}
"""


def notify(title: str, message: str, sound: bool = True) -> None:
    """Show a notification (platform-dependent). Never raises; failures are logged.

    On Windows, if sound=True, also plays an alert sound via alert(1).
    Also prints a visible line to stdout: "\n🔔 {title}: {message}\n" -- skipped
    entirely when sys.stdout is None (pythonw.exe has no console), and never allowed
    to raise even if stdout exists but encoding the line fails.
    """
    logger.info("notify: %s - %s", title, message)
    # Print visible line to stdout (encoding-safe; sys.stdout is None under pythonw.exe
    # on Windows, which has no console at all -- print() itself would raise there).
    if sys.stdout is not None:
        try:
            print(f"\n🔔 {title}: {message}\n", flush=True)
        except Exception:
            pass  # Fallback: just continue if stdout print fails (e.g. encoding error)

    try:
        if sys.platform == "win32":
            _notify_windows(title, message)
            if sound:
                alert(1)
        elif sys.platform == "darwin":
            _notify_macos(title, message, sound)
        else:
            _notify_linux(title, message)
    except Exception:
        logger.exception("failed to show notification: title=%r message=%r", title, message)


def _notify_macos(title: str, message: str, sound: bool) -> None:
    """macOS notification via osascript."""
    subprocess.run(
        ["osascript", "-e", _MACOS_NOTIFY_SCRIPT, title, message, "1" if sound else "0"],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _notify_windows(title: str, message: str) -> None:
    """Windows notification via PowerShell toast notification API."""
    env = os.environ.copy()
    env["IBB_TITLE"] = escape(title)
    env["IBB_MESSAGE"] = escape(message)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=_WINDOWS_NOTIFY_SCRIPT.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )


def _notify_linux(title: str, message: str) -> None:
    """Linux notification via notify-send (best-effort; silent if unavailable)."""
    try:
        subprocess.run(
            ["notify-send", title, message],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError:
        pass  # notify-send not available; silent fallback


# macOS: AppleScript `display dialog`, timing out via `giving up after`. Same "on run
# argv" pattern as _MACOS_NOTIFY_SCRIPT -- title/message/labels arrive as positional
# arguments, never interpolated into the script source.
_MACOS_ASK_SCRIPT = """
on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    set yesLabel to item 3 of argv
    set hasNo to item 4 of argv
    set noLabel to item 5 of argv
    set timeoutS to item 6 of argv
    if hasNo is "1" then
        display dialog theMessage with title theTitle buttons {noLabel, yesLabel} default button yesLabel giving up after (timeoutS as number)
    else
        display dialog theMessage with title theTitle buttons {yesLabel} default button yesLabel giving up after (timeoutS as number)
    end if
end run
"""


def ask(
    title: str, message: str, *, yes_label: str, no_label: str | None, timeout_s: float
) -> bool | None:
    """Modal dialog with a timeout; True if the primary button (`yes_label`) was
    clicked, False if dismissed/the other button was clicked, None on timeout, an
    unsupported platform (Linux), or any failure. Never raises.

    Windows: `MessageBoxTimeoutW` (MB_YESNO when `no_label` is given, else MB_OK) --
    the buttons are the OS-localized Yes/No, so they can't actually be relabelled with
    `yes_label`/`no_label`, which is acceptable here. macOS: `display dialog` via
    osascript, with its own `giving up after` timeout.
    """
    try:
        if sys.platform == "win32":
            return _ask_windows(title, message, no_label, timeout_s)
        elif sys.platform == "darwin":
            return _ask_macos(title, message, yes_label, no_label, timeout_s)
        return None
    except Exception:
        logger.exception("failed to show ask dialog: title=%r message=%r", title, message)
        return None


def _win32_message_box_timeout_w(
    hwnd: int, text: str, caption: str, flags: int, lang_id: int, timeout_ms: int
) -> int:
    """Thin wrapper around user32!MessageBoxTimeoutW, so tests can monkeypatch this
    single call point directly -- `ctypes.windll` only exists on real Windows, so the
    ctypes lookup itself can't happen at import time on macOS/Linux dev machines."""
    import ctypes

    return ctypes.windll.user32.MessageBoxTimeoutW(hwnd, text, caption, flags, lang_id, timeout_ms)


def _ask_windows(title: str, message: str, no_label: str | None, timeout_s: float) -> bool | None:
    """Windows ask() via MessageBoxTimeoutW. IDOK/IDYES -> True, 32000 (timeout) ->
    None, anything else (IDNO, IDCANCEL, ...) -> False."""
    MB_OK = 0x00000000
    MB_YESNO = 0x00000004
    MB_ICONINFORMATION = 0x00000040
    MB_SETFOREGROUND = 0x00010000
    MB_TOPMOST = 0x00040000
    IDOK = 1
    IDYES = 6
    TIMEOUT = 32000

    flags = (MB_YESNO if no_label is not None else MB_OK) | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
    result = _win32_message_box_timeout_w(0, message, title, flags, 0, int(timeout_s * 1000))
    if result in (IDOK, IDYES):
        return True
    if result == TIMEOUT:
        return None
    return False


def _ask_macos(
    title: str, message: str, yes_label: str, no_label: str | None, timeout_s: float
) -> bool | None:
    """macOS ask() via osascript `display dialog ... giving up after`. Parses the
    AppleScript result text (e.g. "button returned:Tamam, gave up:false"); a non-zero
    return code (e.g. the dialog closed via Esc / the red button) is treated as False
    (dismissed)."""
    argv = [
        "osascript", "-e", _MACOS_ASK_SCRIPT,
        title, message, yes_label, "1" if no_label is not None else "0", no_label or "",
        # Whole seconds: AppleScript's `"30.0" as number` fails under a Turkish (comma
        # decimal) locale and the dialog would vanish instantly (seen live).
        str(max(1, int(timeout_s))),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s + 10, text=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        logger.warning("osascript dialog hata verdi (%s): %s", proc.returncode, proc.stderr.strip()[:200])
        return False
    out = proc.stdout
    if "gave up:true" in out:
        return None
    if "button returned:" in out:
        button = out.split("button returned:", 1)[1].split(",", 1)[0].strip()
        return button == yes_label
    return None


def alert(repeat: int = 3) -> None:
    """Play a loud attention sound to wake the user. Never raises.

    Windows: winsound.MessageBeep(MB_ICONEXCLAMATION) / Beep(1000, 400) repeated. winsound
    works fine with no console attached (pythonw.exe), so this path needs no stdout guard.
    macOS: afplay /System/Library/Sounds/Glass.aiff (fallback: terminal bell, itself a
    no-op rather than a crash when there is no stdout to print it to).
    Other: terminal bell (\a), same no-stdout no-op.
    """
    try:
        if sys.platform == "win32":
            _alert_windows(repeat)
        elif sys.platform == "darwin":
            _alert_macos(repeat)
        else:
            _alert_generic(repeat)
    except Exception:
        logger.exception("failed to play alert sound")


def _alert_windows(repeat: int) -> None:
    """Windows alert: winsound.MessageBeep or Beep tone."""
    try:
        import winsound
        for _ in range(repeat):
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except (AttributeError, ValueError):
                # Fallback to Beep if MessageBeep unavailable
                winsound.Beep(1000, 400)
    except ImportError:
        # winsound not available (e.g., on non-Windows); silent fallback
        pass


def _bell() -> None:
    """Print a terminal bell. No-op (not an error) when there is no stdout to print to,
    e.g. pythonw.exe on Windows, which has no console at all."""
    if sys.stdout is None:
        return
    try:
        print("\a", flush=True)
    except Exception:
        pass


def _alert_macos(repeat: int) -> None:
    """macOS alert: afplay Glass.aiff, fallback to terminal bell."""
    for _ in range(repeat):
        try:
            subprocess.run(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Fallback to terminal bell
            _bell()


def _alert_generic(repeat: int) -> None:
    """Generic alert: terminal bell."""
    for _ in range(repeat):
        _bell()


@contextmanager
def keep_awake() -> Iterator[None]:
    """Context manager: prevent system sleep while inside.

    Windows: SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED), restored on exit.
    macOS: a `caffeinate -i -w <pid>` subprocess, terminated on exit. Other: no-op.

    Setup/teardown problems are logged and ignored. Exceptions raised by the code
    inside the `with` block are NOT caught here: they propagate unchanged (the engine
    relies on LoggedOut/Stopped/SiteError reaching its own handlers).
    """
    proc = None
    try:
        if sys.platform == "win32":
            _keep_awake_windows()
        elif sys.platform == "darwin":
            proc = _keep_awake_macos()
    except Exception:
        logger.exception("keep_awake: uyku engelleme başlatılamadı")
    try:
        yield
    finally:
        try:
            if sys.platform == "win32":
                _keep_awake_windows_restore()
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            logger.exception("keep_awake: uyku engelleme kapatılamadı")


def _keep_awake_windows() -> None:
    """Windows: set ES_CONTINUOUS|ES_SYSTEM_REQUIRED to prevent sleep."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass  # Silent fallback


def _keep_awake_windows_restore() -> None:
    """Windows: restore to ES_CONTINUOUS only."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass  # Silent fallback


def _keep_awake_macos() -> subprocess.Popen | None:
    """macOS: start caffeinate process."""
    try:
        return subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
