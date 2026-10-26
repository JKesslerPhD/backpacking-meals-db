#!/usr/bin/env python3
"""
Scrape nutrition facts + ingredients for backpacking meals into SQLite.

Brands: Mountain House, Backpacker's Pantry, Peak Refuel, Huel.

Pipeline (each stage is resumable; raw pages/images are cached in ./cache):
  discover  - list products per brand (Shopify products.json, Huel sitemap)
  fetch     - download product pages, parse with BeautifulSoup:
                ingredients, price, weight, servings, calories/protein text,
                and the nutrition-panel table when it is real HTML
                (Mountain House). Records label-image URLs for the rest.
  ocr       - for products whose panel is only an image (Backpacker's Pantry,
                Peak Refuel, Huel), run tesseract on the label image and parse it.
                Rows are flagged needs_review when the macros don't add up.
  report    - coverage summary

Usage:
  python scrape_foods.py all                  # everything
  python scrape_foods.py discover fetch --brand mountain_house
  python scrape_foods.py ocr --brand peak
  python scrape_foods.py report

Output: foods.db next to this script.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
DB_PATH = HERE / "foods.db"
CACHE = HERE / "cache"
DELAY = 1.0  # seconds between requests to the same site
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TrailPeachesFoodDB/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}

BRANDS = {
    "mountain_house": {"name": "Mountain House",       "base": "https://mountainhouse.com",       "kind": "shopify"},
    "backpackers_pantry": {"name": "Backpacker's Pantry", "base": "https://backpackerspantry.com", "kind": "shopify"},
    "peak": {"name": "Peak Refuel",                     "base": "https://peakrefuel.com",          "kind": "shopify"},
    "huel": {"name": "Huel",                            "base": "https://huel.com",                "kind": "huel"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY,
    brand         TEXT NOT NULL,
    brand_key     TEXT NOT NULL,
    name          TEXT NOT NULL,
    handle        TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    product_type  TEXT,
    kind          TEXT,              -- meal | bundle | gear | other  (only 'meal' is scraped)
    price_usd     REAL,
    net_weight_oz REAL,
    net_weight_g  REAL,
    weight_src    TEXT,              -- page | derived (servings x grams/serving) | shipping (fallback, includes packaging)
    ship_weight_g REAL,              -- Shopify variant shipping weight; NOT net weight, kept for fallback only
    format        TEXT,              -- pouch | pro-pak | can | classic | military | ...
    servings_per_container REAL,
    serving_desc  TEXT,
    serving_g     REAL,              -- grams in one serving, parsed from serving_desc like '1 cup (46g)'
    prep_water_oz REAL,
    prep_minutes  REAL,
    image_url     TEXT,
    label_urls    TEXT,              -- JSON list of nutrition-label image URLs (image-only brands)
    fetched_at    TEXT,
    UNIQUE(brand_key, handle)
);
CREATE TABLE IF NOT EXISTS nutrition (
    product_id    INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    basis         TEXT,              -- 'package' or 'serving' (what the numbers below are per)
    calories      REAL,
    total_fat_g   REAL,
    sat_fat_g     REAL,
    trans_fat_g   REAL,
    cholesterol_mg REAL,
    sodium_mg     REAL,
    carbs_g       REAL,
    fiber_g       REAL,
    sugars_g      REAL,
    added_sugars_g REAL,
    protein_g     REAL,
    vitamin_d_mcg REAL,
    calcium_mg    REAL,
    iron_mg       REAL,
    potassium_mg  REAL,
    source        TEXT,              -- html | text | ocr
    needs_review  INTEGER DEFAULT 0,
    review_note   TEXT,
    raw_text      TEXT
);
CREATE TABLE IF NOT EXISTS ingredients (
    product_id    INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    text          TEXT,
    allergens     TEXT
);
CREATE VIEW IF NOT EXISTS v_meals AS
SELECT p.id, p.brand, p.name, p.format, p.url, p.price_usd, p.net_weight_oz, p.net_weight_g, p.weight_src,
       p.servings_per_container, n.basis, n.calories, n.total_fat_g, n.carbs_g, n.protein_g,
       n.sodium_mg, n.fiber_g, n.sugars_g, n.source, n.needs_review,
       -- whole-package calories regardless of how the label states them
       CASE WHEN n.basis = 'serving' THEN n.calories * COALESCE(p.servings_per_container, 1) ELSE n.calories END AS calories_total,
       CASE WHEN p.net_weight_g > 0 THEN ROUND(
            (CASE WHEN n.basis = 'serving' THEN n.calories * COALESCE(p.servings_per_container, 1) ELSE n.calories END)
            / (p.net_weight_g / 28.3495), 1) END AS cal_per_oz,
       i.text AS ingredients
FROM products p
LEFT JOIN nutrition n ON n.product_id = p.id
LEFT JOIN ingredients i ON i.product_id = p.id
WHERE p.kind = 'meal';

DROP VIEW IF EXISTS v_optimizer;
-- One row per thing a hiker actually carries and eats, in package-independent "portion" terms.
--   pouch / single-serve meals  -> portion = the whole package
--   cans, tubs, bulk powders    -> portion = one serving (weight from the serving size in grams)
CREATE VIEW IF NOT EXISTS v_optimizer AS
SELECT id, brand, name, format, url, kind_of_portion,
       portion_label, portion_g, portion_kcal, portion_protein_g, portion_carbs_g, portion_fat_g,
       portion_fiber_g, portion_sodium_mg, portion_price_usd,
       portion_sugars_g, portion_satfat_g, portion_chol_mg, portion_calcium_mg, portion_iron_mg,
       portion_potassium_mg, portion_vitd_mcg,
       ROUND(portion_kcal / (portion_g / 28.3495), 1) AS kcal_per_oz,
       ROUND(portion_kcal / portion_g, 3) AS kcal_per_g,
       weight_src, needs_review
FROM (
  SELECT p.id, p.brand, p.name, p.format, p.url,
         CASE WHEN n.basis = 'package' OR COALESCE(p.servings_per_container, 1) <= 2 THEN 'package' ELSE 'serving' END AS kind_of_portion,
         CASE WHEN n.basis = 'package' OR COALESCE(p.servings_per_container, 1) <= 2 THEN 'whole package' ELSE COALESCE(p.serving_desc, '1 serving') END AS portion_label,
         CASE WHEN n.basis = 'package' OR COALESCE(p.servings_per_container, 1) <= 2 THEN
                   CASE WHEN n.basis = 'package' THEN p.net_weight_g ELSE p.serving_g * COALESCE(p.servings_per_container, 1) END
              ELSE p.serving_g END AS portion_g,
         -- nutrient columns are stored per `basis`; scale to the portion
         n.calories * k.f AS portion_kcal, n.protein_g * k.f AS portion_protein_g, n.carbs_g * k.f AS portion_carbs_g,
         n.total_fat_g * k.f AS portion_fat_g, n.fiber_g * k.f AS portion_fiber_g, n.sodium_mg * k.f AS portion_sodium_mg,
         n.sugars_g * k.f AS portion_sugars_g, n.sat_fat_g * k.f AS portion_satfat_g, n.cholesterol_mg * k.f AS portion_chol_mg,
         n.calcium_mg * k.f AS portion_calcium_mg, n.iron_mg * k.f AS portion_iron_mg,
         n.potassium_mg * k.f AS portion_potassium_mg, n.vitamin_d_mcg * k.f AS portion_vitd_mcg,
         p.price_usd * k.pf AS portion_price_usd, p.weight_src, n.needs_review
  FROM products p
  JOIN nutrition n ON n.product_id = p.id
  JOIN (SELECT p2.id AS pid,
               -- factor from stored basis to portion
               CASE WHEN n2.basis = 'package' THEN 1.0
                    WHEN COALESCE(p2.servings_per_container, 1) <= 2 THEN COALESCE(p2.servings_per_container, 1)
                    ELSE 1.0 END AS f,
               CASE WHEN n2.basis = 'package' OR COALESCE(p2.servings_per_container, 1) <= 2 THEN 1.0
                    ELSE 1.0 / p2.servings_per_container END AS pf
        FROM products p2 JOIN nutrition n2 ON n2.product_id = p2.id) k ON k.pid = p.id
  WHERE p.kind = 'meal' AND n.calories IS NOT NULL
)
WHERE portion_g > 0;
"""

# --------------------------------------------------------------------------- #
# HTTP + cache
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.headers.update(HEADERS)
_last_hit = {}


def _throttle(url):
    host = url.split("/")[2]
    wait = DELAY - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def get(url, *, binary=False, subdir="pages", refresh=False):
    """GET with on-disk cache and throttling. Returns text or bytes, None on 404."""
    d = CACHE / subdir
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    ext = ".bin" if binary else ".html"
    f = d / (key + ext)
    if f.exists() and not refresh:
        return f.read_bytes() if binary else f.read_text(encoding="utf-8")
    _throttle(url)
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if binary:
                f.write_bytes(r.content)
                return r.content
            f.write_text(r.text, encoding="utf-8")
            return r.text
        except requests.RequestException as e:
            print(f"  retry {attempt + 1} {url}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    for c, t in (("ship_weight_g", "REAL"), ("serving_g", "REAL")):  # migrate older DBs
        if c not in cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {c} {t}")
    return conn


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
NOT_FOOD_TYPES = re.compile(r"gear|gift|apparel|merch|accessor|packs?$|purchased finished", re.I)
BUNDLE_WORDS = re.compile(
    r"\b(\d+[- ]?(?:pack|day|count|ct|pouches)|bundle|kit|variety|bucket|box|case|sampler|"
    r"assortment|subscription|collection|pack of|multipack|bulk|duo|trio|starter|best sellers?|"
    r"summit selects|3-pack|2-pack|supply)\b|\bpack\b", re.I)
GEAR_WORDS = re.compile(
    r"\b(spork|spoon|hat|hoodie|t-shirt|shirt|tote|cap|sock|joggers?|denim|scoop|gift cards?|"
    r"shaker|bottle|pot|stove|cup|mug|water filter|beanie|glove|jacket|cooler|reusable)\b", re.I)


def classify(title, ptype):
    t, pt = title or "", ptype or ""
    if GEAR_WORDS.search(t) and not re.search(r"instant|hot|meal|noodle|soup", t, re.I):
        return "gear"
    if NOT_FOOD_TYPES.search(pt) and not re.search(r"meal|food", pt, re.I):
        return "gear" if re.search(r"gear|gift|apparel|merch|accessor|purchased", pt, re.I) else "bundle"
    if BUNDLE_WORDS.search(t):
        return "bundle"
    return "meal"


# --------------------------------------------------------------------------- #
# Stage 1: discover
# --------------------------------------------------------------------------- #
def guess_format(handle, ptype):
    h, t = handle.lower(), (ptype or "").lower()
    if "pro-pak" in h or "pro-pak" in t:
        return "pro-pak"
    if h.endswith("-10-can") or "can" in h.split("-")[-1:]:
        return "can"
    if h.endswith("-pouch") or "adventure meal" in t:
        return "pouch"
    return t.replace(" ", "-") or None


def upsert_product(conn, brand_key, **kw):
    b = BRANDS[brand_key]
    conn.execute(
        """INSERT INTO products (brand, brand_key, name, handle, url, product_type, format, kind, price_usd, image_url, ship_weight_g)
           VALUES (:brand, :brand_key, :name, :handle, :url, :product_type, :format, :kind, :price_usd, :image_url, :ship_weight_g)
           ON CONFLICT(url) DO UPDATE SET name=excluded.name, product_type=excluded.product_type, format=excluded.format,
               kind=excluded.kind, price_usd=COALESCE(excluded.price_usd, products.price_usd),
               image_url=COALESCE(excluded.image_url, products.image_url),
               ship_weight_g=COALESCE(excluded.ship_weight_g, products.ship_weight_g)""",
        {"brand": b["name"], "brand_key": brand_key, "product_type": None, "price_usd": None,
         "image_url": None, "ship_weight_g": None, "format": guess_format(kw["handle"], kw.get("product_type")), **kw},
    )


def discover_shopify(conn, key):
    base = BRANDS[key]["base"]
    page, n = 1, 0
    while True:
        txt = get(f"{base}/products.json?limit=250&page={page}", subdir="lists", refresh=True)
        prods = json.loads(txt)["products"] if txt else []
        if not prods:
            break
        for p in prods:
            price = None
            try:
                price = float(p["variants"][0]["price"])
            except (KeyError, IndexError, ValueError, TypeError):
                pass
            img = p["images"][0]["src"] if p.get("images") else None
            grams = (p["variants"][0].get("grams") or None) if p.get("variants") else None
            upsert_product(conn, key, ship_weight_g=grams, name=p["title"].strip(), handle=p["handle"],
                           url=f"{base}/products/{p['handle']}", product_type=p.get("product_type"),
                           kind=classify(p["title"], p.get("product_type")), price_usd=price, image_url=img)
            n += 1
        page += 1
    return n


def discover_huel(conn, key):
    base = BRANDS[key]["base"]
    xml = get(f"{base}/sitemaps/sitemap/en-us.xml", subdir="lists", refresh=True) or ""
    urls = re.findall(r"<loc>(https://huel\.com/products/[^<]+)</loc>", xml)
    n = 0
    for u in dict.fromkeys(urls):
        handle = u.rsplit("/", 1)[1]
        name = handle.replace("-", " ").title()
        upsert_product(conn, key, name=name, handle=handle, url=u, kind="meal" if handle in HUEL_FOOD else "other")
        n += 1
    return n


def stage_discover(conn, keys):
    for k in keys:
        n = (discover_huel if BRANDS[k]["kind"] == "huel" else discover_shopify)(conn, k)
        conn.commit()
        meals = conn.execute("SELECT COUNT(*) FROM products WHERE brand_key=? AND kind='meal'", (k,)).fetchone()[0]
        print(f"[discover] {BRANDS[k]['name']}: {n} products, {meals} classified as meals")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
NUM = r"(\d+(?:\.\d+)?)"

# label -> (column, unit regex)
NUTRIENTS = [
    ("calories",       r"calories?"),
    ("total_fat_g",    r"total\s*fat"),
    ("sat_fat_g",      r"sat(?:urated)?\s*fat"),
    ("trans_fat_g",    r"trans\s*fat"),
    ("cholesterol_mg", r"cholesterol"),
    ("sodium_mg",      r"sodium"),
    ("carbs_g",        r"total\s*carb(?:ohydrates?)?|carbohydrates?"),
    ("fiber_g",        r"(?:dietary\s*)?fib(?:er|re)"),
    ("sugars_g",       r"total\s*sugars?"),
    ("added_sugars_g", r"incl(?:udes?|\.)?\s*(?:\w+\s*)?added\s*sugars?|added\s*sugars?"),
    ("protein_g",      r"protein"),
    ("vitamin_d_mcg",  r"vitamin\s*d"),
    ("calcium_mg",     r"calcium"),
    ("iron_mg",        r"iron"),
    ("potassium_mg",   r"potassium"),
]


def clean_ocr(t):
    t = t.replace("\u2019", "'").replace("|", " ").replace("\t", " ")
    # common tesseract confusions inside numeric tokens
    t = re.sub(r"(?<=\d)[Oo](?=\d|g\b|mg\b)", "0", t)
    t = re.sub(r"\b[Oo](?=\d)", "0", t)
    t = re.sub(r"(?<=\d)[lI](?=\d|g\b|mg\b)", "1", t)
    t = re.sub(r"(\d)\s*mcg\b", r"\1mcg", t)
    return t


def parse_panel_text(text):
    """Extract nutrient values from free text like 'Total Fat 23g / 29%'. Returns dict (may be partial)."""
    t = clean_ocr(text)
    out = {}
    for col, label in NUTRIENTS:
        if col == "calories":
            m = re.search(r"calories?(?:\s*/\s*per\s*\w+)?(?!\s*from)\s*[:\-]?\s*" + NUM, t, re.I)
        else:
            # value directly after the label; ignore '%DV' tokens
            m = re.search(r"(?:" + label + r")\s*[:\-]?\s*" + NUM + r"\s*(?:g|mg|mcg|µg|ug)?\b", t, re.I)
        if m:
            v = float(m.group(1))
            if col == "carbs_g" and v > 500:  # glued digits from OCR
                continue
            out[col] = v
    return out


def parse_serving(text):
    t = clean_ocr(text)
    out = {}
    m = re.search(r"serving\s*size\s*[:\-]?\s*([^\n]{1,60})", t, re.I)
    if m:
        out["serving_desc"] = m.group(1).strip(" .:-")
    m = re.search(NUM + r"\s*servings?\s*(?:per|/)\s*(?:container|package|pouch|bag|pot)", t, re.I) or \
        re.search(r"servings?\s*(?:per|/)\s*(?:container|package|pouch|bag|pot)\s*[:\-]?\s*(?:about\s*)?" + NUM, t, re.I)
    if m:
        out["servings_per_container"] = float(m.group(1))
    return out


def check_macros(n):
    """Return (needs_review, note). Atwater check: 4P + 4C + 9F ~= calories (fiber/alcohol give slack)."""
    cal, p, c, f = (n.get(k) for k in ("calories", "protein_g", "carbs_g", "total_fat_g"))
    if cal is None:
        return 1, "no calories parsed"
    missing = [k for k, v in (("protein", p), ("carbs", c), ("fat", f)) if v is None]
    if missing:
        return 1, "missing " + ",".join(missing)
    est = 4 * p + 4 * c + 9 * f
    if cal > 0 and abs(est - cal) / cal > 0.20:
        return 1, f"macro kcal {est:.0f} vs label {cal:.0f}"
    if n.get("sat_fat_g") is not None and n["sat_fat_g"] > f + 0.5:
        return 1, "sat fat > total fat"
    if n.get("sugars_g") is not None and n["sugars_g"] > c + 0.5:
        return 1, "sugars > carbs"
    return 0, None


def oz_to_g(oz):
    return round(oz * 28.3495, 1) if oz is not None else None


def first_float(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return float(m.group(1)) if m else None


def section_text(soup, heading):
    """Text of the block that follows a heading-like element whose own text equals `heading`."""
    pat = re.compile(r"^\s*" + re.escape(heading) + r"\s*:?\s*$", re.I)
    for el in soup.find_all(string=pat):
        node = el.parent
        for _ in range(4):
            sib = node.find_next_sibling()
            while sib is not None and not sib.get_text(strip=True):
                sib = sib.find_next_sibling()
            if sib is not None:
                return sib.get_text(" ", strip=True)
            node = node.parent
            if node is None:
                break
    return None


def clean_ingredients(t):
    if not t:
        return None
    t = re.sub(r"^\s*ingredients\s*:?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*(Read More|Read Less)\b", "", t)
    t = re.split(r"\b(?:Preparation|Directions|Storage|Nutrition|Allergen Information)\b\s*[:&]", t, 1)[0]
    return re.sub(r"\s+", " ", t).strip(" .") or None


def extract_allergens(soup, ingredients):
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"\bContains\s*[:\u2013\-]\s*([A-Za-z ,&/]+?)(?:\.|Net|Made|$|\s{2,})", txt)
    if m:
        return m.group(1).strip()
    return None


# --------------------------------------------------------------------------- #
# Brand page parsers
# --------------------------------------------------------------------------- #
def parse_mountain_house(soup):
    """Nutrition is a real <table> in the 'Nutrition' tab; ingredients is a tab panel."""
    res = {"ingredients": clean_ingredients(section_text(soup, "Ingredients"))}
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(" ".join(cells))
    panel = "\n".join(rows)
    if re.search(r"calories", panel, re.I):
        n = parse_panel_text(panel)
        res["nutrition"] = n
        res["nutrition_raw"] = panel
        res.update(parse_serving(soup.get_text("\n", strip=True)))
        m = re.search(r"Serving size\s+([^\n]+?)\s+Calories/Per\s+(\w+)", soup.get_text(" ", strip=True), re.I)
        if m:
            res["serving_desc"] = m.group(1).strip()
    m = re.search(r"Calories\s*/\s*Per\s+(\w+)", panel + " " + soup.get_text(" ", strip=True), re.I)
    if m:
        res["basis"] = "serving" if m.group(1).lower() == "serving" else "package"
    # No net weight on the page: dry weight = servings x grams in the serving size, e.g. "1 cup (46g) dry mix"
    g = re.search(r"\((\d+(?:\.\d+)?)\s*g\)", res.get("serving_desc") or "")
    if g and res.get("servings_per_container"):
        res["net_weight_g"] = round(float(g.group(1)) * res["servings_per_container"], 1)
        res["weight_src"] = "derived"
    return res


def parse_backpackers_pantry(soup):
    txt = soup.get_text(" ", strip=True)
    res = {"ingredients": clean_ingredients(section_text(soup, "Ingredients"))}
    n = {}
    cal = first_float(r"\bCalories:\s*" + NUM, txt)
    pro = first_float(r"\bProtein:\s*" + NUM + r"\s*g", txt)
    if cal is not None:
        n["calories"] = cal
    if pro is not None:
        n["protein_g"] = pro
    res["nutrition"] = n
    m = re.search(r"\bWeight:\s*" + NUM + r"\s*oz", txt, re.I)
    if m:
        res["net_weight_oz"] = float(m.group(1))
    m = re.search(r"\bAllergens:\s*([A-Za-z, &]+?)\s*(?:Weight|Single|Add to cart)", txt)
    if m:
        res["allergens"] = m.group(1).strip()
    # Label images: product images that look like the back-of-pouch / nutrition panel
    imgs = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = img.get("alt") or ""
        if "cdn/shop/files" in src and re.search(r"[._\-]back[._\-]|nutri|label|facts", src + " " + alt, re.I):
            imgs.append(strip_size(src))
    res["label_urls"] = list(dict.fromkeys(imgs))
    return res


def parse_peak(soup):
    txt = soup.get_text(" ", strip=True).replace("\u2013", "-").replace("\u2014", "-")
    res = {}
    # Ingredients live in a metafield <span>; allergens trail it in the same <p> ("... acid) Wheat")
    ing, allergen_txt = None, None
    for sp in soup.select("span.metafield-multi_line_text_field"):
        t = sp.get_text(" ", strip=True)
        if len(t) > 30 and "," in t:
            ing = t
            rest = sp.parent.get_text(" ", strip=True)[len(t):].strip() if sp.parent else ""
            allergen_txt = rest or None
            break
    if not ing:
        m = re.search(r"Nutrition\s+Ingredients\s+(?:INGREDIENTS\s*:\s*)?(.+?)\s+CONTAINS\s*:", txt)
        ing = m.group(1) if m else None
    res["ingredients"] = clean_ingredients(ing)
    m = re.search(r"CONTAINS\s*:\s*([A-Z][A-Z, ]*?)(?=\s+[A-Z][a-z]|\s*$)", txt)
    cal_pouch = first_float(r"Calories per Pouch\s*[-:]?\s*" + NUM, txt)
    n = {}
    if cal_pouch is not None:
        n["calories"] = cal_pouch
    pro = first_float(r"Protein\s*-\s*" + NUM + r"\s*g per pouch", txt) or \
        first_float(r"(\d+(?:\.\d+)?)\s*(?:g|grams)\s*(?:of\s*)?protein", txt) or \
        first_float(r"(\d+(?:\.\d+)?)\s*grams of protein", txt)
    if pro is not None:
        n["protein_g"] = pro
    res["nutrition"] = n
    res["net_weight_oz"] = first_float(r"Net Weight\s*[-:]\s*" + NUM + r"\s*oz", txt) or \
        first_float(NUM + r"\s*net weight \(oz", txt)
    res["servings_per_container"] = first_float(r"(\d+(?:\.\d+)?)\s*total servings", txt) or \
        first_float(r"(\d+(?:\.\d+)?)\s*Servings Size", txt)
    sd = re.search(r"Serving size\s*[-:]\s*([^\n]{1,30}?)\s*(?:Contains|Net|Made)", txt, re.I)
    if sd:
        res["serving_desc"] = sd.group(1).strip()
    res["prep_water_oz"] = first_float(r"(\d+(?:\.\d+)?)\s*water \(oz", txt)
    res["prep_minutes"] = first_float(r"(\d+(?:\.\d+)?)\s*prep time \(minutes", txt) or \
        first_float(r"Prep Time\s*[-:]\s*" + NUM, txt)
    cm = re.search(r"Contains\s*-\s*([A-Za-z, ]+?)\s+Net Weight", txt)
    al = (cm.group(1) if cm else None) or allergen_txt or (m.group(1) if m else None)
    if al:
        res["allergens"] = re.sub(r"\s+", " ", al).strip(" ,.").title()
    imgs = []
    for img in soup.find_all("img"):
        alt = img.get("alt") or ""
        src = img.get("src") or ""
        if re.search(r"nutrition facts", alt, re.I) and src:
            imgs.append(strip_size("https:" + src if src.startswith("//") else src))
    res["label_urls"] = list(dict.fromkeys(imgs))
    return res


HUEL_FOOD = {"huel-black-edition", "huel-complete-protein", "huel-essential", "huel-gluten-free",
             "hot-and-savoury-meal-packs", "hot-and-savoury-lite-ramen", "huel-bar", "huel-lite-ready-to-drink",
             "huel-ready-to-drink", "huel-black-edition-ready-to-drink", "huel-diet-powder", "huel-energy-plus"}
HUEL_SKIP_ALT = re.compile(r"testimonial|quote|office|infographic|benefit|quality|callout|composite|enjoy a|close.?up|box of|stack of|bag of|"
                           r"ingredients$|back-of-pack with ingredients", re.I)


def parse_huel(soup, html):
    """One variant per flavor label image: the page's RSC payload carries Sanity CDN image + alt pairs."""
    variants = {}
    for m in re.finditer(r'(https://cdn\.sanity\.io/images/[^"\\]+?\.(?:png|jpg|jpeg|webp))', html):
        a = re.search(r'alt\\":\\"((?:[^"\\]|\\\\u[0-9a-fA-F]{4})*)', html[m.end(): m.end() + 600])
        if not a:
            continue
        alt = re.sub(r"\\u0026", "&", a.group(1))
        if re.search(r"nutrition(?:al)? (?:info|label|facts)|back.of.pack (?:nutrition|label)|nutritional (?:info|label)|nutrition information", alt, re.I) and not HUEL_SKIP_ALT.search(alt):
            variants.setdefault(m.group(1), alt)
    # Nutrition-modal accordion: {"heading":"<flavor>","details":null,"image":{"sources":{"default":"<url>"}...
    heading_pat = r'''heading\\"\s*:\s*\\"((?:[^"\\]|\\u[0-9a-fA-F]{4})*)\\"\s*,\s*\\"details\\"\s*:\s*null\s*,\s*\\"image\\"\s*:\s*\{\s*\\"sources\\"\s*:\s*\{\s*\\"default\\"\s*:\s*\\"(https://cdn\.sanity\.io/[^"\\]+)'''
    for m in re.finditer(heading_pat, html):
        head = re.sub(r"\\u0026", "&", m.group(1)).strip()
        if not HUEL_SKIP_ALT.search(head):
            variants[m.group(2)] = head
    res = {"nutrition": {}, "variants": [{"url": u, "alt": al} for u, al in variants.items()]}
    t = soup.find("title")
    if t:
        res["title"] = re.split(r"\s+[|\u2013\-]\s+", t.get_text(strip=True))[0]
    return res


def huel_flavor_name(alt):
    if not re.search(r"(?i)nutrition|label|back.of.pack", alt):
        return alt  # already a clean flavor heading
    n = re.sub(r"(?i)\b(nutritional (?:info(?:rmation)?|label)|back[- ]of[- ]pack( nutritional label)?|for the|for|label|us)\b", " ", alt)
    return re.sub(r"\s+", " ", n).strip(" -:.")


def strip_size(u):
    if u.startswith("//"):
        u = "https:" + u
    return re.sub(r"[?&]width=\d+", "", u).rstrip("?&")


# --------------------------------------------------------------------------- #
# Stage 2: fetch + parse
# --------------------------------------------------------------------------- #
def store_parsed(conn, pid, parsed, src_default):
    sets = {k: parsed[k] for k in ("net_weight_oz", "servings_per_container", "serving_desc",
                                   "prep_water_oz", "prep_minutes") if parsed.get(k) is not None}
    if parsed.get("net_weight_oz") is not None:
        sets["net_weight_g"] = oz_to_g(parsed["net_weight_oz"])
        sets["weight_src"] = "page"
    elif parsed.get("net_weight_g") is not None:
        sets["net_weight_g"] = parsed["net_weight_g"]
        sets["net_weight_oz"] = round(parsed["net_weight_g"] / 28.3495, 2)
        sets["weight_src"] = parsed.get("weight_src", "derived")
    if parsed.get("format"):
        sets["format"] = parsed["format"]
    if parsed.get("label_urls") is not None:
        sets["label_urls"] = json.dumps(parsed["label_urls"])
    if parsed.get("title"):
        sets["name"] = parsed["title"]
    sets["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("UPDATE products SET " + ", ".join(f"{k}=:{k}" for k in sets) + " WHERE id=:id", {**sets, "id": pid})

    if parsed.get("ingredients") or parsed.get("allergens"):
        conn.execute("INSERT INTO ingredients (product_id, text, allergens) VALUES (?,?,?) "
                     "ON CONFLICT(product_id) DO UPDATE SET text=COALESCE(excluded.text, text), "
                     "allergens=COALESCE(excluded.allergens, allergens)",
                     (pid, parsed.get("ingredients"), parsed.get("allergens")))
    n = parsed.get("nutrition") or {}
    if n:
        full = n.get("total_fat_g") is not None and n.get("carbs_g") is not None
        review, note = check_macros(n) if full else (1, "label image needed")
        cols = ["product_id", "basis", "source", "needs_review", "review_note", "raw_text"] + list(n)
        vals = [pid, parsed.get("basis", "package"), "html" if full else "text", review, note, parsed.get("nutrition_raw")] + list(n.values())
        conn.execute(f"INSERT OR REPLACE INTO nutrition ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)


PARSERS = {
    "mountain_house": lambda s, h: parse_mountain_house(s),
    "backpackers_pantry": lambda s, h: parse_backpackers_pantry(s),
    "peak": lambda s, h: parse_peak(s),
    "huel": parse_huel,
}


def stage_fetch(conn, keys, limit=None, refresh=False):
    for k in keys:
        rows = conn.execute("SELECT * FROM products WHERE brand_key=? AND kind IN ('meal','group') ORDER BY id", (k,)).fetchall()
        if k == "huel":
            rows = [r for r in rows if r["handle"] in HUEL_FOOD]
        if limit:
            rows = rows[:limit]
        ok = dead = 0
        for r in rows:
            html = get(r["url"], refresh=refresh)
            if not html:
                dead += 1
                conn.execute("UPDATE products SET kind='dead' WHERE id=?", (r["id"],))
                print(f"  404  {r['url']}")
                continue
            soup = BeautifulSoup(html, "lxml")
            parsed = PARSERS[k](soup, html)
            if k == "huel":
                # parent page row becomes a container; each flavor label is its own product row
                for v in parsed["variants"]:
                    fl = huel_flavor_name(v["alt"])
                    vh = f"{r['handle']}--{v['url'].rsplit('/', 1)[1][:12]}"
                    conn.execute("""INSERT INTO products (brand, brand_key, name, handle, url, product_type, format, kind, label_urls)
                                    VALUES (?,?,?,?,?,?,?,'meal',?) ON CONFLICT(url) DO UPDATE SET
                                    name=excluded.name, label_urls=excluded.label_urls""",
                                 ("Huel", "huel", f"{(parsed.get('title') or r['name'])} - {fl}", vh,
                                  f"{r['url']}#{vh}", r["product_type"], r["handle"], json.dumps([v["url"]])))
                conn.execute("UPDATE products SET kind='group' WHERE id=?", (r["id"],))
                print(f"  ok   {r['handle']:36} {len(parsed['variants'])} flavor labels")
                ok += 1
                continue
            store_parsed(conn, r["id"], parsed, "html")
            ok += 1
            flag = ""
            nn = parsed.get("nutrition") or {}
            if nn.get("calories") is None and not parsed.get("label_urls"):
                flag = "  (no nutrition found)"
            print(f"  ok   {r['name'][:48]:48} cal={nn.get('calories')} ing={'y' if parsed.get('ingredients') else 'n'}"
                  f" labels={len(parsed.get('label_urls') or [])}{flag}")
        conn.commit()
        print(f"[fetch] {BRANDS[k]['name']}: {ok} parsed, {dead} dead links")


# --------------------------------------------------------------------------- #
# Stage 3: OCR for image-only panels
# --------------------------------------------------------------------------- #
def ocr_image(data, psm=6, crop=True):
    """Run tesseract on image bytes, with grayscale + upscale preprocessing. Returns text."""
    from io import BytesIO
    from PIL import Image, ImageOps
    img = Image.open(BytesIO(data))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    if img.width >= 1800 and crop:
        c = find_label_crop(img)
        if c is not None:
            img = c
    img = ImageOps.autocontrast(img.convert("L"))
    if img.width < 1800:
        s = 1800 / img.width
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    tmp = CACHE / "_ocr_tmp.png"
    img.save(tmp)
    p = subprocess.run(["tesseract", str(tmp), "stdout", "--psm", str(psm), "-l", "eng"],
                       capture_output=True, text=True)
    return p.stdout


def find_label_crop(img):
    """Locate the 'Nutrition Facts' heading with tesseract TSV and crop to the panel (for pouch photos)."""
    tmp = CACHE / "_ocr_find.png"
    g = img.convert("L")
    g.save(tmp)
    p = subprocess.run(["tesseract", str(tmp), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
    rows = [l.split("\t") for l in p.stdout.splitlines()[1:] if l.count("\t") >= 11]
    for a, b in zip(rows, rows[1:]):
        if a[11].strip().lower().startswith("nutrition") and b[11].strip().lower().startswith("fact"):
            x, y, w = int(a[6]), int(a[7]), int(b[6]) + int(b[8]) - int(a[6])
            box = (max(0, int(x - 0.08 * w)), max(0, int(y - 0.15 * w)),
                   min(img.width, int(x + 1.12 * w)), min(img.height, int(y + 2.1 * w)))
            return img.crop(box)
    return None


def parse_rows(text, servings=None):
    """Row-wise parse handling single- and dual-column ('Per serving | Per container') panels.
    Returns (per_serving, per_package) dicts. Values missing from a column stay absent."""
    t = clean_ocr(text)
    per_s, per_p = {}, {}
    for col, label in NUTRIENTS:
        for line in t.splitlines():
            m = re.search(r"(?:" + label + r")\b(.*)", line, re.I)
            if not m:
                continue
            rest = re.sub(r"\d+(?:\.\d+)?\s*%", " ", m.group(1))        # drop %DV
            if col == "calories":
                vals = [float(x) for x in re.findall(NUM, rest)]
            else:
                vals = [float(x) for x in re.findall(NUM + r"\s*(?:g|mg|mcg|µg|ug)\b", rest)]
            if not vals:
                continue
            if len(vals) >= 2:
                per_s[col], per_p[col] = vals[0], vals[1]
            else:
                per_s[col] = vals[0]
                per_s.setdefault("_single", []).append(col)
            break
    return per_s, per_p


G_COLS = {"total_fat_g", "sat_fat_g", "trans_fat_g", "carbs_g", "fiber_g", "sugars_g", "added_sugars_g", "protein_g"}


def _cands(v, col):
    """OCR often reads a trailing 'g' as '9' ('2g' -> '29'). Offer both readings."""
    if v is None:
        return [None]
    out = [v]
    if col in G_COLS:
        txt = f"{v:g}"
        if len(txt) > 1 and txt.endswith("9"):
            out.append(float(txt[:-1]))
    return out


def _consistent(s, p, servings):
    return abs(p - s * servings) <= max(0.6, 0.1 * p)


def resolve_label(text, servings, cal_pkg=None):
    """Return (package_nutrition_dict, notes). Uses per-serving x servings == per-package to repair OCR."""
    per_s, per_p = parse_rows(text)
    per_s.pop("_single", None)
    servings = servings or (per_p.get("calories") / per_s["calories"] if per_p.get("calories") and per_s.get("calories") else None)
    out, notes = {}, []
    for col, _ in NUTRIENTS:
        cs, cp = _cands(per_s.get(col), col), _cands(per_p.get(col), col)
        val = None
        if servings and per_s.get(col) is not None and per_p.get(col) is not None:
            for p_ in cp:
                for s_ in cs:
                    if p_ is not None and s_ is not None and _consistent(s_, p_, servings):
                        val = p_
                        break
                if val is not None:
                    break
            if val is None:
                notes.append(f"{col} columns disagree")
        elif per_p.get(col) is not None:
            val = cp[-1] if (col in G_COLS and len(cp) > 1) else cp[0]   # lone column: prefer 'g' repair
        elif per_s.get(col) is not None and servings:
            val = (cs[-1] if (col in G_COLS and len(cs) > 1) else cs[0]) * servings
        if val is not None:
            out[col] = round(val, 1)
    if cal_pkg and out.get("calories") and abs(out["calories"] - cal_pkg) > max(10, 0.05 * cal_pkg):
        notes.append(f"label kcal {out['calories']:g} vs page {cal_pkg:g}")
    # fix ambiguous g-reads using the Atwater check against page/label package calories
    cal = cal_pkg or out.get("calories")
    if cal and all(k in out for k in ("total_fat_g", "carbs_g", "protein_g")):
        best = None
        for f in _cands(out["total_fat_g"], "total_fat_g"):
            for c in _cands(out["carbs_g"], "carbs_g"):
                for pr in _cands(out["protein_g"], "protein_g"):
                    err = abs(4 * pr + 4 * c + 9 * f - cal) / cal
                    if best is None or err < best[0]:
                        best = (err, f, c, pr)
        if best and best[0] < abs(4 * out["protein_g"] + 4 * out["carbs_g"] + 9 * out["total_fat_g"] - cal) / cal - 0.02:
            out["total_fat_g"], out["carbs_g"], out["protein_g"] = best[1], best[2], best[3]
            notes.append("repaired g/9 OCR confusion")
    return out, notes


def score(n):
    return sum(v is not None for v in n.values()) + (5 if check_macros(n)[0] == 0 else 0)


def stage_ocr(conn, keys, limit=None, redo=False):
    for k in keys:
        q = ("SELECT p.* FROM products p LEFT JOIN nutrition n ON n.product_id=p.id "
             "WHERE p.brand_key=? AND p.kind='meal' AND p.label_urls IS NOT NULL AND p.label_urls!='[]'")
        if not redo:
            q += " AND (n.source IS NULL OR n.source NOT IN ('html','ocr','manual'))"
        rows = conn.execute(q + " ORDER BY p.id", (k,)).fetchall()
        if limit:
            rows = rows[:limit]
        done = clean = 0
        for r in rows:
            old = conn.execute("SELECT * FROM nutrition WHERE product_id=?", (r["id"],)).fetchone()
            cal_pkg = old["calories"] if old else None
            best, best_notes, best_txt, best_ing = {}, [], "", None
            for u in json.loads(r["label_urls"]):
                data = get(strip_size(u), binary=True, subdir="labels")
                if not data:
                    continue
                for psm in (6, 4):
                    try:
                        txt = ocr_image(data, psm, crop=(k == "backpackers_pantry"))
                    except Exception as e:  # noqa: BLE001
                        print(f"  ocr failed {u}: {e}", file=sys.stderr)
                        continue
                    sv = parse_serving(txt)
                    n, notes = resolve_label(txt, r["servings_per_container"] or sv.get("servings_per_container"), cal_pkg)
                    sc = score(n) - 3 * len(notes)
                    if sc > (score(best) - 3 * len(best_notes) if best else -99):
                        best, best_notes, best_txt = n, notes, txt
                    m = re.search(r"INGREDIENTS?\s*[:\-]?\s*(.+)", txt, re.I | re.S)
                    if m and not best_ing and len(m.group(1)) > 40:
                        best_ing = clean_ingredients(m.group(1))
            if not best:
                print(f"  --   {r['name'][:48]}: nothing parsed")
                continue
            # page text is authoritative for whole-package calories/protein
            if old:
                for col in ("calories", "protein_g"):
                    if old[col] is not None and best.get(col) is not None and abs(old[col] - best[col]) > max(2, 0.06 * old[col]):
                        best_notes.append(f"{col} label {best[col]:g} vs page {old[col]:g}; used page")
                    if old[col] is not None:
                        best[col] = old[col]
            review, note = check_macros(best)
            if best_notes:
                review, note = 1, "; ".join(best_notes + ([note] if note else []))
            sv = parse_serving(best_txt)
            cols = ["product_id", "basis", "source", "needs_review", "review_note", "raw_text"] + list(best)
            vals = [r["id"], "package", "ocr", review, note, best_txt] + list(best.values())
            conn.execute(f"INSERT OR REPLACE INTO nutrition ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            if sv.get("serving_desc") or sv.get("servings_per_container"):
                conn.execute("UPDATE products SET serving_desc=COALESCE(?,serving_desc), "
                             "servings_per_container=COALESCE(servings_per_container,?) WHERE id=?",
                             (sv.get("serving_desc"), sv.get("servings_per_container"), r["id"]))
            if best_ing:
                conn.execute("INSERT INTO ingredients (product_id, text) VALUES (?,?) "
                             "ON CONFLICT(product_id) DO UPDATE SET text=COALESCE(text, excluded.text)", (r["id"], best_ing))
            done += 1
            clean += 1 - review
            print(f"  ocr  {r['name'][:44]:44} fields={len(best):2} review={review} {note or ''}")
        conn.commit()
        print(f"[ocr] {BRANDS[k]['name']}: {done}/{len(rows)} labels parsed, {clean} passed all checks")


# --------------------------------------------------------------------------- #
MANUAL_CSV = HERE / "manual_nutrition.csv"
MANUAL_COLS = ["calories", "total_fat_g", "sat_fat_g", "trans_fat_g", "cholesterol_mg", "sodium_mg", "carbs_g",
               "fiber_g", "sugars_g", "added_sugars_g", "protein_g", "vitamin_d_mcg", "calcium_mg", "iron_mg",
               "potassium_mg"]


def stage_crops(conn, keys, only_review=True, per_sheet=2):
    """Write cropped label images + paired contact sheets (cache/sheets/) for manual transcription."""
    from io import BytesIO
    from PIL import Image, ImageDraw
    out = CACHE / "sheets"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    q = ("SELECT p.* FROM products p LEFT JOIN nutrition n ON n.product_id=p.id WHERE p.kind='meal' "
         "AND p.brand_key IN (%s) AND p.label_urls IS NOT NULL AND p.label_urls!='[]' "
         "AND (n.source IS NULL OR n.source!='manual')" % ",".join("?" * len(keys)))
    if only_review:
        q += " AND (n.needs_review=1 OR n.product_id IS NULL OR n.calories IS NULL OR n.source='ocr')"
    items = []
    for r in conn.execute(q + " ORDER BY p.brand_key, p.id", keys).fetchall():
        data = get(strip_size(json.loads(r["label_urls"])[0]), binary=True, subdir="labels")
        if not data:
            continue
        img = Image.open(BytesIO(data)).convert("RGB")
        c = find_label_crop(img) if (img.width >= 1800 and r["brand_key"] == "backpackers_pantry") else None
        img = c or img
        img.thumbnail((900, 1300))
        items.append((r["handle"], r["name"], img))
    for i in range(0, len(items), per_sheet):
        grp = items[i:i + per_sheet]
        w = sum(im.width for _, _, im in grp) + 10 * len(grp)
        h = max(im.height for _, _, im in grp) + 34
        sheet = Image.new("RGB", (w, h), "white")
        d = ImageDraw.Draw(sheet)
        x = 0
        for handle, name, im in grp:
            d.text((x + 4, 4), handle, fill="red")
            sheet.paste(im, (x, 30))
            x += im.width + 10
        sheet.save(out / f"sheet_{i // per_sheet:03d}.png")
    print(f"[crops] {len(items)} labels -> {(len(items) + per_sheet - 1) // per_sheet} sheets in {out}")
    (out / "index.txt").write_text("\n".join(f"{n:03d} {h}" for n, (h, _, _) in enumerate(items)))


def stage_manual(conn):
    """Apply hand-transcribed label values (manual_nutrition.csv, keyed by product handle)."""
    import csv
    if not MANUAL_CSV.exists():
        print("[manual] no manual_nutrition.csv")
        return
    n = 0
    with open(MANUAL_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            h = (row.get("handle") or "").strip()
            if not h or h.startswith("#"):
                continue
            p = conn.execute("SELECT id FROM products WHERE handle=?", (h,)).fetchone()
            if not p:
                print(f"  ?? unknown handle {h}")
                continue
            vals = {c: float(row[c]) for c in MANUAL_COLS if row.get(c) not in (None, "")}
            sets = {}
            for k in ("servings_per_container", "net_weight_g"):
                if row.get(k):
                    sets[k] = float(row[k])
            if row.get("serving_desc"):
                sets["serving_desc"] = row["serving_desc"]
            if row.get("net_weight_g"):
                sets["weight_src"] = "label"
                sets["net_weight_oz"] = round(float(row["net_weight_g"]) / 28.3495, 2)
            if sets:
                conn.execute("UPDATE products SET " + ", ".join(f"{k}=:{k}" for k in sets) + " WHERE id=:id", {**sets, "id": p["id"]})
            if row.get("ingredients"):
                conn.execute("INSERT INTO ingredients (product_id, text) VALUES (?,?) ON CONFLICT(product_id) DO UPDATE SET text=excluded.text",
                             (p["id"], row["ingredients"]))
            if row.get("name"):
                conn.execute("UPDATE products SET name=? WHERE id=?", (row["name"], p["id"]))
            if row.get("kind"):
                conn.execute("UPDATE products SET kind=? WHERE id=?", (row["kind"], p["id"]))
            if vals:
                basis = row.get("basis") or "package"
                review, note = check_macros(vals)
                cols = ["product_id", "basis", "source", "needs_review", "review_note"] + list(vals)
                conn.execute(f"INSERT OR REPLACE INTO nutrition ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [p["id"], basis, "manual", review, note] + list(vals.values()))
            n += 1
    conn.commit()
    print(f"[manual] applied {n} rows")


def fill_weights(conn):
    """Parse grams-per-serving, then fall back: no page/derived net weight -> shipping weight (flagged), so every meal has a carry-weight estimate."""
    import re as _re
    for r in conn.execute("SELECT id, serving_desc FROM products WHERE kind='meal' AND serving_desc IS NOT NULL").fetchall():
        m = _re.search(r"\((\d+(?:\.\d+)?)\s*g\)", r["serving_desc"])
        if m:
            conn.execute("UPDATE products SET serving_g=? WHERE id=?", (float(m.group(1)), r["id"]))
    n = conn.execute("UPDATE products SET net_weight_g=ship_weight_g, net_weight_oz=ROUND(ship_weight_g/28.3495,2), "
                     "weight_src='shipping' WHERE kind='meal' AND net_weight_g IS NULL AND ship_weight_g > 0").rowcount
    conn.commit()
    return n


def stage_export(conn):
    """Write foods.csv / foods.json: one row per carryable portion, full nutrition + ingredients."""
    import csv
    rows = conn.execute("""
        SELECT o.id AS product_id, o.brand, o.name, o.format, o.kind_of_portion AS portion_type, o.portion_label, o.portion_g,
               o.portion_kcal AS kcal, o.portion_protein_g AS protein_g, o.portion_carbs_g AS carbs_g,
               o.portion_fat_g AS fat_g, o.portion_fiber_g AS fiber_g, o.portion_sodium_mg AS sodium_mg,
               o.portion_sugars_g AS sugars_g, o.portion_satfat_g AS sat_fat_g, o.portion_chol_mg AS cholesterol_mg,
               o.portion_calcium_mg AS calcium_mg, o.portion_iron_mg AS iron_mg, o.portion_potassium_mg AS potassium_mg,
               o.portion_vitd_mcg AS vitamin_d_mcg,
               o.kcal_per_oz, o.portion_price_usd AS price_usd, o.weight_src, o.needs_review,
               p.servings_per_container, p.serving_desc, i.allergens, i.text AS ingredients, o.url
        FROM v_optimizer o JOIN products p ON p.id = o.id LEFT JOIN ingredients i ON i.product_id = o.id
        ORDER BY o.brand, o.name, o.format""").fetchall()
    out = [{k: (round(r[k], 2) if isinstance(r[k], float) else r[k]) for k in r.keys()} for r in rows]
    with open(HERE / "foods.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    (HERE / "foods.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[export] {len(out)} portions -> foods.csv, foods.json")


def stage_report(conn):
    print(f"\n{'brand':22} {'meals':>5} {'nutr':>5} {'full':>5} {'ingr':>5} {'review':>6} {'wt':>4}")
    for r in conn.execute("""
        SELECT p.brand, COUNT(*) meals,
               SUM(n.calories IS NOT NULL) nutr,
               SUM(n.total_fat_g IS NOT NULL AND n.carbs_g IS NOT NULL AND n.protein_g IS NOT NULL) fullrows,
               SUM(i.text IS NOT NULL) ingr,
               SUM(n.needs_review = 1) rev, SUM(p.net_weight_g IS NOT NULL) wt
        FROM products p LEFT JOIN nutrition n ON n.product_id=p.id LEFT JOIN ingredients i ON i.product_id=p.id
        WHERE p.kind='meal' GROUP BY p.brand"""):
        print(f"{r['brand']:22} {r['meals']:>5} {r['nutr'] or 0:>5} {r['fullrows'] or 0:>5} {r['ingr'] or 0:>5} {r['rev'] or 0:>6} {r['wt'] or 0:>4}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stages", nargs="+", choices=["all", "discover", "fetch", "ocr", "crops", "manual", "export", "report"])
    ap.add_argument("--brand", action="append", choices=list(BRANDS), help="limit to brand(s)")
    ap.add_argument("--limit", type=int, help="max products per brand (testing)")
    ap.add_argument("--refresh", action="store_true", help="ignore page cache")
    ap.add_argument("--redo-ocr", action="store_true", help="re-OCR labels that already have OCR data")
    a = ap.parse_args()
    keys = a.brand or list(BRANDS)
    stages = ["discover", "fetch", "ocr", "manual", "export", "report"] if "all" in a.stages else a.stages
    conn = db()
    for s in stages:
        if s == "discover":
            stage_discover(conn, keys)
        elif s == "fetch":
            stage_fetch(conn, keys, a.limit, a.refresh)
        elif s == "ocr":
            stage_ocr(conn, keys, a.limit, a.redo_ocr)
        elif s == "crops":
            stage_crops(conn, keys)
        elif s == "manual":
            stage_manual(conn)
        elif s == "export":
            fill_weights(conn)
            stage_export(conn)
        elif s == "report":
            print(f"[weights] {fill_weights(conn)} products fell back to shipping weight")
            stage_report(conn)


if __name__ == "__main__":
    main()
