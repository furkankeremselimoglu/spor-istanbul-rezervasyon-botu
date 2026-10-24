#!/bin/bash

# macOS launcher for the reservation bot
# This script should be at the project root
# Run: double-click or bash "Rezervasyon Botu.command"

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "HATA: .venv bulunamadi. Lutfen once install.command'i calistirin."
    read -p "Devam etmek icin Enter tuslayın..."
    exit 1
fi

.venv/bin/python app.pyw "$@"
