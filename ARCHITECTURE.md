# Sloshbot — Architecture

A multi-user tool that aggregates SF events, scores them with pluggable
scorers (including free-booze likelihood), and presents a tiered candidate
list with tentative-hold calendar links. The event catalog and scores are
shared and crowdsourced across every signed-up user; only preferences and
personal state (home address, filters, feedback, holds) are per-account.

## Layout

```
sloshbot/
├── ARCHITECTURE.md      # this file — the contract everything builds against
├── sloshbot.db          # SQLite; the ONLY interface between pipeline and app
├── pipeline.py          # host-agnostic refresh: ingest -> dedup -> geocode ->
│                         #   score -> prune. `python -m pipeline` (one-shot) or
│                         #   `--loop --interval N` for hosts with no native cron
├── ingest/
│   ├── schema.sql       # canonical DDL
│   ├── sources/         # one module per source (luma, eventbrite, funcheap,
│   │   └── ...          #   dothebay, garysguide, meetup, ra, nineteenhz);
│   │                     #   each exposes fetch() -> list[RawEvent]
│   ├── tags.py          # assigns each event tags from a hand-pruned vocabulary
│   ├── dedup.py         # cross-source duplicate detection (sets duplicate_of)
│   ├── geocode.py       # fills lat/lon from address (cached Nominatim lookups);
│   │                     #   also backfills home_lat/home_lon for every user
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
    │                     #   plus auth routes, the write paths
    │                     #   (feedback/holds/settings), and a JSON API
    ├── auth.py           # password hashing, session-cookie identity,
    │                     #   require_user_html / require_user_api dependencies
    ├── models.py        # Event: the in-memory contract + JSON serialization
    ├── data.py          # pure DB reads -> Event (the only read-path SELECTs);
    │                     #   every function takes user_id
    ├── filters.py       # hard filters (source/price/distance/tag/booze) + chip counts
    ├── policy.py        # ranking policy: composite/tier, source demotion, per-day cap
    ├── presenter.py     # view-model: datetimes, distance, gcal link, calendar geometry
    └── templates/       # tonight/week/calendar/map/login/signup views + partials
                          #   (Jinja, no build chain)
```

The read pipeline is one directed flow, one responsibility per module:
`data.fetch_events → presenter.enrich → filters.apply → policy.rank`. Ranking
primitives (`composite`, `tier`, thresholds) live in `scoring/weights.py`; the
`MAX_PICKS` scarcity cap is an app *presentation* policy in `app/policy.py`. The
same pipeline feeds both the Jinja views and the JSON API (`Event.to_dict()`),
so a future client-rendered frontend needs no new backend logic.

Run locally with `uv run uvicorn app.main:app --reload`; refresh data with
`uv run python -m pipeline` (runs ingest → dedup → geocode → score → prune in
one shot; `--loop --interval N` keeps it running without native cron, `--rescore`
forces a full booze rescore instead of unscored-only — expensive, use after a
scorer prompt/heuristic change). `SLOSHBOT_SECRET_KEY` must be set to a real
random value before hosting publicly — it signs the session cookie.

Event-type preference used to be a third scorer ("category"/"fit") driven by a
hardcoded keyword dict in `preferences.py`. It's gone in favor of a tag filter:
`event_tags` (populated by `ingest/tags.py`) already carries per-event tags, and
the week view lets you check which tags you want; the selection lives in the
`settings` table (`included_tags`, now per-user — see Accounts below) and is a
hard filter applied in `app/filters.py::apply` — events with none of your
selected tags are hidden, events with no tags at all are never hidden by it. No
code edit or rescore needed to change what you're interested in.

**Data flow (batch, never live):**
`pipeline.py` (cron / `--loop` / manual) → ingest each source → dedup → geocode
→ `scoring.run` (scores unscored events, cached) → prune → SQLite → web app
(reads only) → user actions (feedback, calendar holds, settings) write back to
SQLite, scoped to the signed-in user_id.

The web app never scrapes and never calls an LLM. If the UI is broken, the
pipeline is fine; if scraping breaks, the UI still serves yesterday's data.

## Accounts & multi-user scoping

Public signup (`/signup`, `/login`, `/logout` in `app/main.py`; password
hashing + session-cookie identity in `app/auth.py` — no separate sessions
table, the cookie itself carries `{"user_id": ...}`, signed via
`SLOSHBOT_SECRET_KEY`). Every HTML route depends on `require_user_html`
(redirects signed-out visitors to `/login?next=...`); every JSON/write route
depends on `require_user_api` (401, since a redirect would just confuse a
`fetch()` caller).

**What's shared vs. per-user is a deliberate split, not an accident:**

| Scoped per-user | Shared / crowdsourced across everyone |
|---|---|
| `settings` (home address, included_tags/sources, price_filter) | `events`, `scores`, `event_tags` — the catalog itself |
| `feedback` — *your* went/skipped/as-promised taps (drives card button state) | host reputation — `_host_reputation()` in `booze.py` aggregates **every** user's feedback for a host, not just yours; it gets more accurate as more people use the app |
| `holds` — *your* calendar holds (drives *your* morning-after debrief) | `geocode_cache` — an address string → lat/lon lookup, safe to share |

`app/data.py::fetch_events(start, end, user_id)` is where this split actually
happens: the per-event `feedback` set it attaches is filtered to `user_id`, but
the `host_rep` it attaches is computed from an unfiltered join — same function,
two different scoping rules, on purpose.

Pre-multi-user installs migrate automatically and losslessly: `db.py`'s
`init_db()` detects the old unscoped `settings`/`feedback`/`holds` shape and
rescopes every existing row to a stable `LEGACY_USER_ID` ("legacy") account.
Nothing is deleted. Once the site owner signs up for a real account,
`db.reassign_legacy_user(new_user_id)` moves that data onto it (one-time,
idempotent — a second call is a no-op since the legacy rows will already be
gone).

## App layering & the frontend seam

The web app is split into single-responsibility layers with one directed read
flow. The HTTP layer (`app/main.py`) is thin: it authenticates the caller, then
composes the read pipeline and either renders Jinja OR serializes JSON — the
SAME data either way.

```
                         ┌───────────────────────── app/main.py (FastAPI) ─────────────────────────┐
  browser / client  ───▶ │  auth.require_user_html / require_user_api  →  user_id                  │
                         │        │                                                                 │
                         │  GET /  /week  /calendar  /map          GET /api/events  /api/counts     │
                         │        │  (HTML via Jinja templates)            │  (JSON via Event.to_dict)│
                         │        └──────────────┬─────────────────────────┘                          │
                         │                       ▼   load_events(user_id, start, end, max_mi, min_booze)│
                         │   data.fetch_events ─▶ presenter.enrich ─▶ filters.apply ─▶ policy.rank     │
                         │   (SQLite reads →      (start/end dt,       (source/price/   (composite/    │
                         │    Event objects,      distance, gcal)      distance/tag/    tier, source  │
                         │    THIS user's fb,                          booze filters)   demotion,     │
                         │    shared host_rep)                                          per-day cap)  │
                         │                                                                             │
                         │   writes:  POST /feedback/{id}/{verdict}   /hold/{id}   /settings/*  ──────▶│──▶ SQLite
                         │            (all scoped to user_id from the session cookie)                  │
                         └─────────────────────────────────────────────────────────────────────────────┘
                                     ▲ scoring/weights.py: composite(), tier(), thresholds, LENSES, LENS_META
```

**Layer responsibilities (`app/`):**

| Module | Owns | A frontend rewrite… |
|---|---|---|
| `models.py` | `Event` — field contract + `to_dict()` JSON shape | **consumes** (it's the API payload) |
| `auth.py` | password hashing, session-cookie identity, `require_user_html`/`require_user_api` | never touches (a client app still needs the session cookie or an equivalent) |
| `data.py` | the only read-path SQL → `Event`; every function takes `user_id` | never touches |
| `presenter.py` | datetimes, distance, gcal link, **calendar geometry** (`layout_day`) | consumes dt/distance/gcal via API; see calendar note below |
| `filters.py` | hard filters + `chip_counts` | consumes counts; see filter-state note |
| `policy.py` | composite/tier rollup, source demotion, per-day `MAX_PICKS` cap | consumes `tier`/`composite` |
| `main.py` | routes only — auth + compose + render/serialize + write paths | **replace** the HTML routes; keep auth + the API + write paths |
| `templates/` | Jinja HTML, client JS, design tokens (incl. `login.html`/`signup.html`) | **replace entirely** except the auth flow's semantics |

Ranking *primitives* (`composite`, `tier`, thresholds, `LENSES`, `LENS_META`)
live in `scoring/weights.py`; the `MAX_PICKS` scarcity cap is a *presentation*
policy in `app/policy.py`. Per-lens display metadata (emoji, label) lives once
in `scoring/weights.py::LENS_META` — adding a perk lens is a config entry there
plus its scorer, not copy-pasted markup.

## HTTP surface

```
AUTH   GET/POST /signup      email + password + password_confirm → session cookie
       GET/POST /login       email + password (+ ?next=)         → session cookie
       POST     /logout      clear the session                   → 303 /login
         (signed-out visitors hit require_user_html → 303 /login?next=...;
          signed-out API/write calls hit require_user_api → 401 JSON)
HTML   GET /                 hero "tonight's pick" + backups + rest-of-week
       GET /week             7-day digest, grouped by day, tiered
       GET /calendar         week grid (uses presenter.layout_day)
       GET /map              Leaflet pins + home/radius rings
         (all accept ?max_mi= &min_booze= ; home also ?lens=)
JSON   GET /api/events       {events: [Event.to_dict(), ...]}  ?days= &max_mi= &min_booze=
       GET /api/counts       {tags:[{tag,n}], sources:[{source,n}], price:{free,paid}}
WRITE  POST /feedback/{event_id}/{verdict}?lens=   toggle YOUR verdict → {ok, state}
       POST /hold/{event_id}?lens=                 record YOUR calendar hold → {ok}
       POST /settings/tags|sources|price/toggle    flip YOUR filter → {ok, state}
       POST /settings/tags|sources|price/clear     clear YOUR filter dimension
       POST /settings/home  (form: home_address)   save + geocode YOUR home → 303 redirect
         (every write above is scoped to the session's user_id — see Accounts above)
```

`Event.to_dict()` already carries everything a view needs: base fields, `scores`
& `rationales` (keyed by scorer), `tags`, `feedback` (the CALLING user's own
verdicts), `host_rep` (shared across every user), `composite`, `tier`,
`distance_mi`, `gcal`, and ISO `start_dt`/`end_dt`. So the read side of a client
frontend needs **no new backend logic** — just the session cookie + `/api/events`.

## Rewriting the frontend (React or similar)

**Touch / replace:** everything under `app/templates/` (Jinja HTML, the inline
client JS in `_filterbar.html` / `_feedback.html` / `map.html`, and the design
system in `_theme.html`). Port the `_theme.html` `:root` CSS variables — the
speakeasy palette, Fraunces serif, "tier shown by light not badges" — those are
the visual contract. The HTML routes in `main.py` (`home`, `week`, `calendar`,
`map_view`) get deleted or reduced to serving the SPA shell.

**Keep / don't touch:** `auth.py`, `data.py`, `filters.py`, `policy.py`,
`models.py`, `scoring/`, and the auth + `/api/*` + write routes. That's the
whole backend the client talks to — a client app still needs to carry the
session cookie (or swap in a bearer-token variant of `require_user_api`).

**Three decisions the rewrite must make** (seams exist; pick a side):

1. **Calendar geometry** — `presenter.layout_day` (overlap lane-packing → % top/
   height/left/width) is currently only called by the `/calendar` HTML route.
   Either add `GET /api/calendar` that returns positioned blocks (keeps the one
   algorithm server-side — recommended), or reimplement lane-packing in JS.
2. **"Tonight" selection** — the hero/backups/rest-of-week split lives in the
   `home()` route, not the API. Either add `GET /api/tonight`, or replicate the
   "pick the day's best" selection client-side over `/api/events`.
3. **Filter state** — today it's split and server-authoritative: `max_mi`/
   `min_booze` are URL query params (already accepted by `/api/events`), while
   tag/source/price are per-user rows in the `settings` table (scoped by
   `user_id`, not global anymore) toggled via POST + full reload. A client app
   will likely still want per-session, client-held filters on top of that.
   Either (a) filter client-side over the `/api/events` payload, or (b) extend
   `/api/events` to accept `tag`/`source`/`price` as query params (moving that
   evaluation off the `settings` table). Note the per-user-but-still-global-
   across-tabs settings model can't support two tabs / two what-if states for
   the same account — resolving that is part of this call.

None of the three is blocked; each is additive because the read pipeline and its
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
-- table below this line is scoped to a signed-up user.

CREATE TABLE users (
  id            TEXT PRIMARY KEY,  -- uuid4 hex; LEGACY_USER_ID ("legacy") for
                                    --   data migrated from a pre-accounts DB
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,     -- bcrypt; "!" is the legacy user's unusable sentinel
  created_at    TEXT NOT NULL
);

CREATE TABLE settings (            -- home_address/home_lat/home_lon,
  user_id TEXT NOT NULL REFERENCES users(id),   --   included_tags, included_sources,
  key     TEXT NOT NULL,                        --   price_filter
  value   TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

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
- No email verification / password reset flow on signup yet — an account is
  just an email + password, no confirmation loop. Fine for now; a real reset
  flow needs outbound email, which the app doesn't send anywhere else either.
- No per-account rate limiting / abuse controls on `/signup` — acceptable for
  a small-scale share-the-link launch, revisit before wider distribution.

**No longer exclusions (were v1 cuts, since built):** auth/multi-user (see
Accounts above); feedback IS consumed, by `_host_reputation()` in
`booze.py`, aggregated across every user; cross-source dedup auto-merges,
it doesn't just flag.
