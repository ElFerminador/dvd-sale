# dvd-sale

Fotos (Vorder-/Rückseite) → `items.csv` mit Metadaten → statische Verkaufsseite (GitHub Pages).

## Einrichten (einmal)
    cp .env.example .env     # TMDB_API_KEY (themoviedb.org → Einstellungen → API → "API Key") und optional ANTHROPIC_API_KEY eintragen
    ./run.sh                 # legt beim ersten Mal die Python-Umgebung an

## Ablauf
1. Neue Fotos in `photos/` legen: pro Titel zuerst die Vorderseite, dann die Rückseite (nach Dateiname sortiert).
2. `./run.sh --no-upc` (schnell, liest die Titel von den Rückseiten; ohne `--no-upc` wird zuerst die Barcode-Datenbank befragt).
   - Fotos werden fortlaufend nummeriert und umbenannt (`001front.jpg`, `001back.jpg`); die alten Namen stehen in `renamed.log`.
   - Fehlt eine Seite, entsteht der Eintrag trotzdem, mit Bemerkung "Rückseite/Vorderseite fehlt". Nachliefern: Datei als `028back.jpg` ablegen.
   - Danach `items.csv` prüfen (Spalte `check`), von Hand korrigieren und `./run.sh` erneut starten.
3. Die Seite entsteht in `docs/` (Grossansichten 1800 px, Vorschau 480 px), dazu `liste.txt` (Text für Inserate).
4. Veröffentlichen: `git add -A && git commit -m "Update" && git push` (GitHub Pages: Branch `main`, Ordner `/docs`).

## Weiteres
- `sale.py describe` schreibt Beschreibungen für Titel ohne Daten; `sale.py regions` übernimmt Region/Sprache aus den gespeicherten Bildanalysen; `site` ergänzt fehlende Genres und übersetzt leere Bemerkungs-Spalten.
- Spalten `remarks_de`/`remarks_en`: eine ausfüllen, die andere wird automatisch übersetzt. `sort_title` überschreibt die Sortierung eines Titels.
- Duplikate: `duplicates.txt` und gelbe Markierung auf der Seite.
- CD-Variante: Ordner kopieren, in `config.json` `"kind": "cd"` setzen.
