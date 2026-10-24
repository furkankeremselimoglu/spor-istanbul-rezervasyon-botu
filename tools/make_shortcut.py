"""Create the desktop launcher for the GUI (called by install.bat / install.command).

Windows: "Spor İstanbul Rezervasyon Botu.lnk" -> .venv\\Scripts\\pythonw.exe "<root>\\app.pyw" (no console window).
macOS:   "Spor İstanbul Rezervasyon Botu.command" -> cd <root> && .venv/bin/python app.pyw

Paths reach PowerShell through environment variables, never through the script text, so
spaces, quotes or non-ASCII characters in the install folder can't break the command.
Exit code 0 on success, 1 on failure (the installers only warn on failure).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Spor İstanbul Rezervasyon Botu"
LEGACY_NAME = "Rezervasyon Botu"  # old name; remove the old icon so installs don't end up with two
# WScript.Shell saves the .lnk through the ANSI code page ("İ" becomes "I" on non-Turkish
# Windows), so it writes an ASCII name and Python renames it to NAME.
_TMP_NAME = "spor-istanbul-rezervasyon-botu-kisayol"

_PS_SCRIPT = r"""
$desktop = [Environment]::GetFolderPath('Desktop')
Remove-Item -LiteralPath (Join-Path $desktop ($env:IBB_LEGACY_NAME + '.lnk')) -ErrorAction SilentlyContinue
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop ($env:IBB_TMP_NAME + '.lnk')))
$lnk.TargetPath = $env:IBB_TARGET
$lnk.Arguments = '"' + $env:IBB_SCRIPT + '"'
$lnk.WorkingDirectory = $env:IBB_ROOT
$lnk.Description = $env:IBB_NAME
$lnk.Save()
Write-Output (Join-Path $desktop ($env:IBB_TMP_NAME + '.lnk'))
"""


def windows_shortcut() -> Path:
    env = dict(os.environ,
               IBB_NAME=NAME,
               IBB_TMP_NAME=_TMP_NAME,
               IBB_LEGACY_NAME=LEGACY_NAME,
               IBB_TARGET=str(ROOT / ".venv" / "Scripts" / "pythonw.exe"),
               IBB_SCRIPT=str(ROOT / "app.pyw"),
               IBB_ROOT=str(ROOT))
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
        input=_PS_SCRIPT, env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "PowerShell hata verdi")
    tmp = Path(out.stdout.strip().splitlines()[-1])
    final = tmp.with_name(f"{NAME}.lnk")
    os.replace(tmp, final)
    return final


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_launcher() -> Path:
    """A real double-clickable "Spor İstanbul Rezervasyon Botu.app" on the Desktop (built with osacompile).

    It starts the app in the background, so no Terminal window appears -- with a
    .command launcher, closing that Terminal window would also kill the bot. Falls back
    to a .command file if osacompile is unavailable.
    """
    import shutil
    import tempfile

    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        raise RuntimeError(f"Masaüstü klasörü yok: {desktop}")

    app = desktop / f"{NAME}.app"
    command = desktop / f"{NAME}.command"
    for legacy in (desktop / f"{LEGACY_NAME}.app", desktop / f"{LEGACY_NAME}.command"):
        if legacy in (app, command):
            continue
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
        else:
            legacy.unlink(missing_ok=True)
    shell = f"cd {shlex.quote(str(ROOT))} && nohup .venv/bin/python app.pyw >/dev/null 2>&1 &"
    script = f"do shell script {_applescript_string(shell)}\n"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False, encoding="utf-8") as f:
            f.write(script)
        if app.exists():
            shutil.rmtree(app)
        subprocess.run(["osacompile", "-o", str(app), f.name], check=True, capture_output=True, timeout=60)
        Path(f.name).unlink(missing_ok=True)
        command.unlink(missing_ok=True)  # replace an older .command launcher
        return app
    except Exception:
        command.write_text(
            "#!/bin/bash\n"
            f"cd {shlex.quote(str(ROOT))} || exit 1\n"
            'exec .venv/bin/python app.pyw "$@"\n',
            encoding="utf-8",
        )
        command.chmod(0o755)
        return command


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        if sys.platform == "win32":
            path = windows_shortcut()
        elif sys.platform == "darwin":
            path = macos_launcher()
        else:
            print("Bu platformda masaüstü kısayolu oluşturulmuyor; `python app.pyw` ile başlatın.")
            return 0
    except Exception as e:  # installers treat this as a warning
        print(f"UYARI: masaüstü kısayolu oluşturulamadı: {e}")
        return 1
    print(f"Masaüstü kısayolu: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
