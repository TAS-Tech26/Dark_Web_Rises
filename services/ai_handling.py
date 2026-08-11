from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import threading
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
import nltk
import open_clip
import torch
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

from paths import GENERATED_IMAGE_DIR, REFERENCE_IMAGE_DIR, resolve_static_path

load_dotenv()
logger = logging.getLogger("dwr.ai_handling")

# --- Tunables (overridable via environment for different deployment sizes) ---
# At ~1200 concurrent users (~300 four-person teams), rounds are synchronized,
# so all active teams can request an image from the free Pollinations API at
# roughly the same moment. A semaphore caps how many of those requests are
# in flight at once so we behave like a well-mannered client instead of
# hammering a third-party free-tier endpoint (and getting rate-limited or
# blocked mid-event).
MAX_CONCURRENT_IMAGE_REQUESTS = int(os.getenv("MAX_CONCURRENT_IMAGE_REQUESTS", "40"))
IMAGE_GEN_TIMEOUT_SECONDS = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "30.0"))
IMAGE_GEN_MAX_RETRIES = int(os.getenv("IMAGE_GEN_MAX_RETRIES", "3"))
IMAGE_GEN_BACKOFF_SECONDS = float(os.getenv("IMAGE_GEN_BACKOFF_SECONDS", "1.5"))
MAX_PROMPT_LENGTH = int(os.getenv("MAX_PROMPT_LENGTH", "300"))

# Hard cap on how many bytes we will pull down from the image API. Without
# this, `response.content` reads the whole body into memory unbounded: a
# hostile or malfunctioning upstream could return a multi-gigabyte body and
# OOM the single game process, taking down every team at once.
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))

# How generated images reach the players.
#   "url"    -- write the bytes to static/generated/ and send a short path.
#   "base64" -- inline the image in the websocket frame (legacy behaviour).
# See get_image() for why "url" is the default at the 1200-user target.
IMAGE_DELIVERY = os.getenv("IMAGE_DELIVERY", "url").strip().lower()

# CLIP scoring is CPU-bound. asyncio.to_thread() uses the interpreter's
# *default* executor, which is shared with every other blocking call in the
# process and is sized min(32, cpu_count + 4) -- far more threads than there
# are cores. Oversubscribing cores with torch inference makes every score
# slower, and one slow scoring burst then starves unrelated to_thread work.
# A dedicated, explicitly sized pool keeps scoring predictable and bounded.
CLIP_WORKER_THREADS = int(os.getenv("CLIP_WORKER_THREADS", "0")) or max(1, (os.cpu_count() or 2))
TORCH_THREADS_PER_WORKER = int(os.getenv("TORCH_THREADS_PER_WORKER", "1"))

torch.set_num_threads(TORCH_THREADS_PER_WORKER)

DEFAULT_IMAGE_FALLBACK = "/static/default.png"

_scoring_executor = ThreadPoolExecutor(
    max_workers=CLIP_WORKER_THREADS, thread_name_prefix="dwr-clip"
)

_pollinations_semaphore: asyncio.Semaphore | None = None
_http_client: httpx.AsyncClient | None = None
_client_lock = threading.Lock()


def _get_http_client() -> httpx.AsyncClient:
    """Lazily create the pooled HTTP client *inside* the running event loop.

    It used to be built at module import time. httpx binds some of its
    internals to the loop that first uses them, so an import-time client is
    fragile: it breaks under `uvicorn --reload`, under any test that spins up
    a second event loop, and after `close_http_client()` runs at shutdown
    (the closed client was never rebuilt, so a second app start in the same
    process failed with "Cannot send a request, as the client has been
    closed"). Creating it on first use inside the loop avoids all three.

    A single pooled client -- rather than `async with httpx.AsyncClient()`
    per call -- is still the point: a fresh TCP+TLS handshake per image
    request does not scale to hundreds of teams generating concurrently.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        with _client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(IMAGE_GEN_TIMEOUT_SECONDS, connect=10.0),
                    limits=httpx.Limits(
                        max_connections=MAX_CONCURRENT_IMAGE_REQUESTS * 2,
                        max_keepalive_connections=MAX_CONCURRENT_IMAGE_REQUESTS,
                    ),
                    follow_redirects=True,
                )
    return _http_client


def _get_semaphore() -> asyncio.Semaphore:
    """Same reasoning as _get_http_client: an asyncio.Semaphore created at
    import time attaches to whatever loop happens to touch it first."""
    global _pollinations_semaphore
    if _pollinations_semaphore is None:
        _pollinations_semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_REQUESTS)
    return _pollinations_semaphore


async def close_http_client():
    """Call during app shutdown to release pooled connections cleanly."""
    global _http_client
    client = _http_client
    _http_client = None
    if client is not None and not client.is_closed:
        await client.aclose()


def shutdown_scoring_pool(wait: bool = False):
    """Release the CLIP worker threads at shutdown."""
    _scoring_executor.shutdown(wait=wait)


def _load_nltk_words():
    try:
        nltk.data.find("corpora/words")
    except LookupError:
        # Bounded via a daemon worker thread + timeout: on a host with no
        # outbound internet access (or a slow/unreachable proxy), a bare
        # nltk.download() call can hang for a long time with no built-in
        # timeout, which would previously stall the entire app at import
        # time. Give it a fixed budget and move on if it doesn't finish; the
        # thread is daemonic so an eventual hang can't block process exit.
        done = {"ok": False}

        def _download():
            try:
                done["ok"] = nltk.download("words", quiet=True)
            except Exception:  # pragma: no cover - network dependent
                done["ok"] = False

        worker = threading.Thread(target=_download, daemon=True)
        worker.start()
        worker.join(timeout=15)
        if worker.is_alive() or not done["ok"]:
            raise TimeoutError("nltk.download('words') did not complete within 15s")
    from nltk.corpus import words
    return set(w.lower() for w in words.words())


try:
    english_words = _load_nltk_words()
except Exception as exc:  # pragma: no cover - depends on network/environment
    # Krutrim Cloud / local-server / air-gapped deployments may not have
    # outbound internet access at startup. Prompt validation degrades
    # gracefully instead: the dictionary check below is skipped, and the
    # other classify_prompt heuristics still apply.
    logger.warning(
        "Could not load NLTK 'words' corpus (%s). Falling back to heuristic-only prompt validation.",
        exc,
    )
    english_words = set()

COMMON_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "shall",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "but", "or", "so", "because", "if", "then",
    "my", "your", "his", "her", "its", "our", "their",
    "what", "who", "where", "when", "how", "why"
}

_model_lock = threading.Lock()
_model_state = {"comparator": None, "preprocess": None}


def _get_model():
    """Lazily load the CLIP (RN50) model on first use instead of at import
    time. Two benefits: (1) the app -- and its /health endpoint -- can start
    and answer requests immediately instead of blocking on a ~100MB weight
    load/download before Uvicorn even binds the port (matters for Azure App
    Service health probes and cold starts); (2) modules that import this file
    transitively can be unit tested without the real model available."""
    if _model_state["comparator"] is None:
        with _model_lock:
            if _model_state["comparator"] is None:
                try:
                    comparator, _, preprocess = open_clip.create_model_and_transforms('RN50', pretrained='openai')
                    comparator.eval()
                except Exception as exc:
                    # Fail with a message that actually tells whoever is
                    # deploying this what to do, instead of a bare
                    # open_clip/torch stack trace.
                    raise RuntimeError(
                        "Failed to load the CLIP (RN50) image-similarity model. If "
                        "this server has no outbound internet access, pre-download "
                        "the 'openai' RN50 open_clip weights into the local model "
                        f"cache before starting the app. Original error: {exc}"
                    ) from exc
                _model_state["comparator"] = comparator
                _model_state["preprocess"] = preprocess
    return _model_state["comparator"], _model_state["preprocess"]


def warm_up_model():
    """Force the CLIP weights to load. Call once at startup (in a worker
    thread) so the *first team to finish round 1* isn't the one that pays the
    one-off ~10-30s model load -- previously that cost landed inside a live
    round and delayed that team's score while holding a scoring thread."""
    _get_model()


def load_image(img_input):
    """Open an image from a data URI, a server-relative /static path, or a
    file-like object / path."""
    if isinstance(img_input, str) and img_input.startswith("data:image"):
        if "base64," not in img_input:
            raise UnidentifiedImageError("Data URI is not base64-encoded.")
        payload = img_input.split("base64,", 1)[1]
        return Image.open(BytesIO(base64.b64decode(payload)))
    if isinstance(img_input, str) and img_input.startswith("/static/"):
        # resolve_static_path() rejects traversal outside static/ and returns
        # an absolute path, so this no longer depends on the process CWD.
        return Image.open(resolve_static_path(img_input))
    return Image.open(img_input)


def _should_retry(status_code: int) -> bool:
    """Only retry things that can plausibly succeed on a second attempt.

    Previously *every* non-200 was retried three times with backoff, so a
    permanent 400/404 (a prompt the upstream refuses) burned ~4.5s of a
    90-second turn doing nothing useful, three times over.
    """
    return status_code == 429 or 500 <= status_code < 600


async def _fetch_pollinations_image(prompt: str) -> bytes | None:
    """Fetch a generated image from Pollinations with bounded retries.

    Returns the raw image bytes, or None if every attempt failed (the caller
    decides how to degrade).
    """
    # safe="" is load-bearing. urllib.parse.quote() leaves "/" unescaped by
    # default, and the prompt is interpolated into the URL *path* -- so a
    # prompt containing a slash silently rewrote the request path rather than
    # being sent as prompt text.
    encoded_prompt = urllib.parse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/p/{encoded_prompt}?model=flux&width=1024&height=1024"

    semaphore = _get_semaphore()
    client = _get_http_client()

    for attempt in range(1, IMAGE_GEN_MAX_RETRIES + 1):
        try:
            async with semaphore:
                async with client.stream("GET", url) as response:
                    if response.status_code == 200:
                        chunks = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_IMAGE_BYTES:
                                logger.error(
                                    "Image response exceeded %s bytes; aborting download.",
                                    MAX_IMAGE_BYTES,
                                )
                                chunks = []
                                break
                            chunks.append(chunk)
                        if chunks:
                            return b"".join(chunks)
                    else:
                        logger.warning(
                            "Pollinations returned status %s on attempt %s/%s",
                            response.status_code, attempt, IMAGE_GEN_MAX_RETRIES,
                        )
                        if not _should_retry(response.status_code):
                            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "Pollinations request failed on attempt %s/%s: %s",
                attempt, IMAGE_GEN_MAX_RETRIES, exc,
            )

        if attempt < IMAGE_GEN_MAX_RETRIES:
            await asyncio.sleep(IMAGE_GEN_BACKOFF_SECONDS * attempt)

    logger.error(
        "Pollinations image generation failed after %s attempts for prompt %r",
        IMAGE_GEN_MAX_RETRIES, prompt[:80],
    )
    return None


def _write_generated_image(image_bytes: bytes) -> str | None:
    """Persist generated bytes under static/generated/ and return the URL path.

    Returns None if the directory isn't writable, so the caller can fall back
    to inlining base64.
    """
    try:
        os.makedirs(GENERATED_IMAGE_DIR, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.jpg"
        target = os.path.join(GENERATED_IMAGE_DIR, filename)
        tmp = f"{target}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(image_bytes)
        os.replace(tmp, target)
        return f"/static/generated/{filename}"
    except OSError as exc:
        logger.warning(
            "Could not write generated image to %s (%s); falling back to inline base64.",
            GENERATED_IMAGE_DIR, exc,
        )
        return None


def pick_reference_image() -> str | None:
    """Return a random reference image URL path, or None if none are present."""
    try:
        all_files = os.listdir(REFERENCE_IMAGE_DIR)
    except FileNotFoundError:
        logger.error("Reference image folder %r does not exist.", REFERENCE_IMAGE_DIR)
        return None

    valid_extensions = (".png", ".jpeg", ".jpg", ".gif", ".webp")
    image_files = [f for f in all_files if f.lower().endswith(valid_extensions)]
    if not image_files:
        logger.error("No reference images found in %r.", REFERENCE_IMAGE_DIR)
        return None

    return f"/static/images/{random.choice(image_files)}"


async def get_image(prompt):
    """Return an image reference for `prompt`, or None if generation failed.

    Returning None (rather than a placeholder path) is deliberate: the caller
    in models/team.py keeps the *previous* image in the chain when generation
    fails, instead of replacing a real game image with a placeholder that the
    next player would then be asked to describe.
    """
    if prompt == "default_prompt":
        return pick_reference_image()

    # Defense in depth: classify_prompt() already gates what reaches this
    # point, but we still clamp length here so a pathological (if
    # technically "valid") prompt can't produce an oversized outbound URL.
    safe_prompt = (prompt or "")[:MAX_PROMPT_LENGTH]

    image_bytes = await _fetch_pollinations_image(safe_prompt)
    if image_bytes is None:
        return None

    # Inlining a 1024x1024 image as base64 means every turn ships roughly
    # 200-400 KB *per player*. At ~300 teams x 4 players x 20 turns that is
    # multiple gigabytes of websocket egress per event, all of it serialised
    # through the single game process's event loop, plus a multi-hundred-KB
    # Python string held per team for the whole round. Writing the file once
    # and sending a short path lets the static handler (or a reverse proxy /
    # CDN in front of it) serve the bytes instead.
    if IMAGE_DELIVERY == "url":
        url_path = await asyncio.to_thread(_write_generated_image, image_bytes)
        if url_path is not None:
            return url_path

    image_base64 = base64.b64encode(image_bytes).decode()
    return f"data:image/jpeg;base64,{image_base64}"


def _compare_image_sync(penalty, original, new):
    """CPU-bound CLIP similarity scoring. Runs off the event loop on a
    dedicated bounded thread pool (see compare_image)."""
    image_comparator, preprocess = _get_model()
    image1 = preprocess(load_image(original)).unsqueeze(0)
    image2 = preprocess(load_image(new)).unsqueeze(0)

    with torch.no_grad(), torch.amp.autocast('cpu'):
        feat1 = image_comparator.encode_image(image1)
        feat2 = image_comparator.encode_image(image2)
        feat1 /= feat1.norm(dim=-1, keepdim=True)
        feat2 /= feat2.norm(dim=-1, keepdim=True)
        similarity = (feat1 @ feat2.T).item()

    sim_clipped = max(0.0, min(similarity, 1.0))
    return clamp_score((sim_clipped * 100) - penalty)


def clamp_score(raw_score) -> float:
    """Round scores are documented as "out of 100" -- so keep them in [0, 100].

    Penalties are unbounded (`penalty * penalty_multiplier` accumulates per
    missed turn, on top of the per-missing-member structural handicap), so a
    team that no-showed a round could previously post a strongly negative
    round score. That is not just cosmetic: negative rounds drag a team below
    the -1 sentinel used for teams that never connected at all, so a team
    that turned up and struggled could rank *below* a team that never showed.
    """
    return round(max(0.0, min(float(raw_score), 100.0)), 2)


async def compare_image(penalty, original, new):
    """Score `new` against `original`, minus `penalty`, clamped to [0, 100]."""
    # torch/PIL/open_clip inference is synchronous and CPU-bound. Awaiting it
    # directly would block the single asyncio event loop -- and therefore
    # every websocket connection -- for the duration of each scoring call.
    if original is None or new is None:
        logger.warning("Cannot score a round with a missing image; awarding the penalty floor.")
        return clamp_score(0.0)

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _scoring_executor, _compare_image_sync, penalty, original, new
        )
    except (UnidentifiedImageError, FileNotFoundError, ValueError, OSError) as exc:
        logger.error(
            "Image comparison failed (original=%r, new=%r): %s",
            str(original)[:60], str(new)[:60], exc,
        )
        # A missing/corrupt image shouldn't crash the round for a team.
        return clamp_score(0.0)
    except Exception:
        # Model load failures, torch errors, ... Never let a scoring problem
        # propagate into start_games() and abort the event for every team.
        logger.exception("Unexpected failure scoring a round; awarding 0.")
        return clamp_score(0.0)


def classify_prompt(prompt):
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        return False

    if len(prompt) > MAX_PROMPT_LENGTH:
        return False

    stripped = prompt.strip()
    words_list = stripped.lower().split()

    if len(words_list) < 4:
        return False

    letters = sum(c.isalpha() for c in stripped)
    if letters / len(stripped) < 0.6:
        return False

    if re.search(r'(.)\1{4,}', stripped):
        return False

    if not any(word in COMMON_WORDS for word in words_list):
        return False

    # stricter check for longer prompts (skipped gracefully if the
    # dictionary corpus wasn't available at startup)
    if len(words_list) >= 8 and english_words:
        real_words = sum(1 for w in words_list if w in english_words)
        if real_words / len(words_list) < 0.5:
            return False

    return True
