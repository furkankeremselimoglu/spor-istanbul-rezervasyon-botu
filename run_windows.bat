@echo off
REM CLI runner for Windows: launch the bot with UTF-8 encoding support
REM For the GUI, use the Desktop shortcut created by install.bat

REM Set code page to UTF-8 for Turkish characters
chcp 65001 >nul

REM Change to the script's directory
cd /d "%~dp0"

REM Display usage information
echo.
echo ==================================
echo Spor Istanbul Rezervasyon Botu - CLI
echo ==================================
echo.
echo NORMAL KULLANIM: Desktop'taki "Spor Istanbul Rezervasyon Botu" kisayolunu cift tiklayin
echo.
echo Komut satiri secenekleri:
echo   run_windows.bat              Bot'u calistir (normal mod)
echo   run_windows.bat --dry-run    Secim yap ama Kaydet'e basilmaz
echo   run_windows.bat --once       Tek kontrol yap ve cik
echo.
echo Diger komutlar (komut satiriinda):
echo   .venv\Scripts\python bot.py probe     Tesisin haftalik seans tablosunu goster
echo   .venv\Scripts\python bot.py watch    Seans degisikliklerini kaydet
echo   .venv\Scripts\python bot.py reservations   Mevcut rezervasyonlarimi listele
echo.

REM Run the bot with all passed arguments
.venv\Scripts\python bot.py run %*

pause
