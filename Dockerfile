# Base image pinned by digest (python:3.12-slim, the same as demo-rag).
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VAR_DIR=/var/lib/demo-brief

WORKDIR /app

RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/app app \
    && mkdir -p /var/lib/demo-brief \
    && chown app:app /var/lib/demo-brief

COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock

# app/recorded/*.json (recorded examples) ship inside app/. scripts/ and tests/ stay out.
COPY app ./app

USER app

# Hourly limits, the daily budget and runs (30 days: masked brief, specs, prototypes, events) live here.
VOLUME ["/var/lib/demo-brief"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/brief/health', timeout=4)"

# One worker: the run limit, the thread pool and the SSE readers live in one process.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", \
     "--workers", "1", "--limit-concurrency", "32"]
