# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.16 AS uv
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --python /usr/local/bin/python

FROM base AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    XDG_CACHE_HOME=/home/narrate/.cache \
    HF_HOME=/home/narrate/.cache/huggingface \
    MODELSCOPE_CACHE=/home/narrate/.cache/modelscope \
    VOXCPM_WEB_HOST=0.0.0.0 \
    VOXCPM_WEB_PORT=7860 \
    VOXCPM_WEB_OUT=/app/output/voxcpm2/web_jobs

# Keep the application read-only to the runtime user; only data/cache are writable.
RUN groupadd --gid 10001 narrate \
    && useradd --uid 10001 --gid narrate --create-home narrate \
    && mkdir -p /app/output /app/workspace /home/narrate/.cache \
    && chown -R narrate:narrate /app/output /app/workspace /home/narrate

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY examples/ ./examples/
COPY benchmarks/ ./benchmarks/
USER narrate

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('VOXCPM_WEB_PORT', '7860') + '/api/health', timeout=3); assert json.load(r)['ok']"]

# Explicit opt-in is required for the non-loopback listener (set by compose.yml).
CMD ["voxcpm-narrate-web"]
