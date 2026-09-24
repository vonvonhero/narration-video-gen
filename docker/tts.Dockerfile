# syntax=docker/dockerfile:1.7
FROM python:3.10-slim@sha256:8c97ebedc32fd60935cdf5992e935753e2a0f98231830028050e1e04bd3c13c2

ARG IRODORI_TTS_BACKEND=cpu
ARG IRODORI_SERVER_COMMIT=841fb7c6ec57729c56b9b75c0ef2562249b13a10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Pin the installer as well as the upstream server source. The upstream lock
# pins Irodori-TTS itself and every Python wheel used by the selected backend.
COPY --from=ghcr.io/astral-sh/uv:0.8.15@sha256:1ececcacbbde240ffca54d400df86e4fdd38f29c1a2366299279d197e92eaed3 /uv /uvx /usr/local/bin/

RUN git init . \
    && git remote add origin https://github.com/Aratako/Irodori-TTS-Server.git \
    && git fetch --depth 1 origin "${IRODORI_SERVER_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${IRODORI_SERVER_COMMIT}" \
    && rm -rf .git

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-dev --no-editable --extra "${IRODORI_TTS_BACKEND}"

# ASR is a review aid. Keep it in the same isolated environment so neither the
# host nor the dependency-free CLI imports torch. The model itself is fetched
# by the explicit prepare step, never while building this image.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install openai-whisper==20250625

EXPOSE 8088
CMD ["/app/.venv/bin/python", "-m", "irodori_openai_tts", "--host", "0.0.0.0", "--port", "8088"]
