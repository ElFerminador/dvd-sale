#!/bin/bash
# Liest neue Fotos aus photos/, holt die Metadaten und baut die Seite neu.
# Bereits erfasste Titel (und deine Änderungen in items.csv) bleiben unangetastet.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Einmalige Einrichtung (Python-Umgebung)…"
  python3 -m venv .venv
  .venv/bin/pip install -q opencv-python-headless pillow pillow-heif requests python-dotenv anthropic jinja2
  .venv/bin/pip install -q --only-binary :all: zxing-cpp || echo "(zxing-cpp nicht verfügbar, es geht auch ohne)"
fi
if [ ! -f .env ]; then
  echo "Fehler: .env fehlt. Kopiere .env.example nach .env und trage die Keys ein." >&2
  exit 1
fi

export PYTHONWARNINGS=ignore
.venv/bin/python sale.py scan "$@"
.venv/bin/python sale.py site

echo
echo "Fertig. Vorschau: open docs/index.html"
