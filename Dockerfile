# Note: uv no longer updates its bookworm images; trixie is the maintained Debian base.
FROM ghcr.io/astral-sh/uv:0.13.0-trixie-slim

WORKDIR /app

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON=3.15 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

RUN uv python install 3.15

# Dependencies first so code changes do not bust the cache
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY main.py icon.svg ./
COPY procore_mcp ./procore_mcp

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
