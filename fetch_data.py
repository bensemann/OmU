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
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────

CITIES = [
    {"name": "Wiesbaden",         "slug": "Wiesbaden",         "optional": False},
    {"name": "Mainz",             "slug": "Mainz",             "optional": False},
    {"name": "Geisenheim",        "slug": "Geisenheim",        "optional": False},
    {"name": "Frankfurt am Main", "slug": "Frankfurt am Main", "optional": True},
    {"name": "Darmstadt",         "slug": "Darmstadt",         "optional": True},
    {"name": "Sulzbach (Taunus)", "slug": "Sulzbach (Taunus)", "optional": True},
    # Gießen entfernt – keine OmU-Vorstellungen
]

OMDB_KEY  = os.environ.get("OMDB_API_KEY", "")
TMDB_KEY  = os.environ.get("TMDB_API_KEY", "")
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"
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
tmdb_cache: dict = {}

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

    def _query_by_id(imdb_id: str) -> dict:
        return _query({"apikey": OMDB_KEY, "i": imdb_id, "type": "movie", "plot": "full"})

    base = {"apikey": OMDB_KEY, "t": title, "type": "movie", "plot": "full"}

    # 1) Exact title + year
    params = {**base}
    if year:
        params["y"] = year
    result = _query(params)

    # 2) Exact title without year
    if not result and year:
        result = _query(base)

    # 3) Simplified title (strip subtitle after " – " / " - " / ": ")
    short_title = re.split(r'\s+[–—-]\s+|\s*:\s+', title, maxsplit=1)[0].strip()
    if not result and short_title != title:
        p3 = {**base, "t": short_title}
        if year:
            p3["y"] = year
        result = _query(p3)
        if not result and year:
            result = _query({**base, "t": short_title})

    # NOTE: OMDb search mode (s=) was tried but removed – it returns false positives
    # for German-titled films (e.g. "Das Drama" matches random English films).
    # TMDb integration (see TMDB_API_KEY) is the correct long-term fix.

    omdb_cache[key] = result
    return result

def get_tmdb(title: str, year: str | None = None) -> dict:
    """
    Query TMDb for movie metadata.  Returns the same keys as get_omdb() so the
    two enrichment functions are interchangeable.  TMDb covers German-titled and
    international films far better than OMDb.
    Requires TMDB_API_KEY env variable (free account at themoviedb.org).
    """
    if not TMDB_KEY:
        return {}
    key = f"{title}|{year}"
    if key in tmdb_cache:
        return tmdb_cache[key]

    now_year = datetime.now().year

    def _search_all(query: str) -> list[dict]:
        """Return all TMDb results for a query (no year filter – we do that ourselves)."""
        params: dict = {"api_key": TMDB_KEY, "query": query}
        try:
            r = requests.get(
                "https://api.themoviedb.org/3/search/movie",
                params=params, timeout=8,
            )
            return r.json().get("results", [])
        except Exception:
            return []

    def _year_score(hit: dict) -> int:
        """
        Score a TMDb result by year plausibility.
        Films currently in German cinemas are almost always ≤ 3 years old.
        If the caller supplied a known year (from the scraped page), use that;
        otherwise apply a recency window.

        Returns:
          3  – strong match (within ±1 of known year, or released ≤ 2 years ago)
          1  – acceptable  (within ±3, or released 3–4 years ago)
         -1  – weak        (older but plausible)
        -99  – implausible (e.g. a 2005 film when we know year=2025)
        """
        release = (hit.get("release_date") or "")[:4]
        if not release or not release.isdigit():
            return 0  # no date → neutral
        hy = int(release)
        if year:
            ky = int(year)
            diff = abs(hy - ky)
            if diff <= 1: return 3
            if diff <= 3: return 1
            return -99           # year mismatch → almost certainly wrong film
        else:
            # No known year: cinema programmes show mostly recent releases
            age = now_year - hy
            if age <= 2:  return 3
            if age <= 4:  return 1
            if age <= 10: return -1
            return -99           # very old film unlikely without year confirmation

    def _best(results: list[dict]) -> dict:
        """Pick the highest-scoring result; return {} if best score is implausible."""
        if not results:
            return {}
        scored = sorted(results, key=_year_score, reverse=True)
        best = scored[0]
        if _year_score(best) <= -99:
            return {}   # refuse to return an implausible match
        return best

    def _title_variants(t: str) -> list[str]:
        """
        Return title search candidates in priority order:
        1. Exact title as given
        2. Content in parentheses  ← "VERFLUCHT NORMAL (I swear)" → "I swear"
        3. Title without parenthetical content
        4. Title without subtitle after " – " / " - " / ": "
        Duplicates are silently skipped.
        """
        seen: set[str] = set()
        out: list[str] = []
        def add(s: str) -> None:
            s = s.strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
        add(t)
        for m in re.finditer(r'\(([^)]{2,})\)', t):
            add(m.group(1))
        no_paren = re.sub(r'\s*\([^)]*\)', '', t).strip()
        add(no_paren)
        add(re.split(r'\s+[–—-]\s+|\s*:\s+', no_paren, maxsplit=1)[0])
        return out

    hit: dict = {}
    newest_fallback: dict = {}   # most recent result seen across all variants

    for variant in _title_variants(title):
        candidates = _search_all(variant)
        if candidates and not newest_fallback:
            # Keep the most recently released result as a last-resort fallback
            newest_fallback = max(
                candidates,
                key=lambda h: h.get("release_date") or "",
            )
        hit = _best(candidates)
        if hit:
            break

    # If no plausible hit found after all variants, use the most recent result
    if not hit:
        hit = newest_fallback

    if not hit:
        tmdb_cache[key] = {}
        return {}

    movie_id = hit.get("id")
    poster_path = hit.get("poster_path") or ""
    overview = hit.get("overview") or ""
    release_year = (hit.get("release_date") or "")[:4]

    # Fetch external IDs (incl. imdb_id) + credits from detail endpoint
    imdb_id = ""
    director = ""
    actors = ""
    try:
        detail = requests.get(
            f"https://api.themoviedb.org/3/movie/{movie_id}",
            params={"api_key": TMDB_KEY, "append_to_response": "external_ids,credits", "language": "de"},
            timeout=8,
        ).json()
        imdb_id = detail.get("external_ids", {}).get("imdb_id") or ""
        credits = detail.get("credits", {})
        director = ", ".join(
            p["name"] for p in credits.get("crew", []) if p.get("job") == "Director"
        )[:100]
        actors = ", ".join(
            p["name"] for p in credits.get("cast", [])[:4]
        )
        # Always prefer German overview from detail endpoint; keep English as fallback
        if detail.get("overview"):
            overview = detail["overview"]
    except Exception:
        pass

    result = {
        "imdb_rating": "",           # TMDb doesn't expose IMDb rating; OMDb used for this
        "tmdb_rating": str(round(hit.get("vote_average", 0), 1)) if hit.get("vote_average") else "",
        "tmdb_id":     str(movie_id) if movie_id else "",
        "imdb_id":     imdb_id,
        "poster_omdb": f"{TMDB_IMG}{poster_path}" if poster_path else "",
        "plot":        overview,
        "director":    director,
        "actors":      actors,
    }
    tmdb_cache[key] = result
    return result


def get_enrichment(title: str, year: str | None = None) -> dict:
    """
    Merge TMDb (posters, plot, cast, imdb_id) with OMDb (imdb_rating).
    TMDb is tried first for everything; OMDb fills in the rating gap.
    """
    tmdb = get_tmdb(title, year)
    omdb = get_omdb(title, year)

    # Prefer TMDb for everything except IMDb rating
    return {
        "imdb_rating": omdb.get("imdb_rating", ""),
        "tmdb_rating": tmdb.get("tmdb_rating", ""),
        "tmdb_id":     tmdb.get("tmdb_id", ""),
        "imdb_id":     tmdb.get("imdb_id") or omdb.get("imdb_id", ""),
        "poster_omdb": tmdb.get("poster_omdb") or omdb.get("poster_omdb", ""),
        "plot":        tmdb.get("plot") or omdb.get("plot", ""),
        "director":    tmdb.get("director") or omdb.get("director", ""),
        "actors":      tmdb.get("actors") or omdb.get("actors", ""),
    }


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

    # ── poster (img before h2, only if it belongs to this film) ──
    # Scan up to 3 preceding imgs in case an icon or ad img sits between
    # the film poster and the h2.
    poster = ""
    for img in h2.find_all_previous("img", limit=3):
        # Only accept if no other h2 sits between this img and the current h2.
        next_h2_from_img = img.find_next("h2")
        if next_h2_from_img is not h2:
            break  # We've passed into a different film's territory
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and not src.endswith(".svg"):
            poster = src if src.startswith("http") else "https://allekinos.de" + src
            poster = poster[:MAX_URL_LEN]
            break

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

    # ── description from allekinos.de page ──────────────────────────────────
    # allekinos.de includes a short plot paragraph after the genre/year line and
    # before the cinema blocks.  Extract it as a fallback when OMDb has nothing.
    ak_description = ""
    for elem in h2.next_elements:
        if elem is next_h2:
            break
        if hasattr(elem, "name") and elem.name == "h2" and elem is not h2:
            break
        # Stop at the cinema section (first kino= link)
        if hasattr(elem, "name") and elem.name == "a":
            if "kino=" in (elem.get("href") or ""):
                break
        if hasattr(elem, "name") and elem.name in ("p", "div", "span"):
            text = elem.get_text(" ", strip=True)
            # Substantial text that doesn't look like pure metadata
            if (len(text) > 60 and
                    not re.match(r'^\d{4}|^FSK|^\d+ Std', text) and
                    "://" not in text and
                    "kino=" not in text):
                # Exclude if it's mostly link text (genre tags etc.)
                links = elem.find_all("a") if hasattr(elem, "find_all") else []
                link_text = " ".join(a.get_text() for a in links)
                non_link = text
                for lt in link_text.split():
                    non_link = non_link.replace(lt, "", 1)
                if len(non_link.strip()) > 60:
                    ak_description = text[:500]
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
                        # Sequential fallback: skip past dates so a cinema that only
                        # has linked (future) showtimes doesn't get assigned to dates[0]
                        # when dates[0] is already in the past (e.g. June 13 when today
                        # is June 15 – those films would be hidden by the frontend).
                        today_idx = next((i for i, d in enumerate(dates) if d >= today), 0)
                        n_linked = sum(1 for s in current["showtimes"] if s.get("url"))
                        has_today = any(s.get("date") == today for s in current["showtimes"])
                        idx = today_idx + (1 if has_today else 0) + n_linked
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
    omdb = get_enrichment(clean_title, year)

    return {
        "title":       clean_title,
        "version":     "OmU",
        "genres":      genres[:5],
        "year":        year,
        "runtime":     runtime,
        "fsk":         fsk,
        "poster":      poster or omdb.get("poster_omdb", ""),
        "imdb_rating": omdb.get("imdb_rating", ""),
        "tmdb_rating": omdb.get("tmdb_rating", ""),
        "tmdb_id":     omdb.get("tmdb_id", ""),
        "imdb_id":     omdb.get("imdb_id", ""),
        # Prefer enrichment plot; fall back to allekinos.de excerpt
        "plot":        omdb.get("plot", "") or ak_description,
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

        # Poster from kinopolis CDN (check data-src for lazy-loaded images)
        poster = ""
        for img in section.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            if "plakate_" in src:
                poster = src[:MAX_URL_LEN]
                break

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

        omdb = get_enrichment(title, year)

        movies.append({
            "title":       title,
            "version":     "OmU",
            "genres":      [],
            "year":        year,
            "runtime":     runtime,
            "fsk":         fsk,
            "poster":      poster or omdb.get("poster_omdb", ""),
            "imdb_rating": omdb.get("imdb_rating", ""),
            "tmdb_rating": omdb.get("tmdb_rating", ""),
            "tmdb_id":     omdb.get("tmdb_id", ""),
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

        # Try to extract poster img from this row (Drupal may include a thumbnail)
        row_poster = ""
        for img in row.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:") and not src.endswith(".svg"):
                if src.startswith("http"):
                    row_poster = src[:MAX_URL_LEN]
                else:
                    row_poster = ("https://www.murnau-stiftung.de" + src)[:MAX_URL_LEN]
                break

        if clean_t not in title_showtimes:
            title_showtimes[clean_t] = {"showtimes": [], "poster": row_poster}
        elif row_poster and not title_showtimes[clean_t]["poster"]:
            title_showtimes[clean_t]["poster"] = row_poster
        title_showtimes[clean_t]["showtimes"].append({
            "time": time_str, "date": date_str, "url": ticket_url
        })

    movies = []
    for title, data in title_showtimes.items():
        showtimes = data["showtimes"]
        site_poster = data["poster"]
        omdb = get_enrichment(title, None)
        movies.append({
            "title":       title,
            "version":     "OmU",
            "genres":      [],
            "year":        None,
            "runtime":     None,
            "fsk":         None,
            "poster":      omdb.get("poster_omdb", "") or site_poster,
            "imdb_rating": omdb.get("imdb_rating", ""),
            "tmdb_rating": omdb.get("tmdb_rating", ""),
            "tmdb_id":     omdb.get("tmdb_id", ""),
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


# ── Caligari FilmBühne scraper (caligari.wiesbaden.de – JS-rendered event widget) ─

CALIGARI_WEBSITE = "https://caligari.wiesbaden.de/programmuebersicht/aktuelles-programm"
CALIGARI_CINEMA  = "Caligari FilmBühne"
CALIGARI_CITY    = "Wiesbaden"
CALIGARI_ADDR    = "Marktplatz 9"

# German day-name date pattern: "Montag, 16. Juni 2026" / "Mo. 16. Jun" / "Di 17.6.2026"
CAL_DATE_RE = re.compile(
    r'(?:Mo(?:ntag)?|Di(?:enstag)?|Mi(?:ttwoch)?|Do(?:nnerstag)?|Fr(?:eitag)?'
    r'|Sa(?:mstag)?|So(?:nntag)?)\w*\s*[\.,]?\s*'
    r'(\d{1,2})\.\s*(\w+)(?:\.)?(?:\s+(\d{4}))?',
    re.IGNORECASE,
)
CAL_TIME_RE = re.compile(r'\b(\d{1,2}:\d{2})\s*(?:Uhr)?\b')

def parse_caligari_date(text: str) -> str | None:
    """Parse German date like 'Montag, 16. Juni 2026' or numeric '16.06.2026'."""
    # Strategy 1: day-name + month-name format
    m = CAL_DATE_RE.search(text)
    if m:
        day       = int(m.group(1))
        month_raw = m.group(2)
        year_str  = m.group(3)
        # Try full German month name (MONTH_MAP has "Januar"…"Dezember")
        month = MONTH_MAP.get(month_raw.capitalize())
        if not month:
            abbr = {
                'jan':1,'feb':2,'mär':3,'mar':3,'apr':4,'mai':5,'jun':6,
                'jul':7,'aug':8,'sep':9,'okt':10,'nov':11,'dez':12,
            }
            month = abbr.get(month_raw.lower()[:3])
        if month:
            year = int(year_str) if year_str and year_str.isdigit() else datetime.now().year
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    # Strategy 2: numeric "16.06.2026"
    m2 = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b', text)
    if m2:
        try:
            return datetime(int(m2.group(3)), int(m2.group(2)), int(m2.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def fetch_caligari_html() -> str:
    """
    Render caligari.wiesbaden.de with Playwright and return the full page HTML.
    The page embeds an inet-mainz.de event-calendar widget that needs JS to load.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠ playwright not installed – Caligari übersprungen")
        return ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            print(f"  → Navigiere zu {CALIGARI_WEBSITE}")
            page.goto(CALIGARI_WEBSITE, wait_until="networkidle", timeout=45_000)

            # The event calendar widget loads asynchronously; try multiple selectors
            found = False
            for selector in [
                "text=OmU", "text=OmF",
                ".sp-eventlist", ".eventlist", ".sp-content",
                "[class*='event']", "[class*='cal-']",
                "article",
            ]:
                try:
                    page.wait_for_selector(selector, timeout=8_000)
                    print(f"  ✓ Selector gefunden: {selector!r}")
                    found = True
                    break
                except Exception:
                    pass

            if not found:
                print("  ⚠ Kein Event-Selector gefunden – warte 5 s zusätzlich")
                page.wait_for_timeout(5_000)

            html    = page.content()
            preview = page.inner_text("body")[:500].replace('\n', ' | ')
            print(f"  ℹ HTML-Größe: {len(html)} Zeichen")
            print(f"  ℹ Text-Preview: {preview}")

        except Exception as exc:
            print(f"  ⚠ Caligari Ladefehler: {exc}")
            html = ""
        finally:
            browser.close()

    return html


def parse_caligari_website(html: str) -> list[dict]:
    """
    Parse caligari.wiesbaden.de (after JS rendering) for OmU events.
    Two strategies: structured HTML element scan, then line-by-line text fallback.
    """
    if not html:
        return []

    soup   = BeautifulSoup(html, "html.parser")
    today  = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

    # Remove chrome (nav/header/footer) to cut down noise
    for tag in soup.find_all(["nav", "footer", "header"]):
        tag.decompose()

    movies_by_title: dict[str, list[dict]] = {}

    # ── Strategy 1: look for structured event items ──────────────────────────
    event_items: list = []
    for selector in [
        ".sp-eventlist-entry", ".eventlist-entry",
        "[class*='eventlist']", "[class*='event-item']",
        "[class*='veranstaltung']", "[class*='cal-entry']",
        "article", "li",
    ]:
        candidates = soup.select(selector)
        omu = [it for it in candidates
               if "(OmU)" in it.get_text() or "(OmF)" in it.get_text()]
        if omu:
            event_items = omu
            print(f"  ℹ Strategy 1: {len(omu)} OmU-Elemente via «{selector}»")
            break

    for item in event_items:
        text = item.get_text("\n", strip=True)
        title_line = next(
            (l.strip() for l in text.split("\n") if "(OmU)" in l or "(OmF)" in l),
            None,
        )
        if not title_line:
            continue
        title = re.sub(r'\s*\(OmU[^)]*\)\s*|\s*\(OmF[^)]*\)\s*', '',
                       title_line, flags=re.IGNORECASE).strip("-– \t")
        if not title or len(title) < 2:
            continue

        date_str = parse_caligari_date(text)
        if not date_str or date_str < today or date_str > cutoff:
            continue

        time_m   = CAL_TIME_RE.search(text)
        time_str = time_m.group(1) if time_m else ""

        url = ""
        for a in item.find_all("a", href=True):
            href = a.get("href", "")
            if any(kw in href.lower() for kw in
                   ["ticket", "booking", "cinetixx", "kinoheld", "reservier", "kauf"]):
                url = href if href.startswith("http") else "https://caligari.wiesbaden.de" + href
                break

        movies_by_title.setdefault(title, []).append(
            {"time": time_str, "date": date_str, "url": url}
        )

    # ── Strategy 2: line-by-line text parsing ────────────────────────────────
    if not movies_by_title:
        print("  ℹ Strategy 2: zeilenweises Text-Parsing")
        lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if "(OmU)" not in line and "(OmF)" not in line:
                continue
            title = re.sub(r'\s*\(OmU[^)]*\)\s*|\s*\(OmF[^)]*\)\s*', '',
                           line, flags=re.IGNORECASE).strip("-– \t:|")
            if not title or len(title) < 2:
                continue
            # Search a ±5-line window for date and time
            window = lines[max(0, i - 5): i + 6]
            date_str = None
            for wl in window:
                d = parse_caligari_date(wl)
                if d and today <= d <= cutoff:
                    date_str = d
                    break
            if not date_str:
                continue
            time_str = ""
            for wl in window:
                tm = CAL_TIME_RE.search(wl)
                if tm:
                    time_str = tm.group(1)
                    break
            movies_by_title.setdefault(title, []).append(
                {"time": time_str, "date": date_str, "url": ""}
            )

    # ── Diagnostics ──────────────────────────────────────────────────────────
    if not movies_by_title:
        print("  ⚠ Keine OmU-Filme auf caligari.wiesbaden.de gefunden")
        body = soup.get_text()
        print(f"  ℹ '(OmU)' im Text vorhanden: {'(OmU)' in body or '(OmF)' in body}")
        return []

    print(f"  ℹ Gefundene Titel: {list(movies_by_title.keys())}")

    movies = []
    for title, showtimes in movies_by_title.items():
        if not showtimes:
            continue
        omdb = get_enrichment(title, None)
        movies.append({
            "title":       title,
            "version":     "OmU",
            "genres":      [],
            "year":        None,
            "runtime":     None,
            "fsk":         None,
            "poster":      omdb.get("poster_omdb", ""),
            "imdb_rating": omdb.get("imdb_rating", ""),
            "tmdb_rating": omdb.get("tmdb_rating", ""),
            "tmdb_id":     omdb.get("tmdb_id", ""),
            "imdb_id":     omdb.get("imdb_id", ""),
            "plot":        omdb.get("plot", ""),
            "director":    omdb.get("director", ""),
            "actors":      omdb.get("actors", ""),
            "cinemas": [{
                "name":      CALIGARI_CINEMA,
                "address":   CALIGARI_ADDR,
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
    output = {"generated_at": datetime.now(timezone.utc).isoformat(), "cities": {}}

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

    # ── Caligari FilmBühne (caligari.wiesbaden.de → Playwright, allekinos.de fallback) ──
    # Targets the official Caligari website which lists all events for 365 days.
    # If Playwright returns films, drop the (often date-imprecise) allekinos.de entries.
    # If it returns nothing, keep allekinos.de data as fallback.
    print("\n=== Caligari FilmBühne (caligari.wiesbaden.de) ===")
    caligari_movies = []
    try:
        caligari_html   = fetch_caligari_html()
        caligari_movies = parse_caligari_website(caligari_html)
        print(f"  → {len(caligari_movies)} OmU-Film(e) von caligari.wiesbaden.de")
    except Exception as exc:
        print(f"  ✗ Caligari-Fehler: {exc}")

    if caligari_movies:
        # Remove unreliable allekinos.de Caligari entries
        if CALIGARI_CITY in output["cities"]:
            for movie in output["cities"][CALIGARI_CITY]["movies"]:
                movie["cinemas"] = [
                    c for c in movie["cinemas"]
                    if "Caligari" not in c["name"]
                ]
            output["cities"][CALIGARI_CITY]["movies"] = [
                m for m in output["cities"][CALIGARI_CITY]["movies"] if m["cinemas"]
            ]
        merge_kinopolis(output, caligari_movies, CALIGARI_CITY, optional=False)
    else:
        # Keep allekinos.de Caligari data as fallback (dates may be imprecise)
        print("  ⚠ Keine Caligari-Filme gefunden – allekinos.de-Daten als Fallback behalten")

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
