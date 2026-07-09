FROM python:3.12-slim

# uv, per current best practice: copy the static binary from its official
# distroless image rather than installing via pip/curl.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Litestream binary, via the same image-copy pattern as uv above. Only used
# if the owner later sets LITESTREAM_BUCKET (see start.sh + litestream.yml);
# harmless dead weight otherwise.
COPY --from=litestream/litestream:0.5 /usr/local/bin/litestream /usr/local/bin/litestream

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
# deploy. The effective SLOSHBOT_DB path is resolved and exported at container
# boot by start.sh instead of hardcoded here, because it differs by host shape:
#   - Railway-style hosts: a volume mounted at /data -> start.sh exports
#     SLOSHBOT_DB=/data/sloshbot.db.
#   - OpenHost: persistent storage is communicated via OPENHOST_APP_DATA_DIR
#     at runtime, not a fixed /data path -> start.sh exports
#     SLOSHBOT_DB=$OPENHOST_APP_DATA_DIR/sloshbot.db.
# An explicitly-set SLOSHBOT_DB always wins over both. Either way the
# dockerignore excludes sloshbot.db* from the build context, so nothing is
# baked in regardless.

# Optional Litestream replication config (see litestream.yml for details).
# Reading this file at startup is itself gated behind LITESTREAM_BUCKET in
# start.sh, so copying it here is always safe.
COPY litestream.yml /etc/litestream.yml

EXPOSE 8000
CMD ["./start.sh"]
