# Sloshbot — Architecture

A personal tool that aggregates SF events, scores them with pluggable scorers
(including free-booze likelihood), and presents a tiered candidate list with
tentative-hold calendar links.

## End state (v1, one-night build)

```
sloshbot/
├── ARCHITECTURE.md      # this file — the contract everything builds against
├── sloshbot.db          # SQLite; the ONLY interface between pipeline and app
├── ingest/
│   ├── schema.sql       # canonical DDL
│   ├── sources/         # one module per source (luma, eventbrite, funcheap,
│   │   └── ...          #   dothebay, garysguide, meetup, ra, nineteenhz);
│   │                     #   each exposes fetch() -> list[RawEvent]
│   ├── tags.py          # assigns each event tags from a hand-pruned vocabulary
│   ├── dedup.py         # cross-source duplicate detection (sets duplicate_of)
│   ├── geocode.py       # fills lat/lon from address (cached Nominatim lookups)
│   ├── normalize.py     # RawEvent -> Event (schema below), dedup, upsert
│   └── run.py           # CLI: python -m ingest.run [--source luma]
├── scoring/
│   ├── scorers/         # each: score(event) -> {score: 0-1, rationale: str}
│   │   └── booze.py     # LLM + heuristics; the only scorer (see weights.py)
│   ├── weights.py       # config: scorer weights + tier thresholds
│   ├── eval.py          # eyeball the booze scorer against golden_set.csv
│   └── run.py           # CLI: scores unscored events, caches results in DB;
│                         #   discovers scorer modules dynamically
└── app/
    ├── main.py          # FastAPI, server-rendered (Jinja), no build chain
    └── templates/       # tonight/week/calendar/map views + partials
```

Run locally with `uv run uvicorn app.main:app --reload`; refresh data with
`python -m ingest.run` then `python -m scoring.run` (no deploy config in the
repo — that's added when a host is chosen).

Event-type preference used to be a third scorer ("category"/"fit") driven by a
hardcoded keyword dict in `preferences.py`. It's gone in favor of a tag filter:
`event_tags` (populated by `ingest/tags.py`) already carries per-event tags, and
the week view lets you check which tags you want; the selection lives in the
`settings` table (`included_tags`) and is a hard filter applied in
`app/main.py::load_events` — events with none of your selected tags are hidden,
events with no tags at all are never hidden by it. No code edit or rescore
needed to change what you're interested in.

**Data flow (batch, never live):**
`ingest.run` (cron/manual) → SQLite → `scoring.run` (scores once per event,
cached) → SQLite → web app (reads only) → user actions (feedback, calendar
links) write back to SQLite.

The web app never scrapes and never calls an LLM. If the UI is broken, the
pipeline is fine; if scraping breaks, the UI still serves yesterday's data.

## Event schema — THE contract

Every source spike must answer: "can I fill these fields?" Every UI element
renders from exactly these fields. Change this file first if the schema must
change.

```sql
CREATE TABLE events (
  id           TEXT PRIMARY KEY,   -- "<source>:<source_id>" e.g. "luma:evt-abc123"
  source       TEXT NOT NULL,      -- 'luma' | 'eventbrite' | 'funcheap'
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

CREATE TABLE feedback (
  event_id   TEXT NOT NULL REFERENCES events(id),
  verdict    TEXT NOT NULL,        -- 'went'|'skipped'|'as_promised'|'not_as_promised'
  lens       TEXT NOT NULL DEFAULT '', -- scorer a promise verdict applies to ('booze');
                                       --   '' for the universal went/skipped
  note       TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_id, verdict, lens)
);

CREATE TABLE holds (               -- calendar holds actually placed; drives the debrief
  event_id   TEXT PRIMARY KEY REFERENCES events(id),
  lens       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
-- The UI surfaces feedback as host trust marks ("host 2/2 as promised");
-- a host-reputation scorer consumes it in a later session.
```

**Dedup:** primary key on `(source, source_id)` makes re-scraping idempotent.
Cross-source dedup (same party on Luma AND Eventbrite) = fuzzy match on
(title, starts_at date, venue) — flagged, not auto-merged, in v1.

**Tiers:** weighted sum of scores → `confident` (top section) / `maybe`
(collapsed section) / hidden. Thresholds live in `scoring/weights.py`.

## Core interactions (what the UI must get right)

0. **Design stance** — sloshbot is a recommender, not a browser. The home view
   answers "what's my move tonight" with ONE hero pick; the top tier is hard-
   capped at 3 picks per window (scarcity = taste). "Booze" is a *lens* — one
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

## Deliberate v1 exclusions

- No auth, no multi-user (scoring config in `weights.py`, filters in the
  `settings` table, so multi-user is additive later, not a rewrite).
- No Calendar API / OAuth — template links only.
- No auto-apply to gated events (`rsvp_type` captured now to enable it later).
- No Partiful (no public discovery surface; revisit via Gmail invite parsing).
- Feedback collected but not yet consumed by any scorer.
- No unit tests; instead a hand-labeled golden set (~10 events) to eyeball
  scorer changes against.
