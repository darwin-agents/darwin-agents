# Darwin platform — supervisor runtime image
#
# Multi-stage build:
#   1. ``builder``  — uv installs deps + the platform extra into a venv.
#   2. ``runtime``  — slim Python 3.12 with just the venv + source. Non-root,
#                     /healthz HEALTHCHECK, default CMD runs the supervisor.
#
# The image is provider-neutral: no cloud SDKs, no AWS/GCP/Azure-specific
# bits. It listens on 8000 and persists to /data/state.db by default.

# -- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv is the fast resolver/installer we already use locally. Pin a known
# version so reproducible builds land the same wheel set in CI + dev.
ARG UV_VERSION=0.5.20
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /build

# Copy only what's needed to install: the lockfile + manifest + source.
# .dockerignore prunes everything else.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Create the runtime venv under /opt/venv and install the platform extra
# (FastAPI, uvicorn, OpenTelemetry, LiteLLM client). System site-packages
# stays clean so the runtime image is reproducible.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache \
        ".[platform,llm,graph,postgres,web3,hvac,browser,e2b,solana,stellar,stripe,pdfwatermark,k8s,cdp,webpush]"

# -- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Same Python, no compilers, smaller image. Adds curl for the healthcheck
# script (avoids embedding a Python one-liner in the HEALTHCHECK line).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root operation. /data is the persistence volume — owned by the
# runtime user so SQLite + the audit chain can write without sudo.
RUN useradd --uid 1000 --create-home --shell /bin/bash darwin && \
    mkdir -p /data && chown darwin:darwin /data

# Bring the venv from the builder. PATH+VIRTUAL_ENV so `python` resolves
# to the installed interpreter; no need to activate.
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=darwin:darwin src/ ./src/
# Site-packages already has the package installed; setting PYTHONPATH=src
# keeps an editable-style fallback for local mounts.
ENV PYTHONPATH=/app/src

USER darwin

VOLUME ["/data"]
EXPOSE 8000

# Liveness probe targets the unauthenticated /healthz endpoint. k8s
# readinessProbe / livenessProbe maps cleanly onto this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1

# Default to running the supervisor with SQLite-backed persistence at
# /data/state.db. ``serve --auth-tokens /run/secrets/tokens.json`` is the
# typical production layering on top.
CMD ["python", "-m", "darwin_platform", "serve", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--db", "/data/state.db"]
