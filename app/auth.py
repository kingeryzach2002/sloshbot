"""Anonymous cookie identity: no passwords, no login, no signup.

Every visitor gets a random user_id the first time they hit any route. It is
stored in the signed, httponly session cookie (Starlette's SessionMiddleware,
wired in main.py with a long max_age so the identity survives browser
restarts) and mirrored into a `users` row so per-user writes (settings,
feedback, holds) have a valid foreign key to point at.

current_user() is a FastAPI dependency used on every per-user route, so the
users row is guaranteed to exist before any per-user write happens on that
same request. INSERT OR IGNORE makes the row-creation race-safe if two
requests for a brand-new id somehow land concurrently (shouldn't happen in
practice since the id is only minted once and immediately written into the
session, but it costs nothing to be safe). After the first request, the
cookie already carries the id, so this is just a session-store read.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import Request

from db import get_conn


def current_user(request: Request) -> str:
    uid = request.session.get("user_id")
    if uid:
        return uid
    uid = uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, password_hash, created_at) VALUES (?, NULL, NULL, ?)",
            (uid, datetime.now().isoformat(timespec="seconds")),
        )
    request.session["user_id"] = uid   # SessionMiddleware writes the cookie on the response
    return uid
