"""SQLite is the only interface between the pipeline and the app."""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
# SLOSHBOT_DB overrides the location explicitly (e.g. a persistent disk on a
# host). Otherwise, if OPENHOST_APP_DATA_DIR is set (OpenHost's persistent
# per-app data mount — the repo checkout itself is ephemeral there), the DB
# lives inside it. Otherwise it lives beside the code (local dev).
_data_dir = os.environ.get("OPENHOST_APP_DATA_DIR")
DB_PATH = Path(os.environ.get("SLOSHBOT_DB")
               or (Path(_data_dir) / "sloshbot.db" if _data_dir else ROOT / "sloshbot.db"))
SCHEMA_PATH = ROOT / "ingest" / "schema.sql"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # parallel ingest runs share this DB
    # WAL lets readers (web app) proceed while a writer (pipeline) holds a long
    # transaction, instead of blocking page loads during a refresh. It's a
    # persistent, per-database setting on first write, so setting it on every
    # connection is redundant after the first — but harmless and self-healing
    # (e.g. if the DB file is ever replaced/restored without WAL set).
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Stable id for data written before multi-user accounts existed. Preserved,
# never deleted — it just stays a permanently-anonymous bucket now that
# there's no login to reassign it onto.
LEGACY_USER_ID = "legacy"


def ensure_blurb_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add scores.blurb to DBs created before it existed.
    Fresh DBs already have it (see ingest/schema.sql). Called from init_db (so
    both the app and the scoring run get it) and standalone from the blurb
    backfill script, which never runs the full init."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    if "blurb" not in cols:
        conn.execute("ALTER TABLE scores ADD COLUMN blurb TEXT")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        ensure_blurb_column(conn)
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
        _migrate_to_multi_user(conn)
        _migrate_users_to_anonymous(conn)


def _migrate_users_to_anonymous(conn: sqlite3.Connection) -> None:
    """Pre-anonymous-identity DBs had NOT NULL email/password_hash on users
    (from the password-login era). Anonymous users have neither, so drop the
    NOT NULL constraints, preserving every existing row. Fresh DBs built from
    the current schema.sql are already nullable and this is a no-op.

    Pre-existing password accounts (if any) simply become orphaned rows once
    login is gone — their data stays in the DB but is no longer reachable by
    anyone, which is fine for this transition."""
    cols = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(users)")}
    if not (cols.get("email") or cols.get("password_hash")):
        return  # already nullable (or a fresh DB)

    conn.executescript("""
        ALTER TABLE users RENAME TO users_old;
        CREATE TABLE users (
          id            TEXT PRIMARY KEY,
          email         TEXT UNIQUE,
          password_hash TEXT,
          created_at    TEXT NOT NULL
        );
        INSERT INTO users (id, email, password_hash, created_at)
        SELECT id, email, password_hash, created_at FROM users_old;
        DROP TABLE users_old;
    """)


def _migrate_to_multi_user(conn: sqlite3.Connection) -> None:
    """Pre-accounts DBs had global (unscoped) settings/feedback/holds. Scope
    them to LEGACY_USER_ID so existing personal data survives the upgrade."""
    settings_cols = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
    feedback_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    holds_cols = {r["name"] for r in conn.execute("PRAGMA table_info(holds)")}
    if "user_id" in settings_cols and "user_id" in feedback_cols and "user_id" in holds_cols:
        return  # already migrated (or a fresh DB, created with user_id already)

    conn.execute(
        """INSERT OR IGNORE INTO users (id, email, password_hash, created_at)
           VALUES (?, 'legacy@local', '!', datetime('now'))""",
        (LEGACY_USER_ID,))

    if "user_id" not in settings_cols:
        conn.executescript("""
            ALTER TABLE settings RENAME TO settings_old;
            CREATE TABLE settings (
              user_id TEXT NOT NULL REFERENCES users(id),
              key     TEXT NOT NULL,
              value   TEXT NOT NULL,
              PRIMARY KEY (user_id, key)
            );
        """)
        conn.execute(
            "INSERT INTO settings (user_id, key, value) SELECT ?, key, value FROM settings_old",
            (LEGACY_USER_ID,))
        conn.execute("DROP TABLE settings_old")

    if "user_id" not in feedback_cols:
        conn.executescript("""
            ALTER TABLE feedback RENAME TO feedback_old;
            CREATE TABLE feedback (
              user_id    TEXT NOT NULL REFERENCES users(id),
              event_id   TEXT NOT NULL REFERENCES events(id),
              verdict    TEXT NOT NULL,
              lens       TEXT NOT NULL DEFAULT '',
              note       TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (user_id, event_id, verdict, lens)
            );
        """)
        conn.execute(
            """INSERT INTO feedback (user_id, event_id, verdict, lens, note, created_at)
               SELECT ?, event_id, verdict, lens, note, created_at FROM feedback_old""",
            (LEGACY_USER_ID,))
        conn.execute("DROP TABLE feedback_old")

    if "user_id" not in holds_cols:
        conn.executescript("""
            ALTER TABLE holds RENAME TO holds_old;
            CREATE TABLE holds (
              user_id    TEXT NOT NULL REFERENCES users(id),
              event_id   TEXT NOT NULL REFERENCES events(id),
              lens       TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              PRIMARY KEY (user_id, event_id)
            );
        """)
        conn.execute(
            """INSERT INTO holds (user_id, event_id, lens, created_at)
               SELECT ?, event_id, lens, created_at FROM holds_old""",
            (LEGACY_USER_ID,))
        conn.execute("DROP TABLE holds_old")
