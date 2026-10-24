#!/bin/bash
# IBB Spor rezervasyon botu - tek satirlik kurulum (macOS).
# Kullanim: bash -c "$(curl -fsSL https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.sh)"
# CI icin: bash install.sh --ci
set -euo pipefail

main() {
    CI_ARG="${1:-}"
    API="${IBB_RELEASE_API:-https://api.github.com/repos/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/releases/latest}"
    DEST="${IBB_INSTALL_DIR:-$HOME/spor-istanbul-rezervasyon-botu}"

    echo "Surum bilgisi aliniyor..."
    release_json="$(curl -fsSL -H 'Accept: application/vnd.github+json' "$API")" || {
        echo "HATA: surum bilgisi alinamadi ($API)"; exit 1;
    }

    # shortcut: assumes one zip asset per release (true for make_release.py); if a 2nd asset is ever added, parse per-asset
    zip_field="$(printf '%s' "$release_json" | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -1)"
    zip_url="$(printf '%s' "$zip_field" | grep -o '"[^"]*\.zip"')"
    zip_url="${zip_url#\"}"
    zip_url="${zip_url%\"}"
    digest_field="$(printf '%s' "$release_json" | grep -o '"digest": *"sha256:[0-9a-f]*"' | head -1)"
    digest="$(printf '%s' "$digest_field" | grep -o 'sha256:[0-9a-f]*' | cut -d: -f2)"

    if [ -z "$zip_url" ] || ! printf '%s' "$digest" | grep -qE '^[0-9a-f]{64}$'; then
        echo "HATA: surum paketinin sha256 ozeti yok"
        exit 1
    fi

    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT

    echo "Indiriliyor: $zip_url"
    curl -fsSL -o "$tmp/release.zip" "$zip_url" || { echo "HATA: indirme basarisiz"; exit 1; }

    actual="$(shasum -a 256 "$tmp/release.zip" | awk '{print $1}')"
    if [ "$actual" != "$digest" ]; then
        echo "HATA: sha256 uyusmuyor"
        exit 1
    fi
    echo "Dogrulandi"

    unzip -q "$tmp/release.zip" -d "$tmp" || { echo "HATA: zip acilamadi"; exit 1; }
    SRC="$tmp/spor-istanbul-rezervasyon-botu"
    [ -d "$SRC" ] || { echo "HATA: paket icerigi beklenen klasorde degil"; exit 1; }

    echo "Kuruluyor: $DEST"
    mkdir -p "$DEST"
    cp -R "$SRC/." "$DEST/"

    cd "$DEST" && bash install.command "$CI_ARG"

    echo "Kurulum klasoru: $DEST"
}

main "$@"
