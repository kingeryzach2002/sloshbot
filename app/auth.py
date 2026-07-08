"""Account auth: password hashing + session-cookie identity.

Sessions are Starlette's cookie-session middleware (signed, httponly,
samesite="lax") — no separate sessions table, the cookie itself carries
{"user_id": ...}. main.py wires SessionMiddleware with a secret key.

CONTRACT
  create_user(email, password) -> user_id
      Raises ValueError if the email is already registered.
  authenticate(email, password) -> user_id | None
  current_user(request) -> str | None       # reads request.session["user_id"]
  require_user_html(request) -> str         # FastAPI dependency for page routes;
                                              # redirects to /login if signed out
  require_user_api(request) -> str          # FastAPI dependency for JSON/write
                                              # routes; raises 401 if signed out
"""
from __future__ import annotations

import uuid
from datetime import datetime

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from db import get_conn


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False  # malformed/sentinel hash (e.g. the legacy user's "!")


def create_user(email: str, password: str) -> str:
    email = email.strip().lower()
    user_id = uuid.uuid4().hex
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError("An account with that email already exists.")
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, email, _hash_password(password), datetime.now().isoformat(timespec="seconds")),
        )
    return user_id


def authenticate(email: str, password: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?",
                           (email.strip().lower(),)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return row["id"]


def current_user(request: Request) -> str | None:
    return request.session.get("user_id")


class RedirectToLogin(StarletteHTTPException):
    """Raised by require_user_html; a dedicated exception handler in main.py
    turns this into a 303 redirect to /login instead of a JSON error body."""
    def __init__(self, next_path: str):
        super().__init__(status_code=303, detail=next_path)


def require_user_html(request: Request) -> str:
    """Dependency for HTML page routes: bounces signed-out visitors to /login
    (with ?next= so they land back where they meant to go)."""
    user_id = current_user(request)
    if not user_id:
        raise RedirectToLogin(request.url.path)
    return user_id


def require_user_api(request: Request) -> str:
    """Dependency for JSON/write routes (fetch()-driven, not navigation): a
    303 redirect would just get silently followed by fetch and confuse the
    caller, so signed-out here is a plain 401 instead."""
    user_id = current_user(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="not signed in")
    return user_id


def login_redirect_handler(request: Request, exc: RedirectToLogin) -> RedirectResponse:
    return RedirectResponse(f"/login?next={exc.detail}", status_code=303)
