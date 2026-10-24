@echo off
REM IBB Spor rezervasyon botu - tek seferlik kurulum (Windows 10/11).
REM Cift tiklayin. CI icin: install.bat --ci  (soru sormaz, kisayol olusturmaz)
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "CI_MODE=0"
if /i "%~1"=="--ci" set "CI_MODE=1"

REM ---- 1) Python 3.12+ bul ---------------------------------------------------
REM "py" launcher variants first (never ambiguous, never the Microsoft Store stub).
set "PY="
for %%C in ("py -3.12" "py -3.13" "py -3") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1 && set "PY=%%~C"
    )
)
if defined PY goto have_python

REM Bare "python" can resolve to the Microsoft Store stub
REM (...\Microsoft\WindowsApps\python.exe), which just opens the Store listing and
REM returns without ever running our version check. Walk every "python" match on
REM PATH (in PATH order), skip any under \Microsoft\WindowsApps\, and test the rest
REM directly by full path -- never the bare "python" word, since on some machines the
REM stub comes first in PATH and a bare call would hit it again regardless.
for /f "delims=" %%P in ('where python 2^>nul') do call :try_python_candidate "%%P"
if defined PY goto have_python

:need_install
echo.
echo Python 3.12 veya ustu bulunamadi.
if "%CI_MODE%"=="1" exit /b 1
where winget >nul 2>&1
if errorlevel 1 goto no_winget
echo Python, Windows paket yoneticisi winget ile kuruluyor...
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto no_winget

REM winget just installed Python, but THIS cmd.exe window captured its PATH before the
REM install, so it will not see the new python.exe without a fresh login session.
REM Probe the well-known install locations directly and continue in the same run,
REM instead of making the user close the window and try again.
set "PY="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set PY="%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set PY="%ProgramFiles%\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" set PY="%LocalAppData%\Programs\Python\Launcher\py.exe" -3.12
if defined PY goto have_python

echo.
echo Python kuruldu, ancak bu pencerede (oturum) henuz kullanilamiyor.
echo Lutfen Windows oturumunu KAPATIP TEKRAR ACIN (sadece bu pencereyi kapatmak
echo yetmez), sonra install.bat dosyasina tekrar cift tiklayin.
pause
exit /b 0

:no_winget
echo python.org indirme sayfasi aciliyor.
echo Python 3.12 kurulumunda ilk ekrandaki "Add python.exe to PATH" kutusunu isaretleyin,
echo kurulum bitince install.bat dosyasina tekrar cift tiklayin.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:have_python
echo Python bulundu: %PY%

REM ---- 2) Sanal ortam ve paketler ------------------------------------------
echo [1/4] Python ortami hazirlaniyor...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto fail
echo [2/4] Paketler yukleniyor...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
echo [3/4] Bot tarayicisi (Chromium) indiriliyor, birkac dakika surebilir...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail

if "%CI_MODE%"=="1" (
    echo [CI] Kurulum tamam.
    ".venv\Scripts\python.exe" bot.py version
    exit /b 0
)

REM ---- 3) Masaustu kisayolu ------------------------------------------------
echo [4/4] Masaustu kisayolu olusturuluyor...
".venv\Scripts\python.exe" tools\make_shortcut.py

echo.
echo KURULUM TAMAM.
echo Masaustundeki "Spor Istanbul Rezervasyon Botu" kisayoluna cift tiklayarak uygulamayi acin.
echo Hesap sekmesinden TC ve sifrenizi girin, Hedefler sekmesinden aradiginiz seanslari secin.
echo.
".venv\Scripts\python.exe" bot.py version
echo.
choice /c EH /n /m "Uygulama simdi acilsin mi? [E/H] "
if errorlevel 2 goto end
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.pyw"
:end
exit /b 0

:fail
echo.
echo HATA: kurulum tamamlanamadi. Yukaridaki mesaji kontrol edin; internet baglantisini
echo kontrol edip install.bat dosyasini tekrar calistirin.
if not "%CI_MODE%"=="1" pause
exit /b 1

:try_python_candidate
REM Called once per "where python" match, in PATH order (no delayed expansion needed:
REM this runs as a single statement per for /f iteration, not inside a parenthesized
REM block). %~1 is the full path to one candidate python.exe.
if defined PY goto :eof
echo %~1 | find /i "\Microsoft\WindowsApps\" >nul
if not errorlevel 1 goto :eof
"%~1" -c "import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1 && set PY="%~1"
goto :eof
