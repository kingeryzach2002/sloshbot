FROM python:3.12-slim

# uv, per current best practice: copy the static binary from its official
# distroless image rather than installing via pip/curl.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Event times are stored as Pacific ISO 8601 and the app uses naive
# datetime.now(); the container clock must agree, or "tonight" drifts.
ENV TZ=America/Los_Angeles
# Compile .py -> .pyc at build time instead of on first import at runtime —
# faster cold start, worth the extra build time for a long-lived container.
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Copy only the dependency manifest first so `uv sync` is cached across
# rebuilds that don't touch dependencies — the app code changes far more
# often than pyproject.toml/uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Now copy the rest of the app (see .dockerignore for what's excluded).
COPY . .

# IMPORTANT: the database is NOT baked into the image. Deploys replace the
# container wholesale, so any state stored inside it (i.e. the default
# sloshbot.db-beside-the-code path in db.py) would be discarded on every
# deploy. SLOSHBOT_DB points at a path under /data instead — the host must
# mount a persistent volume at /data and the app's dockerignore already
# excludes sloshbot.db* from the build context, so this is belt-and-suspenders.
ENV SLOSHBOT_DB=/data/sloshbot.db

EXPOSE 8000
CMD ["./start.sh"]
