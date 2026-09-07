# Two stages: build the wheel and its dependency set with uv, then copy only
# the virtualenv into a slim runtime that runs as an unprivileged user.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    ROUTER_CONFIG=/app/router.yaml \
    DATABASE_URL=sqlite:////data/router.db
COPY --chown=app:app router.yaml /app/router.yaml
USER 10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"
CMD ["model-router", "serve"]
