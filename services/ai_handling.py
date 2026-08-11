from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
import nltk
import open_clip
import torch
from PIL import Image, UnidentifiedImageError

# Importing paths also loads .env -- deliberately, so that every module which
# reads configuration sees it, including paths.py's own DWR_STATE_DIR.
from paths import GENERATED_IMAGE_DIR, REFERENCE_IMAGE_DIR, resolve_static_path
from services.providers import build_chain

logger = logging.getLogger("dwr.ai_handling")

_provider_chain = None


def _get_chain():
    """Built lazily so the environment (including .env) is fully loaded and
    tests can rebuild the chain after changing configuration."""
    global _provider_chain
    if _provider_chain is None:
        _provider_chain = build_chain()
    return _provider_chain


def reset_chain():
    """Force the chain to be rebuilt on next use. Used by tests."""
    global _provider_chain
    _provider_chain = None

# --- Tunables (overridable via environment for different deployment sizes) ---
# Rounds are synchronised, so every active team requests an image at roughly
# the same moment. A semaphore caps how many are in flight at once, keeping
# us inside the provider's concurrency allowance instead of triggering the
# rate limiting we are trying to avoid. Set this to the limit the primary
# provider actually grants (DeepInfra's documented default is 200 per model).
MAX_CONCURRENT_IMAGE_REQUESTS = int(os.getenv("MAX_CONCURRENT_IMAGE_REQUESTS", "40"))
IMAGE_GEN_TIMEOUT_SECONDS = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "20.0"))
# Retries against a single endpoint were removed: failover to the next
# provider is faster and more likely to succeed than retrying a provider
# that is already struggling. See services/providers.py.
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

# Scoring model. Overridable so a rehearsal can compare candidates
# (tools/clip_bench.py) without a code change -- but note that scores from
# different models are NOT comparable, so changing this after the reference
# scores are known invalidates any expectation of what a "good" score is.
CLIP_MODEL = os.getenv("CLIP_MODEL", "RN50-quickgelu").strip()
CLIP_PRETRAINED = os.getenv("CLIP_PRETRAINED", "openai").strip()

torch.set_num_threads(TORCH_THREADS_PER_WORKER)

DEFAULT_IMAGE_FALLBACK = "/static/default.png"

_scoring_executor = ThreadPoolExecutor(
    max_workers=CLIP_WORKER_THREADS, thread_name_prefix="dwr-clip"
)

_request_semaphore: asyncio.Semaphore | None = None
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
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_REQUESTS)
    return _request_semaphore


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
                    # RN50-quickgelu, not RN50. OpenAI's CLIP weights were
                    # trained with QuickGELU activations; loading them into a
                    # model built with standard GELU silently computes every
                    # embedding with the wrong activation function. open_clip
                    # warns about this ("QuickGELU mismatch between final
                    # model config and pretrained tag 'openai'") rather than
                    # failing, so it produced plausible-looking but subtly
                    # wrong scores. Measured cost of the correct variant:
                    # 181.5 ms vs 177.0 ms per encode -- i.e. free.
                    comparator, _, preprocess = open_clip.create_model_and_transforms(
                        CLIP_MODEL, pretrained=CLIP_PRETRAINED
                    )
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


async def _generate_image_bytes(prompt: str) -> bytes | None:
    """Generate an image via the provider chain.

    Failover is per-request and deliberate: the request that hits a slow or
    failing provider fails over *itself* rather than being retried, because
    the player waiting on it has a hard turn deadline. Recovery probing is
    handled by each provider's circuit breaker (services/providers.py), so
    no waiting player ever pays for it.

    The old single-provider retry loop is gone. Three retries with backoff
    against one endpoint burned 60s+ of a 90-second turn and could still
    return nothing; one fast attempt per provider is strictly better for the
    player and for the round clock.
    """
    chain = _get_chain()
    if not chain.providers:
        logger.error("No image providers configured; cannot generate.")
        return None

    semaphore = _get_semaphore()
    client = _get_http_client()

    async with semaphore:
        return await chain.generate(client, prompt)


def provider_status() -> list:
    """Breaker state per provider, surfaced on the admin dashboard so an
    operator can see a failover happening rather than inferring it."""
    return _get_chain().snapshot()


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

    image_bytes = await _generate_image_bytes(safe_prompt)
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


# Cached embeddings for the *reference* images only. Bounded because the
# cache key is a caller-supplied path; an unbounded dict keyed on user-
# reachable input is a slow memory leak in a process that cannot be
# horizontally scaled.
_EMBED_CACHE_MAX = int(os.getenv("CLIP_EMBED_CACHE_SIZE", "64"))
_embed_cache: "OrderedDict[str, object]" = OrderedDict()
_embed_cache_lock = threading.Lock()


def _cacheable(reference) -> bool:
    """Only the fixed reference images under /static/images/ are cached.

    Generated images are unique per turn, so caching them would fill the
    cache with entries that are never read again. Data URIs are worse: the
    key would be the entire base64 payload.
    """
    return isinstance(reference, str) and reference.startswith("/static/images/")


def _encode(reference, comparator, preprocess):
    """Embed an image, reusing a cached embedding for reference images.

    The result is bit-identical to encoding every time -- reference images
    are immutable files and the model is deterministic under no_grad -- so
    this changes latency and nothing else. Scores are unaffected.
    """
    if _cacheable(reference):
        with _embed_cache_lock:
            cached = _embed_cache.get(reference)
            if cached is not None:
                _embed_cache.move_to_end(reference)
                return cached

    tensor = preprocess(load_image(reference)).unsqueeze(0)
    # NO autocast here, deliberately. `torch.amp.autocast('cpu')` casts to
    # bfloat16, and on a CPU without AMX or AVX512-BF16 those kernels are
    # emulated. Measured on the dev machine with tools/clip_bench.py
    # --diagnose:
    #
    #     encode WITH autocast('cpu')      9867.5 ms
    #     encode WITHOUT autocast (fp32)     43.3 ms     <- 228x faster
    #
    # It reads like an optimisation, which is exactly why it survived two
    # review passes. Do not add it back without re-running that diagnostic
    # on the actual deployment hardware.
    with torch.no_grad():
        features = comparator.encode_image(tensor)
        features = features / features.norm(dim=-1, keepdim=True)

    if _cacheable(reference):
        with _embed_cache_lock:
            _embed_cache[reference] = features
            while len(_embed_cache) > _EMBED_CACHE_MAX:
                _embed_cache.popitem(last=False)
    return features


def reset_embed_cache():
    """Drop cached reference embeddings. Used by tests, and safe to call if
    the reference images are ever replaced while the process is running."""
    with _embed_cache_lock:
        _embed_cache.clear()


def _compare_image_sync(penalty, original, new):
    """CPU-bound CLIP similarity scoring. Runs off the event loop on a
    dedicated bounded thread pool (see compare_image).

    Two encodes per call, one of which is the round's reference image and is
    therefore the same for every team that drew it. That one is cached: at
    150 teams sharing a handful of reference images it removes almost half
    the scoring work, for identical scores.
    """
    image_comparator, preprocess = _get_model()
    feat1 = _encode(original, image_comparator, preprocess)
    feat2 = _encode(new, image_comparator, preprocess)

    with torch.no_grad():
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
