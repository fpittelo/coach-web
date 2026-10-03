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

# Local compose topology default is 8000 (compose.yaml, issue #63).
# EXPOSE and HEALTHCHECK document the local default only.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200 else 1)"

# Local topology (ADR-007, #112): no trusted reverse proxy sits in front of the
# container — compose publishes coach-web on 127.0.0.1 only. uvicorn therefore
# trusts X-Forwarded-* headers from loopback peers only (127.0.0.1, ::1), so a
# remote client cannot spoof the scheme/host used to derive the OIDC
# redirect_uri. Direct http://localhost access still derives correct http URLs.
# Any future ingress change (proxy, non-loopback publish) must re-validate this
# flag in the same change (see docs/security.md, Boundary 3).
#
# exec keeps uvicorn as PID 1 so SIGTERM is delivered directly to the ASGI server.
ENTRYPOINT ["sh", "-c", "exec uvicorn coach_web.app:create_app --factory --host \"${APP_HOST:-0.0.0.0}\" --port \"${APP_PORT:-8000}\" --proxy-headers --forwarded-allow-ips='127.0.0.1,::1'"]
