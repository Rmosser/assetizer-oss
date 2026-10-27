# Assetizer runtime for the LazyCat package: Python 3.13 (Debian 13, like the OVH host), ffmpeg, deno and the
# Python dependencies pinned to what production runs (requirements.lock, frozen from the OVH venvs).
# App code is mounted from the package content (/lzcapp/pkg/content); agy, its login and the whisper model live
# in /lzcapp/var because they change at runtime (agy updates itself).
FROM python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

ARG DENO_VERSION=2.9.6
ARG DENO_SHA256=394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2

# Runtime startup must not reach package repositories: everything it needs is installed here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates curl tzdata procps unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -filters | grep -q " subtitles "

# yt-dlp solves YouTube's JS challenges with deno.
RUN curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && python -c "import faster_whisper, ctranslate2, av, numpy, fastapi, uvicorn, jsonschema, jieba, httpx, jwt, PIL, opencc" \
    && yt-dlp --version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
