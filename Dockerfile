# Multi-stage build for Coach Web — Streamlit dashboard
# Non-root execution (UID 10001), same hardening as Coach MCP server

FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml README.md ./

RUN pip install --no-cache-dir .

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

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "src/coach_web/app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
