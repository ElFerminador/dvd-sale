#!/usr/bin/env python3
"""Photos of DVD/CD fronts and backs -> items.csv (with metadata) -> static sale page.

    python sale.py scan            pair photos, read barcodes, look up metadata, write items.csv
    python sale.py site            build docs/ (index.html, images, items.json) from items.csv
    python sale.py all             both

Edit items.csv by hand between `scan` and `site`; existing rows are never overwritten
unless you pass --redo <id> or --force.
"""
import argparse
import base64
import csv
import difflib
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps

try:  # iPhone photos are often HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
CFG = json.loads((ROOT / "config.json").read_text())
KIND = CFG["kind"]  # "dvd" | "cd"
PHOTOS = ROOT / CFG.get("photos_dir", "photos")
OUT = ROOT / CFG.get("output_dir", "docs")
CSV_PATH = ROOT / "items.csv"
CACHE_PATH = ROOT / ".cache.json"
UA = "media-sale/0.1 (fermin@fermin.ch)"
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic"}
FIELDS = ["id", "front", "back", "barcode", "title", "year", "runtime_min", "genre",
          "description_de", "description_en", "language", "region", "sort_title", "source", "check",
          "remarks_de", "remarks_en"]

GENRES = {  # canonical English name (stored in the CSV) -> German
    "dvd": {"Action": "Action", "Adventure": "Abenteuer", "Animation": "Animation", "Comedy": "Komödie",
            "Crime": "Krimi", "Documentary": "Dokumentarfilm", "Drama": "Drama", "Family": "Familie",
            "Fantasy": "Fantasy", "History": "Historie", "Horror": "Horror", "Music": "Musik/Konzert",
            "Mystery": "Mystery", "Romance": "Liebesfilm", "Science Fiction": "Science-Fiction",
            "Thriller": "Thriller", "War": "Krieg", "Western": "Western", "TV Series": "Serie", "TV Movie": "TV-Film"},
    "cd": {"Pop": "Pop", "Rock": "Rock", "Dance & Electronic": "Dance & Electronic", "Hip-Hop & R&B": "Hip-Hop & R&B",
           "Jazz & Blues": "Jazz & Blues", "Classical": "Klassik", "Soundtrack": "Soundtrack",
           "Folk & Country": "Folk & Country", "Schlager": "Schlager", "Compilation": "Sampler", "Other": "Andere"},
}


LANG_NAMES = {  # code -> (de, en)
    "en": ("Englisch", "English"), "de": ("Deutsch", "German"), "fr": ("Französisch", "French"),
    "es": ("Spanisch", "Spanish"), "it": ("Italienisch", "Italian"), "ja": ("Japanisch", "Japanese"),
    "zh": ("Chinesisch", "Chinese"), "nl": ("Niederländisch", "Dutch"), "pt": ("Portugiesisch", "Portuguese"),
    "sv": ("Schwedisch", "Swedish"), "da": ("Dänisch", "Danish"), "ko": ("Koreanisch", "Korean"),
    "ru": ("Russisch", "Russian"), "pl": ("Polnisch", "Polish"), "cs": ("Tschechisch", "Czech"),
    "hu": ("Ungarisch", "Hungarian"), "th": ("Thailändisch", "Thai"), "tr": ("Türkisch", "Turkish"),
    "el": ("Griechisch", "Greek"), "he": ("Hebräisch", "Hebrew"), "no": ("Norwegisch", "Norwegian"),
    "fi": ("Finnisch", "Finnish"), "hi": ("Hindi", "Hindi"),
}

# ---------------------------------------------------------------- cache / http

_cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


_cache_lock = threading.Lock()


def cache_put(key, value):
    with _cache_lock:
        _cache[key] = value
        CACHE_PATH.write_text(json.dumps(_cache, indent=1, ensure_ascii=False))


def cached(key, fn):
    if key not in _cache:
        cache_put(key, fn())  # fn runs outside the lock; two threads may rarely do the same lookup twice
    return _cache[key]


_next_slot = {}
_slot_lock = threading.Lock()


def get(url, host_delay=0.0, **kw):
    host = url.split("/")[2]
    with _slot_lock:  # reserve the next allowed time slot for this host, so threads queue up politely
        now = time.time()
        start = max(now, _next_slot.get(host, 0))
        _next_slot[host] = start + host_delay
    if start > now:
        time.sleep(start - now)
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {})["User-Agent"] = UA
    return requests.get(url, **kw)


# ---------------------------------------------------------------- photos

IMPORTED = re.compile(r"^(\d{3,})(front|back)\.[a-z0-9]+$", re.I)  # 001front.jpg / 001back.jpg: already imported


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.stem)]


def list_photos():
    """New photos in file name order (IMG_1999 < IMG_2000), as front, back, front, back… Imported photos have been
    renamed to <nr>front/<nr>back and are not listed. Name a re-taken photo like IMG_1413b.jpeg to slot it in
    right behind IMG_1413.jpeg."""
    files = [p for p in PHOTOS.iterdir()
             if p.suffix.lower() in IMG_EXT and not p.name.startswith(".") and not IMPORTED.match(p.name)]
    return sorted(files, key=natural_key)


def load_image(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def ean_ok(code):
    if not (code.isdigit() and len(code) in (12, 13)):
        return False
    d = [int(c) for c in code.zfill(13)]
    return (10 - sum(d[i] * (3 if i % 2 else 1) for i in range(12)) % 10) % 10 == d[12]


def read_barcode(path):
    return cached(f"bc2:{path.name}:{path.stat().st_size}", lambda: _read_barcode(path))


def _read_barcode(path):
    import numpy as np
    rgb = np.array(load_image(path))
    try:  # zxing-cpp: more robust (rotation, glare); optional because it needs a prebuilt wheel
        import zxingcpp
        for r in zxingcpp.read_barcodes(rgb, formats=zxingcpp.BarcodeFormat.EAN13 | zxingcpp.BarcodeFormat.UPCA,
                                        try_rotate=True, try_downscale=True):
            if ean_ok(r.text):
                return r.text
    except ImportError:
        pass
    det = cv2.barcode.BarcodeDetector()
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    for scale in (1.0, 0.5, 0.35):
        im = img if scale == 1.0 else cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
            r = im if rot is None else cv2.rotate(im, rot)
            ok, info, _typ, _pts = det.detectAndDecodeWithType(r)
            if ok:
                for code in info:
                    if ean_ok(code or ""):
                        return code
    return ""


def save_variants(src, item_id, suffix):
    img = load_image(src)
    d = OUT / "img"
    d.mkdir(parents=True, exist_ok=True)
    for name, size, q in (("t", 480, 78), ("l", 1800, 82)):
        copy = img.copy()
        copy.thumbnail((size, size))
        copy.save(d / f"{item_id}-{suffix}-{name}.jpg", quality=q, optimize=True, progressive=True)


# ---------------------------------------------------------------- lookups

def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


ARTICLES = re.compile(r"^(the|a|an|der|die|das|le|la|les)\s+", re.I)


def sort_key(title):
    """Sort 'The Amazing Spider-Man 2' under A; leading quotes/punctuation are ignored too."""
    t = title.strip().lstrip("\"'“”‘’«»([¡¿.-").strip()
    return ARTICLES.sub("", t) or t


def clean_title(t):
    t = re.sub(r"\s*[\[(][^\])]*[\])]", "", t)  # (Director's Cut), [DVD], (Unrated) …
    t = re.sub(r"\b(dvd|widescreen|full screen|fullscreen|special edition|collector'?s edition|director'?s cut|unrated)\b",
               "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -:,")


def upc_title(code):
    def fetch():
        r = get("https://api.upcitemdb.com/prod/trial/lookup", params={"upc": code}, host_delay=12)
        if r.status_code == 429:
            raise RuntimeError("upcitemdb daily limit reached; run `scan` again tomorrow")
        data = r.json()
        return data["items"][0]["title"] if data.get("items") else ""
    return cached(f"upc:{code}", fetch)


def tmdb(path, **params):
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        raise RuntimeError("TMDB_API_KEY missing in .env")
    params["api_key"] = key
    r = get(f"https://api.themoviedb.org/3/{path}", params=params, host_delay=0.05)
    r.raise_for_status()
    return r.json()


def tmdb_details(mid):
    en = cached(f"tmdb:{mid}:en", lambda: tmdb(f"movie/{mid}", language="en-US"))
    de = cached(f"tmdb:{mid}:de", lambda: tmdb(f"movie/{mid}", language="de-DE"))
    return en, de


def tmdb_movie(title, year=None, back_path=None):
    """Return (row-fields or None, note). An empty note means a confident match.

    The most similar title wins, not TMDB's first hit ('Spider-Man 2' vs. 'The Amazing Spider-Man 2').
    If several films share the best title (remakes, same name), the back cover decides via
    runtime/year when a vision key is available; otherwise the first is taken and flagged.
    """
    query = clean_title(title)
    candidates = [query]
    if ":" in query:  # 'Living It Up: La Gran Vida' -> also try 'Living It Up'
        candidates.append(query.split(":")[0].strip())
    results = []
    for q in candidates:
        results = cached(f"tmdb-search:{q}", lambda: tmdb("search/movie", query=q, language="en-US")["results"])
        if results:
            query = q
            break
    if not results:
        return None, ""
    qn = norm(query)
    scored = [(difflib.SequenceMatcher(None, qn, norm(r["title"])).ratio(), r) for r in results]
    best = max(sim for sim, _ in scored)
    cands = [r for sim, r in scored if sim >= best - 0.02][:4]  # stable: keeps TMDB popularity order
    note = "" if best >= 0.85 else f"unsicherer TMDB-Treffer für '{query}'"

    chosen = cands[0]
    if len(cands) > 1:
        years = ", ".join((c.get("release_date") or "?")[:4] for c in cands)
        v = vision_read(back_path) if back_path else None
        pick = None
        if v:
            rt, yr = v.get("runtime_min"), v.get("year")
            if rt:
                near = [(abs((tmdb_details(c["id"])[0].get("runtime") or 0) - rt), i) for i, c in enumerate(cands)]
                d, i = min(near)
                if d <= 4:
                    pick = cands[i]
            if not pick and yr:
                pick = next((c for c in cands if abs(int((c.get("release_date") or "0")[:4] or 0) - yr) <= 1), None)
        if pick:
            chosen = pick
        else:
            note = f"mehrere Filme mit gleichem Titel ({years}) – bitte Jahr prüfen"

    en, de = tmdb_details(chosen["id"])
    fields = {
        "title": en["title"],
        "year": (en.get("release_date") or "")[:4],
        "runtime_min": en.get("runtime") or "",
        "genre": ", ".join([g["name"] for g in en.get("genres", []) if g["name"] in GENRES["dvd"]][:3]),
        "description_en": en.get("overview", ""),
        "description_de": de.get("overview", "") or en.get("overview", ""),
    }
    return fields, note


def musicbrainz(code):
    def fetch():
        r = get("https://musicbrainz.org/ws/2/release", params={"query": f"barcode:{code}", "fmt": "json", "limit": 5},
                host_delay=1.1)
        rels = r.json().get("releases", [])
        if not rels:
            return None
        rel = rels[0]
        d = get(f"https://musicbrainz.org/ws/2/release/{rel['id']}", params={"inc": "recordings+artist-credits", "fmt": "json"},
                host_delay=1.1).json()
        tracks = [t["title"] for m in d.get("media", []) for t in m.get("tracks", [])]
        return {"title": d["title"], "artist": "".join(a["name"] + a.get("joinphrase", "") for a in d["artist-credit"]),
                "year": (d.get("date") or "")[:4], "tracks": tracks}
    return cached(f"mb:{code}", fetch)


def vision_read(back_path, side="back"):
    """Fallback: let Claude read a cover photo (the back, or the front if the back is missing). Needs ANTHROPIC_API_KEY."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    ckey = f"vision:{KIND}:{back_path.name}:{back_path.stat().st_size}"  # (file name is unique per photo)
    if ckey in _cache:
        return _cache[ckey]
    import anthropic
    img = load_image(back_path)
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()
    what = "DVD" if KIND == "dvd" else "CD"
    prompt = (f"This is the {side} cover of a {what}. Reply with ONLY a JSON object: "
              '{"title": str, "artist": str|null, "year": int|null, "runtime_min": int|null, '
              '"languages": [ISO 639-1 codes of audio tracks], "region": str|null}. '
              "Use null for anything you cannot read. Do not guess.")
    msg = anthropic.Anthropic(max_retries=6).messages.create(
        model=os.environ.get("VISION_MODEL", "claude-sonnet-5-5"), max_tokens=4000,  # thinking is on by default and counts against this
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": prompt}]}])
    try:
        text = next(b.text for b in msg.content if b.type == "text")
        result = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (StopIteration, ValueError):  # no usable answer: caller falls back to manual check
        return None
    cache_put(ckey, result)  # only successes are cached
    return result


def parse_region(text):
    """'2 (PAL)' -> '2', '2+4' -> '2,4', 'U.S. and Canada (NTSC)' -> '1', 'ALL' -> '0' (free). '' if unclear.
    A bare 'PAL' is not enough to say which region, so it stays unclear."""
    t = str(text or "").lower()
    if not t or t == "none":
        return ""
    digits = sorted(set(re.findall(r"(?<!\d)[0-6](?!\d)", t)))
    if digits and "0" not in digits:
        return ",".join(digits)
    if re.search(r"free|\ball\b|worldwide", t) or digits == ["0"]:
        return "0"
    if re.search(r"u\.?s\.?a?|united states|canada|ntsc", t):
        return "1"
    return ""


def apply_vision_hints(row, v, notes):
    """Region and (only for non-region-1 discs) the audio languages the back cover states."""
    reg = parse_region(v.get("region"))
    if reg:
        row["region"] = reg
    langs = v.get("languages") or []
    if langs and reg and reg != "1":  # PAL/other-region discs: the stated audio languages are trusted
        row["language"] = ",".join(langs)
    elif langs and "en" not in langs:  # region 1 without English audio is unusual: keep default, ask for a look
        notes.append(f"Sprache laut Rückseite: {','.join(langs)} – prüfen (Standard en)")


UPC_OFF = False  # set when UPCitemdb's free daily limit is hit; rest of the run uses the photo analysis


def lookup(row, front_path, back_path):
    global UPC_OFF
    code = row["barcode"]
    notes, source = [], ""
    fields = None
    shot, side = (back_path, "back") if back_path else (front_path, "front")  # photo the vision fallback reads

    if KIND == "dvd":
        title = ""
        if code and not UPC_OFF:
            try:
                title = upc_title(code)
            except RuntimeError:
                UPC_OFF = True
                print("  UPCitemdb-Tageslimit erreicht – der Rest läuft über die Bildanalyse (Claude).")
        if title:
            try:
                fields, note = tmdb_movie(title, back_path=back_path)
                source = "upc+tmdb"
                if note:
                    notes.append(note)
            except RuntimeError as e:
                notes.append(str(e))
        if not fields and title and not os.environ.get("ANTHROPIC_API_KEY"):
            # no TMDB data, no vision: at least keep the title the barcode database knows
            fields = {"title": clean_title(title), "year": "", "runtime_min": "",
                      "description_en": "", "description_de": ""}
            source = "upc"
        if not fields:
            v = vision_read(shot, side)
            if v and v.get("title"):
                fields, note = tmdb_movie(v["title"], v.get("year"), back_path)
                source = "vision+tmdb"
                if note:
                    notes.append(note)
                if not fields:
                    fields = {"title": v["title"], "year": v.get("year") or "", "runtime_min": v.get("runtime_min") or "",
                              "description_en": "", "description_de": ""}
                    source = "vision"
                    notes.append("nur per Bildanalyse erkannt, nicht in TMDB gefunden – bitte prüfen")
                apply_vision_hints(row, v, notes)
    else:
        mb = musicbrainz(code) if code else None
        if mb:
            tl = "; ".join(f"{i}. {t}" for i, t in enumerate(mb["tracks"], 1))
            fields = {"title": f"{mb['artist']} – {mb['title']}", "year": mb["year"], "runtime_min": "",
                      "description_en": f"Tracklist: {tl}", "description_de": f"Titelliste: {tl}"}
            source = "musicbrainz"
        else:
            v = vision_read(shot, side)
            if v and v.get("title"):
                fields = {"title": f"{v.get('artist') or ''} – {v['title']}".strip(" –"), "year": v.get("year") or "",
                          "runtime_min": "", "description_en": "", "description_de": ""}
                source = "vision"
                notes.append("from vision, please verify")

    if fields:
        row.update(fields)
    else:
        notes.append("nothing found – fill in by hand")
    row["source"] = source
    row["check"] = "; ".join(notes)


# ---------------------------------------------------------------- scan

def read_rows():
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = [{k: (v or "") for k, v in r.items() if k} for r in csv.DictReader(f)]
    for r in rows:
        r["id"] = r.get("id", "").strip()
        if r["id"].isdigit():
            r["id"] = r["id"].zfill(3)  # a spreadsheet may have turned 001 into 1
        for k in FIELDS:
            r.setdefault(k, "")
    return rows


def write_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


MISSING = {"front": ("Vorderseite fehlt", "Front cover photo missing"),
           "back": ("Rückseite fehlt", "Back cover photo missing")}


def pair_photos(photos):
    """Walk the photos as front, back, front, back… Fronts never carry a barcode, backs usually do.
    Returns [(front|None, back|None)]: a gap (forgotten photo) becomes an item with a missing side instead of
    shifting every following pair. Also returns human-readable notes for the gaps it had to guess."""
    print(f"{len(photos)} Fotos, ordne Vorder-/Rückseiten zu (Barcode-Erkennung, wird gemerkt)…", flush=True)
    codes = [bool(read_barcode(p)) for p in photos]
    n, i, pairs, issues, unreadable = len(photos), 0, [], [], 0
    while i < n:
        if codes[i]:  # a back where a front is expected
            pairs.append((None, photos[i]))
            issues.append(f"{photos[i].name}: Vorderseite fehlt")
            i += 1
        elif i + 1 >= n:
            pairs.append((photos[i], None))
            issues.append(f"{photos[i].name}: Rückseite fehlt")
            i += 1
        elif codes[i + 1]:
            pairs.append((photos[i], photos[i + 1]))
            i += 2
        elif i + 2 < n and codes[i + 2]:  # next photo is a front again, so this item has no back
            pairs.append((photos[i], None))
            issues.append(f"{photos[i].name}: Rückseite fehlt")
            i += 1
        else:  # back without a readable barcode
            pairs.append((photos[i], photos[i + 1]))
            unreadable += 1
            i += 2
    if unreadable:
        print(f"  Hinweis: bei {unreadable} Rückseite(n) wurde kein Barcode erkannt; Titel kommt dort aus der Bildanalyse.")
    if issues:
        print(f"  {len(issues)} Lücke(n) in den Fotos, als Bemerkung eingetragen:")
        for m in issues:
            print("   !", m)
    return pairs, dict(i.split(": ", 1) for i in issues)


def canonical(item_id, side, name):
    ext = Path(name).suffix.lower()
    return f"{item_id}{side}{'.jpg' if ext in ('.jpg', '.jpeg') else ext}"


def rename_photo(old, new):
    """Rename a photo and carry its cached barcode/vision results over to the new file name."""
    os.rename(PHOTOS / old, PHOTOS / new)
    with open(ROOT / "renamed.log", "a", encoding="utf-8") as f:  # original name -> new name, for reference
        f.write(f"{old} -> {new}\n")
    with _cache_lock:
        for k in list(_cache):
            parts = k.split(":")
            if (parts[0] == "bc2" and parts[1] == old) or (parts[0] == "vision" and len(parts) > 3 and parts[2] == old):
                parts[1 if parts[0] == "bc2" else 2] = new
                _cache[":".join(parts)] = _cache.pop(k)


def cmd_scan(args):
    rows = read_rows()
    seen = set()
    for r in rows:  # ids are permanent and unique; repair duplicates/blanks from older runs
        if not r["id"] or r["id"] in seen:
            r["id"] = ""
        else:
            seen.add(r["id"])
    next_id = max([int(i) for i in seen if i.isdigit()] + [0]) + 1
    for r in rows:
        if not r["id"]:
            r["id"], next_id = f"{next_id:03d}", next_id + 1
    by_id = {r["id"]: r for r in rows}
    redo = {i.zfill(3) if i.isdigit() else i for i in (args.redo or [])}
    attached = set()

    # 1) rows from earlier runs: give their photos the canonical names (001front.jpg / 001back.jpg)
    renamed = 0
    for r in rows:
        for side in ("front", "back"):
            old = r[side]
            if old and not IMPORTED.match(old) and (PHOTOS / old).exists():
                new = canonical(r["id"], side, old)
                if not (PHOTOS / new).exists():
                    rename_photo(old, new)
                    r[side] = new
                    renamed += 1
    # 2) photos named by hand like 063back.jpg: attach to that row (a missing side delivered later) or adopt
    for p in sorted(PHOTOS.iterdir(), key=natural_key):
        m = IMPORTED.match(p.name)
        if not m or p.name.startswith("."):
            continue
        item_id, side = m.group(1).zfill(3), m.group(2).lower()
        r = by_id.get(item_id)
        if r is None:
            r = by_id[item_id] = {k: "" for k in FIELDS}
            r["id"] = item_id
            rows.append(r)
            attached.add(item_id)
        if r[side] != p.name:
            r[side] = p.name
            attached.add(item_id)
    # 3) new photos: pair them, give them the next numbers, rename
    pairs, gaps = pair_photos(list_photos())
    gap_of = {}
    for front, back in pairs:
        r = {k: "" for k in FIELDS}
        r["id"], next_id = f"{next_id:03d}", next_id + 1
        gap_of[r["id"]] = gaps.get((front or back).name, "")
        for side, p in (("front", front), ("back", back)):
            if p:
                r[side] = canonical(r["id"], side, p.name)
                rename_photo(p.name, r[side])
        rows.append(r)
        by_id[r["id"]] = r
        attached.add(r["id"])
    rows.sort(key=lambda r: (not r["id"].isdigit(), int(r["id"]) if r["id"].isdigit() else 0, r["id"]))
    if renamed or pairs:
        print(f"{renamed} bestehende Foto(s) umbenannt, {len(pairs)} neue(s) Paar(e) nummeriert")
    write_rows(rows)
    with _cache_lock:
        CACHE_PATH.write_text(json.dumps(_cache, indent=1, ensure_ascii=False))

    global UPC_OFF
    UPC_OFF = UPC_OFF or args.no_upc

    todo = []
    for r in rows:
        retry = "TMDB_API_KEY" in r.get("check", "")  # key was missing last time
        new_photo = r["id"] in attached and not (r["source"] == "manual" and r["title"])  # keep hand-edited rows
        if not (args.force or retry or r["id"] in redo or new_photo or not r["title"]):
            continue
        r["language"] = r["language"] or CFG.get("default_language", "en")
        r["region"] = r["region"] or CFG.get("default_region", "")
        for de, en in MISSING.values():  # auto remark for a missing side; drop it again once the photo exists
            if r["remarks_de"] == de:
                r["remarks_de"] = r["remarks_en"] = ""
        miss = "front" if not r["front"] else "back" if not r["back"] else ""
        if miss and not r["remarks_de"] and not r["remarks_en"]:
            r["remarks_de"], r["remarks_en"] = MISSING[miss]
        todo.append(r)

    def work(row):
        front = PHOTOS / row["front"] if row["front"] else None
        back = PHOTOS / row["back"] if row["back"] else None
        row["barcode"] = row.get("barcode") or (read_barcode(back) if back else "")
        try:
            lookup(row, front, back)
        except Exception as e:  # one bad row must not stop the other 600
            row["check"] = f"error: {e}"
        gap = gap_of.get(row["id"], "")
        if gap:
            row["check"] = "; ".join(x for x in (row["check"], f"Paarung automatisch erkannt ({gap}) – bitte prüfen") if x)
        return row

    print(f"{len(todo)} zu bearbeiten, {len(rows) - len(todo)} bereits erledigt ({args.workers} parallel)")
    done = 0
    ex = ThreadPoolExecutor(max_workers=args.workers)
    try:
        for fut in as_completed([ex.submit(work, r) for r in todo]):
            row = fut.result()
            done += 1
            print(f"[{done}/{len(todo)}] {row['id']} {row['barcode'] or '(no barcode)':14} "
                  f"{row['title'] or '???':45} {row['check']}", flush=True)
            write_rows(rows)  # incremental save: an abort loses nothing
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        write_rows(rows)
        sys.exit("\nAbgebrochen. Bisherige Ergebnisse sind gespeichert, ./run.sh macht dort weiter.")
    ex.shutdown()
    write_rows(rows)
    flagged = sum(1 for r in rows if r["check"])
    print(f"\n{len(rows)} items ({len(todo)} bearbeitet), {flagged} flagged in column 'check' -> {CSV_PATH.name}")


def translate_remarks(rows):
    """Fill the empty one of remarks_de / remarks_en from the other (Claude). Edited cells are never overwritten."""
    todo = {r["id"]: (r["remarks_de"], "en") for r in rows
            if (r.get("remarks_de") or "").strip() and not (r.get("remarks_en") or "").strip()}
    todo.update({r["id"]: (r["remarks_en"], "de") for r in rows
                 if (r.get("remarks_en") or "").strip() and not (r.get("remarks_de") or "").strip()})
    if not todo:
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{len(todo)} Bemerkung(en) ohne Übersetzung – ANTHROPIC_API_KEY fehlt, bitte in der CSV ergänzen.")
        return False
    import anthropic
    payload = {i: {"text": t, "to": lang} for i, (t, lang) in todo.items()}
    msg = anthropic.Anthropic().messages.create(
        model=os.environ.get("VISION_MODEL", "claude-sonnet-5-5"), max_tokens=4000,
        messages=[{"role": "user", "content":
                   "Translate these short condition notes of used DVDs/CDs for a sale listing. 'to' is the target "
                   "language (en or de). Reply with ONLY a JSON object {id: translation}.\n"
                   + json.dumps(payload, ensure_ascii=False)}])
    try:
        text = next(b.text for b in msg.content if b.type == "text")
        out = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (StopIteration, ValueError):
        print("Übersetzung der Bemerkungen fehlgeschlagen, bitte später erneut versuchen.")
        return False
    for r in rows:
        if r["id"] in out:
            r["remarks_en" if todo[r["id"]][1] == "en" else "remarks_de"] = out[r["id"]]
    print(f"{len(out)} Bemerkung(en) übersetzt (in items.csv eintragen, bitte prüfen).")
    return True


def fill_genres(rows):
    """Assign up to 3 genres from a fixed list to rows that have none (TMDB sets them for most DVDs already)."""
    allowed = GENRES.get(KIND)
    todo = [r for r in rows if not r["genre"] and r["title"]]
    if not allowed or not todo:
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{len(todo)} Titel ohne Genre – ANTHROPIC_API_KEY fehlt, bitte in der CSV ergänzen.")
        return False
    import anthropic
    client = anthropic.Anthropic(max_retries=6)
    names = list(allowed)

    def classify(batch):
        payload = [{"id": r["id"], "title": r["title"], "year": r["year"],
                    "description": (r["description_en"] or r["description_de"])[:300]} for r in batch]
        msg = client.messages.create(
            model=os.environ.get("VISION_MODEL", "claude-sonnet-5-5"), max_tokens=8000,
            messages=[{"role": "user", "content":
                       f"Assign 1 to 3 genres to each {'DVD' if KIND == 'dvd' else 'CD'} below, most fitting first, "
                       f"using ONLY these genre names exactly as written: {json.dumps(names)}. "
                       "Reply with ONLY a JSON object {id: [genres]}.\n" + json.dumps(payload, ensure_ascii=False)}])
        try:
            text = next(b.text for b in msg.content if b.type == "text")
            return json.loads(text[text.index("{"):text.rindex("}") + 1])
        except (StopIteration, ValueError):
            return {}
    batches = [todo[i:i + 40] for i in range(0, len(todo), 40)]
    by_id = {r["id"]: r for r in todo}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for result in ex.map(classify, batches):
            for item_id, genres in result.items():
                good = [g for g in genres if g in allowed][:3]
                if item_id in by_id and good:
                    by_id[item_id]["genre"] = ", ".join(good)
    filled = sum(1 for r in todo if r["genre"])
    print(f"{filled} von {len(todo)} Genres ergänzt (in items.csv prüf- und änderbar).")
    return filled > 0


def find_duplicates(items):
    """Group items that are probably the same film: same barcode, or same normalized title + year.
    Sets dup_count / dup_ids on every item; returns the groups with more than one member."""
    parent = {r["id"]: r["id"] for r in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    first = {}
    for r in items:
        keys = []
        if r.get("barcode"):
            keys.append(("bc", r["barcode"]))
        if r.get("title"):
            keys.append(("ty", norm(sort_key(r["title"])), r.get("year", "")))
        for k in keys:
            if k in first:
                parent[find(r["id"])] = find(first[k])
            else:
                first[k] = r["id"]
    groups = {}
    for r in items:
        groups.setdefault(find(r["id"]), []).append(r)
    for g in groups.values():
        for r in g:
            r["dup_count"] = len(g)
            r["dup_ids"] = ", ".join(x["id"] for x in g if x is not r)
    return [g for g in groups.values() if len(g) > 1]


def describe_from_photos(row):
    """For items TMDB/MusicBrainz knew nothing about: Claude reads both cover photos and writes a short neutral
    description in German and English (paraphrased, only what is printed on the cover)."""
    import anthropic
    content = []
    for name in (row["front"], row["back"]):
        if name:
            img = load_image(PHOTOS / name)
            img.thumbnail((1600, 1600))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(buf.getvalue()).decode()}})
    what = "DVD" if KIND == "dvd" else "CD"
    known = f"Known title: {row['title']}." if row["title"] else "The title is not known yet: read it from the cover."
    content.append({"type": "text", "text": (
        f"These are the cover photos of a {what} (front and/or back). {known} Write a neutral description for a "
        "second-hand sales listing: 2-3 sentences, in your own words (do not copy the blurb), based ONLY on what is "
        "printed on the cover (plot or content, for box sets which seasons/films/episodes it contains, notable extras). "
        "Do not invent anything. Reply with ONLY a JSON object: "
        '{"title": str|null, "year": int|null, "runtime_min": int|null, "description_de": str, "description_en": str}. '
        "Use null where unreadable.")})
    msg = anthropic.Anthropic(max_retries=6).messages.create(
        model=os.environ.get("VISION_MODEL", "claude-sonnet-5-5"), max_tokens=4000,
        messages=[{"role": "user", "content": content}])
    try:
        text = next(b.text for b in msg.content if b.type == "text")
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (StopIteration, ValueError):
        return None


def cmd_describe(args):
    rows = read_rows()
    todo = [r for r in rows if not (r["description_en"] or r["description_de"]) and (r["front"] or r["back"])]
    if args.redo:
        todo = [r for r in rows if r["id"] in set(args.redo)]
    print(f"{len(todo)} Titel ohne Beschreibung")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(describe_from_photos, r): r for r in todo}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                d = fut.result()
            except Exception as e:
                print(f"{r['id']} Fehler: {e}")
                continue
            if not d or not (d.get("description_de") or d.get("description_en")):
                print(f"{r['id']} {r['title'] or '(leer)'}: keine Antwort")
                continue
            if not r["title"] and d.get("title"):
                r["title"], r["source"] = d["title"], "vision"
            r["year"] = r["year"] or d.get("year") or ""
            r["runtime_min"] = r["runtime_min"] or d.get("runtime_min") or ""
            r["description_de"] = d.get("description_de", "")
            r["description_en"] = d.get("description_en", "")
            r["check"] = "; ".join(x for x in (r["check"], "Beschreibung per Bildanalyse erstellt – bitte prüfen") if x)
            print(f"{r['id']} {r['title'][:50]}")
            write_rows(rows)
    write_rows(rows)


def cmd_regions(args):
    """Apply region/language hints from already cached photo analyses to rows that still have the default
    region. Rows you changed by hand (region differs from the default) are left alone."""
    rows = read_rows()
    changed = 0
    default_lang = CFG.get("default_language", "en")
    for r in rows:
        if not r["back"]:
            continue
        if r["region"] != CFG.get("default_region", ""):
            # region was already set (earlier run): only complete the languages, never touch edited ones
            path = PHOTOS / r["back"]
            v = _cache.get(f"vision:{KIND}:{path.name}:{path.stat().st_size}") if path.exists() else None
            langs = (v or {}).get("languages") or []
            if r["language"] == default_lang and langs and set(langs) != {default_lang} and r["source"] != "manual":
                print(f"{r['id']} {r['title'][:45]:45} Sprache {r['language']} → {','.join(langs)}")
                r["language"] = ",".join(langs)
                r["check"] = "; ".join(x for x in r["check"].split("; ") if x and not x.startswith("Sprache laut Rückseite"))
                changed += 1
            continue
        path = PHOTOS / r["back"]
        v = _cache.get(f"vision:{KIND}:{path.name}:{path.stat().st_size}") if path.exists() else None
        if not v:
            continue
        notes = []
        before = (r["region"], r["language"])
        apply_vision_hints(r, v, notes)
        r["check"] = "; ".join([x for x in r["check"].split("; ") if x and not x.startswith("Sprache laut Rückseite")] + notes)
        if (r["region"], r["language"]) != before:
            changed += 1
            print(f"{r['id']} {r['title'][:45]:45} Region {before[0]} → {r['region']}, Sprache {before[1]} → {r['language']}")
    write_rows(rows)
    print(f"{changed} Zeilen angepasst")


def cmd_site(args):
    rows = read_rows()
    changed = translate_remarks(rows)
    changed = fill_genres(rows) or changed
    if changed:
        write_rows(rows)
    items = []
    (OUT / "img").mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "img" / ".sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for r in rows:
        for side, name in (("f", r["front"]), ("b", r["back"])):
            if not name:  # photo missing: the page shows a placeholder
                continue
            src = PHOTOS / name
            st = src.stat()
            sig = f"{st.st_size}:{int(st.st_mtime)}"  # not the name: renaming a photo must not redo its images
            done = all((OUT / "img" / f"{r['id']}-{side}-{v}.jpg").exists() for v in "tl")
            if args.force or not done or manifest.get(f"{r['id']}-{side}") != sig:
                save_variants(src, r["id"], side)
                manifest[f"{r['id']}-{side}"] = sig
        langs = [LANG_NAMES.get(c.strip(), (c.strip(), c.strip())) for c in r["language"].split(",") if c.strip()]
        genres = [g.strip() for g in r["genre"].split(",") if g.strip()]
        names = GENRES.get(KIND, {})
        items.append({**r,
                      "language_de": ", ".join(l[0] for l in langs),
                      "language_en": ", ".join(l[1] for l in langs),
                      "genres": genres,
                      "genre_de": ", ".join(names.get(g, g) for g in genres),
                      "genre_en": ", ".join(genres)})
    for r in items:
        r["sort_key"] = sort_key(r.get("sort_title") or r["title"])
    items.sort(key=lambda r: r["sort_key"].lower())
    counts = {}
    for r in items:
        for g in r["genres"]:
            counts[g] = counts.get(g, 0) + 1
    genre_opts = [{"en": g, "de": GENRES.get(KIND, {}).get(g, g), "n": n} for g, n in sorted(counts.items())]
    dups = find_duplicates(items)
    lines = [f"{g[0]['title']} ({g[0]['year']}) – {len(g)}×: IDs {', '.join(x['id'] for x in g)}"
             + ("  [gleicher Barcode]" if len({x['barcode'] for x in g}) == 1 and g[0]['barcode'] else "")
             for g in sorted(dups, key=lambda g: g[0]["sort_key"].lower())]
    (ROOT / "duplicates.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    print(f"{len(dups)} mehrfach vorhandene Titel" + (" → duplicates.txt" if dups else ""))
    manifest_path.write_text(json.dumps(manifest, indent=1))
    keep = {f"{r['id']}-{s}-{v}.jpg" for r in items for s in "fb" for v in "tl"}
    for f in (OUT / "img").glob("*.jpg"):  # drop images of items that no longer exist
        if f.name not in keep:
            f.unlink()
    (OUT / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    from jinja2 import Environment, FileSystemLoader
    html = Environment(loader=FileSystemLoader(ROOT), autoescape=True).get_template("template.html").render(
        items=items, cfg=CFG, kind=KIND, dup_total=len(dups),
        genre_opts=genre_opts)
    (OUT / "index.html").write_text(html)
    (OUT / ".nojekyll").write_text("")
    # plain-text list for the Ricardo/Tutti ad (no links, no HTML allowed there)
    lines = []
    for r in items:
        bits = [r["id"], r["title"], r["year"], f"{r['runtime_min']} min" if r["runtime_min"] else "", r["language_de"],
                r.get("remarks_de", "")]
        lines.append(" · ".join(b for b in bits if b))
    (OUT / "liste.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(items)} items -> {OUT}/index.html, liste.txt")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "site", "all", "regions", "describe"):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true", help="redo everything, overwriting manual edits")
        p.add_argument("--redo", nargs="*", help="ids to look up again")
        p.add_argument("--workers", type=int, default=6, help="parallel lookups (default 6)")
        p.add_argument("--no-upc", action="store_true", help="skip the barcode database, read titles from the photos only")
    args = ap.parse_args()
    if args.cmd == "regions":
        cmd_regions(args)
    if args.cmd == "describe":
        cmd_describe(args)
    if args.cmd in ("scan", "all"):
        cmd_scan(args)
    if args.cmd in ("site", "all"):
        cmd_site(args)


if __name__ == "__main__":
    main()
