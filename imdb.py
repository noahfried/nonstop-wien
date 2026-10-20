"""IMDb lookups for ranking films by how popular / well regarded they are.

Uses the free IMDb datasets (https://datasets.imdbws.com, personal non-commercial use).
They are ~630 MB compressed, so they are streamed once a week and boiled down to a
small index in cache/imdb_index.json.gz (titles with enough votes or recent release,
their directors, and each director's best-known film).
"""
import csv
import gzip
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = "https://datasets.imdbws.com/"
INDEX_MAX_AGE = timedelta(days=7)
KEEP_TYPES = {"movie", "tvMovie", "short", "video", "tvSpecial", "tvMiniSeries"}
MIN_VOTES = 50

csv.field_size_limit(sys.maxsize)


def norm(s: str) -> str:
    s = re.sub(r"['’`´]", "", s or "")  # Pan's → pans, on both sides
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _rows(name: str, user_agent: str):
    req = urllib.request.Request(BASE + name, headers={"User-Agent": user_agent})
    t0 = time.monotonic()
    print(f"  streaming {name}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=resp), encoding="utf-8")
        reader = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
        next(reader)
        yield from reader
    print(f"    done in {time.monotonic() - t0:.0f}s")


def build_index(path: Path, user_agent: str) -> dict:
    this_year = datetime.now().year
    ratings = {}
    for tconst, avg, votes in _rows("title.ratings.tsv.gz", user_agent):
        if int(votes) >= MIN_VOTES:
            ratings[tconst] = (float(avg), int(votes))

    titles = {}
    for tconst, ttype, primary, original, _adult, start, _end, runtime, genres in _rows("title.basics.tsv.gz", user_agent):
        if ttype not in KEEP_TYPES:
            continue
        year = int(start) if start.isdigit() else None
        rating = ratings.get(tconst)
        if not rating and not (year and year >= this_year - 2):
            continue
        titles[tconst] = {
            "t": primary, "o": original if original != primary else "", "y": year, "k": ttype,
            "rt": int(runtime) if runtime.isdigit() else None,
            "g": genres.split(",") if genres != "\\N" else [],
            "r": rating[0] if rating else None, "v": rating[1] if rating else 0,
        }
    del ratings

    for tconst, directors, _writers in _rows("title.crew.tsv.gz", user_agent):
        if tconst in titles and directors != "\\N":
            titles[tconst]["d"] = directors.split(",")

    fame = defaultdict(int)  # director -> votes of their most-voted feature
    for t in titles.values():
        if t["k"] in ("movie", "tvMovie", "tvMiniSeries"):
            for d in t.get("d", ()):
                fame[d] = max(fame[d], t["v"])
    names = {}
    for nconst, name, *_ in _rows("name.basics.tsv.gz", user_agent):
        if nconst in fame:
            names[nconst] = name

    index = {"built": datetime.now().isoformat(timespec="seconds"), "titles": titles,
             "names": names, "fame": dict(fame)}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    print(f"  IMDb index: {len(titles)} titles, {len(names)} directors → {path.stat().st_size / 1e6:.1f} MB")
    return index


class Imdb:
    def __init__(self, cache_dir: Path, user_agent: str, offline: bool = False):
        path = cache_dir / "imdb_index.json.gz"
        fresh = path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < INDEX_MAX_AGE
        if fresh or (offline and path.exists()):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                index = json.load(fh)
        elif offline:
            index = {"titles": {}, "names": {}, "fame": {}}
        else:
            print("Building IMDb index (weekly)…")
            try:
                index = build_index(path, user_agent)
            except Exception as exc:
                print(f"  ! IMDb download failed ({exc}); ranking without it")
                index = {"titles": {}, "names": {}, "fame": {}}
        self.titles = index["titles"]
        self.names = index["names"]
        self.fame = index["fame"]
        self.by_title = defaultdict(list)
        for tc, t in self.titles.items():
            for s in {norm(t["t"]), norm(t["o"])} - {""}:
                self.by_title[s].append(tc)
        self.by_name = defaultdict(list)
        for nc, name in self.names.items():
            self.by_name[norm(name)].append(nc)
        self.by_director = defaultdict(list)
        for tc, t in self.titles.items():
            for d in t.get("d", ()):
                self.by_director[d].append(tc)

    @staticmethod
    def title_variants(title: str) -> list:
        """'E.T. – Der Außerirdische' → ['et der ausserirdische', 'et', 'der ausserirdische'] etc."""
        parts = {title}
        for piece in re.split(r"\s[–—-]\s|[()]|:\s", title):
            if piece.strip():
                parts.add(piece.strip())
        parts.add(re.sub(r"\s*\(.*?\)", "", title))
        return [v for v in dict.fromkeys(norm(p) for p in parts) if v]

    def _director_ids(self, director: str) -> set:
        ids = set()
        for person in re.split(r",|\bund\b|&", director or ""):
            ids.update(self.by_name.get(norm(person), []))
        return ids

    def match(self, title: str, director: str, year):
        variants = self.title_variants(title)
        dir_ids = self._director_ids(director)

        def year_ok(t, slack=1):
            return year is None or t["y"] is None or abs(t["y"] - year) <= slack

        def title_score(t):
            names = {norm(t["t"]), norm(t["o"])} - {""}
            best = 0
            for v in variants:
                for n in names:
                    if v == n:
                        return 3
                    if v in n or n in v:
                        best = max(best, 2)
                    elif v.split()[0] == n.split()[0] and len(v.split()[0]) >= 4:
                        best = max(best, 1)  # 'nausicaa aus dem tal…' ~ 'nausicaa of the valley…'
            return best

        def rank(tc):
            t = self.titles[tc]
            gap = abs(t["y"] - year) if year and t["y"] else 0
            return (title_score(t), -gap, t["k"] == "movie", t["v"])
        if dir_ids:
            own = list(dict.fromkeys(tc for d in dir_ids for tc in self.by_director.get(d, [])))
            # 1) same director + exact title, any year (nonstop's year is sometimes the restoration's)
            exact = [tc for tc in own if title_score(self.titles[tc]) == 3]
            if exact:
                return max(exact, key=lambda tc: (year_ok(self.titles[tc]), self.titles[tc]["v"]))
            # 2) same director, same year (±1): robust against German release titles
            cands = [tc for tc in own if year_ok(self.titles[tc])]
            if cands:
                best = max(cands, key=rank)
                if title_score(self.titles[best]) > 0:
                    return best
                features = [tc for tc in cands if self.titles[tc]["k"] == "movie"]
                if len(features) == 1:
                    return features[0]
        # 2) exact title + year (director name may be misspelt or missing)
        if year:
            cands = {tc for v in variants for tc in self.by_title.get(v, []) if year_ok(self.titles[tc], 1)}
            if cands:
                return max(cands, key=lambda tc: (self.titles[tc]["k"] == "movie", self.titles[tc]["v"]))
        return None

    def info(self, tconst: str) -> dict:
        t = self.titles[tconst]
        dirs = t.get("d", [])
        return {
            "imdb": tconst, "rating": t["r"], "votes": t["v"], "genres": t["g"],
            "imdbTitle": t["t"], "imdbYear": t["y"], "runtime": t["rt"],
            "directorFame": max((self.fame.get(d, 0) for d in dirs), default=0),
            "directorNames": [self.names[d] for d in dirs if d in self.names],
        }
