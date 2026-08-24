# Dark Web Rises -- Phase 1 backend (FastAPI + websockets + CLIP scoring)
#
# Notes for anyone editing this file:
#   * torch/torchvision are pinned to CPU builds via the extra index in
#     requirements.txt. Do NOT let pip resolve the default CUDA wheels --
#     they add ~2GB and this app never touches a GPU.
#   * numpy is pinned <2.0 because torch 2.3.1's compiled extensions are not
#     ABI-compatible with numpy 2.x (see requirements.txt).
#   * The CLIP RN50 weights and the nltk `words` corpus are baked in at build
#     time. If you don't do this, the first round of the event pays a cold
#     download, and `_load_nltk_words()` has a hard 15s timeout that will
#     fail on a slow venue network.

FROM python:3.11-slim

# HOME must be set before the model pre-fetch: open_clip caches the openai
# RN50 weights under $HOME/.cache/clip, and we want that inside the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    NLTK_DATA=/usr/share/nltk_data \
    APP_PORT=8000

# Unprivileged runtime user. Created before the pip install so the cache
# directories below land with the right ownership.
RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first, in their own layer -- application code changes far more
# often than requirements.txt, so this keeps rebuilds fast during integration.
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Pre-fetch the CLIP RN50 weights and the nltk corpus. Both are otherwise
# fetched lazily on first use, at the worst possible moment (mid-event).
# CLIP_MODEL must match the default in services/ai_handling.py -- it is
# RN50-quickgelu, not RN50; the plain RN50 entry uses the wrong activation
# and silently produces bad similarity scores.
RUN python -c "import nltk; nltk.download('words', download_dir='/usr/share/nltk_data')" && \
    mkdir -p /home/app/.cache && \
    python -c "import open_clip; open_clip.create_model_and_transforms('RN50-quickgelu', pretrained='openai')" && \
    chown -R app:app /home/app

# Application code.
COPY --chown=app:app . .

# Runtime-writable directory for generated round images (IMAGE_DELIVERY=url).
# Also declared as a volume in docker-compose.yml so images survive a restart.
RUN mkdir -p /app/static/generated && chown -R app:app /app/static

USER app

EXPOSE 8000

# Liveness: the process is up and the event loop is responsive. Deliberately
# does not touch the scoring path -- CLIP encodes are CPU-bound and a
# healthcheck that competes with round-end scoring is a self-inflicted wound.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status < 500 else 1)"

# EXACTLY ONE WORKER. Game state (GameServer, Team, connected sockets, scores,
# the admin session-token table) lives in this process's memory. A second
# worker splits players across processes with inconsistent state. Scale this
# service vertically (more CPU/RAM), never with --workers or `deploy.replicas`.
#
# --proxy-headers / --forwarded-allow-ips: uvicorn trusts X-Forwarded-* only
# from 127.0.0.1 by default. Caddy runs as a SEPARATE container on the docker
# bridge, so its peer address is a 172.x address and the headers were being
# discarded -- which makes Starlette build absolute redirect Locations with
# scheme http from an https page (blocked as mixed content) and logs every
# client as Caddy's container IP.
#
# "*" is safe ONLY because port 8000 stops being published once the reverse
# proxy is in front (DEPLOY_KRUTRIM.md 7c). Do both in the same change: while
# 8000 is reachable directly, any client can forge these headers.
CMD ["uvicorn", "game:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
