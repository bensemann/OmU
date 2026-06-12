#!/usr/bin/env python3
"""
OmU Kino Scraper
Fetches OmU/OV showtimes from allekinos.de and enriches with IMDb data.
Run daily via GitHub Actions – output: data.json
"""

import requests
from bs4 import BeautifulSoup
import json
import re
import os
from datetime import datetime, timedelta

# ── Configuration ────────────────────────────────────────────────────────────

CITIES = [
    {"name": "Wiesbaden",         "slug": "Wiesbaden",         "optional": False},
    {"name": "Mainz",             "slug": "Mainz",             "optional": False},
    {"name": "Geisenheim",        "slug": "Geisenheim",        "optional": False},
    {"name": "Frankfurt am Main", "slug": "Frankfurt am Main", "optional": True},
    {"name": "Sulzbach (Taunus)", "slug": "Sulzbach (Taunus)", "optional": True},
]

OMDB_KEY  = os.environ.get("OMDB_API_KEY", "")
BASE_URL  = "https://allekinos.de/programm"
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

MONTH_MAP = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4, "Mai": 5,
    "Juni": 6, "Juli": 7, "August": 8, "September": 9,
    "Oktober": 10, "November": 11, "Dezember": 12,
}

omdb_cache: dict = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date_header(text: str) -> str | None:
    """'Do. 11. Juni' → '2026-06-11'"""
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
    """'2 Std. 25 Min.' → 145"""
    m = re.search(r'(\d+)\s*Std\.\s*(\d+)\s*Min\.', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r'(\d+)\s*Min\.', text)
    if m:
        return int(m.group(1))
    return None


def get_omdb(title: str, year: str | None = None) -> dict:
    """OMDb API lookup; returns {} if no key or no match."""
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
        if d.get("Response") == "True":
            result = {
                "imdb_rating": d.get("imdbRating", ""),
                "imdb_id":     d.get("imdbID", ""),
                "poster_omdb": d.get("Poster", ""),
            }
        else:
            result = {}
    except Exception:
        result = {}
    omdb_cache[key] = result
    return result

# ── Core parser ───────────────────────────────────────────────────────────────

TIME_RE    = re.compile(r'^\d{1,2}:\d{2}$')
VERSION_RE = re.compile(r'\s*\((OmU|OV|OmeU)\)\s*')


def fetch_city(slug: str) -> str:
    r = requests.get(BASE_URL, params={"stadt": slug}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def extract_dates(soup: BeautifulSoup) -> list[str]:
    """Return ordered list of ISO date strings from the day-navigation."""
    date_pat = re.compile(r'(Mo|Di|Mi|Do|Fr|Sa|So)\.\s+\d+\.\s+\w+')
    seen, dates = set(), []
    for el in soup.find_all(string=date_pat):
        raw = el.strip()
        parsed = parse_date_header(raw)
        if parsed and parsed not in seen:
            seen.add(parsed)
            dates.append(parsed)
    return dates


def parse_city(html: str, city_name: str) -> list[dict]:
    soup  = BeautifulSoup(html, "html.parser")
    dates = extract_dates(soup)
    print(f"  Dates found: {dates}")

    movies: list[dict] = []

    for h2 in soup.find_all("h2"):
        title_text = h2.get_text(" ", strip=True)

        is_omu  = "(OmU)"  in title_text
        is_ov   = "(OV)"   in title_text
        is_omeu = "(OmeU)" in title_text

        if not (is_omu or is_ov or is_omeu):
            continue

        version     = "OmU" if is_omu else ("OmeU" if is_omeu else "OV")
        clean_title = VERSION_RE.sub("", title_text).strip()

        # --- navigate up to the movie 'card' container ---
        container = h2
        for _ in range(6):
            if container.parent:
                container = container.parent
            if container.name in ("article", "section", "main"):
                break

        # --- poster ---
        img = h2.find_previous("img")
        poster = ""
        if img:
            src = img.get("data-src") or img.get("src") or ""
            if src and not src.startswith("data:"):
                poster = src if src.startswith("http") else "https://allekinos.de" + src

        # --- metadata: genres, year, runtime, FSK ---
        genres = []
        for a in container.find_all("a", href=re.compile(r"genre=")):
            g = a.get_text(strip=True)
            if g and g not in genres:
                genres.append(g)

        container_text = container.get_text(" ", strip=True)
        year_m  = re.search(r'\b(20\d{2})\b', container_text)
        year    = year_m.group(1) if year_m else None
        runtime = parse_runtime(container_text)
        fsk_m   = re.search(r'FSK\s*(\d+)', container_text)
        fsk     = fsk_m.group(1) if fsk_m else None

        # --- OMDb enrichment ---
        omdb = get_omdb(clean_title, year)

        # --- cinemas & showtimes ---
        cinemas = extract_cinemas(container, dates, city_name)

        if not cinemas:
            continue

        movies.append({
            "title":      clean_title,
            "version":    version,
            "genres":     genres,
            "year":       year,
            "runtime":    runtime,
            "fsk":        fsk,
            "poster":     poster or omdb.get("poster_omdb", ""),
            "imdb_rating": omdb.get("imdb_rating", ""),
            "imdb_id":    omdb.get("imdb_id", ""),
            "cinemas":    cinemas,
        })

    return movies


def extract_cinemas(container, dates: list[str], city_name: str) -> list[dict]:
    """
    Extract cinema name + per-date showtimes from a movie card.

    Structure on allekinos.de (as observed):
      <a href="?kino=...">Cinema Name</a>
      <span/p>Address</span>
      <table> or grid with one row/cell per day, each containing time links.
    """
    cinemas: list[dict] = []

    # Find cinema-name links (they filter by ?kino=)
    kino_links = container.find_all("a", href=re.compile(r"kino="))

    for kino_a in kino_links:
        cinema_name = kino_a.get_text(strip=True)
        if not cinema_name:
            continue

        # Address: next non-empty sibling text
        address = ""
        for sibling in kino_a.next_siblings:
            t = sibling.get_text(strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
            if t and not re.search(r'kino=|genre=|film=', getattr(sibling, "attrs", {}).get("href", "")):
                if re.match(r'[A-ZÄÖÜa-z]', t) and len(t) < 80:
                    address = t
                    break

        # Showtime table/rows: find the nearest parent that acts as a grid
        # and extract one cell per date column.
        showtimes = extract_showtimes_for_cinema(kino_a, dates)

        if showtimes or True:  # always add, even without showtimes
            cinemas.append({
                "name":      cinema_name,
                "address":   address,
                "city":      city_name,
                "showtimes": showtimes,
            })

    # Fallback: cinema name shown as plain text (e.g. on single-cinema filtered pages)
    if not cinemas:
        cinemas = extract_cinemas_from_text(container, dates, city_name)

    return cinemas


def extract_showtimes_for_cinema(kino_a, dates: list[str]) -> list[dict]:
    """
    Walk forward from the cinema-name anchor to collect date-indexed showtimes.

    allekinos.de renders a table where each <tr> (or equivalent row) corresponds
    to one day in the date header. Within a row, <a> tags carry ticket URLs and
    plain text nodes carry unlinkable times (e.g. already-started today).
    """
    showtimes: list[dict] = []

    # Find the showtime grid: look for nearest table or container with rows
    # that follows the kino_a element.
    parent = kino_a.parent

    # Walk up a bit to find the cinema block
    for _ in range(5):
        if parent is None:
            break
        # Look for a table sibling or descendant
        table = parent.find("table")
        if table:
            showtimes = parse_showtime_table(table, dates)
            return showtimes

        # Or look for a row-like structure (divs/tds that act as day cells)
        rows = parent.find_all(["tr", "div"], class_=re.compile(r"row|day|show|time", re.I))
        if rows:
            showtimes = parse_showtime_rows(rows, dates)
            return showtimes

        parent = parent.parent

    # Last resort: collect all time-links after kino_a until next cinema link
    showtimes = collect_time_links_after(kino_a, dates)
    return showtimes


def parse_showtime_table(table, dates: list[str]) -> list[dict]:
    """Parse an HTML <table> where each <td> = one date column."""
    showtimes: list[dict] = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        for i, cell in enumerate(cells):
            date = dates[i] if i < len(dates) else None
            for a in cell.find_all("a"):
                t = a.get_text(strip=True)
                if TIME_RE.match(t):
                    showtimes.append({
                        "date": date,
                        "time": t,
                        "url":  a.get("href", ""),
                    })
            # Unlinked times
            for string in cell.strings:
                t = string.strip()
                if TIME_RE.match(t):
                    showtimes.append({
                        "date": date,
                        "time": t,
                        "url":  "",
                    })
    return showtimes


def parse_showtime_rows(rows, dates: list[str]) -> list[dict]:
    """Parse a list of row-elements where row[i] = dates[i]."""
    showtimes: list[dict] = []
    for i, row in enumerate(rows):
        date = dates[i] if i < len(dates) else None
        for a in row.find_all("a"):
            t = a.get_text(strip=True)
            if TIME_RE.match(t):
                showtimes.append({"date": date, "time": t, "url": a.get("href", "")})
        for string in row.strings:
            t = string.strip()
            if TIME_RE.match(t):
                showtimes.append({"date": date, "time": t, "url": ""})
    return showtimes


def collect_time_links_after(kino_a, dates: list[str]) -> list[dict]:
    """
    Fallback: walk siblings/descendants after kino_a, collecting time links
    until we hit the next cinema link. Associate times with dates by position.
    """
    showtimes: list[dict] = []
    date_idx  = 0
    in_cinema = False

    for el in kino_a.next_elements:
        if el is kino_a:
            in_cinema = True
            continue

        if not in_cinema:
            continue

        # Stop when we hit another cinema link
        if hasattr(el, "attrs") and "kino=" in el.attrs.get("href", ""):
            break

        if hasattr(el, "name") and el.name == "a":
            t = el.get_text(strip=True)
            if TIME_RE.match(t):
                date = dates[date_idx] if date_idx < len(dates) else None
                showtimes.append({"date": date, "time": t, "url": el.get("href", "")})
                # After collecting times for one day, advance date on next different time or new group
        elif isinstance(el, str):
            for t in re.findall(r'\d{1,2}:\d{2}', el):
                if TIME_RE.match(t):
                    date = dates[date_idx] if date_idx < len(dates) else None
                    showtimes.append({"date": date, "time": t, "url": ""})

    return showtimes


def extract_cinemas_from_text(container, dates, city_name) -> list[dict]:
    """
    Fallback for pages where cinema is plain text (e.g. single-cinema pages).
    Find the cinema name by exclusion: not a link with kino= or genre=,
    appears between movie description and showtimes.
    """
    # Collect all a-tags with time text → these are showtimes
    time_links = []
    for a in container.find_all("a"):
        t = a.get_text(strip=True)
        if TIME_RE.match(t):
            time_links.append({"time": t, "url": a.get("href", ""), "date": None})

    # Try to find a cinema name from text
    text_lines = [t.strip() for t in container.get_text("\n").split("\n") if t.strip()]
    cinema_name = ""
    for line in text_lines:
        # A cinema name is a medium-length line, not containing times or genres
        if (10 < len(line) < 60
                and not re.search(r'\d{1,2}:\d{2}', line)
                and not re.search(r'FSK|Std\.|Min\.|20\d\d', line)
                and not line.startswith("[")):
            cinema_name = line
            break

    if not cinema_name:
        cinema_name = city_name

    # Assign dates by position (naive)
    dated: list[dict] = []
    for i, st in enumerate(time_links):
        st["date"] = dates[i % len(dates)] if dates else None
        dated.append(st)

    return [{"name": cinema_name, "address": "", "city": city_name, "showtimes": dated}]


# ── Output sanitizer ─────────────────────────────────────────────────────────

def sanitize_output(obj, max_str: int = 400):
    """
    Recursively walk the output dict/list and:
    - Drop any string that starts with 'data:' (base64 inline images/URIs)
    - Cap any remaining string at max_str characters
    This prevents allekinos.de lazy-load placeholders from bloating data.json.
    """
    if isinstance(obj, dict):
        return {k: sanitize_output(v, max_str) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_output(i, max_str) for i in obj]
    if isinstance(obj, str):
        if obj.startswith("data:"):
            return ""
        return obj[:max_str] if len(obj) > max_str else obj
    return obj


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    output = {
        "generated_at": datetime.now().isoformat(),
        "cities": {},
    }

    for cfg in CITIES:
        name, slug = cfg["name"], cfg["slug"]
        print(f"\nFetching {name}…")
        try:
            html   = fetch_city(slug)
            movies = parse_city(html, name)
            print(f"  → {len(movies)} OmU/OV film(e)")
            output["cities"][name] = {
                "optional": cfg["optional"],
                "movies":   movies,
            }
        except Exception as exc:
            print(f"  ✗ Fehler: {exc}")
            output["cities"][name] = {
                "optional": cfg["optional"],
                "movies":   [],
                "error":    str(exc),
            }

    output = sanitize_output(output)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    size_mb = os.path.getsize("data.json") / 1_000_000
    total = sum(len(c["movies"]) for c in output["cities"].values())
    print(f"\n✓ data.json gespeichert ({total} Filme, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
