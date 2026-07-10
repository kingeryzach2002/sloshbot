# Sloshbot — Architecture

A multi-user tool that aggregates SF events, scores them with pluggable
scorers (including free-booze likelihood), and presents a tiered candidate
list with tentative-hold calendar links. The event catalog and scores are
shared and crowdsourced across every visitor; only preferences and
personal state (home address, filters, feedback, holds) are per-visitor
(anonymous).

## Layout

```
sloshbot/
├── ARCHITECTURE.md      # this file — the contract everything builds against
├── sloshbot.db          # SQLite; the ONLY interface between pipeline and app
├── pipeline.py          # host-agnostic refresh: ingest -> dedup -> geocode ->
│                         #   geofilter -> score -> prune. `python -m pipeline`
│                         #   (one-shot) or `--loop --interval N` (no native cron)
├── ingest/
│   ├── schema.sql       # canonical DDL
│   ├── sources/         # one module per source (luma, eventbrite, funcheap,
│   │   └── ...          #   dothebay, garysguide); each exposes
│   │                     #   fetch() -> list[RawEvent]. Disabled sources are
│   │                     #   kept as _-prefixed modules (run.py skips them):
│   │                     #   _ra, _nineteenhz, _meetup — surfaced nothing for
│   │                     #   the booze lens, so pruned from ingestion.
│   ├── tags.py          # assigns each event tags from a hand-pruned vocabulary
│   ├── dedup.py         # cross-source duplicate detection (sets duplicate_of)
│   ├── geocode.py       # fills lat/lon from address (cached Nominatim lookups);
│   │                     #   also backfills home_lat/home_lon for every user
│   ├── geofilter.py     # drops events with coords >12mi from central SF (out
│   │                     #   of Bay Area); backstop for feeds that leak non-SF
│   ├── normalize.py     # RawEvent -> Event (schema below), dedup, upsert
│   └── run.py           # CLI: python -m ingest.run [--source luma]
├── scoring/
│   ├── scorers/         # each: score(event) -> {score: 0-1, rationale: str}
│   │   └── booze.py     # LLM + heuristics; the only scorer (see weights.py)
│   ├── weights.py       # config: scorer weights, tier thresholds,
│   │                     #   NEVER_CONFIDENT_SOURCES (nightlife listings that
│   │                     #   can surface as "maybe" but never win a confident slot)
│   ├── eval.py          # eyeball the booze scorer against golden_set.csv
│   └── run.py           # CLI: scores unscored events, caches results in DB;
│                         #   discovers scorer modules dynamically
└── app/
    ├── main.py          # FastAPI routes only — composes the layers below,
    │                     #   plus the write paths (feedback/holds/settings/home),
    │                     #   and a JSON API
    ├── auth.py           # anonymous cookie identity (current_user dependency)
    ├── models.py        # Event: the in-memory contract + JSON serialization
    ├── data.py          # pure DB reads -> Event (the only read-path SELECTs);
    │                     #   every function takes user_id
    ├── filters.py       # FilterState + hard filters (source/price/distance/tag/
    │                     #   booze) + chip counts; URL-first, sticky-default fallback
    ├── policy.py        # ranking policy: composite/tier, source demotion, per-day cap
    ├── presenter.py     # view-model: datetimes, distance, gcal link, calendar geometry
    └── templates/       # tonight/week/calendar/map views + partials (Jinja, no
                          #   build chain): _main_home.html / _main_week.html /
                          #   _main_calendar.html are the fetch+swap fragments for
                          #   #page-main; _filterbar.html is the URL-state +
                          #   fetch+swap JS filter engine; _meter.html is the
                          #   booze-meter macro; _feedback.html / _empty.html are
                          #   shared partials. No login/signup templates.
```

The read pipeline is one directed flow, one responsibility per module:
`data.fetch_events → presenter.enrich → filters.apply → policy.rank`. Ranking
primitives (`composite`, `tier`, thresholds) live in `scoring/weights.py`; the
`MAX_PICKS` scarcity cap is an app *presentation* policy in `app/policy.py`. The
same pipeline feeds both the Jinja views and the JSON API (`Event.to_dict()`),
so a future client-rendered frontend needs no new backend logic.

Run locally with `uv run uvicorn app.main:app --reload`; refresh data with
`uv run python -m pipeline` (runs ingest → dedup → geocode → geofilter → score → prune in
one shot; `--loop --interval N` keeps it running without native cron, `--rescore`
forces a full booze rescore instead of unscored-only — expensive, use after a
scorer prompt/heuristic change). `SLOSHBOT_SECRET_KEY` must be set to a real
random value before hosting publicly — it signs each visitor's anonymous
identity cookie so no one can forge another visitor's identity.

Event-type preference used to be a third scorer ("category"/"fit") driven by a
hardcoded keyword dict in `preferences.py`. It's gone in favor of a tag filter:
`event_tags` (populated by `ingest/tags.py`) already carries per-event tags, and
the filter bar lets you check which tags you want. Tag selection is resolved
per-request as a `FilterState` (see `app/filters.py`): it comes from the URL
query string when present (shipped no-reload filtering — see "The fetch+swap
frontend" below), falling back to the visitor's sticky defaults saved in the
`settings` table (`included_tags`, per-visitor — see Anonymous identity below)
on a bare URL. Either way it's a hard filter applied in `app/filters.py::apply`
— events with none of your selected tags are hidden, events with no tags at
all are never hidden by it. No code edit or rescore needed to change what
you're interested in.

**Data flow (batch, never live):**
`pipeline.py` (cron / `--loop` / manual) → ingest each source → dedup → geocode
→ geofilter (drop out-of-Bay-Area) → `scoring.run` (scores unscored events, cached) → prune → SQLite → web app
(reads only) → user actions (feedback, calendar holds, settings) write back to
SQLite, scoped to the visitor's anonymous user_id.

The web app never scrapes and never calls an LLM. If the UI is broken, the
pipeline is fine; if scraping breaks, the UI still serves yesterday's data.

## Anonymous identity & per-visitor scoping

There is no login, no signup, no password, no email. On a visitor's first
request, `app/auth.py`'s `current_user(request)` dependency mints a random
`uuid4` id, stores it in the signed session cookie (Starlette's
`SessionMiddleware`, `max_age` set to 400 days so the identity survives
browser restarts, signed via `SLOSHBOT_SECRET_KEY`), and inserts a `users` row
with NULL `email`/`password_hash` to give per-user writes a valid foreign key.
Every route — HTML and JSON/write alike — depends on `current_user`; there's
no html-vs-api split anymore, no redirects, no 401. Every visitor always has
an identity from their very first request.

**What's shared vs. per-visitor is a deliberate split, not an accident:**

| Scoped per-visitor | Shared / crowdsourced across everyone |
|---|---|
| `settings` (home address, sticky filter defaults — included_tags/sources, price_filter, max_mi, min_booze) | `events`, `scores`, `event_tags` — the catalog itself |
| `feedback` — *your* went/skipped/as-promised taps (drives card button state) | host reputation — `_host_reputation()` in `booze.py` aggregates **every** visitor's feedback for a host, not just yours; it gets more accurate as more people use the app |
| `holds` — *your* calendar holds (drives *your* morning-after debrief) | `geocode_cache` — an address string → lat/lon lookup, safe to share |

`app/data.py::fetch_events(start, end, user_id)` is where this split actually
happens: the per-event `feedback` set it attaches is filtered to `user_id`, but
the `host_rep` it attaches is computed from an unfiltered join — same function,
two different scoping rules, on purpose.

The `users` table's `email` and `password_hash` columns are nullable — NULL is
the norm now, for every anonymous user. Pre-multi-user installs still migrate
automatically and losslessly: `db.py`'s `init_db()` detects the old unscoped
`settings`/`feedback`/`holds` shape and rescopes every existing row to a
stable `LEGACY_USER_ID` ("legacy") account, with sentinel `email`/
`password_hash` values (`'legacy@local'` / `'!'`) left over from before those
columns were nullable. Nothing is deleted. The app also went through a brief
password-accounts era between the pre-accounts world and today's anonymous
identity; that era's signed-up rows (real email + bcrypt hash) are now
orphaned — no login exists to reach them anymore — but they're harmless dead
rows, not worth a migration to clean up.

## App layering & the frontend seam

The web app is split into single-responsibility layers with one directed read
flow. The HTTP layer (`app/main.py`) is thin: it resolves the caller's
anonymous identity, resolves the active `FilterState` (URL vs. sticky
defaults — see `resolve_filters` below), then composes the read pipeline and
either renders Jinja (full page or, for `?partial=1`, just the swappable
fragment) OR serializes JSON — the SAME data either way.

```
                         ┌───────────────────────── app/main.py (FastAPI) ─────────────────────────┐
  browser / client  ───▶ │  auth.current_user  →  user_id                                          │
                         │        │                                                                 │
                         │  resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)     │
                         │        │  → FilterState (URL if f present, else sticky settings default)  │
                         │        ▼                                                                  │
                         │  GET /  /week  /calendar  /map          GET /api/events  /api/counts      │
                         │        │  (HTML, or fragment if           /api/home                       │
                         │        │   ?partial=1 — see below)              │  (JSON via Event.to_dict)│
                         │        └──────────────┬─────────────────────────┘                          │
                         │                       ▼   load_events(user_id, start, end, fs)              │
                         │   data.fetch_events ─▶ presenter.enrich ─▶ filters.apply ─▶ policy.rank     │
                         │   (SQLite reads →      (start/end dt,       (source/price/   (composite/    │
                         │    Event objects,      distance, gcal)      distance/tag/    tier, source  │
                         │    THIS user's fb,                          booze filters)   demotion,     │
                         │    shared host_rep)                                          per-day cap)  │
                         │                                                                             │
                         │   writes:  POST /feedback/{id}/{verdict}  /hold/{id}                        │
                         │            /settings/filters (sticky defaults)  /api/home  /settings/home  │
                         │            (all scoped to user_id from the session cookie)         ────────▶│──▶ SQLite
                         └─────────────────────────────────────────────────────────────────────────────┘
                                     ▲ scoring/weights.py: composite(), tier(), thresholds, LENSES, LENS_META
```

**Layer responsibilities (`app/`):**

| Module | Owns |
|---|---|
| `models.py` | `Event` — field contract + `to_dict()` JSON shape |
| `auth.py` | anonymous cookie identity, `current_user` dependency |
| `data.py` | the only read-path SQL → `Event`; every function takes `user_id` |
| `presenter.py` | datetimes, distance, gcal link, calendar geometry (`layout_day`) |
| `filters.py` | `FilterState` + hard filters + `chip_counts` (URL-first, sticky-default fallback — see below) |
| `policy.py` | composite/tier rollup, source demotion, per-day `MAX_PICKS` cap |
| `main.py` | routes only — identity + filter resolution + compose + render/serialize + write paths |
| `templates/` | Jinja HTML, the fetch+swap client JS, design tokens |

Ranking *primitives* (`composite`, `tier`, thresholds, `LENSES`, `LENS_META`)
live in `scoring/weights.py`; the `MAX_PICKS` scarcity cap is a *presentation*
policy in `app/policy.py`. Per-lens display metadata (emoji, label) lives once
in `scoring/weights.py::LENS_META` — adding a perk lens is a config entry there
plus its scorer, not copy-pasted markup.

## The fetch+swap frontend

Filtering used to mean a POST to a settings endpoint plus a full page reload.
It doesn't anymore. Filter state now lives in the URL query string, and
clicking a filter swaps content in place:

1. The visitor clicks a tag/source/price chip or drags a distance/booze
   slider in `_filterbar.html`.
2. Its JS updates the URL query string (`tags=`, `sources=`, `price=`,
   `max_mi=`, `min_booze=`, plus `f=1` marking "filters come from the URL")
   and calls `history.pushState` — the URL is shareable/bookmarkable and
   back/forward works.
3. It also debounce-saves the same selection via `POST /settings/filters` so
   it becomes the visitor's *sticky default* the next time they land on a
   bare URL (no `f` param).
4. It fetches the current page's path with the same query string plus
   `&partial=1`. The server reruns the exact same route handler and pipeline,
   but renders the fragment template (`_main_home.html` / `_main_week.html` /
   `_main_calendar.html`) instead of the full-page template — same context,
   smaller response.
5. The JS swaps the fragment's HTML into `#page-main` (present in `home.html`,
   `week.html`, `calendar.html`) — no navigation, no full-page reload.

`/map` doesn't use this fragment-swap path — Leaflet pins aren't server-
rendered HTML. Instead its filter chips re-fetch `GET /api/events` with the
new query params and redraw pins client-side from the JSON response.

`resolve_filters()` in `app/main.py` is the seam: if the URL carries `f`,
`FilterState` comes *only* from the URL (`filters.from_query`) — an absent
param means that filter is off even if a sticky default exists. If `f` is
absent, the visitor's sticky defaults apply (`filters.from_settings`), with
`max_mi`/`min_booze` still overridable by query params for old bookmarked
links. The `settings` table therefore holds only *sticky defaults* now, not
the live filter — the six old per-dimension toggle-and-clear write endpoints
that used to mutate it directly (one toggle + one clear each for tags,
sources, and price) are gone, replaced by the single `POST /settings/filters`.

## HTTP surface

```
UNAUTH GET  /healthz                unauthenticated liveness check → {ok, events: int}

HTML   GET  /                 hero "tonight's pick" + backups + rest-of-week
       GET  /week             7-day digest, grouped by day, tiered
       GET  /calendar         week grid (uses presenter.layout_day)
       GET  /map              Leaflet pins + home/radius rings (no partial mode)
       GET  /tonight          307 redirect → /  (tonight IS the home view now)
         `/`, `/week`, `/calendar` also accept `?partial=1` → render just the
         `#page-main` fragment (`_main_home.html` / `_main_week.html` /
         `_main_calendar.html`) instead of the full page — see "The fetch+swap
         frontend" above.
         All four GET list routes (`/`, `/week`, `/calendar`, `/map`) — plus
         `/api/events` — accept the URL filter params: `f` (sentinel: filters
         come only from the URL, ignoring sticky defaults), `tags` (csv),
         `sources` (csv), `price` (free|paid), `max_mi` (float), `min_booze`
         (float). `/` also takes `?lens=`.

JSON   GET  /api/events       {events: [Event.to_dict(), ...]}  ?days= + the URL filter params above
       GET  /api/counts       {tags:[{tag,n}], sources:[{source,n}], price:{free,paid}}
       GET  /api/home         {address, lat, lon} — current visitor's saved home
       POST /api/home  (JSON {address})  set/clear (address="") YOUR home, geocoded
                                          inline → {ok, resolved, address, lat, lon}

WRITE  POST /settings/filters  (JSON {tags, sources, price, max_mi, min_booze})
                                          persist YOUR sticky filter defaults → {ok}
       POST /settings/home  (form: home_address)   legacy map-page form setter;
                                          saves + geocodes YOUR home → 303 redirect to /map
                                          (the fetch-based /api/home above is the
                                          path for any new UI)
       POST /feedback/{event_id}/{verdict}?lens=   toggle YOUR verdict → {ok, state}
       POST /hold/{event_id}?lens=                 record YOUR calendar hold → {ok}
         (every write above is scoped to the visitor's anonymous user_id — see
          Anonymous identity above)
```

`Event.to_dict()` already carries everything a view needs: base fields, `scores`
& `rationales` (keyed by scorer), `tags`, `feedback` (the CALLING user's own
verdicts), `host_rep` (shared across every user), `composite`, `tier`,
`distance_mi`, `gcal`, and ISO `start_dt`/`end_dt`. The session cookie carries
the anonymous id; the read side of a client frontend needs **no new backend
logic** — just `/api/events`.

## Rewriting the frontend (React or similar, full SPA)

The URL-state + fetch+swap fragment design above already gets most of the way
to a no-reload frontend without a framework or build chain. A *full* client-
rendered SPA rewrite is still a bigger step — it would drop server-rendered
HTML entirely in favor of the JSON API — and two seams there are still open:

**Touch / replace:** everything under `app/templates/` (Jinja HTML, the inline
client JS in `_filterbar.html` / `_feedback.html` / `map.html`, and the design
system in `_theme.html`). Port the `_theme.html` `:root` CSS variables — the
speakeasy palette, Fraunces serif, "tier shown by light not badges" — those are
the visual contract. The HTML routes in `main.py` (`home`, `week`, `calendar`,
`map_view`) get deleted or reduced to serving the SPA shell.

**Keep / don't touch:** `auth.py`, `data.py`, `filters.py`, `policy.py`,
`models.py`, `scoring/`, and the identity + `/api/*` + write routes. That's the
whole backend the client talks to — a client app still needs to carry the
session cookie (or swap in a bearer-token variant of `current_user`). Filter
state itself is no longer an open question — see "The fetch+swap frontend"
above; a full SPA can keep reading/writing the same URL params + `/settings/filters`
sticky defaults, or hold filter state entirely client-side over `/api/events`.

**Two decisions a full SPA rewrite still must make** (seams exist; pick a side):

1. **Calendar geometry** — `presenter.layout_day` (overlap lane-packing → % top/
   height/left/width) is currently only called by the `/calendar` HTML route.
   Either add `GET /api/calendar` that returns positioned blocks (keeps the one
   algorithm server-side — recommended), or reimplement lane-packing in JS.
2. **"Tonight" selection** — the hero/backups/rest-of-week split lives in the
   `home()` route, not the API. Either add `GET /api/tonight`, or replicate the
   "pick the day's best" selection client-side over `/api/events`.

Neither is blocked; each is additive because the read pipeline and its
serialization boundary already exist.

## Event schema — THE contract

Every source spike must answer: "can I fill these fields?" Every UI element
renders from exactly these fields. Change this file first if the schema must
change.

```sql
CREATE TABLE events (
  id           TEXT PRIMARY KEY,   -- "<source>:<source_id>" e.g. "luma:evt-abc123"
  source       TEXT NOT NULL,      -- 'luma' | 'eventbrite' | 'funcheap' | ...
  source_id    TEXT NOT NULL,      -- stable ID on the source platform
  url          TEXT NOT NULL,      -- canonical event page
  title        TEXT NOT NULL,
  description  TEXT,               -- full text; scorers need this rich
  host_name    TEXT,               -- load-bearing: host reputation is the
  host_url     TEXT,               --   strongest long-term booze signal
  venue_name   TEXT,
  address      TEXT,
  neighborhood TEXT,               -- normalized later; nullable
  starts_at    TEXT NOT NULL,      -- ISO 8601, America/Los_Angeles
  ends_at      TEXT,
  is_free      INTEGER,            -- ticket price, NOT booze (0/1/NULL unknown)
  price_min    REAL,
  price_max    REAL,
  rsvp_type    TEXT,               -- 'open'|'approval'|'application'|'waitlist'|'sold_out'|NULL
  image_url    TEXT,
  lat          REAL,               -- filled by ingest/geocode.py
  lon          REAL,
  duplicate_of TEXT,               -- NULL = canonical; else id of the event this duplicates
  raw          TEXT,               -- original scraped JSON, for reprocessing
  scraped_at   TEXT NOT NULL,
  UNIQUE(source, source_id)
);

CREATE TABLE scores (
  event_id  TEXT NOT NULL REFERENCES events(id),
  scorer    TEXT NOT NULL,         -- 'booze' (the only scorer today; more may join)
  score     REAL NOT NULL,         -- 0.0–1.0
  rationale TEXT NOT NULL,         -- shown in UI as "why we think this"
  scored_at TEXT NOT NULL,
  PRIMARY KEY (event_id, scorer)
);

CREATE TABLE event_tags (
  event_id TEXT NOT NULL REFERENCES events(id),
  tag      TEXT NOT NULL,          -- free-form: 'hackathon'|'panel'|'happy hour'|...
  PRIMARY KEY (event_id, tag)      -- arbitrary set per event; no fixed vocabulary yet
);

-- events/scores/event_tags above are the shared, crowdsourced catalog — every
-- table below this line is scoped to a visitor.

-- One row per visitor. Identity is anonymous: no login/password, no PII — a
-- random id is minted into a signed session cookie on first visit (see
-- app.auth.current_user) and mirrored here as the FK target for their
-- per-user state.
CREATE TABLE users (
  id            TEXT PRIMARY KEY,  -- uuid4 hex; LEGACY_USER_ID ("legacy") for
                                    --   data migrated from a pre-accounts DB
  email         TEXT UNIQUE,       -- NULL for anonymous users (the norm now)
  password_hash TEXT,              -- NULL for anonymous users; the legacy
                                    --   user row may still carry the old "!"
                                    --   sentinel from before this column was
                                    --   nullable
  created_at    TEXT NOT NULL
);

CREATE TABLE settings (            -- home_address/home_lat/home_lon, and the
  user_id TEXT NOT NULL REFERENCES users(id),   --   visitor's STICKY filter
  key     TEXT NOT NULL,                        --   defaults only (included_tags,
  value   TEXT NOT NULL,                        --   included_sources, price_filter,
  PRIMARY KEY (user_id, key)                    --   max_mi, min_booze) — the live
);                                               --   filter comes from the URL when
                                                  --   present; see "The fetch+swap
                                                  --   frontend" above

CREATE TABLE feedback (
  user_id    TEXT NOT NULL REFERENCES users(id),
  event_id   TEXT NOT NULL REFERENCES events(id),
  verdict    TEXT NOT NULL,        -- 'went'|'skipped'|'as_promised'|'not_as_promised'
  lens       TEXT NOT NULL DEFAULT '', -- scorer a promise verdict applies to ('booze');
                                       --   '' for the universal went/skipped
  note       TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, event_id, verdict, lens)
);

CREATE TABLE holds (                -- calendar holds a user placed; drives THEIR debrief
  user_id    TEXT NOT NULL REFERENCES users(id),
  event_id   TEXT NOT NULL REFERENCES events(id),
  lens       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, event_id)
);
-- The UI surfaces feedback as host trust marks ("host 2/2 as promised"),
-- consumed by scoring/scorers/booze.py::_host_reputation() — deliberately
-- aggregated across EVERY user's feedback, not scoped to the viewer. That's
-- the one place "per-user" data feeds back into something shared: the more
-- people use the app, the better host trust gets for everyone.
```

**Dedup:** primary key on `(source, source_id)` makes re-scraping idempotent.
Cross-source dedup (same party on Luma AND Eventbrite) runs in `ingest/dedup.py`
(URL-identity match, then fuzzy title/date/venue match) and sets
`duplicate_of` on the non-canonical copies — every read query filters
`duplicate_of IS NULL`, so duplicates are fully merged out of every view, not
just flagged.

**Tiers:** weighted sum of scores → `confident` (top section) / `maybe`
(collapsed section) / hidden. Thresholds live in `scoring/weights.py`.

## Core interactions (what the UI must get right)

0. **Design stance** — sloshbot is a recommender, not a browser. The home view
   answers "what's my move tonight" with ONE hero pick; the top tier is hard-
   capped at 3 picks per day (scarcity = taste). "Booze" is a *lens* — one
   pluggable perk scorer — today it's the only one, so ranking is just the booze
   score; adding a perk (food, live music) means adding a scorer + weight in
   `scoring/weights.py`. Event-type preference is the tag filter, not a scorer.
   Voice stays neutral.
1. **Tonight view (home)** — hero pick + collapsed backups + collapsed
   "everything else this week" compact rows. Includes the morning-after
   debrief: after a held event ends, one card asks went/skipped and
   as-promised/not (per lens). Feedback lives there, not on every card.
1b. **Week view** — Sunday-skim digest: next 7 days, grouped by day, tiered.
2b. **Calendar view** — Google-Calendar-style week grid: 7 day columns, time
   axis, events rendered as blocks spanning their start→end times (missing
   `ends_at` assumed 2h, same as the calendar-hold link). Overlapping events
   share the column side by side.
3. **Event card** — title, day/time, venue, host, tier badge, and the scorer
   rationales ("Open bar mentioned; sponsor logos; host's past events had
   drinks"). Trust comes from showing the *why*.
4. **Add to calendar** — pre-filled Google Calendar template link
   (`calendar.google.com/calendar/render?action=TEMPLATE&...`). Creates a
   tentative hold, zero OAuth. Overlapping holds are fine by design.
5. **Feedback taps** — went / skipped, and booze yes / no. One tap,
   writes to `feedback`, no page reload needed beyond a swap.

## Deliberate exclusions

- No Calendar API / OAuth — template links only.
- No auto-apply to gated events (`rsvp_type` captured now to enable it later).
- No Partiful (no public discovery surface; revisit via Gmail invite parsing).
- No unit tests; instead a hand-labeled golden set (~10 events) to eyeball
  scorer changes against.

**No longer exclusions (were v1 cuts, since built):** multi-user scoping (see
Anonymous identity above) — identity is now anonymous, no accounts at all;
feedback IS consumed, by `_host_reputation()` in `booze.py`, aggregated
across every user; cross-source dedup auto-merges, it doesn't just flag.
