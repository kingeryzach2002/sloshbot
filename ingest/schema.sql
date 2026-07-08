CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  url          TEXT NOT NULL,
  title        TEXT NOT NULL,
  description  TEXT,
  host_name    TEXT,
  host_url     TEXT,
  venue_name   TEXT,
  address      TEXT,
  neighborhood TEXT,
  starts_at    TEXT NOT NULL,
  ends_at      TEXT,
  is_free      INTEGER,
  price_min    REAL,
  price_max    REAL,
  rsvp_type    TEXT,
  image_url    TEXT,
  lat          REAL,
  lon          REAL,
  duplicate_of TEXT,                       -- NULL = canonical; else id of the event this duplicates
  raw          TEXT,
  scraped_at   TEXT NOT NULL,
  UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS scores (
  event_id  TEXT NOT NULL REFERENCES events(id),
  scorer    TEXT NOT NULL,
  score     REAL NOT NULL,
  rationale TEXT NOT NULL,
  scored_at TEXT NOT NULL,
  PRIMARY KEY (event_id, scorer)
);

-- One row per signed-up person. The event catalog (events/scores/event_tags)
-- is shared and crowdsourced across everyone — only preferences and personal
-- state (settings/feedback/holds below) are scoped per-user.
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  user_id    TEXT NOT NULL REFERENCES users(id),
  event_id   TEXT NOT NULL REFERENCES events(id),
  verdict    TEXT NOT NULL,             -- 'went'|'skipped'|'as_promised'|'not_as_promised'
  lens       TEXT NOT NULL DEFAULT '',  -- scorer a promise verdict applies to; '' for went/skipped
  note       TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, event_id, verdict, lens)
);

-- calendar holds a user actually placed; drives their morning-after debrief
CREATE TABLE IF NOT EXISTS holds (
  user_id    TEXT NOT NULL REFERENCES users(id),
  event_id   TEXT NOT NULL REFERENCES events(id),
  lens       TEXT NOT NULL DEFAULT '',  -- lens active when the hold was placed
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, event_id)
);

-- free-form labels ("hackathon", "panel", "happy hour"); arbitrary set per event
CREATE TABLE IF NOT EXISTS event_tags (
  event_id TEXT NOT NULL REFERENCES events(id),
  tag      TEXT NOT NULL,
  PRIMARY KEY (event_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_events_starts_at ON events(starts_at);
CREATE INDEX IF NOT EXISTS idx_event_tags_tag ON event_tags(tag);

-- per-user key/value settings (e.g. home_address, home_lat, home_lon,
-- included_tags, included_sources, price_filter)
CREATE TABLE IF NOT EXISTS settings (
  user_id TEXT NOT NULL REFERENCES users(id),
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
