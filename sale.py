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
import time
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
FIELDS = ["id", "front", "back", "barcode", "title", "year", "runtime_min",
          "description_de", "description_en", "language", "region", "sort_title", "source", "check"]

LANG_NAMES = {  # code -> (de, en)
    "en": ("Englisch", "English"), "de": ("Deutsch", "German"), "fr": ("Französisch", "French"),
    "es": ("Spanisch", "Spanish"), "it": ("Italienisch", "Italian"), "ja": ("Japanisch", "Japanese"),
}

# ---------------------------------------------------------------- cache / http

_cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def cached(key, fn):
    if key not in _cache:
        _cache[key] = fn()
        CACHE_PATH.write_text(json.dumps(_cache, indent=1, ensure_ascii=False))
    return _cache[key]


_last_call = {}


def get(url, host_delay=0.0, **kw):
    host = url.split("/")[2]
    wait = host_delay - (time.time() - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {})["User-Agent"] = UA
    return requests.get(url, **kw)


# ---------------------------------------------------------------- photos

def list_photos():
    files = [p for p in PHOTOS.iterdir() if p.suffix.lower() in IMG_EXT and not p.name.startswith(".")]

    def key(p):
        try:
            exif = Image.open(p).getexif()
            ts = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132) or ""
        except Exception:
            ts = ""
        return (ts, p.name)
    # Capture time first, filename as tie-breaker. If no EXIF (e.g. screenshots), filename order.
    return sorted(files, key=key)


def load_image(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def read_barcode(path):
    det = cv2.barcode.BarcodeDetector()
    img = cv2.cvtColor(__import__("numpy").array(load_image(path)), cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    for scale in (1.0, 0.5, 0.35):
        im = img if scale == 1.0 else cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
            r = im if rot is None else cv2.rotate(im, rot)
            ok, info, _typ, _pts = det.detectAndDecodeWithType(r)
            if ok:
                for code in info:
                    if code and code.isdigit() and len(code) in (12, 13):
                        return code
    return ""


def save_variants(src, item_id, suffix):
    img = load_image(src)
    d = OUT / "img"
    d.mkdir(parents=True, exist_ok=True)
    for name, size, q in (("t", 480, 78), ("l", 1400, 82)):
        copy = img.copy()
        copy.thumbnail((size, size))
        copy.save(d / f"{item_id}-{suffix}-{name}.jpg", quality=q, optimize=True, progressive=True)


# ---------------------------------------------------------------- lookups

def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


ARTICLES = re.compile(r"^(the|der|die|das)\s+", re.I)


def sort_key(title):
    """Sort 'The Amazing Spider-Man 2' under A; leading quotes/punctuation are ignored too."""
    t = title.strip().lstrip("\"'“”‘’«»([¡¿.-").strip()
    return ARTICLES.sub("", t) or t


def clean_title(t):
    t = re.sub(r"[\[\(][^\])]*(dvd|widescreen|fullscreen|edition|blu-?ray|special)[^\])]*[\]\)]", "", t, flags=re.I)
    t = re.sub(r"\b(dvd|widescreen|full screen|fullscreen|special edition|collector'?s edition)\b", "", t, flags=re.I)
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
    params = {"query": query, "language": "en-US"}
    if year:
        params["year"] = year
    results = cached(f"tmdb-search:{query}:{year}", lambda: tmdb("search/movie", **params)["results"])
    if not results:
        return None, ""
    qn = norm(query)
    scored = [(difflib.SequenceMatcher(None, qn, norm(r["title"])).ratio(), r) for r in results]
    best = max(sim for sim, _ in scored)
    cands = [r for sim, r in scored if sim >= best - 0.02][:4]  # stable: keeps TMDB popularity order
    note = "" if best >= 0.85 else f"unsicherer TMDB-Treffer für '{query}'"

    chosen = cands[0]
    if len(cands) > 1 and not year:
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


def vision_read(back_path):
    """Fallback: let Claude read the back cover. Needs ANTHROPIC_API_KEY."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    img = load_image(back_path)
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()
    what = "DVD" if KIND == "dvd" else "CD"
    prompt = (f"This is the back cover of a {what}. Reply with ONLY a JSON object: "
              '{"title": str, "artist": str|null, "year": int|null, "runtime_min": int|null, '
              '"languages": [ISO 639-1 codes of audio tracks], "region": str|null}. '
              "Use null for anything you cannot read. Do not guess.")
    msg = anthropic.Anthropic().messages.create(
        model=os.environ.get("VISION_MODEL", "claude-sonnet-5-5"), max_tokens=4000,  # thinking is on by default and counts against this
        
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": prompt}]}])
    try:
        text = next(b.text for b in msg.content if b.type == "text")
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (StopIteration, ValueError):  # no usable answer: caller falls back to manual check
        return None


def lookup(row, back_path):
    code = row["barcode"]
    notes, source = [], ""
    fields = None

    if KIND == "dvd":
        title = upc_title(code) if code else ""
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
            v = vision_read(back_path)
            if v and v.get("title"):
                fields, note = tmdb_movie(v["title"], v.get("year"), back_path)
                source = "vision+tmdb"
                if note:
                    notes.append(note)
                if not fields:
                    fields = {"title": v["title"], "year": v.get("year") or "", "runtime_min": v.get("runtime_min") or "",
                              "description_en": "", "description_de": ""}
                    source = "vision"
                if v.get("languages"):
                    row["language"] = ",".join(v["languages"])
                notes.append("from vision, please verify")
    else:
        mb = musicbrainz(code) if code else None
        if mb:
            tl = "; ".join(f"{i}. {t}" for i, t in enumerate(mb["tracks"], 1))
            fields = {"title": f"{mb['artist']} – {mb['title']}", "year": mb["year"], "runtime_min": "",
                      "description_en": f"Tracklist: {tl}", "description_de": f"Titelliste: {tl}"}
            source = "musicbrainz"
        else:
            v = vision_read(back_path)
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
        return list(csv.DictReader(f))


def write_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def cmd_scan(args):
    photos = list_photos()
    if len(photos) % 2:
        print(f"WARNING: odd number of photos ({len(photos)}); the pairing will be off after the missing one.")
    pairs = list(zip(photos[0::2], photos[1::2]))
    existing = read_rows()
    seen = set()
    for r in existing:  # ids are permanent and unique; repair duplicates/blanks from older runs
        if not r["id"] or r["id"] in seen:
            r["id"] = ""
        else:
            seen.add(r["id"])
    next_id = max([int(i) for i in seen if i.isdigit()] + [0]) + 1
    for r in existing:
        if not r["id"]:
            r["id"], next_id = f"{next_id:03d}", next_id + 1
    rows = {r["front"]: r for r in existing}
    redo = set(args.redo or [])
    out = []
    n_new = 0
    for i, (front, back) in enumerate(pairs, 1):
        row = rows.get(front.name)
        retry = "TMDB_API_KEY" in row.get("check", "") if row else False  # key was missing last time
        if row and not args.force and not retry and row["id"] not in redo and row.get("title"):
            out.append(row)
            continue
        row = row or {k: "" for k in FIELDS}
        if not row["id"]:
            row["id"], next_id = f"{next_id:03d}", next_id + 1
        row.update(front=front.name, back=back.name)
        row.setdefault("language", "")
        row["language"] = row["language"] or CFG.get("default_language", "en")
        row["region"] = row["region"] or CFG.get("default_region", "")
        row["barcode"] = row["barcode"] or read_barcode(back)
        try:
            lookup(row, back)
        except Exception as e:  # keep going; one bad row must not stop 400 others
            row["check"] = f"error: {e}"
            if "daily limit" in str(e):
                out.append(row)
                out.extend(rows[f.name] for f, _ in pairs[i:] if f.name in rows)
                write_rows(out)
                sys.exit(str(e))
        out.append(row)
        n_new += 1
        print(f"{row['id']} {row['barcode'] or '(no barcode)':14} {row['title'] or '???':45} {row['check']}")
        write_rows(out)  # incremental save
    write_rows(out)
    flagged = sum(1 for r in out if r["check"])
    print(f"\n{len(out)} items ({n_new} new), {flagged} flagged in column 'check' -> {CSV_PATH.name}")


# ---------------------------------------------------------------- site

def cmd_site(args):
    rows = read_rows()
    items = []
    (OUT / "img").mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "img" / ".sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for r in rows:
        for side, name in (("f", r["front"]), ("b", r["back"])):
            src = PHOTOS / name
            sig = f"{name}:{src.stat().st_size}"
            done = all((OUT / "img" / f"{r['id']}-{side}-{v}.jpg").exists() for v in "tl")
            if args.force or not done or manifest.get(f"{r['id']}-{side}") != sig:
                save_variants(src, r["id"], side)
                manifest[f"{r['id']}-{side}"] = sig
        langs = [LANG_NAMES.get(c.strip(), (c.strip(), c.strip())) for c in r["language"].split(",") if c.strip()]
        items.append({**r,
                      "language_de": ", ".join(l[0] for l in langs),
                      "language_en": ", ".join(l[1] for l in langs)})
    for r in items:
        r["sort_key"] = sort_key(r.get("sort_title") or r["title"])
    items.sort(key=lambda r: r["sort_key"].lower())
    manifest_path.write_text(json.dumps(manifest, indent=1))
    keep = {f"{r['id']}-{s}-{v}.jpg" for r in items for s in "fb" for v in "tl"}
    for f in (OUT / "img").glob("*.jpg"):  # drop images of items that no longer exist
        if f.name not in keep:
            f.unlink()
    (OUT / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    from jinja2 import Environment, FileSystemLoader
    html = Environment(loader=FileSystemLoader(ROOT), autoescape=True).get_template("template.html").render(
        items=items, cfg=CFG, kind=KIND)
    (OUT / "index.html").write_text(html)
    (OUT / ".nojekyll").write_text("")
    # plain-text list for the Ricardo/Tutti ad (no links, no HTML allowed there)
    lines = []
    for r in items:
        bits = [r["title"], r["year"], f"{r['runtime_min']} min" if r["runtime_min"] else "", r["language_de"]]
        lines.append(" · ".join(b for b in bits if b))
    (OUT / "liste.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(items)} items -> {OUT}/index.html, liste.txt")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "site", "all"):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true", help="redo everything, overwriting manual edits")
        p.add_argument("--redo", nargs="*", help="ids to look up again")
    args = ap.parse_args()
    if args.cmd in ("scan", "all"):
        cmd_scan(args)
    if args.cmd in ("site", "all"):
        cmd_site(args)


if __name__ == "__main__":
    main()
