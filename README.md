# dvd-sale

Fotos (Vorder-/Rückseite) → `items.csv` mit Metadaten → statische Verkaufsseite (GitHub Pages).

## Einrichten (einmal)
    python3 -m venv .venv && .venv/bin/pip install opencv-python-headless pillow pillow-heif requests python-dotenv anthropic jinja2
    cp .env.example .env     # TMDB_API_KEY eintragen (themoviedb.org → Einstellungen → API → "API Key")

## Ablauf
1. Fotos in `photos/` legen. Pro Titel zwei Fotos: erst Vorderseite, dann Rückseite (Barcode sichtbar). Sortierung nach Aufnahmezeit.
2. `.venv/bin/python sale.py scan` – liest Barcodes, holt Daten (UPCitemdb → TMDB; bei CDs MusicBrainz), schreibt `items.csv`.
   Zeilen mit Eintrag in Spalte `check` prüfen/korrigieren (in Numbers/Excel öffnen, als UTF-8 CSV speichern).
   Bestehende Zeilen werden nicht überschrieben (`--redo 012 013` für einzelne, `--force` für alles).
3. `.venv/bin/python sale.py site` – erzeugt `docs/` (Seite, Bilder, `liste.txt` für das Inserat).
4. `git add docs items.csv && git commit && git push` – GitHub Pages: Settings → Pages → Branch `main`, Ordner `/docs`.

UPCitemdb (gratis) erlaubt ca. 100 Abfragen/Tag; bei Limit bricht `scan` ab, einfach am nächsten Tag erneut starten (Ergebnisse sind gecacht).
Optional: `ANTHROPIC_API_KEY` in `.env` → Fallback, der die Rückseite per Vision liest, wenn Barcode/Datenbank nichts liefert.
CD-Variante: Ordner kopieren, in `config.json` `"kind": "cd"` setzen.
