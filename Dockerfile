# ── Build stage ───────────────────────────────────────────────────────
# Pinned by digest (AEP-014): two builds resolve the same bytes; Dependabot's
# docker ecosystem bumps the digest when the upstream tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1 AS builder

WORKDIR /app

# Production-oriented uv settings: compile bytecode for faster cold starts,
# and copy (rather than hardlink) so the venv is portable across layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install workspace dependencies (cached layer).
# Copy all pyproject files first so uv can resolve the workspace
# independently of source code changes.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY agents/docker-agent/pyproject.toml agents/docker-agent/pyproject.toml
COPY agents/elasticsearch/pyproject.toml agents/elasticsearch/pyproject.toml
COPY agents/kafka-health/pyproject.toml agents/kafka-health/pyproject.toml
COPY agents/k8s-health/pyproject.toml agents/k8s-health/pyproject.toml
COPY agents/orrery-assistant/pyproject.toml agents/orrery-assistant/pyproject.toml
COPY agents/observability/pyproject.toml agents/observability/pyproject.toml
COPY agents/ops-journal/pyproject.toml agents/ops-journal/pyproject.toml
COPY agents/slack-bot/pyproject.toml agents/slack-bot/pyproject.toml
COPY agents/google-chat-bot/pyproject.toml agents/google-chat-bot/pyproject.toml

# Placeholder packages so `uv sync` can resolve the workspace before
# real source is copied in.
RUN mkdir -p core/orrery_core && touch core/orrery_core/__init__.py && \
    mkdir -p agents/docker-agent/docker_agent && touch agents/docker-agent/docker_agent/__init__.py && \
    mkdir -p agents/elasticsearch/elasticsearch_agent && touch agents/elasticsearch/elasticsearch_agent/__init__.py && \
    mkdir -p agents/kafka-health/kafka_health_agent && touch agents/kafka-health/kafka_health_agent/__init__.py && \
    mkdir -p agents/k8s-health/k8s_health_agent && touch agents/k8s-health/k8s_health_agent/__init__.py && \
    mkdir -p agents/orrery-assistant/orrery_assistant && touch agents/orrery-assistant/orrery_assistant/__init__.py && \
    mkdir -p agents/observability/observability_agent && touch agents/observability/observability_agent/__init__.py && \
    mkdir -p agents/ops-journal/ops_journal_agent && touch agents/ops-journal/ops_journal_agent/__init__.py && \
    mkdir -p agents/slack-bot/slack_bot && touch agents/slack-bot/slack_bot/__init__.py && \
    mkdir -p agents/google-chat-bot/google_chat_bot && touch agents/google-chat-bot/google_chat_bot/__init__.py

# Cache mount keeps repeated builds fast without bloating the final image.
# `--extra postgres` pulls in the Postgres driver used by the session store.
# `--extra server` pulls in FastAPI, Uvicorn, and PyJWT for the authenticated HTTP front door.
# `--extra otel` pulls in OpenTelemetry so OTEL_TRACING_ENABLED works in the image.
# `--extra mcp` pulls in the MCP SDK so ORRERY_MCP_CONFIG works in the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen --extra postgres --extra server --extra otel --extra mcp --extra a2a

# Copy actual source code
COPY core/ core/
COPY agents/ agents/

# Reinstall workspace packages with real source
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen --extra postgres --extra server --extra otel --extra mcp --extra a2a

# ── Web console build stage (AEP-019) ─────────────────────────────────
# Builds the React SPA to a static bundle. An isolated Node toolchain — no
# Node reaches the runtime image; only the compiled dist/ is copied in.
FROM node:26-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS web-builder

WORKDIR /web

# Install with the committed lockfile first (cached layer, independent of src).
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY web/ ./
RUN npm run build

# ── Runtime stage ─────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

# Apply Debian security updates on top of the pinned base.
#
# The official python images are rebuilt on their own cadence, which lags
# bookworm-security. The pinned digest ships libpcre2-8-0 10.42-1 while
# bookworm-security has 10.42-1+deb12u1 (CVE-2026-86145, CVE-2026-89161) —
# and re-pinning does not help: the newest published digest carries the same
# 10.42-1 (checked, not assumed). Since the Trivy gate runs with
# `ignore-unfixed`, precisely this class of finding — a fix exists upstream but
# is not in the image — is what turns it red, on any commit, unrelated to the
# change being merged.
#
# This trades strict byte-for-byte reproducibility for staying patched: the
# digest still fixes the starting layer, but the upgrade resolves against
# whatever security archive is current at build time. That is the right way
# round for a runtime image — a reproducible build of known-vulnerable bytes
# is not the property worth keeping.
#
# The release build uses the GHA layer cache, and a RUN line whose text never
# changes is a cache hit forever: the upgrade above ran once, baked in whatever
# was current then, and every later build reused it — so a fix published after
# that (libpcre2-8-0 +deb12u2, CVE-2026-103111) never reached the image and the
# gate went red with the remedy already in the Dockerfile. Referencing a
# per-day build arg invalidates this layer (and only the cheap COPYs after it)
# once a day. Unset in local builds, where the plain cache semantics are fine.
ARG SECURITY_UPDATES_DATE=
RUN echo "security updates as of ${SECURITY_UPDATES_DATE:-local}" && \
    apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# The base image's pip is never run: the app lives in the uv-built venv, which
# has no pip. It only adds findings (CVE-2026-13346) and a way to install code
# at runtime, so it goes, along with the wheel ensurepip would reinstall.
RUN python -m pip uninstall --yes pip && \
    rm -rf /usr/local/lib/python3.14/ensurepip/_bundled

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --create-home appuser

WORKDIR /app

# Docker CLI for container monitoring (used by docker-agent tools).
# Digest-pinned like the FROM bases; docker:27-cli was built with Go 1.22,
# whose stdlib carries 15 HIGH/CRITICAL CVEs that tripped the Trivy gate.
# 29.9.2 / Go 1.26.10 scans clean; 29.9.0 / Go 1.26.9 hit CVE-2026-111943
# (crypto/x509, HIGH) and failed the release gate (the 29.6.2 / Go 1.26.5 pin
# before it needed eight triaged ignores).
COPY --from=docker:29-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb /usr/local/bin/docker /usr/local/bin/docker

# Copy the virtual environment and source from builder with correct ownership
# in a single layer (avoids a separate `chown -R` that would double disk use).
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/core/ /app/core/
COPY --from=builder --chown=appuser:appuser /app/agents/ /app/agents/

# Built web console bundle. The FastAPI front door serves it from here when
# ORRERY_WEB_CONSOLE_ENABLED=true (AEP-019). Copied after core/ so it lands in
# the packaged serving/static directory.
COPY --from=web-builder --chown=appuser:appuser /web/dist /app/core/orrery_core/serving/static

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# LiteLLM fetches its model price list and its Anthropic beta-header table
# from raw.githubusercontent.com at startup, unless told to use the copies it
# ships. Behind an egress allowlist that is a refused request and a warning on
# every start; anywhere else it is an unpinned download deciding behaviour.
# The bundled copies move with the pinned LiteLLM version. Set either to
# "false" to fetch the live tables instead.
ENV LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=true

# Default: the HTTP front door (JWT auth, RBAC from verified claims, rate
# limits, CSP, the web console, /healthz and /readyz on :8000). It reads
# AUTH_ENABLED / JWT_*: with neither set it refuses to start, so a bare
# `docker run` can't serve an unauthenticated API by accident.
#
# Not `adk web`: that is Google's developer UI. It ignores AUTH_ENABLED and
# JWT_*, and its session endpoints let anyone read any user's conversations.
# Use it locally (`make run-dev`), never as a deployment.
#
# Shutdown: uvicorn waits up to 15 s for requests whose clients are still
# connected, then the app waits up to ORRERY_SHUTDOWN_GRACE_SECONDS (30) for
# turns whose clients left. Both fit a 60 s grace period minus a 10 s preStop.
# Other services (Slack bot, Google Chat bot, Pub/Sub worker) override this.
EXPOSE 8000 8080
CMD ["uvicorn", "orrery_assistant.app:api", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
