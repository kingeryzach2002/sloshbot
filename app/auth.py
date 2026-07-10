"""Anonymous cookie identity: no passwords, no login, no signup.

Every visitor gets a random user_id the first time they hit any route. It is
stored in the signed, httponly session cookie (Starlette's SessionMiddleware,
wired in main.py with a long max_age so the identity survives browser
restarts) and mirrored into a `users` row so per-user writes (settings,
feedback, holds) have a valid foreign key to point at.

current_user() is a FastAPI dependency used on every per-user route, so the
users row is guaranteed to exist before any per-user write happens on that
same request. The INSERT OR IGNORE runs on EVERY request, not just when a new
id is minted: a returning visitor's signed cookie can outlive its users row
(the secret key persists across deploys, so old cookies keep validating even
if the DB/volume was rebuilt or restored without that row) — and when it does,
returning the cookie's id without re-ensuring the row would make every
per-user write (settings/feedback/holds) fail the users(id) foreign key with a
500. Re-asserting the row is idempotent, self-heals that case, and is also
race-safe if two requests for a brand-new id land concurrently. The cost is
one no-op INSERT per request, negligible at this app's scale and far cheaper
than a class of 500s that only some visitors hit.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import Request

from db import get_conn


def current_user(request: Request) -> str:
    uid = request.session.get("user_id")
    minted = uid is None
    if minted:
        uid = uuid.uuid4().hex
    # INSERT OR IGNORE unconditionally: for a minted id it creates the row; for
    # a returning id it's a no-op UNLESS the row is missing (cookie outlived its
    # users row — see module docstring), in which case it self-heals the FK
    # target instead of letting the next per-user write 500.
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, password_hash, created_at) VALUES (?, NULL, NULL, ?)",
            (uid, datetime.now().isoformat(timespec="seconds")),
        )
    if minted:
        request.session["user_id"] = uid   # SessionMiddleware writes the cookie on the response
    return uid
