# Multi-stage build for Coach Web — FastAPI application
# Non-root execution (UID 10001), hardened for local and container deployment.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir build && \
    python -m build --wheel && \
    pip install --no-cache-dir --target=/install/wheels dist/*.whl

# --- Runtime stage ---
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="coach-web" \
      org.opencontainers.image.description="FastAPI web frontend for the Coach MCP ecosystem" \
      org.opencontainers.image.authors="Frederic Pitteloud" \
      org.opencontainers.image.vendor="fpittelo" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/fpittelo/coach-web" \
      org.opencontainers.image.documentation="https://github.com/fpittelo/coach-web/blob/main/README.md"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/site-packages \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    LOG_LEVEL=INFO

# Create non-root user and group (UID/GID 10001)
RUN groupadd -g 10001 coach-web && \
    useradd -u 10001 -g coach-web -s /bin/false -m -d /home/coach-web coach-web

WORKDIR /app

# Copy installed site-packages from builder
COPY --from=builder --chown=coach-web:coach-web /install/wheels /app/site-packages
COPY --from=builder --chown=coach-web:coach-web /app/src /app/src

ENV PATH="/app/site-packages/bin:${PATH}" \
    PYTHONPATH="/app/src:/app/site-packages:${PYTHONPATH}"

USER coach-web:coach-web

# Local/compose topology default is 8000 (docker-compose.yml, issue #63).
# Cloud Run (issue #66) overrides APP_PORT=8080 via the service spec; the
# ENTRYPOINT below resolves both APP_HOST and APP_PORT at runtime.
# EXPOSE and HEALTHCHECK document the local default only — Cloud Run ignores
# them and uses the probes declared in infra/modules/cloud-run/main.tf.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200 else 1)"

# Cloud Run terminates TLS and forwards plain HTTP to the container; the
# proxy-headers flags below make uvicorn honor X-Forwarded-Proto so the app
# derives https URLs (live incident: OIDC redirect_uri mismatch; refs Cloud
# Run docs). forwarded-allow-ips='*' is safe here because Cloud Run's
# front-end always sets these headers and is the only ingress path
# (INGRESS_TRAFFIC_ALL still routes through the front-end); local compose
# sets no forwarded headers, so behavior is unchanged locally.
#
# exec keeps uvicorn as PID 1 so SIGTERM (Cloud Run shutdown signal) is
# delivered directly to the ASGI server.
ENTRYPOINT ["sh", "-c", "exec uvicorn coach_web.app:create_app --factory --host \"${APP_HOST:-0.0.0.0}\" --port \"${APP_PORT:-8000}\" --proxy-headers --forwarded-allow-ips='*'"]
