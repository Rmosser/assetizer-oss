# Assetizer runtime for the LazyCat package: Python 3.13 (Debian 13, like the OVH host), ffmpeg, deno and the
# Python dependencies pinned to what production runs (requirements.lock, frozen from the OVH venvs).
# App code is mounted from the package content (/lzcapp/pkg/content); agy, its login and the whisper model live
# in /lzcapp/var because they change at runtime (agy updates itself).
FROM python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

ARG DENO_VERSION=2.9.6
ARG DENO_SHA256=394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2
# 不用 RCLONE_ 开头：rclone 把 RCLONE_* 环境变量当参数读（RCLONE_VERSION 会被当成 --version 的值）。
ARG PINNED_RCLONE_VERSION=1.75.2
ARG PINNED_RCLONE_SHA256=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702
ARG CLOUDFLARED_VERSION=2026.10.4
ARG CLOUDFLARED_SHA256=ae5e8add7884b0531d66bbca4570d1de491ff6da51e4ebe7eb160cdf241d9e42

# Runtime startup must not reach package repositories: everything it needs is installed here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates curl tzdata procps unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -filters | grep -q " subtitles "

# Optional Notion webhook connector: fixed release, verified while building, never downloaded at startup.
RUN curl -fsSL --retry 3 -o /tmp/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
    && echo "${CLOUDFLARED_SHA256}  /tmp/cloudflared" | sha256sum -c - \
    && install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared \
    && rm /tmp/cloudflared

# yt-dlp solves YouTube's JS challenges with deno.
RUN curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

# 专题原片与底稿只增备份到用户自己的 Google Drive，并在 YouTube 不可用时取回。
RUN curl -fsSL -o /tmp/rclone.zip \
        "https://downloads.rclone.org/v${PINNED_RCLONE_VERSION}/rclone-v${PINNED_RCLONE_VERSION}-linux-amd64.zip" \
    && echo "${PINNED_RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && install -m 0755 "/tmp/rclone/rclone-v${PINNED_RCLONE_VERSION}-linux-amd64/rclone" /usr/local/bin/rclone \
    && rm -rf /tmp/rclone.zip /tmp/rclone \
    && rclone version

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && python -c "import faster_whisper, ctranslate2, av, numpy, fastapi, uvicorn, jsonschema, jieba, httpx, jwt, PIL, opencc" \
    && yt-dlp --version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
