#!/usr/bin/env python3
"""
OmU Kino Scraper – rewrites from scratch after DOM analysis.
Scrapes allekinos.de city pages, scopes each film to h2→next-h2 boundaries,
extracts only OmU showings. Enriches with OMDb for IMDb ratings.
Also scrapes kinopolis.de wochenübersicht pages for next-week data.
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
    {"name": "Gießen",           "slug": "Gießen",           "optional": True},
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

# ── Kinopolis config ───────────────────────────────────────────────────────────

KP_BASE = "https://www.kinopolis.de"
KP_DATE_RE = re.compile(r'(\d{1,2})\.(\d{2})\.')

# Venues to scrape from kinopolis.de for supplemental next-week data.
# week=2 = Wochenübersicht for next week (Mon–Sun).
KINOPOLIS_VENUES = [
    {"code": "su", "cinema": "Kinopolis MTZ",        "city": "Sulzbach (Taunus)", "optional": True},
    {"code": "kp", "cinema": "Kinopolis Darmstadt",  "city": "Darmstadt",         "optional": True},
    {"code": "cd", "cinema": "Citydome Darmstadt",   "city": "Darmstadt",         "optional": True},
    {"code": "rx", "cinema": "programmkino rex",     "city": "Darmstadt",         "optional": True},
]

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
        # allekinos.de shows only the next ~8 days, never past years
        # → no year rollover; past dates get filtered by the frontend
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

    def _query(params: dict) -> dict:
        try:
            r = requests.get("https://www.omdbapi.com/", params=params, timeout=6)
            d = r.json()
            if d.get("Response") == "True":
                return {
                    "imdb_rating": d.get("imdbRating", ""),
                    "imdb_id":     d.get("imdbID", ""),
                    "poster_omdb": d.get("Poster", ""),
                    "plot":        d.get("Plot", ""),
                    "director":    d.get("Director", ""),
                    "actors":      d.get("Actors", ""),
                }
        except Exception:
            pass
        return {}

    params = {"apikey": OMDB_KEY, "t": title, "type": "movie", "plot": "full"}
    if year:
        params["y"] = year
    result = _query(params)
    # Retry without year for better foreign-title matching
    if not result and year:
        params2 = {k: v for k, v in params.items() if k != "y"}
        result = _query(params2)

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

    # ── poster (img immediately before h2, only if it belongs to this film) ──
    poster = ""
    img = h2.find_previous("img")
    if img:
        # Only use if no other h2 sits between this img and the current h2.
        # find_next("h2") from the img must point to THIS h2, not a different one.
        next_h2_from_img = img.find_next("h2")
        if next_h2_from_img is h2:
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

# ── Kinopolis scraper ─────────────────────────────────────────────────────────

def parse_kinopolis_date(text: str) -> str | None:
    """Parse 'Sa.  20.06.' → '2026-06-20', handling Dec/Jan rollover."""
    m = KP_DATE_RE.search(text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    now = datetime.now()
    year = now.year
    try:
        d = datetime(year, month, day)
        # If date is more than 30 days in the past it must be next year
        if (now - d).days > 30:
            d = datetime(year + 1, month, day)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None

def fetch_kinopolis(code: str, week: int) -> str:
    url = f"{KP_BASE}/{code}/programm/woche-{week}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def parse_kinopolis_page(html: str, cinema_name: str, city_name: str) -> list[dict]:
    """Extract OmU movies from a kinopolis.de Wochenübersicht page."""
    soup = BeautifulSoup(html, "html.parser")
    movies: list[dict] = []

    for section in soup.select("section.movie"):
        h2 = section.find("h2")
        if not h2:
            continue
        title_link = h2.find("a")
        raw_title = (title_link or h2).get_text(strip=True)
        # Strip alternate original title: "German Title / ORIGINAL TITLE"
        title = re.split(r'\s*/\s*', raw_title)[0].strip()

        # Poster from kinopolis CDN
        poster = ""
        img = section.find("img", src=re.compile(r"plakate_"))
        if img:
            poster = img.get("src", "")[:MAX_URL_LEN]

        # Dates (.prog-nav__link) align 1:1 with day wrappers (.prog-day__wrapper)
        nav_links   = section.select(".prog-nav__link")
        day_wrappers = section.select(".prog-day__wrapper")

        showtimes_list: list[dict] = []

        for nav_link, wrapper in zip(nav_links, day_wrappers):
            date_str = parse_kinopolis_date(nav_link.get_text())
            if not date_str:
                continue

            # Each .prog2__cont is one showing; check for OmU badge
            for cont in wrapper.select(".prog2__cont"):
                if not cont.select(".tech__omu"):
                    continue
                # Booking link contains the time text
                time_link = cont.find("a", href=re.compile(r"/vorstellung/"))
                if not time_link:
                    continue
                time_text = time_link.get_text(strip=True)
                if not TIME_RE.match(time_text):
                    continue
                href = time_link.get("href", "")
                if href and not href.startswith("http"):
                    href = KP_BASE + href
                showtimes_list.append({
                    "time": time_text,
                    "date": date_str,
                    "url":  clean_url(href),
                })

        if not showtimes_list:
            continue

        # Metadata from section text
        meta_text = section.get_text(" ", strip=True)
        year_m    = re.search(r'Produktionsjahr:\s*(20\d{2})', meta_text)
        year      = year_m.group(1) if year_m else None
        runtime_m = re.search(r'Dauer:\s*(\d+)\s*Minuten', meta_text)
        runtime   = int(runtime_m.group(1)) if runtime_m else None
        fsk_m     = re.search(r'FSK:\s*(?:ab\s*)?(\d+)', meta_text)
        fsk       = fsk_m.group(1) if fsk_m else None

        omdb = get_omdb(title, year)

        movies.append({
            "title":       title,
            "version":     "OmU",
            "genres":      [],
            "year":        year,
            "runtime":     runtime,
            "fsk":         fsk,
            "poster":      poster or omdb.get("poster_omdb", ""),
            "imdb_rating": omdb.get("imdb_rating", ""),
            "imdb_id":     omdb.get("imdb_id", ""),
            "plot":        omdb.get("plot", ""),
            "director":    omdb.get("director", ""),
            "actors":      omdb.get("actors", ""),
            "cinemas": [{
                "name":      cinema_name,
                "address":   "",
                "city":      city_name,
                "showtimes": showtimes_list,
            }],
        })

    return movies

def merge_kinopolis(output: dict, kp_movies: list[dict], city_name: str, optional: bool) -> None:
    """
    Merge kinopolis movies into the output dict for the given city.
    Films with the same title are merged: showtimes are added to the existing
    cinema entry (deduped by date+time), or a new cinema entry is appended.
    New films are appended as-is.
    """
    if city_name not in output["cities"]:
        output["cities"][city_name] = {"optional": optional, "movies": []}

    city_movies = output["cities"][city_name]["movies"]

    for kp_movie in kp_movies:
        # Look for existing film with same title (case-insensitive)
        existing = next(
            (m for m in city_movies if m["title"].lower() == kp_movie["title"].lower()),
            None,
        )
        if existing is None:
            city_movies.append(kp_movie)
            continue

        # Film exists → merge cinemas/showtimes
        for kp_cinema in kp_movie["cinemas"]:
            ex_cinema = next(
                (c for c in existing["cinemas"] if c["name"] == kp_cinema["name"]),
                None,
            )
            if ex_cinema is None:
                existing["cinemas"].append(kp_cinema)
            else:
                existing_keys = {(s["date"], s["time"]) for s in ex_cinema["showtimes"]}
                for st in kp_cinema["showtimes"]:
                    if (st["date"], st["time"]) not in existing_keys:
                        ex_cinema["showtimes"].append(st)
                        existing_keys.add((st["date"], st["time"]))

# ── Murnau-Filmtheater scraper (server-rendered Drupal Views) ─────────────────

MURNAU_URL      = "https://www.murnau-stiftung.de/index.php/filmtheater/kinoprogramm"
MURNAU_CINEMA   = "Murnau-Filmtheater"
MURNAU_CITY     = "Wiesbaden"
MURNAU_DATE_RE  = re.compile(r'\w+\s+(\d{1,2})\.(\d{1,2})\.(\d{4})')
MURNAU_WINDOW   = 60   # days ahead to include

def parse_murnau_date(text: str) -> str | None:
    m = MURNAU_DATE_RE.search(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None

def fetch_parse_murnau() -> list[dict]:
    """Fetch Murnau-Filmtheater program and return OmU movies."""
    try:
        r = requests.get(MURNAU_URL, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except Exception as exc:
        print(f"  ✗ Murnau Fehler: {exc}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cutoff = (datetime.now() + timedelta(days=MURNAU_WINDOW)).strftime("%Y-%m-%d")

    # Group showtimes by clean title
    title_showtimes: dict[str, list[dict]] = {}

    for row in soup.select(".views-row"):
        date_el   = row.select_one(".views-field-field-cinema-show-date")
        time_el   = row.select_one(".views-field-field-cinema-show-date-1")
        title_el  = row.select_one(".views-field-field-cinema-show-movie")
        ticket_el = row.select_one(".views-field-field-dd-cinema-show-ticket")

        if not (date_el and time_el and title_el):
            continue

        title_text = title_el.get_text(strip=True)
        if "(OMU)" not in title_text.upper():
            continue

        date_str = parse_murnau_date(date_el.get_text(strip=True))
        time_m   = re.search(r'(\d{1,2}:\d{2})', time_el.get_text(strip=True))
        if not date_str or not time_m:
            continue
        if date_str > cutoff:
            continue

        time_str = time_m.group(1)

        ticket_url = ""
        if ticket_el:
            a = ticket_el.find("a")
            if a:
                href = a.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.murnau-stiftung.de" + href
                ticket_url = clean_url(href)

        # Strip OmU suffix (handles "(OmU)" / "(OMU)") from end of title
        clean_t = re.sub(r'\s*\(OmU\)\s*$', '', title_text, flags=re.IGNORECASE).strip()

        if clean_t not in title_showtimes:
            title_showtimes[clean_t] = []
        title_showtimes[clean_t].append({
            "time": time_str, "date": date_str, "url": ticket_url
        })

    movies = []
    for title, showtimes in title_showtimes.items():
        omdb = get_omdb(title, None)
        movies.append({
            "title":       title,
            "version":     "OmU",
            "genres":      [],
            "year":        None,
            "runtime":     None,
            "fsk":         None,
            "poster":      omdb.get("poster_omdb", ""),
            "imdb_rating": omdb.get("imdb_rating", ""),
            "imdb_id":     omdb.get("imdb_id", ""),
            "plot":        omdb.get("plot", ""),
            "director":    omdb.get("director", ""),
            "actors":      omdb.get("actors", ""),
            "cinemas": [{
                "name":      MURNAU_CINEMA,
                "address":   "Murnaustraße 6",
                "city":      MURNAU_CITY,
                "showtimes": showtimes,
            }],
        })
    return movies


# ── Caligari scraper (Playwright – JS-rendered cinetixx Angular app) ──────────

CALIGARI_URL    = ("https://booking.cinetixx.de/frontend/index.html"
                   "?cinemaId=2079319745&bgswitch=false&resize=false#/program/2079319745")
CALIGARI_CINEMA = "Caligari FilmBühne"
CALIGARI_CITY   = "Wiesbaden"
CT_DATE_RE      = re.compile(r'(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{1,2})\.(\d{2})')
CT_DATE_ONLY_RE = re.compile(r'^(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+\d{1,2}\.\d{2}$')
CT_TIME_ONLY_RE = re.compile(r'^(?:\d{1,2}:\d{2}|-)$')

def parse_cinetixx_dates(text: str) -> list[str]:
    """Parse 14-date header row into ISO date list."""
    now = datetime.now()
    dates = []
    for m in CT_DATE_RE.finditer(text):
        day, month = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            d = datetime(year, month, day)
            if (now - d).days > 30:
                d = datetime(year + 1, month, day)
            dates.append(d.strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return dates

def _normalize_caligari_text(text: str) -> str:
    """
    Playwright inner_text() may render each date/time grid cell on its own line
    instead of all 14 on a single line.  Join consecutive runs of pure date tokens
    (e.g. "Sa 13.06") or pure time tokens (e.g. "20:00" / "-") into a single line
    so the downstream parser can match them as a 14-token row.
    """
    raw_lines = text.split('\n')
    result: list[str] = []
    i = 0
    while i < len(raw_lines):
        stripped = raw_lines[i].strip()

        # Accumulate consecutive individual date lines
        if CT_DATE_ONLY_RE.match(stripped):
            run: list[str] = []
            while i < len(raw_lines) and CT_DATE_ONLY_RE.match(raw_lines[i].strip()):
                run.append(raw_lines[i].strip())
                i += 1
            # Only join if it looks like a real date header (≥7 dates)
            if len(run) >= 7:
                result.append(' '.join(run))
            else:
                result.extend(run)
            continue

        # Accumulate consecutive individual time/dash lines
        if stripped and CT_TIME_ONLY_RE.match(stripped):
            run = []
            while i < len(raw_lines) and raw_lines[i].strip() and CT_TIME_ONLY_RE.match(raw_lines[i].strip()):
                run.append(raw_lines[i].strip())
                i += 1
            if len(run) >= 7:
                result.append(' '.join(run))
            else:
                result.extend(run)
            continue

        result.append(raw_lines[i])
        i += 1

    return '\n'.join(result)


def fetch_caligari_text() -> str:
    """Render the Caligari cinetixx page with Playwright and return body text."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠ playwright not installed – Caligari übersprungen")
        return ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(CALIGARI_URL, wait_until="networkidle", timeout=45_000)
            page.wait_for_selector("text=FILMINFO", timeout=20_000)
            text = page.inner_text("body")
        except Exception as exc:
            print(f"  ⚠ Caligari Ladefehler: {exc}")
            text = ""
        finally:
            browser.close()
    return text

def parse_caligari(text: str) -> list[dict]:
    """Parse the plain text of the rendered Caligari program page."""
    if not text:
        return []

    # Drop upcoming section
    dem_idx = text.find("Demnächst in Caligari")
    if dem_idx > 0:
        text = text[:dem_idx]

    # Normalise: join per-line date/time tokens into single rows so the parser
    # works regardless of whether Playwright rendered them inline or block-level.
    text = _normalize_caligari_text(text)

    movies = []

    for block in text.split("FILMINFO"):
        block = block.strip()
        if not block:
            continue

        # Split at "Saal" (allow surrounding whitespace / blank lines)
        parts = re.split(r'\n\s*Saal\s*\n', block)
        if len(parts) < 2:
            continue

        # Film metadata from preamble (first part)
        preamble = parts[0]
        runtime = None
        rm = re.search(r'Länge:\s*(\d+)\s*min', preamble)
        if rm:
            runtime = int(rm.group(1))
        gm = re.search(r'Genre:\s*([^\n]+)', preamble)
        genres = [g.strip() for g in gm.group(1).split(",")] if gm else []
        genres = [g for g in genres if g and g != '-']
        year_m = re.search(r'\b(20\d{2})\b', preamble)
        year = year_m.group(1) if year_m else None

        # Each Saal section (parts[1], parts[2], …)
        for i, saal in enumerate(parts[1:], 1):
            # OmU title = last line containing "(OmU)" in the preceding part
            prev_lines = [l.strip() for l in parts[i-1].split("\n") if l.strip()]
            title_line = next(
                (l for l in reversed(prev_lines) if "(OmU)" in l or "(OV)" in l),
                None
            )
            if not title_line or "(OmU)" not in title_line:
                continue

            title = re.sub(r'\s*\(OmU[^)]*\)\s*', '', title_line).strip()
            title = re.sub(r'\s*\(\d{2}\.\d{2}\.\d{4}\)\s*', '', title).strip()
            if not title:
                continue

            saal_lines = [l.strip() for l in saal.split("\n")]

            # Find date header row (≥7 date tokens on one line after normalisation)
            dates: list[str] = []
            for line in saal_lines:
                candidate = parse_cinetixx_dates(line)
                if len(candidate) >= 7:
                    dates = candidate
                    break
            if not dates:
                continue

            n = len(dates)

            # Find time rows: exactly len(dates) tokens of HH:MM or "-"
            showtimes: list[dict] = []
            for line in saal_lines:
                tokens = line.split()
                if len(tokens) == n and all(
                    re.match(r'^\d{1,2}:\d{2}$', t) or t == '-' for t in tokens
                ):
                    for j, token in enumerate(tokens):
                        if token != '-':
                            showtimes.append({
                                "time": token,
                                "date": dates[j],
                                "url":  "",
                            })

            if not showtimes:
                continue

            omdb = get_omdb(title, year)
            movies.append({
                "title":       title,
                "version":     "OmU",
                "genres":      genres[:5],
                "year":        year,
                "runtime":     runtime,
                "fsk":         None,
                "poster":      omdb.get("poster_omdb", ""),
                "imdb_rating": omdb.get("imdb_rating", ""),
                "imdb_id":     omdb.get("imdb_id", ""),
                "plot":        omdb.get("plot", ""),
                "director":    omdb.get("director", ""),
                "actors":      omdb.get("actors", ""),
                "cinemas": [{
                    "name":      CALIGARI_CINEMA,
                    "address":   "Marktplatz 9",
                    "city":      CALIGARI_CITY,
                    "showtimes": showtimes,
                }],
            })

    return movies


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

    # ── Kinopolis supplemental scraping (woche-2 = next week) ────────────────────
    print("\n=== Kinopolis – nächste Woche (woche-2) ===")
    for venue in KINOPOLIS_VENUES:
        code      = venue["code"]
        cinema    = venue["cinema"]
        city_name = venue["city"]
        optional  = venue["optional"]
        print(f"\n  {cinema} ({code}/woche-2)…")
        try:
            html      = fetch_kinopolis(code, 2)
            kp_movies = parse_kinopolis_page(html, cinema, city_name)
            print(f"    → {len(kp_movies)} OmU-Film(e)")
            merge_kinopolis(output, kp_movies, city_name, optional)
        except Exception as exc:
            print(f"    ✗ Fehler: {exc}")

    # ── Murnau-Filmtheater ────────────────────────────────────────────────────
    print("\n=== Murnau-Filmtheater (Wiesbaden) ===")
    try:
        murnau_movies = fetch_parse_murnau()
        print(f"  → {len(murnau_movies)} OmU-Film(e)")
        merge_kinopolis(output, murnau_movies, MURNAU_CITY, optional=False)
    except Exception as exc:
        print(f"  ✗ Fehler: {exc}")

    # ── Remove Caligari entries from allekinos.de before Playwright scrape ────
    # allekinos.de has no booking URLs for Caligari → all showtimes land on
    # today's date (wrong).  Strip them so the Playwright data is authoritative.
    if CALIGARI_CITY in output["cities"]:
        for movie in output["cities"][CALIGARI_CITY]["movies"]:
            movie["cinemas"] = [
                c for c in movie["cinemas"]
                if not (
                    c["name"].startswith("Caligari") and
                    all(not s.get("url") for s in c["showtimes"])
                )
            ]
        output["cities"][CALIGARI_CITY]["movies"] = [
            m for m in output["cities"][CALIGARI_CITY]["movies"] if m["cinemas"]
        ]

    # ── Caligari FilmBühne (JS-rendered via Playwright) ───────────────────────
    print("\n=== Caligari FilmBühne (Wiesbaden) ===")
    try:
        caligari_text   = fetch_caligari_text()
        caligari_movies = parse_caligari(caligari_text)
        print(f"  → {len(caligari_movies)} OmU-Film(e)")
        merge_kinopolis(output, caligari_movies, CALIGARI_CITY, optional=False)
    except Exception as exc:
        print(f"  ✗ Fehler: {exc}")

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
