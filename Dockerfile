FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Event times are stored as Pacific ISO 8601 and the app uses naive
# datetime.now(); the container clock must agree.
ENV TZ=America/Los_Angeles
ENV UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .

EXPOSE 8000
CMD ["./start.sh"]
