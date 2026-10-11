# nonstop Wien

A one-page overview of every Vienna screening you can see with the
[nonstop Kinoabo](https://nonstopkino.at), built from the public programme.

```bash
python3 update.py
```

This fetches the programme, rebuilds `dashboard.html` and opens it. The
dashboard has the data embedded, so you can also just double-click it. It uses
only the Python standard library (Python 3.9+), so there's nothing to install.

Options: `--no-open` skips opening the browser, and `--offline` rebuilds from
the cached pages without any network requests.

## Where the data comes from

- **Screenings:** `https://nonstopkino.at/programm/`. The page server-renders
  every screening in Austria as `<article class="event">` with structured
  attributes (`data-location`, `data-venue`, `data-language`,
  `data-weekday`, `data-highlight-types`). The site's own filter JS
  works client-side on these, and there is no separate JSON feed.
- **Film details** (director, year, runtime, synopsis, original language):
  the film pages at `/movies/<slug>/`, cached in `cache/films/`. A page is
  re-fetched when it's older than 14 days, or after 12 h if it's missing a
  screening the programme lists.
- **Not used:** the WordPress REST API does expose Culturekit-synced post
  types (`/wp-json/wp/v2/cv_events`, `cv_productions`, `cv_venues`), but
  they only carry a title such as `2026-10-11T14:01:00.000Z / Filmcasino / A New
  Dawn`. They have no language, region, tags or metadata, so the HTML
  attributes are the richest structured source available.

### Quirks handled

- **Rescheduled screenings:** each screening's id embeds the UTC time it
  was imported with. When a cinema later moves a screening, the id keeps the
  old time, so the *displayed* date/time is used (the film page agrees).
- **Near-duplicates:** the same film, cinema and language within 5 minutes
  (e.g. 16:00 and 16:01) are merged.
- **Tags:** Film der Woche, Festival, Q&A, Retrospektive and Open Air are kept.
  Staff tips ("Lauras Tipp"), 3D and "Special" are dropped. Screenings at
  Kino wie noch nie and Burghofkino are always tagged Open Air.
- **Booking links:** nonstop doesn't publish per-screening ticket links. The film
  page's per-screening button is usually just the cinema homepage, so it's
  only used when it points deeper than the homepage (currently the Actors
  Studio/Cineplexx and Haydn ticket pages). Every other chip opens the
  nonstop film page.

Requests use an identifiable User-Agent and are spaced 1 s apart. A normal
re-run makes 1 request plus a few for new films.

## Dashboard

- **Week:** one row per film and a column for each of the next 7 days, with
  chips like `18:15 Votiv · OmeU`. Sort by most screenings, A–Z, or
  "playing tonight" (today from 17:00, or from now if later). Screenings beyond
  the week are summarised in the *Later* column.
- **Day:** one row per cinema on a 10:00–24:00 axis (it extends past
  midnight when needed). Block width is the runtime; a faded end means the
  runtime is unknown and 100 min is assumed. Set *From* to 19:00 to answer
  "what can I still catch tonight?"
- **Filters** apply to both views: search (title/director/cast), cinemas,
  language (the *English-friendly* option selects OV + OmeU), earliest start,
  and tags. They're remembered per browser.
- Colour = language version (DF blue, OV orange, OmdU green, OmeU violet), and
  the code is always printed as text too.

## Files

| File | |
|---|---|
| `update.py` | scraper + build script |
| `template.html` | dashboard source (HTML/CSS/JS, no build step) |
| `dashboard.html` | generated output with embedded data |
| `data.json` | the same data, for inspection |
| `cache/` | programme + film page cache |

## Automatic refresh (GitHub Pages)

`.github/workflows/update.yml` runs `update.py` every day at 03:30 UTC (it can also be
started by hand from the Actions tab, and runs on pushes that change the code). It
deploys `dashboard.html` as the Pages site and commits the fresh `data.json`. That commit
also counts as repo activity, so GitHub doesn't pause the schedule after 60 quiet days.
Film pages are kept between runs with `actions/cache`.
The page carries `noindex`, but the URL is public.
