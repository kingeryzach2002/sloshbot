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
        _repair_users_old_fk_corruption(conn)


def _assert_foreign_key_check_clean(conn: sqlite3.Connection) -> None:
    """PRAGMA foreign_key_check lists every row that violates some FK, across
    the whole DB — empty means clean. Used as a hard gate at the end of every
    risky rebuild below: raising here (inside an open transaction) rolls the
    whole rebuild back rather than ever committing a half-repaired schema."""
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign_key_check failed after rebuild: {[tuple(v) for v in violations]}")


def _disable_fk_rewrite(conn: sqlite3.Connection) -> None:
    """Turn off BOTH pragmas that make `ALTER TABLE x RENAME TO y` follow the
    rename into OTHER tables' FK clauses — this is the exact mechanism that
    corrupted prod (see _migrate_users_to_anonymous's docstring). Empirically
    verified (not just from docs) that BOTH are required together: with only
    `foreign_keys=OFF`, a rename of a referenced table still rewrites
    referencing tables' `REFERENCES` clauses; only `foreign_keys=OFF` AND
    `legacy_alter_table=ON` together restore the pre-3.25 behavior where
    RENAME touches just the one table named in the statement.

    Both are documented no-ops while a transaction is open, so this must be
    called with no pending transaction on `conn` — i.e. before entering a
    `with conn:` block, never inside one."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")


def _restore_fk_rewrite(conn: sqlite3.Connection) -> None:
    """Undo _disable_fk_rewrite. Called in a `finally`, so it runs even if the
    rebuild transaction raised/rolled back — a failed migration must never
    leave a connection with FK enforcement permanently off, since every other
    per-user write on this connection relies on it to catch real bugs."""
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_users_to_anonymous(conn: sqlite3.Connection) -> None:
    """Pre-anonymous-identity DBs had NOT NULL email/password_hash on users
    (from the password-login era). Anonymous users have neither, so drop the
    NOT NULL constraints, preserving every existing row. Fresh DBs built from
    the current schema.sql are already nullable and this is a no-op.

    Pre-existing password accounts (if any) simply become orphaned rows once
    login is gone — their data stays in the DB but is no longer reachable by
    anyone, which is fine for this transition.

    HARDENING (post-mortem on a confirmed prod corruption): get_conn() sets
    PRAGMA foreign_keys=ON for every connection, and with that pragma on,
    `ALTER TABLE users RENAME TO users_old` makes SQLite auto-rewrite every
    OTHER table's `REFERENCES users(id)` clause to `REFERENCES users_old(id)`
    — that's SQLite's documented rename-follows-FK behavior, meant to keep
    references valid across a rename, but here it actively worked against us:
    settings/feedback/holds ended up permanently pointing at the doomed
    `users_old` table instead of the fresh `users` we then created below,
    and nothing in the old version of this function repointed them back. The
    fix (see _disable_fk_rewrite) is to disable BOTH the pragmas that make
    RENAME follow references — `foreign_keys` alone is not enough, verified
    empirically — for the duration of the rename/rebuild, do the rebuild as
    one explicit transaction (so a mid-rebuild failure can't leave a
    committed half-state the way the old bare executescript could), verify
    with foreign_key_check before committing, then restore both pragmas.
    Both are documented no-ops while a transaction is open, so they're
    toggled strictly OUTSIDE the `with conn:` block below."""
    cols = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(users)")}
    if not (cols.get("email") or cols.get("password_hash")):
        return  # already nullable (or a fresh DB)

    _disable_fk_rewrite(conn)
    try:
        with conn:
            conn.execute("ALTER TABLE users RENAME TO users_old")
            conn.execute("""
                CREATE TABLE users (
                  id            TEXT PRIMARY KEY,
                  email         TEXT UNIQUE,
                  password_hash TEXT,
                  created_at    TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO users (id, email, password_hash, created_at)
                SELECT id, email, password_hash, created_at FROM users_old
            """)
            conn.execute("DROP TABLE users_old")
            _assert_foreign_key_check_clean(conn)
    finally:
        _restore_fk_rewrite(conn)


def _migrate_to_multi_user(conn: sqlite3.Connection) -> None:
    """Pre-accounts DBs had global (unscoped) settings/feedback/holds. Scope
    them to LEGACY_USER_ID so existing personal data survives the upgrade.

    HARDENING: these three tables are RENAMEd (to *_old) and rebuilt too, and
    while none of them is itself an FK *target* today (nothing else
    references settings/feedback/holds), the same rename-follows-FK footgun
    that corrupted the users migration (see _migrate_users_to_anonymous)
    applies in principle to any RENAME under PRAGMA foreign_keys=ON. Disabling
    FK enforcement around the rebuilds — and doing each one as an explicit,
    all-or-nothing transaction with a foreign_key_check gate — makes this
    migration safe against that whole class of bug rather than safe only by
    accident of today's schema having no other referrers."""
    settings_cols = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
    feedback_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    holds_cols = {r["name"] for r in conn.execute("PRAGMA table_info(holds)")}
    if "user_id" in settings_cols and "user_id" in feedback_cols and "user_id" in holds_cols:
        return  # already migrated (or a fresh DB, created with user_id already)

    _disable_fk_rewrite(conn)
    try:
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO users (id, email, password_hash, created_at)
                   VALUES (?, 'legacy@local', '!', datetime('now'))""",
                (LEGACY_USER_ID,))

            if "user_id" not in settings_cols:
                conn.execute("ALTER TABLE settings RENAME TO settings_old")
                conn.execute("""
                    CREATE TABLE settings (
                      user_id TEXT NOT NULL REFERENCES users(id),
                      key     TEXT NOT NULL,
                      value   TEXT NOT NULL,
                      PRIMARY KEY (user_id, key)
                    )
                """)
                conn.execute(
                    "INSERT INTO settings (user_id, key, value) SELECT ?, key, value FROM settings_old",
                    (LEGACY_USER_ID,))
                conn.execute("DROP TABLE settings_old")

            if "user_id" not in feedback_cols:
                conn.execute("ALTER TABLE feedback RENAME TO feedback_old")
                conn.execute("""
                    CREATE TABLE feedback (
                      user_id    TEXT NOT NULL REFERENCES users(id),
                      event_id   TEXT NOT NULL REFERENCES events(id),
                      verdict    TEXT NOT NULL,
                      lens       TEXT NOT NULL DEFAULT '',
                      note       TEXT,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY (user_id, event_id, verdict, lens)
                    )
                """)
                conn.execute(
                    """INSERT INTO feedback (user_id, event_id, verdict, lens, note, created_at)
                       SELECT ?, event_id, verdict, lens, note, created_at FROM feedback_old""",
                    (LEGACY_USER_ID,))
                conn.execute("DROP TABLE feedback_old")

            if "user_id" not in holds_cols:
                conn.execute("ALTER TABLE holds RENAME TO holds_old")
                conn.execute("""
                    CREATE TABLE holds (
                      user_id    TEXT NOT NULL REFERENCES users(id),
                      event_id   TEXT NOT NULL REFERENCES events(id),
                      lens       TEXT NOT NULL DEFAULT '',
                      created_at TEXT NOT NULL,
                      PRIMARY KEY (user_id, event_id)
                    )
                """)
                conn.execute(
                    """INSERT INTO holds (user_id, event_id, lens, created_at)
                       SELECT ?, event_id, lens, created_at FROM holds_old""",
                    (LEGACY_USER_ID,))
                conn.execute("DROP TABLE holds_old")

            _assert_foreign_key_check_clean(conn)
    finally:
        _restore_fk_rewrite(conn)


def _repair_users_old_fk_corruption(conn: sqlite3.Connection) -> None:
    """One-time, self-healing repair for a CONFIRMED prod corruption (verified
    against the real prod DB, not hypothetical): settings.user_id,
    feedback.user_id and holds.user_id all ended up with
    `REFERENCES "users_old"(id)` instead of `REFERENCES "users"(id)`.

    Root cause: the old (pre-hardening) _migrate_users_to_anonymous ran
    `ALTER TABLE users RENAME TO users_old` while PRAGMA foreign_keys=ON
    (get_conn() sets that on every connection). SQLite's rename-follows-FK
    behavior then silently rewrote the three child tables' FK clauses to
    track the renamed table, i.e. to point at users_old. The migration went
    on to create a fresh `users`, copy rows into it, and never repointed the
    children back to it — and its bare executescript autocommitted each
    statement individually, so that half-migrated state stuck. `users_old`
    was left behind with only the 1 row (`legacy`) that existed at migration
    time, while the real `users` table kept growing (241 rows on prod at
    time of writing) — so every per-user write (settings/feedback/holds) by
    any user minted after the corrupting migration ran fails the FK check
    with a 500. That migration is now hardened (see its docstring) so this
    can't recur going forward, but an already-corrupted DB needs this
    explicit repair — hence a dedicated migration rather than just trusting
    the hardening to self-heal in place.

    Detection: `users_old` existing at all is the tell — a healthy DB (fresh,
    or already repaired) never has it, since the hardened migrations above
    drop their *_old scratch tables in the same transaction that creates the
    replacement. Checked first and cheaply (one sqlite_master query) so a
    healthy DB pays zero extra cost on every boot.

    Repair strategy per table: same safe-rebuild pattern as the hardened
    migrations above — foreign_keys OFF around the whole operation (a
    no-op pragma if a transaction were open, so it's set before the `with
    conn:` starts), rebuild each of settings/feedback/holds into a *_new
    table declared with the CORRECT `REFERENCES users(id)`, copy every row
    verbatim (no data loss — this is a schema fix, not a data migration),
    drop the corrupt table, rename *_new into place. All of it plus the
    users_old cleanup happens inside one explicit transaction gated by
    foreign_key_check, so a mid-repair failure rolls back to the original
    (still-broken, but not further-broken) state instead of committing a
    half-repaired schema."""
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "users_old" not in tables:
        return  # healthy: no corruption to repair (fresh DB, or already repaired)

    _disable_fk_rewrite(conn)
    try:
        with conn:
            # Defensive belt-and-suspenders: users_old should be a strict
            # subset of users post-migration (that's what the corrupting
            # migration copied), but if any id ever landed in users_old only
            # — e.g. a write that hit the stale FK target between the
            # corrupting migration and this repair running — preserve it
            # rather than silently losing that identity's data when
            # users_old is dropped below. INSERT OR IGNORE keeps 'legacy'
            # (already in both) untouched.
            conn.execute("""
                INSERT OR IGNORE INTO users (id, email, password_hash, created_at)
                SELECT id, email, password_hash, created_at FROM users_old
            """)

            conn.execute("""
                CREATE TABLE settings_new (
                  user_id TEXT NOT NULL REFERENCES users(id),
                  key     TEXT NOT NULL,
                  value   TEXT NOT NULL,
                  PRIMARY KEY (user_id, key)
                )
            """)
            conn.execute("INSERT INTO settings_new (user_id, key, value) "
                          "SELECT user_id, key, value FROM settings")
            conn.execute("DROP TABLE settings")
            conn.execute("ALTER TABLE settings_new RENAME TO settings")

            conn.execute("""
                CREATE TABLE feedback_new (
                  user_id    TEXT NOT NULL REFERENCES users(id),
                  event_id   TEXT NOT NULL REFERENCES events(id),
                  verdict    TEXT NOT NULL,
                  lens       TEXT NOT NULL DEFAULT '',
                  note       TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, event_id, verdict, lens)
                )
            """)
            conn.execute("INSERT INTO feedback_new (user_id, event_id, verdict, lens, note, created_at) "
                          "SELECT user_id, event_id, verdict, lens, note, created_at FROM feedback")
            conn.execute("DROP TABLE feedback")
            conn.execute("ALTER TABLE feedback_new RENAME TO feedback")

            conn.execute("""
                CREATE TABLE holds_new (
                  user_id    TEXT NOT NULL REFERENCES users(id),
                  event_id   TEXT NOT NULL REFERENCES events(id),
                  lens       TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, event_id)
                )
            """)
            conn.execute("INSERT INTO holds_new (user_id, event_id, lens, created_at) "
                          "SELECT user_id, event_id, lens, created_at FROM holds")
            conn.execute("DROP TABLE holds")
            conn.execute("ALTER TABLE holds_new RENAME TO holds")

            conn.execute("DROP TABLE users_old")

            _assert_foreign_key_check_clean(conn)
    finally:
        _restore_fk_rewrite(conn)
