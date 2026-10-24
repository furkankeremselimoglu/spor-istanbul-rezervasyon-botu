#!/bin/bash
# IBB Spor rezervasyon botu - tek seferlik kurulum (macOS).
# Cift tiklayin (ilk seferde: sag tik > Ac). CI icin: bash install.command --ci
cd "$(dirname "$0")" || exit 1
CI_MODE=0
[ "$1" = "--ci" ] && CI_MODE=1

pause_unless_ci() { [ $CI_MODE -eq 1 ] || read -r -p "Kapatmak icin Enter'a basin..." _; }
fail() { echo; echo "HATA: $1"; pause_unless_ci; exit 1; }

# 1) Python 3.12+
PY=""
for c in python3.13 python3.12 python3 \
         /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c "import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)" >/dev/null 2>&1; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.12 veya ustu bulunamadi."
    echo "python.org'dan macOS icin Python 3.12+ indirip kurun, sonra bu dosyayi tekrar acin."
    echo "Homebrew kullaniyorsaniz: brew install python@3.13"
    [ $CI_MODE -eq 1 ] && exit 1
    open "https://www.python.org/downloads/macos/"
    pause_unless_ci; exit 1
fi
echo "Python bulundu: $PY"

# 2) venv + packages + browser
echo "[1/4] Python ortami hazirlaniyor..."
[ -x .venv/bin/python ] || "$PY" -m venv .venv || fail ".venv olusturulamadi"
echo "[2/4] Paketler yukleniyor..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || fail "paketler yuklenemedi"
echo "[3/4] Bot tarayicisi (Chromium) indiriliyor..."
.venv/bin/python -m playwright install chromium || fail "Chromium indirilemedi"
chmod +x "Rezervasyon Botu.command" install.command 2>/dev/null || true

if [ $CI_MODE -eq 1 ]; then
    echo "[CI] Kurulum tamam."
    .venv/bin/python bot.py version
    exit 0
fi

# 3) Desktop launcher + Gatekeeper quarantine removal for this folder
echo "[4/4] Masaustu kisayolu olusturuluyor..."
.venv/bin/python tools/make_shortcut.py
xattr -dr com.apple.quarantine "$PWD" 2>/dev/null || true

echo
echo "KURULUM TAMAM."
echo "Masaustundeki 'Spor Istanbul Rezervasyon Botu' dosyasina cift tiklayarak uygulamayi acin."
echo "Hesap sekmesinden TC ve sifrenizi girin, Hedefler sekmesinden aradiginiz seanslari secin."
.venv/bin/python bot.py version
echo
read -r -p "Uygulama simdi acilsin mi? [E/h] " ans
case "$ans" in [hH]*) ;; *) (.venv/bin/python app.pyw >/dev/null 2>&1 &) ;; esac
