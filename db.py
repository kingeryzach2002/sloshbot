"""SQLite is the only interface between the pipeline and the app."""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
# SLOSHBOT_DB overrides the location (e.g. a persistent disk on a host);
# otherwise the DB lives beside the code.
DB_PATH = Path(os.environ.get("SLOSHBOT_DB") or ROOT / "sloshbot.db")
SCHEMA_PATH = ROOT / "ingest" / "schema.sql"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # parallel ingest runs share this DB
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        # additive migrations for DBs created before a column existed
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        # duplicate_of: NULL = canonical/unique; else the id of the canonical
        # event this row duplicates (set by the cross-source dedup pass).
        for col, ddl in [("lat", "lat REAL"), ("lon", "lon REAL"),
                         ("duplicate_of", "duplicate_of TEXT")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE events ADD COLUMN {ddl}")
        # feedback gained a lens column + generic verdicts; rebuild pre-lens tables
        fb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
        if "lens" not in fb_cols:
            conn.executescript("""
                ALTER TABLE feedback RENAME TO feedback_old;
                CREATE TABLE feedback (
                  event_id   TEXT NOT NULL REFERENCES events(id),
                  verdict    TEXT NOT NULL,
                  lens       TEXT NOT NULL DEFAULT '',
                  note       TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (event_id, verdict, lens)
                );
                INSERT INTO feedback (event_id, verdict, lens, note, created_at)
                SELECT event_id,
                       CASE verdict WHEN 'booze_confirmed' THEN 'as_promised'
                                    WHEN 'booze_lie' THEN 'not_as_promised'
                                    ELSE verdict END,
                       CASE WHEN verdict IN ('booze_confirmed', 'booze_lie')
                            THEN 'booze' ELSE '' END,
                       note, created_at
                FROM feedback_old;
                DROP TABLE feedback_old;
            """)
