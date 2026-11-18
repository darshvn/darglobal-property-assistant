"""Scrape DarGlobal projects and Wasalt listings into SQLite.

Both sites sit behind bot protection (Imperva on darglobal.co.uk, Cloudflare on
wasalt.sa), so plain HTTP clients get a challenge page. We drive a real Chrome
through the agent-browser CLI instead, which renders the page normally.

Wasalt's robots.txt allows crawling and publishes the sitemaps we read.
"""

import argparse
import gzip
import json
import re
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "properties.db"
SITEMAP = "https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz"

# DarGlobal has no usable sitemap behind the WAF, so we list the sections the
# nav exposes. Press pages are discovered from the homepage at run time.
DARGLOBAL_PAGES = [
    "/", "/about", "/why-invest", "/projects", "/luxury-golf-communities",
    "/hospitality", "/commercial", "/exclusive-membership", "/tokenization",
    "/one-of-one", "/investor",
    "/projects/properties-in-uae", "/projects/properties-in-uae/villas",
    "/projects/properties-in-uk", "/projects/properties-in-uk/apartments",
    "/projects/properties-in-saudi-arabia",
    "/projects/properties-in-saudi-arabia/villas",
    "/projects/properties-in-saudi-arabia/apartments",
    "/projects/properties-in-oman", "/projects/properties-in-oman/apartments",
    "/projects/properties-in-oman/hotel-rooms",
    "/projects/properties-in-qatar", "/projects/properties-in-qatar/villas",
    "/projects/properties-in-spain", "/projects/properties-in-spain/villas",
    "/partners/aston-martin", "/partners/elie-saab", "/partners/fendi-casa",
    "/partners/lamborghini", "/partners/missoni", "/partners/mouawad",
    "/partners/pagani", "/partners/the-trump-organization",
    "/partners/w-hotels", "/partners/paramount",
    "/partners/marriott-residences-aida-oman",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  url TEXT PRIMARY KEY, source TEXT, title TEXT, text TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
  USING fts5(url UNINDEXED, title, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS listings (
  url TEXT PRIMARY KEY, title TEXT, deal TEXT, ptype TEXT, city TEXT,
  district TEXT, price_sar INTEGER, bedrooms INTEGER, area_sqm REAL,
  price_per_sqm REAL, description TEXT
);
"""


def browser(*args: str, timeout: int = 75) -> str:
    """Run one agent-browser command. Empty string on timeout.

    `open` blocks until the page settles, and a heavy listing page can sit
    there well past a minute, so a hang is normal rather than fatal — the
    caller retries or moves on.
    """
    try:
        out = subprocess.run(
            ["agent-browser", "--cdp", "9222", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout.strip()


DEAD = ("Error code: 404", "can’t seem to find the page",
        "can't seem to find the page")


def load_page(url: str, settle: float = 6.0, tries: int = 2) -> dict | None:
    """Navigate and return the rendered text.

    None means the page is gone or the WAF won. The href check matters: the
    CLI evaluates against the browser's active tab, so if anything else steals
    focus mid-run we would silently scrape the wrong page.
    """
    for attempt in range(tries):
        # `open` blocks until the network goes idle, and these pages keep
        # chattering, so we cap it short and read the DOM afterwards instead
        # of waiting for a "finished" that never comes.
        browser("open", url, timeout=20)
        time.sleep(settle * (attempt + 1))
        raw = browser(
            "eval",
            'JSON.stringify({u:location.href,t:document.title,'
            'x:document.body.innerText})',
        )
        try:
            page = json.loads(json.loads(raw))  # eval returns a JSON string
        except (json.JSONDecodeError, TypeError):
            continue
        text = page.get("x", "")
        if any(d in text for d in DEAD):
            return None                       # stale sitemap entry
        if page.get("u", "").split("?")[0].rstrip("/") != url.rstrip("/"):
            continue                          # wrong tab, try again
        if len(text) > 400:
            return page
    return None


def sitemap_urls(limit: int) -> list[str]:
    with urllib.request.urlopen(SITEMAP, timeout=60) as r:
        body = r.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    urls = re.findall(r"<loc>([^<]+)</loc>", body.decode("utf-8", "replace"))
    # Spread the sample across the file so we don't get 60 villas on one street.
    step = max(1, len(urls) // limit)
    return urls[::step][:limit]


NUM = r"[\d,]+(?:\.\d+)?"


def _num(s: str | None) -> float | None:
    return float(s.replace(",", "")) if s else None


CITIES = ("Riyadh", "Jeddah", "Makkah", "Madinah", "Dammam", "Al Khobar",
          "Khamis Mushayt", "Taif", "Abha", "Tabuk", "Buraydah", "Hail",
          "Jubail", "Yanbu", "Najran", "Jazan", "Qatif", "Dhahran")


def parse_listing(url: str, title: str, text: str) -> dict:
    """Pull structured fields out of the rendered listing text.

    ponytail: regex over innerText, not DOM selectors — the class names are
    hashed and change every deploy, the visible text doesn't. Move to the
    page's JSON payload if one ever becomes reachable.
    """
    flat = " ".join(text.split())

    # "<title> Al-Mahdiyah, West Riyadh, RiyadhMap view" -> district .. city.
    # Anchored on the string's end, since a search would start too far left.
    cut = flat.find("Map view")
    where = [p.strip() for p in flat[:cut].split(",")[-3:]] if cut > 0 else []
    if where:
        core = re.sub(r"\s+For (?:Sale|Rent)$", "", title).strip()
        where[0] = (where[0].split(core)[-1] if core and core in where[0]
                    else where[0])[-40:].strip()
    city = next((c for c in CITIES if where and c in where[-1]), None)
    if not city:
        city = next((c for c in CITIES
                     if re.search(rf"\bfor (?:Sale|Rent) in {c}\b", flat)), None)
    if not city:
        city = next((c for c in CITIES if c in flat), None)

    area = re.search(rf"({NUM})\s*(?:sqm|SQM)", flat)
    beds = (re.search(r"(\d+)\s*غرف نوم", flat)
            or re.search(r"has (\d+) bedrooms?", flat)
            or re.search(r"with (\d+) [Bb]edrooms?", flat))
    per_sqm = re.search(rf"({NUM})\s*/sqm", flat)
    price = (re.search(rf"Price:\s*({NUM})\s*SAR", flat)
             or re.search(rf"\b({NUM})\s+{NUM}\s*/sqm", flat))

    desc = re.search(r"Property Description (.+?)(?:REGA Information|$)", flat)
    return {
        "url": url,
        "title": title,
        "deal": "rent" if "/rent/" in url else "sale",
        "ptype": next((t for t in ("villa", "apartment", "land", "floor",
                                   "building", "office", "shop", "chalet")
                       if t in url.lower()), "property"),
        "city": city,
        "district": where[0] if where else None,
        "price_sar": int(_num(price.group(1))) if price else None,
        "bedrooms": int(beds.group(1)) if beds else None,
        "area_sqm": _num(area.group(1)) if area else None,
        "price_per_sqm": _num(per_sqm.group(1)) if per_sqm else None,
        "description": (desc.group(1)[:1200] if desc else flat[:1200]),
    }


def _selftest() -> None:
    title = "Villa 333 SQM Facing East on 20m Width Street For Sale"
    sample = (
        "Home Saudi Arabia Riyadh West Riyadh Villas for Sale in Al Mahdiyah "
        "Villa 333 SQM Facing East on 20m Width Street "
        "Al-Mahdiyah, West Riyadh, RiyadhMap view "
        "5 غرف نوم 333 sqm Property Description Villa for Sale in Al-Mahdiyah "
        "Land Area: 333 SQM The property has 5 bedrooms Price: 1600000 SAR "
        "REGA Information REGA Ad License No 7200795042"
    )
    r = parse_listing("https://wasalt.sa/en/property/sale/villa-x-1", title, sample)
    assert r["city"] == "Riyadh", r["city"]
    assert r["district"] == "Al-Mahdiyah", r["district"]
    assert r["price_sar"] == 1600000, r["price_sar"]
    assert r["bedrooms"] == 5, r["bedrooms"]
    assert r["area_sqm"] == 333, r["area_sqm"]
    assert r["ptype"] == "villa" and r["deal"] == "sale"
    print("parse_listing ok")


def save_doc(db, url, source, title, text):
    db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)",
               (url, source, title, text))
    db.execute("DELETE FROM docs_fts WHERE url=?", (url,))
    db.execute("INSERT INTO docs_fts (url,title,text) VALUES (?,?,?)",
               (url, title, text))


def run(site: str, limit: int, settle: float) -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)

    if site == "darglobal":
        targets = ["https://darglobal.co.uk" + p for p in DARGLOBAL_PAGES]
    else:
        targets = sitemap_urls(limit)

    ok = skipped = 0
    for i, url in enumerate(targets, 1):
        page = load_page(url, settle)
        if not page:
            skipped += 1
            print(f"[{i}/{len(targets)}] blocked/empty  {url}")
            continue
        title = (page["t"] or "").split("|")[0].strip()
        save_doc(db, url, site, title, page["x"])
        if site == "wasalt":
            row = parse_listing(url, title, page["x"])
            db.execute(
                "INSERT OR REPLACE INTO listings VALUES "
                "(:url,:title,:deal,:ptype,:city,:district,:price_sar,"
                ":bedrooms,:area_sqm,:price_per_sqm,:description)", row)
            print(f"[{i}/{len(targets)}] {row['city']} · {row['ptype']} · "
                  f"{row['price_sar']} SAR · {row['bedrooms']}bd")
        else:
            print(f"[{i}/{len(targets)}] {title} ({len(page['x'])} chars)")
        ok += 1
        db.commit()

    db.close()
    print(f"\n{site}: saved {ok}, skipped {skipped} -> {DB}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", choices=["wasalt", "darglobal"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--settle", type=float, default=6.0,
                    help="seconds to let the page render (also our rate limit)")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        run(a.site, a.limit, a.settle)
