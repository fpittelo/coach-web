# Multi-stage build for Coach Web — FastAPI application
# Non-root execution (UID 10001), same hardening as Coach MCP server

FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# --- Runtime stage ---
FROM python:3.12-slim

RUN groupadd -g 10001 coach-web && \
    useradd -u 10001 -g coach-web -s /sbin/nologin coach-web

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/README.md /app/README.md

RUN chown -R coach-web:coach-web /app

USER coach-web

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=5).status == 200 else 1)"

ENTRYPOINT ["uvicorn", "coach_web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
