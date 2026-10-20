#!/usr/bin/env python3
"""Fetch the nonstop Kinoabo programme for Vienna and rebuild dashboard.html.

Usage:  python3 update.py [--no-open] [--offline]

Data source: https://nonstopkino.at/programm/ server-renders every screening
as an <article class="event"> with structured data-* attributes (region,
venue, language, highlight types) and an id that embeds the UTC start time.
Film metadata (director, year, runtime, synopsis) and per-screening venue
links come from the film pages at /movies/<slug>/, which are cached in
cache/films/ between runs.
"""
import html
import json
import math
import re
import sys
import time
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from imdb import Imdb, norm

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
FILM_CACHE = CACHE / "films"
DATA_JSON = ROOT / "data.json"
TEMPLATE = ROOT / "template.html"
TASTE = ROOT / "taste.json"
OUTPUT = ROOT / "dashboard.html"

BASE = "https://nonstopkino.at"
PROGRAMM_URL = BASE + "/programm/"
USER_AGENT = "nonstop-vienna-dashboard/1.0 (personal Kinoabo overview; polite scraper, 1 req/s)"
DELAY_S = 1.0
FILM_CACHE_MAX_AGE = timedelta(days=14)   # refetch metadata at least this often
FILM_CACHE_MIN_AGE = timedelta(hours=12)  # ...and at most this often for new screenings

VIENNA = ZoneInfo("Europe/Vienna")
REGION = "wien"
EXPECTED_CINEMAS = [
    "Actors Studio", "Admiral Kino", "Bellaria Kino", "Breitenseer Lichtspiele", "Burg Kino",
    "CineCenter", "De France", "Filmcasino", "Filmhaus", "Filmmuseum", "Gartenbaukino",
    "Haydn Kino", "Metro Kinokulturhaus", "Schikaneder", "Stadtkino Wien", "Top Kino",
    "URANIA", "Votiv Kino",
]
SEASONAL_CINEMAS = ["Burghofkino", "Kino wie noch nie"]
OPEN_AIR_VENUES = {"kino-wie-noch-nie", "burghofkino", "open-air-kino-im-burghof"}

# site highlight type -> dashboard tag (staff tips, 3d, special are dropped)
TAG_MAP = {
    "filmOfTheWeek": "Film der Woche",
    "festival": "Festival",
    "q&a": "Q&A",
    "retrospective": "Retrospektive",
    "openAir": "Open Air",
}
LANGUAGES = {"DF", "OV", "OmdU", "OmeU"}

_last_request = 0.0


def fetch(url: str) -> str:
    global _last_request
    wait = DELAY_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-AT,de;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    finally:
        _last_request = time.monotonic()
    # data-text attributes are byte-truncated upstream, so a few invalid sequences exist
    return body.decode("utf-8", errors="replace")


def text(fragment: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    fragment = re.sub(r"<br\s*/?>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def attr(tag: str, name: str) -> str:
    m = re.search(r'\s%s="([^"]*)"' % re.escape(name), tag)
    return html.unescape(m.group(1)) if m else ""


def parse_start(event_id: str) -> datetime:
    # e.g. "2026-10-11t083000-000z-cinema-paradiso-baden-atmosphere"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})t(\d{2})(\d{2})(\d{2})-\d{3}z", event_id)
    if not m:
        raise ValueError(f"unexpected event id {event_id!r}")
    y, mo, d, h, mi, s = map(int, m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).astimezone(VIENNA)


def parse_venue_names(page: str) -> dict:
    sel = re.search(r'<select name="venue".*?</select>', page, re.S)
    names = {}
    for m in re.finditer(r'<option value="([^"]+)"[^>]*>(.*?)</option>', sel.group(0) if sel else "", re.S):
        if m.group(1) != "all":
            names[m.group(1)] = text(m.group(2))
    return names


def parse_programme(page: str) -> list:
    venue_names = parse_venue_names(page)
    events, moved = [], 0
    for m in re.finditer(r'<article class="event"([^>]*)>(.*?)</article>', page, re.S):
        head, body = m.group(1), m.group(2)
        eid = attr(head, "id")
        link = re.search(r'<a [^>]*href="([^"]+)"', body).group(1)
        # The id embeds the UTC time at import; when a cinema later moves a screening the
        # id keeps the old time, so the displayed date/time is authoritative.
        shown = text(re.search(r'<div class="big">(.*?)</div>', body, re.S).group(1))
        day = datetime.strptime(attr(head, "data-weekday"), "%Y-%m-%d")
        hh, mm = map(int, shown.split(":"))
        start = day.replace(hour=hh, minute=mm, tzinfo=VIENNA)
        if start != parse_start(eid):
            moved += 1
        img = re.search(r'<img src="([^"]+)"', body)
        venue = attr(head, "data-venue")
        loc = re.search(r'<div class="location">(.*?)</div>', body, re.S)
        types = [t for t in attr(head, "data-highlight-types").split(",") if t]
        tags = [TAG_MAP[t] for t in types if t in TAG_MAP]
        if venue in OPEN_AIR_VENUES and "Open Air" not in tags:
            tags.append("Open Air")
        events.append({
            "id": eid,
            "region": attr(head, "data-location"),
            "venue": venue,
            "cinema": venue_names.get(venue) or (text(loc.group(1)).rsplit(" ", 1)[0] if loc else venue),
            "lang": attr(head, "data-language"),
            "start": start,
            "url": link,
            "slug": urlparse(link).path.rstrip("/").split("/")[-1],
            "title": text(re.search(r"<h3>(.*?)</h3>", body, re.S).group(1)),
            "image": img.group(1) if img else "",
            "tags": tags,
        })
    if moved:
        print(f"  {moved} screening(s) were rescheduled after import (displayed time differs from id)")
    return events


def dedupe(events: list) -> list:
    """Drop near-duplicate entries (same film, cinema and language within 5 minutes)."""
    out, dropped = [], 0
    for e in sorted(events, key=lambda e: e["start"]):
        prev = next((p for p in reversed(out[-30:]) if p["slug"] == e["slug"] and p["venue"] == e["venue"]
                     and p["lang"] == e["lang"] and e["start"] - p["start"] <= timedelta(minutes=5)), None)
        if prev:
            prev["tags"] = sorted(set(prev["tags"]) | set(e["tags"]))
            dropped += 1
        else:
            out.append(e)
    if dropped:
        print(f"  dropped {dropped} near-duplicate screening(s)")
    return out


def parse_film_page(page: str) -> dict:
    def meta(cls):
        m = re.search(r'<p class="%s">\s*<span class="key">.*?</span>\s*<span class="value">(.*?)</span>' % cls, page, re.S)
        return text(m.group(1)) if m else ""

    desc = re.search(r'<p class="description">(.*?)</p>', page, re.S)
    runtime = re.search(r"(\d+)", meta("duration"))
    year = re.search(r"(\d{4})", meta("releaseYear"))
    hero = re.search(r"background-image: url\('([^']+)'\)", page)
    links = {}
    for m in re.finditer(r'<article class="event" id="([^"]+)"[^>]*>(.*?)</article>', page, re.S):
        a = re.search(r'<a [^>]*class="button"[^>]*href="([^"]+)"', m.group(2))
        if a:
            links[m.group(1)] = html.unescape(a.group(1))
    return {
        "director": meta("director"),
        "cast": meta("cast"),
        "year": int(year.group(1)) if year else None,
        "runtime": int(runtime.group(1)) if runtime else None,
        "spoken": meta("spokenLanguages"),
        "synopsis": text(desc.group(1)) if desc else "",
        "hero": hero.group(1) if hero else "",
        "venue_links": links,
    }


def is_booking_link(url: str) -> bool:
    """The film page's per-screening button is usually just the cinema homepage;
    only treat it as a booking link when it points deeper than the site root."""
    p = urlparse(url)
    return bool(p.scheme.startswith("http") and (p.path.strip("/") or p.query))


def film_details(slug: str, url: str, needed_ids: set, offline: bool) -> dict:
    FILM_CACHE.mkdir(parents=True, exist_ok=True)
    path = FILM_CACHE / f"{slug}.html"
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        page = path.read_text(encoding="utf-8")
        missing = any(f'id="{i}"' not in page for i in needed_ids)
        fresh = age < FILM_CACHE_MAX_AGE and (not missing or age < FILM_CACHE_MIN_AGE)
        if fresh or offline:
            return parse_film_page(page)
    if offline:
        return parse_film_page("")
    print(f"  fetching {url}")
    try:
        page = fetch(url)
    except Exception as exc:  # keep going with whatever we have
        print(f"  ! failed to fetch {url}: {exc}")
        return parse_film_page(path.read_text(encoding="utf-8") if path.exists() else "")
    path.write_text(page, encoding="utf-8")
    return parse_film_page(page)


def poster(url: str, width: int) -> str:
    return re.sub(r"([?&])w=\d+", rf"\g<1>w={width}", url) if url else ""


def build(offline: bool = False) -> dict:
    CACHE.mkdir(exist_ok=True)
    prog_cache = CACHE / "programm.html"
    if offline and prog_cache.exists():
        page = prog_cache.read_text(encoding="utf-8")
        fetched_at = datetime.fromtimestamp(prog_cache.stat().st_mtime, VIENNA)
    else:
        print(f"Fetching {PROGRAMM_URL}")
        page = fetch(PROGRAMM_URL)
        prog_cache.write_text(page, encoding="utf-8")
        fetched_at = datetime.now(VIENNA)

    all_events = parse_programme(page)
    now = datetime.now(VIENNA)
    vienna = [e for e in all_events if e["region"] == REGION and e["start"] >= now]
    print(f"  {len(all_events)} screenings in Austria, {len(vienna)} upcoming in Vienna")
    vienna = dedupe(vienna)

    by_slug = defaultdict(list)
    for e in vienna:
        by_slug[e["slug"]].append(e)

    films, screenings = [], []
    for slug, evs in sorted(by_slug.items()):
        first = evs[0]
        info = film_details(slug, first["url"], {e["id"] for e in evs}, offline)
        films.append({
            "slug": slug,
            "title": first["title"],
            "url": first["url"],
            "poster": poster(first["image"], 120),
            "director": info["director"],
            "cast": info["cast"],
            "year": info["year"],
            "runtime": info["runtime"],
            "spoken": info["spoken"],
            "synopsis": info["synopsis"],
        })
        for e in evs:
            venue_link = info["venue_links"].get(e["id"], "")
            screenings.append({
                "film": slug,
                "start": e["start"].isoformat(),
                "cinema": e["cinema"],
                "lang": e["lang"],
                "tags": e["tags"],
                "booking": venue_link if is_booking_link(venue_link) else "",
            })

    rank_films(films, Imdb(CACHE, USER_AGENT, offline), json.loads(TASTE.read_text(encoding="utf-8")))
    # nonstop sometimes lists one film twice (e.g. original and German title): merge by IMDb id
    keep, merged = {}, {}
    for f in films:
        if f.get("imdb") and f["imdb"] in keep:
            merged[f["slug"]] = keep[f["imdb"]]
        elif f.get("imdb"):
            keep[f["imdb"]] = f["slug"]
    if merged:
        films = [f for f in films if f["slug"] not in merged]
        for s in screenings:
            s["film"] = merged.get(s["film"], s["film"])
        print(f"  merged {len(merged)} duplicate film listing(s): {', '.join(merged)}")
    screenings.sort(key=lambda s: (s["start"], s["cinema"]))
    return {
        "updated": fetched_at.isoformat(timespec="minutes"),
        "source": PROGRAMM_URL,
        "films": films,
        "screenings": screenings,
    }


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def rank_films(films: list, imdb: Imdb, taste: dict) -> None:
    """Give every film a 0-100 'pick' score: popular + well rated + known director, tilted by taste.json."""
    this_year = datetime.now(VIENNA).year
    favorites = {norm(n) for n in taste.get("favorite_directors", [])}
    genre_adjust = taste.get("genre_adjust", {})
    for f in films:
        tconst = imdb.match(f["title"], f["director"], f["year"])
        if not tconst:
            f["score"] = None
            continue
        m = imdb.info(tconst)
        f.update({"imdb": m["imdb"], "rating": m["rating"], "votes": m["votes"], "genres": m["genres"]})
        f["runtime"] = f["runtime"] or m["runtime"]
        f["year"] = f["year"] or m["imdbYear"]

        pop = clamp01((math.log10(m["votes"] + 1) - 3) / 3)            # 1k votes → 0, 1M → 1
        qual = clamp01((m["rating"] - 5.8) / 2.6) if m["rating"] else 0.45  # 5.8 → 0, 8.4 → 1
        fame = clamp01((math.log10(m["directorFame"] + 1) - 4) / 2.4)   # best film 10k votes → 0, 2.5M → 1
        recent = (f["year"] or 0) >= this_year - 1                      # votes haven't piled up yet
        known = (0.25 * pop + 0.75 * fame) if recent else (0.55 * pop + 0.45 * fame)
        # "popular AND good": a well-known film with a poor rating only keeps ~a third of its score
        score = 100 * known * (0.35 + 0.65 * qual)
        score += sum(genre_adjust.get(g, 0) for g in m["genres"])
        if "englisch" in (f["spoken"] or "").lower():
            score += taste.get("english_bonus", 0)
        if favorites & {norm(n) for n in m["directorNames"]}:
            score += taste.get("favorite_bonus", 0)
            f["favorite"] = True
        f["score"] = round(max(0, score))  # can exceed 100 with taste bonuses; only used for ordering


def summarize(data: dict) -> None:
    sc = data["screenings"]
    per_cinema = Counter(s["cinema"] for s in sc)
    langs = Counter(s["lang"] for s in sc)
    tags = Counter(t for s in sc for t in s["tags"])
    print("\n=== Summary ===")
    print(f"Films: {len(data['films'])}   Screenings: {len(sc)}   Cinemas: {len(per_cinema)}")
    if sc:
        print(f"Date range: {sc[0]['start'][:16].replace('T', ' ')} → {sc[-1]['start'][:16].replace('T', ' ')}")
    print("Screenings per cinema:")
    for name, n in sorted(per_cinema.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:4d}  {name}")
    print("Languages: " + ", ".join(f"{k} {v}" for k, v in langs.most_common()))
    print("Tags: " + (", ".join(f"{k} {v}" for k, v in tags.most_common()) or "none"))
    print(f"Booking links: {sum(1 for s in sc if s['booking'])}")
    films = data["films"]
    matched = [f for f in films if f.get("score") is not None]
    print(f"IMDb matches: {len(matched)}/{len(films)} films")
    top = sorted(matched, key=lambda f: -f["score"])[:10]
    print("Top picks: " + "; ".join(f"{f['title']} ({f['score']})" for f in top))
    unknown = [s for s in sc if s["lang"] not in LANGUAGES]
    if unknown:
        print(f"  ! unexpected language codes: {Counter(s['lang'] for s in unknown)}")

    def norm(n):
        return re.sub(r"[^a-z]", "", n.lower())

    seen = {norm(n) for n in per_cinema}
    missing = [c for c in EXPECTED_CINEMAS if not any(norm(c) in s or s in norm(c) for s in seen)]
    extra = [c for c in per_cinema if not any(norm(c) in norm(e) or norm(e) in norm(c)
                                             for e in EXPECTED_CINEMAS + SEASONAL_CINEMAS)]
    print("Sanity check vs expected cinemas: " + ("all present" if not missing else "no screenings at " + ", ".join(missing)))
    if extra:
        print("  not on the expected list: " + ", ".join(extra))
    seasonal = [c for c in SEASONAL_CINEMAS if any(norm(c) in s for s in seen)]
    print("Seasonal open-air venues: " + (", ".join(seasonal) if seasonal else "none playing (off-season)"))


def render(data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tpl = TEMPLATE.read_text(encoding="utf-8")
    OUTPUT.write_text(tpl.replace("/*__DATA__*/null", payload), encoding="utf-8")
    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nWrote {OUTPUT}")


def main(argv):
    offline = "--offline" in argv
    data = build(offline=offline)
    summarize(data)
    render(data)
    if "--no-open" not in argv:
        webbrowser.open(OUTPUT.as_uri())


if __name__ == "__main__":
    main(sys.argv[1:])
