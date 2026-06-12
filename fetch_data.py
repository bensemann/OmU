#!/usr/bin/env python3
"""
OmU Kino Scraper – rewrites from scratch after DOM analysis.
Scrapes allekinos.de city pages, scopes each film to h2→next-h2 boundaries,
extracts only OmU showings. Enriches with OMDb for IMDb ratings.
"""

import requests
from bs4 import BeautifulSoup
import json, re, os
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

CITIES = [
    {"name": "Wiesbaden",         "slug": "Wiesbaden",         "optional": False},
    {"name": "Mainz",             "slug": "Mainz",             "optional": False},
    {"name": "Geisenheim",        "slug": "Geisenheim",        "optional": False},
    {"name": "Frankfurt am Main", "slug": "Frankfurt am Main", "optional": True},
    {"name": "Darmstadt",         "slug": "Darmstadt",         "optional": True},
    {"name": "Sulzbach (Taunus)", "slug": "Sulzbach (Taunus)", "optional": True},
]

OMDB_KEY  = os.environ.get("OMDB_API_KEY", "")
BASE_URL  = "https://allekinos.de/programm"
HEADERS   = {"User-Agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)}
MONTH_MAP = {
    "Januar":1,"Februar":2,"März":3,"April":4,"Mai":5,"Juni":6,
    "Juli":7,"August":8,"September":9,"Oktober":10,"November":11,"Dezember":12,
}
MAX_CINEMAS   = 10
MAX_SHOWTIMES = 30
MAX_URL_LEN   = 300
MAX_JSON_MB   = 8

TIME_RE       = re.compile(r'^\d{1,2}:\d{2}$')
MULTI_TIME_RE = re.compile(r'\d{1,2}:\d{2}')   # extract from concatenated strings
VERSION_RE    = re.compile(r'\s*\(OmU\)\s*', re.IGNORECASE)
omdb_cache: dict = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date_header(text: str) -> str | None:
    m = re.match(r'\w+\.\s+(\d+)\.\s+(\w+)', text.strip())
    if not m:
        return None
    day, month_name = int(m.group(1)), m.group(2)
    month = MONTH_MAP.get(month_name)
    if not month:
        return None
    year = datetime.now().year
    try:
        d = datetime(year, month, day)
        if d < datetime.now() - timedelta(days=1):
            d = datetime(year + 1, month, day)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None

def parse_runtime(text: str) -> int | None:
    m = re.search(r'(\d+)\s*Std\.\s*(\d+)\s*Min\.', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r'(\d+)\s*Min\.', text)
    return int(m.group(1)) if m else None

def clean_url(url: str) -> str:
    if not url or url.startswith("data:"):
        return ""
    return url[:MAX_URL_LEN]

def get_omdb(title: str, year: str | None = None) -> dict:
    if not OMDB_KEY:
        return {}
    key = f"{title}|{year}"
    if key in omdb_cache:
        return omdb_cache[key]
    params = {"apikey": OMDB_KEY, "t": title, "type": "movie"}
    if year:
        params["y"] = year
    try:
        r = requests.get("https://www.omdbapi.com/", params=params, timeout=6)
        d = r.json()
        result = {
            "imdb_rating": d.get("imdbRating", ""),
            "imdb_id":     d.get("imdbID", ""),
            "poster_omdb": d.get("Poster", ""),
            "plot":        d.get("Plot", ""),
            "director":    d.get("Director", ""),
            "actors":      d.get("Actors", ""),
        } if d.get("Response") == "True" else {}
    except Exception:
        result = {}
    omdb_cache[key] = result
    return result

# ── Date parsing ──────────────────────────────────────────────────────────────

def extract_dates(soup: BeautifulSoup) -> list[str]:
    """Extract ordered date list from the page's date-nav header."""
    date_pat = re.compile(r'(Mo|Di|Mi|Do|Fr|Sa|So)\.\s+\d+\.\s+\w+')
    seen, dates = set(), []
    for el in soup.find_all(string=date_pat):
        raw = el.strip()
        parsed = parse_date_header(raw)
        if parsed and parsed not in seen:
            seen.add(parsed)
            dates.append(parsed)
    return dates

# ── Date assignment for a single time-link ───────────────────────────────────

def date_from_td(a_elem, dates: list[str]) -> str | None:
    """
    If the <a> tag lives inside a <td>, use the td's column index to
    determine which date it belongs to.
    """
    node = a_elem.parent
    for _ in range(4):
        if node is None:
            break
        if node.name == "td":
            row = node.find_parent("tr")
            if row:
                cells = row.find_all("td", recursive=False)
                try:
                    idx = cells.index(node)
                    return dates[idx] if idx < len(dates) else None
                except ValueError:
                    pass
            return None
        node = node.parent
    return None

# ── Core: parse one OmU film block ───────────────────────────────────────────

def parse_film_block(h2, next_h2, dates: list[str], city_name: str) -> dict | None:
    """
    h2            – BeautifulSoup Tag for this film's heading
    next_h2       – Tag for the NEXT film's heading (stop boundary), or None
    dates         – ordered list of ISO date strings for the page's 8 day columns
    city_name     – city context for logging
    Returns a movie dict or None if no OmU showtimes found.
    """
    title_raw  = h2.get_text(" ", strip=True)
    clean_title = VERSION_RE.sub("", title_raw).strip()
    # Also strip leftover parentheses artefacts
    clean_title = re.sub(r'\s*\(\s*\)\s*', '', clean_title).strip()

    # ── poster (img immediately before or within h2's block) ──
    poster = ""
    img = h2.find_previous("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            poster = src if src.startswith("http") else "https://allekinos.de" + src
            poster = poster[:MAX_URL_LEN]

    # ── metadata (pull from text near h2) ──
    meta_text = h2.get_text(" ", strip=True)
    # look a bit further forward for genre/runtime/FSK text
    char_budget = 800
    for elem in h2.next_elements:
        if elem is next_h2:
            break
        if hasattr(elem, "name") and elem.name == "h2" and elem is not h2:
            break
        chunk = elem.get_text(" ", strip=True) if hasattr(elem, "get_text") else str(elem)
        meta_text += " " + chunk
        char_budget -= len(chunk)
        if char_budget <= 0:
            break

    year_m  = re.search(r'\b(20\d{2})\b', meta_text)
    year    = year_m.group(1) if year_m else None
    runtime = parse_runtime(meta_text)
    fsk_m   = re.search(r'FSK\s*(\d+)', meta_text)
    fsk     = fsk_m.group(1) if fsk_m else None
    genres  = []
    for a in h2.find_all_next("a", href=re.compile(r"genre=")):
        if a is next_h2:
            break
        g = a.get_text(strip=True)
        if g and g not in genres:
            genres.append(g)
        if a is next_h2:
            break
        # stop once past the film block boundary
        # (can't check easily so just cap)
        if len(genres) >= 5:
            break

    # ── cinemas & showtimes ──────────────────────────────────────────────────
    #
    # Walk next_elements from h2 to next_h2.
    # Each <a href="...kino=..."> starts a new cinema block.
    # <a> tags with time text are linked showtimes (future, with booking URL).
    # Bare text nodes matching HH:MM are today's unlinked showtimes.
    #
    cinemas: list[dict] = []
    current:      dict | None = None
    need_address: bool        = False
    today = datetime.now().strftime("%Y-%m-%d")

    for elem in h2.next_elements:
        if elem is next_h2:
            break
        if hasattr(elem, "name") and elem.name == "h2" and elem is not h2:
            break

        if hasattr(elem, "name") and elem.name == "a":
            href = elem.get("href", "")
            text = elem.get_text(strip=True)

            if "kino=" in href:
                # New cinema block
                if text and len(cinemas) < MAX_CINEMAS:
                    current = {"name": text[:100], "address": "",
                               "city": city_name, "showtimes": []}
                    cinemas.append(current)
                    need_address = True

            elif TIME_RE.match(text) and current is not None:
                need_address = False
                if len(current["showtimes"]) < MAX_SHOWTIMES:
                    # Try td column position first
                    date = date_from_td(elem, dates)
                    if date is None and dates:
                        # Sequential fallback: linked times = future dates in order
                        n_linked = sum(1 for s in current["showtimes"] if s.get("url"))
                        has_today = any(s.get("date") == today for s in current["showtimes"])
                        idx = (1 if has_today else 0) + n_linked
                        date = dates[idx] if idx < len(dates) else None
                    current["showtimes"].append({
                        "time": text,
                        "date": date,
                        "url":  clean_url(href),
                    })

        elif isinstance(elem, str):
            raw = elem.strip()
            if not raw or current is None:
                continue
            # Skip text nodes inside <a> tags (already counted as linked times)
            par = getattr(elem, "parent", None)
            if par and getattr(par, "name", None) == "a":
                continue
            # Check if string is mostly times (e.g. "14:0017:0020:00")
            found    = MULTI_TIME_RE.findall(raw)
            leftover = MULTI_TIME_RE.sub("", raw).strip()
            is_times = bool(found) and len(leftover) <= 3
            # Capture address: first non-time text right after cinema link
            if need_address and not is_times:
                current["address"] = raw[:120]
                need_address = False
                continue
            need_address = False
            if is_times:
                for t in found:
                    if len(current["showtimes"]) < MAX_SHOWTIMES:
                        current["showtimes"].append({
                            "time": t,
                            "date": today,
                            "url":  "",
                        })

    # Drop cinemas with zero showtimes
    cinemas = [c for c in cinemas if c["showtimes"]]
    if not cinemas:
        return None

    # ── OMDb enrichment ──
    omdb = get_omdb(clean_title, year)

    return {
        "title":       clean_title,
        "version":     "OmU",
        "genres":      genres[:5],
        "year":        year,
        "runtime":     runtime,
        "fsk":         fsk,
        "poster":      poster or omdb.get("poster_omdb", ""),
        "imdb_rating": omdb.get("imdb_rating", ""),
        "imdb_id":     omdb.get("imdb_id", ""),
        "plot":        omdb.get("plot", ""),
        "director":    omdb.get("director", ""),
        "actors":      omdb.get("actors", ""),
        "cinemas":     cinemas,
    }

# ── City scraper ──────────────────────────────────────────────────────────────

def fetch_city(slug: str) -> str:
    r = requests.get(BASE_URL, params={"stadt": slug}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def parse_city(html: str, city_name: str) -> list[dict]:
    soup  = BeautifulSoup(html, "html.parser")
    dates = extract_dates(soup)
    print(f"  Dates: {dates}")

    all_h2 = soup.find_all("h2")
    movies: list[dict] = []

    for idx, h2 in enumerate(all_h2):
        title_text = h2.get_text(" ", strip=True)
        if "(OmU)" not in title_text:
            continue

        next_h2 = all_h2[idx + 1] if idx + 1 < len(all_h2) else None
        movie   = parse_film_block(h2, next_h2, dates, city_name)
        if movie:
            movies.append(movie)

    return movies

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    output = {"generated_at": datetime.now().isoformat(), "cities": {}}

    for cfg in CITIES:
        name, slug = cfg["name"], cfg["slug"]
        print(f"\nFetching {name}…")
        try:
            html   = fetch_city(slug)
            movies = parse_city(html, name)
            print(f"  → {len(movies)} OmU-Film(e)")
            output["cities"][name] = {"optional": cfg["optional"], "movies": movies}
        except Exception as exc:
            print(f"  ✗ Fehler: {exc}")
            output["cities"][name] = {"optional": cfg["optional"], "movies": [], "error": str(exc)}

    # Safety check
    raw = json.dumps(output, ensure_ascii=False)
    size_mb = len(raw) / 1_000_000
    print(f"\nOutput: {size_mb:.2f} MB")
    if size_mb > MAX_JSON_MB:
        print(f"FEHLER: Output zu groß ({size_mb:.1f} MB > {MAX_JSON_MB} MB), breche ab.")
        raise SystemExit(1)

    with open("data.json", "w", encoding="utf-8") as f:
        f.write(raw)

    total = sum(len(c["movies"]) for c in output["cities"].values())
    print(f"✓ data.json gespeichert ({total} OmU-Filme, {size_mb:.2f} MB)")

if __name__ == "__main__":
    main()
