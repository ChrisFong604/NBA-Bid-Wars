# Web app image — one service, no build step. Local:
#   docker build -t nba-draft . && docker run -p 8000:8000 nba-draft
# Hosts (Render/Railway/Fly/Cloud Run) auto-detect this file; $PORT respected.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer caches independently of source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY draftbot ./draftbot
COPY webapp ./webapp
COPY README.md ./
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
