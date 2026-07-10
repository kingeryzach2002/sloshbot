# Sloshbot

**Live at [sloshbot.beer](https://sloshbot.beer)** — hosted on OpenHost, with
the domain pointed there via a Cloudflare redirect (see
[README-DEPLOY.md](./README-DEPLOY.md)).

Sloshbot is an SF free-booze event finder. It scrapes events from a handful
of sources (Luma, Eventbrite, Funcheap, DoTheBay, GarysGuide, Meetup,
Resident Advisor, 19hz), uses an LLM plus heuristics to score each one on
free-drink likelihood, and serves a tiered "what's my move tonight"
recommendation — one confident hero pick, a few backups, and the rest of the
week as a lower-key digest. There's no login: every visitor gets an
anonymous per-visitor identity on first touch, and their filters, feedback,
calendar holds, and home address are scoped to them, while the event catalog
itself is shared and crowdsourced across everyone.

## Run locally

```bash
uv sync

# populate the database: scrape all sources, dedup, geocode, score, prune
uv run python -m pipeline

# start the web app
uv run uvicorn app.main:app --reload
```

Then open `http://localhost:8000`. `pipeline.py` needs `ANTHROPIC_API_KEY`
set (for booze scoring); everything else runs with no config for local dev —
`SLOSHBOT_SECRET_KEY` gets an insecure dev fallback so you don't need to set
it, but you must set a real random value before hosting this publicly (see
`README-DEPLOY.md`).

Re-run `uv run python -m pipeline` whenever you want fresh data; add
`--loop --interval N` to keep it refreshing on a timer instead of running
your own cron, or `--rescore` to force a full re-score after changing the
booze scorer's prompt/heuristics (expensive — normally it only scores
unscored events).

In the deployed setup, `pipeline.py` doesn't run against the prod DB at
all — see "Data flow" below and `sync.py` / `README-DEPLOY.md` for how the
split-host sync actually works day to day.

## Data flow

In production, scraping and scoring run on a separate local machine (a
residential IP, since event sources increasingly block datacenter hosts),
not on the hosted server. `pipeline.py` (daily via launchd on that local
machine) → scrape each source → dedup → geocode → score (booze likelihood) →
`sync.py` pushes the result over HTTPS to the hosted server. The hosted
server itself only ever reads from SQLite (renders Jinja HTML and a JSON
API) → your feedback taps, calendar holds, and filter/home changes write
back to that server's SQLite, scoped to your anonymous visitor id, and get
pulled back down to the local machine before its next run. The hosted server
never scrapes and never calls an LLM itself — if the site looks broken, the
pipeline is fine; if scraping breaks, the site just keeps serving
yesterday's data. See `ARCHITECTURE.md`'s "The split-host sync model" for
details.

## Learn more

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — the design/contract: module
  layout, the read pipeline (`data → presenter → filters → policy`), the
  HTTP surface, the anonymous-identity model, the fetch+swap frontend, and
  the event/DB schema. Read this before changing how the app is put
  together.
- **[README-DEPLOY.md](./README-DEPLOY.md)** — a plain-language runbook for
  hosting Sloshbot (Railway/OpenHost, env vars, offsite backup via
  Litestream, disaster recovery).

## Repo layout at a glance

- `ingest/` — one scraper module per source, plus dedup/geocode/tagging.
- `scoring/` — the pluggable scorer(s) (currently just booze likelihood) and
  ranking config (`weights.py`).
- `app/` — the FastAPI web app: routes, anonymous auth, DB reads, filters,
  ranking policy, view-model presentation, and Jinja templates.
- `pipeline.py` — the one-shot/looping refresh job that ties ingest → dedup
  → geocode → score → prune together.

No unit tests today — scorer changes are eyeballed against a small
hand-labeled `golden_set.csv` (see `scoring/eval.py`).
