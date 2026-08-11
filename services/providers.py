"""Image-generation providers with circuit-breaker failover.

Generation sits inline in every team's turn (models/team.py awaits
`get_image` before the turn can end), so 150 independent coroutines each
block on their own request. There is no central queue of prompts, which
means failover has to be a *per-request* decision made against shared
health state -- not a reassignment of queued work.

The rule that follows from that: when a provider is slow or failing, the
request that hit it fails over **itself**. The player waiting on that image
has a hard turn deadline; making them wait through retries while some later
player gets the healthy provider penalises exactly the wrong person.

    turn window          90s
    primary attempt      20s   <- fail fast, no retry
    secondary attempt     8s
    worst case           28s   <- comfortably inside the turn

Recovery is handled separately by the breaker, so no waiting player ever
pays for probing.

Circuit breaker states
----------------------
    CLOSED     all traffic to this provider; count consecutive failures
    OPEN       provider skipped entirely; cooldown running
    HALF_OPEN  cooldown expired; let one request through as a probe.
               Success -> CLOSED. Failure -> OPEN, cooldown restarts.

Any success resets the failure count, so an isolated slow response does not
trip the breaker -- but a real outage trips it almost immediately, because
all in-flight requests fail together.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("dwr.providers")

# --- Breaker tuning ---------------------------------------------------
BREAKER_FAILURE_THRESHOLD = int(os.getenv("BREAKER_FAILURE_THRESHOLD", "5"))
BREAKER_COOLDOWN_SECONDS = float(os.getenv("BREAKER_COOLDOWN_SECONDS", "30"))

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class ProviderError(Exception):
    """Raised by an adapter when a request fails in a way that should count
    against the breaker (timeout, 429, 5xx, malformed response)."""


class CircuitBreaker:
    def __init__(self, name, threshold=None, cooldown=None):
        self.name = name
        self.threshold = threshold if threshold is not None else BREAKER_FAILURE_THRESHOLD
        self.cooldown = cooldown if cooldown is not None else BREAKER_COOLDOWN_SECONDS
        self.state = CLOSED
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.total_failures = 0
        self.total_successes = 0
        self._probe_in_flight = False

    def allow_request(self) -> bool:
        """Should a request be sent to this provider right now?"""
        if self.state == CLOSED:
            return True

        if self.state == OPEN:
            if time.time() - self.opened_at >= self.cooldown:
                self.state = HALF_OPEN
                self._probe_in_flight = False
                logger.info("Breaker %s -> HALF_OPEN (cooldown elapsed); probing.", self.name)
            else:
                return False

        if self.state == HALF_OPEN:
            # Exactly one probe at a time. With ~150 concurrent requests,
            # letting them all through on recovery would hammer a provider
            # that has only just come back.
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

        return True

    def record_success(self):
        self.total_successes += 1
        self.consecutive_failures = 0
        self._probe_in_flight = False
        if self.state != CLOSED:
            logger.warning("Breaker %s -> CLOSED (provider recovered).", self.name)
        self.state = CLOSED

    def record_failure(self):
        self.total_failures += 1
        self.consecutive_failures += 1
        self._probe_in_flight = False

        if self.state == HALF_OPEN:
            self.state = OPEN
            self.opened_at = time.time()
            logger.warning("Breaker %s -> OPEN (probe failed); cooling down %ss.",
                           self.name, self.cooldown)
            return

        if self.state == CLOSED and self.consecutive_failures >= self.threshold:
            self.state = OPEN
            self.opened_at = time.time()
            logger.error("Breaker %s -> OPEN after %s consecutive failures; "
                         "cooling down %ss.", self.name, self.consecutive_failures, self.cooldown)

    def snapshot(self) -> dict:
        return {
            "provider": self.name,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "seconds_until_retry": (
                max(0.0, round(self.cooldown - (time.time() - self.opened_at), 1))
                if self.state == OPEN else 0.0
            ),
        }


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------
@dataclass
class ImageProvider:
    """Base adapter. `generate` returns raw image bytes or raises ProviderError."""
    name: str
    timeout: float
    max_bytes: int
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(self.name)

    def is_configured(self) -> bool:
        raise NotImplementedError

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes:
        raise NotImplementedError

    def _check_size(self, payload: bytes) -> bytes:
        if len(payload) > self.max_bytes:
            raise ProviderError(
                f"{self.name} returned {len(payload)} bytes, over the {self.max_bytes} cap"
            )
        return payload

    @staticmethod
    def _decode_data_uri(value: str) -> bytes:
        if "base64," in value:
            value = value.split("base64,", 1)[1]
        return base64.b64decode(value)


class DeepInfraProvider(ImageProvider):
    """DeepInfra inference API.

    Response shape is handled defensively: DeepInfra returns image models
    under a few different keys depending on the model family, and this must
    be confirmed against a real response before the event (see the tests for
    the shapes currently accepted).
    """

    def __init__(self, timeout, max_bytes):
        super().__init__(name="deepinfra", timeout=timeout, max_bytes=max_bytes)
        self.api_key = os.getenv("DEEPINFRA_API_KEY", "").strip()
        self.model = os.getenv("DEEPINFRA_MODEL", "black-forest-labs/FLUX-2-klein-4b").strip()
        self.url = f"https://api.deepinfra.com/v1/inference/{self.model}"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes:
        try:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"prompt": prompt},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"deepinfra transport error: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(f"deepinfra returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"deepinfra returned non-JSON: {exc}") from exc

        candidate = None
        if isinstance(payload, dict):
            images = payload.get("images") or payload.get("output") or payload.get("data")
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, str):
                    candidate = first
                elif isinstance(first, dict):
                    candidate = first.get("b64_json") or first.get("url") or first.get("image")
            elif isinstance(payload.get("image"), str):
                candidate = payload["image"]

        if not candidate:
            raise ProviderError(f"deepinfra response had no recognisable image field: {list(payload)[:6]}")

        if candidate.startswith("http://") or candidate.startswith("https://"):
            return await _download(client, candidate, self.timeout, self.max_bytes, self.name)
        return self._check_size(self._decode_data_uri(candidate))


class ReplicateProvider(ImageProvider):
    """Replicate predictions API.

    Uses the `Prefer: wait` header so the prediction resolves in a single
    request rather than requiring a polling loop -- important here, because
    a polling loop inside a player's turn would burn the turn budget.
    Falls back to short polling if the API still returns before completion.
    """

    def __init__(self, timeout, max_bytes):
        super().__init__(name="replicate", timeout=timeout, max_bytes=max_bytes)
        self.api_key = os.getenv("REPLICATE_API_TOKEN", "").strip()
        self.model = os.getenv("REPLICATE_MODEL", "black-forest-labs/flux-schnell").strip()
        self.url = f"https://api.replicate.com/v1/models/{self.model}/predictions"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Prefer": "wait",
        }
        try:
            response = await client.post(
                self.url, headers=headers,
                json={"input": {"prompt": prompt}},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"replicate transport error: {exc}") from exc

        if response.status_code not in (200, 201):
            raise ProviderError(f"replicate returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"replicate returned non-JSON: {exc}") from exc

        payload = await self._await_completion(client, payload, headers)

        output = payload.get("output")
        url = None
        if isinstance(output, list) and output:
            url = output[0]
        elif isinstance(output, str):
            url = output

        if not isinstance(url, str) or not url.startswith("http"):
            raise ProviderError(f"replicate output was not an image URL: {output!r}")

        return await _download(client, url, self.timeout, self.max_bytes, self.name)

    async def _await_completion(self, client, payload, headers):
        """`Prefer: wait` usually returns a finished prediction. If it does
        not, poll briefly rather than giving up -- but keep the whole thing
        inside this provider's timeout budget."""
        deadline = time.time() + self.timeout
        while payload.get("status") in ("starting", "processing"):
            if time.time() >= deadline:
                raise ProviderError("replicate prediction did not finish within the timeout")
            get_url = (payload.get("urls") or {}).get("get")
            if not get_url:
                raise ProviderError("replicate prediction is incomplete and has no polling URL")
            await asyncio.sleep(0.5)
            try:
                poll = await client.get(get_url, headers=headers, timeout=self.timeout)
                payload = poll.json()
            except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                raise ProviderError(f"replicate polling failed: {exc}") from exc

        if payload.get("status") != "succeeded":
            raise ProviderError(f"replicate prediction status {payload.get('status')!r}")
        return payload


class HuggingFaceProvider(ImageProvider):
    """Hugging Face Inference Providers -- DEMO/TESTING ONLY.

    Added so the game can be demonstrated without funded DeepInfra or
    Replicate accounts. It is *not* an event-day provider:

      - HF serverless inference is rate limited per account, and the limit is
        not published as a concurrency figure. The event needs 150 images
        generated in the same few seconds; this will not do that.
      - Cold models return HTTP 503 "currently loading" for the first request
        after an idle period. Inside a 90s turn that is a lost turn.

    Selecting it is a deliberate act (IMAGE_PROVIDERS=huggingface), and
    build_chain() logs a warning whenever it is in the chain, so a demo
    configuration cannot quietly survive into the event.

    Two shape differences from the other adapters, both from the published
    API spec (huggingface.co/docs/inference-providers/tasks/text-to-image):

      request   the prompt field is `inputs`, not `prompt`
      response  raw image bytes, with the format in Content-Type -- there is
                no JSON envelope to parse at all
    """

    def __init__(self, timeout, max_bytes):
        super().__init__(name="huggingface", timeout=timeout, max_bytes=max_bytes)
        # HF_TOKEN is read first so it matches the name the HF docs and every
        # HF snippet use; DWR_HF_TOKEN exists as an override for anyone who
        # already has an unrelated HF_TOKEN exported globally.
        self.api_key = (os.getenv("DWR_HF_TOKEN") or os.getenv("HF_TOKEN", "")).strip()
        # hf-inference retired the heavy image models (FLUX returns HTTP 410
        # there -- observed, not guessed) and now focuses on CPU inference.
        # This is the model its own text-to-image docs use, so it is the one
        # that actually answers on this route. FLUX lives on fal-ai and
        # replicate, which take different request shapes -- reaching them is
        # a new adapter, not an HF_API_BASE change.
        self.model = os.getenv(
            "HF_MODEL", "stabilityai/stable-diffusion-3-medium-diffusers"
        ).strip()
        # Overridable because HF has moved this route once already
        # (api-inference.huggingface.co -> router.huggingface.co). If it moves
        # again this is a .env change, not a code change.
        base = os.getenv("HF_API_BASE", "https://router.huggingface.co/hf-inference/models").rstrip("/")
        self.url = f"{base}/{self.model}"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes:
        try:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"inputs": prompt},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"huggingface transport error: {exc}") from exc

        if response.status_code == 503:
            # Distinct message: this is a cold model, not an outage. Retrying
            # is pointless inside a turn -- loading takes tens of seconds --
            # so it counts as a failure and the chain moves on.
            raise ProviderError(
                "huggingface model is cold (HTTP 503). Send one warm-up request "
                "before the demo."
            )
        if response.status_code != 200:
            raise ProviderError(
                f"huggingface returned HTTP {response.status_code}: {response.text[:200]}"
            )

        # Documented path: raw bytes, no envelope.
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            return self._check_size(response.content)

        # Some routes and some community providers wrap the image in JSON
        # instead. Handled rather than assumed, so an unexpected-but-valid
        # response does not read as an outage.
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError(
                f"huggingface returned neither an image nor JSON "
                f"(content-type {content_type!r}, {len(response.content)} bytes)"
            )

        candidate = None
        if isinstance(payload, dict):
            candidate = payload.get("image") or payload.get("b64_json")
            images = payload.get("images") or payload.get("data")
            if not candidate and isinstance(images, list) and images:
                first = images[0]
                candidate = first if isinstance(first, str) else (
                    first.get("b64_json") or first.get("url") or first.get("image")
                    if isinstance(first, dict) else None
                )
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            candidate = payload[0].get("generated_image") or payload[0].get("b64_json")

        if not candidate:
            raise ProviderError(
                f"huggingface response had no recognisable image field: {str(payload)[:120]}"
            )
        if candidate.startswith(("http://", "https://")):
            return await _download(client, candidate, self.timeout, self.max_bytes, self.name)
        return self._check_size(self._decode_data_uri(candidate))


class PollinationsProvider(ImageProvider):
    """Pollinations -- DEMO/TESTING ONLY, no credentials required.

    This is the service the project originally generated images with, before
    the provider chain existed. It is here because it is the only path that
    needs no key, no credit and no account, which makes it the one that
    cannot fail for a billing or token reason five minutes before a demo.

    Why it is not an event provider:
      - A free public service with no contract, no published rate limit and
        no concurrency guarantee. 150 simultaneous requests from one IP is
        exactly the shape of traffic such a service sheds.
      - No auth means no quota to check in advance and no support if it is
        down on the day.

    Shape: a GET with the prompt in the URL path, responding with raw image
    bytes. No JSON envelope, no POST body, no Authorization header -- all
    three differ from every other adapter here.
    """

    def __init__(self, timeout, max_bytes):
        super().__init__(name="pollinations", timeout=timeout, max_bytes=max_bytes)
        self.base = os.getenv(
            "POLLINATIONS_BASE", "https://image.pollinations.ai/p"
        ).rstrip("/")
        self.model = os.getenv("POLLINATIONS_MODEL", "flux").strip()
        # The original code requested 1024x1024. Scoring is CLIP RN50 at
        # 224x224, so anything above ~512 is bytes and latency spent on
        # detail the scorer cannot see -- the same argument the decision
        # record used to choose klein-4B over 9B.
        self.size = int(os.getenv("POLLINATIONS_SIZE", "512"))
        self.url = self.base  # per-request URL; prompt goes in the path

    def is_configured(self) -> bool:
        # Deliberately always true: there is nothing to configure. Selecting
        # it is the opt-in, and DEMO_ONLY_PROVIDERS makes that loud.
        return True

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes:
        # quote() with an empty safe list: prompts are free text from players
        # and routinely contain "/", "?" and "&", every one of which silently
        # changes the meaning of a URL path or query string if left unescaped.
        url = (
            f"{self.base}/{urllib.parse.quote(prompt, safe='')}"
            f"?model={urllib.parse.quote(self.model, safe='')}"
            f"&width={self.size}&height={self.size}&nologo=true"
        )
        try:
            response = await client.get(url, timeout=self.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"pollinations transport error: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(f"pollinations returned HTTP {response.status_code}")

        # No JSON envelope to check, so the only guard against being handed
        # an error page is the content type plus the magic-byte check that
        # ai_handling performs downstream.
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ProviderError(
                f"pollinations returned {content_type!r}, not an image "
                f"({len(response.content)} bytes)"
            )
        return self._check_size(response.content)


async def _download(client, url, timeout, max_bytes, provider_name) -> bytes:
    """Stream an image URL, aborting if it exceeds the size cap."""
    try:
        async with client.stream("GET", url, timeout=timeout) as response:
            if response.status_code != 200:
                raise ProviderError(f"{provider_name} image URL returned HTTP {response.status_code}")
            chunks, total = [], 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderError(f"{provider_name} image exceeded {max_bytes} bytes")
                chunks.append(chunk)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderError(f"{provider_name} image download failed: {exc}") from exc

    if not chunks:
        raise ProviderError(f"{provider_name} image download was empty")
    return b"".join(chunks)


# ----------------------------------------------------------------------
# Chain
# ----------------------------------------------------------------------
PROVIDER_TYPES = {
    "deepinfra": DeepInfraProvider,
    "replicate": ReplicateProvider,
    "huggingface": HuggingFaceProvider,
    "pollinations": PollinationsProvider,
}

# Providers that must never serve the real event. Selecting one is allowed --
# that is the point of a demo mode -- but it is announced loudly every time
# the chain is built, because a temporary configuration that survives to
# event day is a far more likely failure than a wrong line of code.
DEMO_ONLY_PROVIDERS = {"huggingface", "pollinations"}


class ProviderChain:
    """Ordered providers, each with its own breaker.

    A request walks the chain in order, skipping any provider whose breaker
    is open, and returns the first success. Ordering is configuration, so a
    rehearsal result can promote the backup without a code change.
    """

    def __init__(self, providers):
        self.providers = list(providers)

    @property
    def names(self):
        return [p.name for p in self.providers]

    def forced_provider(self):
        """Operator kill switch: pin all traffic to one provider on the day
        without a redeploy."""
        forced = os.getenv("IMAGE_PROVIDER_OVERRIDE", "").strip().lower()
        return forced or None

    async def generate(self, client: httpx.AsyncClient, prompt: str) -> bytes | None:
        forced = self.forced_provider()
        candidates = self.providers
        if forced:
            candidates = [p for p in self.providers if p.name == forced]
            if not candidates:
                logger.error("IMAGE_PROVIDER_OVERRIDE=%r matches no configured provider; "
                             "falling back to the normal chain.", forced)
                candidates = self.providers

        skipped = []
        for provider in candidates:
            if not provider.breaker.allow_request():
                skipped.append(provider.name)
                continue

            started = time.perf_counter()
            try:
                payload = await provider.generate(client, prompt)
            except ProviderError as exc:
                provider.breaker.record_failure()
                logger.warning("Provider %s failed in %.1fs: %s",
                               provider.name, time.perf_counter() - started, exc)
                continue
            except Exception as exc:
                provider.breaker.record_failure()
                logger.exception("Provider %s raised unexpectedly: %s", provider.name, exc)
                continue

            provider.breaker.record_success()
            if skipped:
                logger.info("Served by %s after skipping %s.", provider.name, ", ".join(skipped))
            return payload

        logger.error("Every image provider failed or was skipped (skipped=%s).", skipped or "none")
        return None

    def snapshot(self) -> list:
        return [p.breaker.snapshot() for p in self.providers]


def build_chain(timeout_primary=None, timeout_secondary=None, max_bytes=None) -> ProviderChain:
    """Construct the chain from IMAGE_PROVIDERS (ordered, comma separated).

    Providers without credentials are dropped with a warning rather than
    failing startup -- a missing backup key should not stop the app booting.
    """
    order = [
        name.strip().lower()
        for name in os.getenv("IMAGE_PROVIDERS", "deepinfra,replicate").split(",")
        if name.strip()
    ]
    max_bytes = max_bytes if max_bytes is not None else int(
        os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024))
    )
    primary_timeout = timeout_primary if timeout_primary is not None else float(
        os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "20")
    )
    secondary_timeout = timeout_secondary if timeout_secondary is not None else float(
        os.getenv("IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS", "8")
    )

    providers = []
    for index, name in enumerate(order):
        cls = PROVIDER_TYPES.get(name)
        if cls is None:
            logger.error("Unknown image provider %r in IMAGE_PROVIDERS; ignoring.", name)
            continue
        provider = cls(
            timeout=primary_timeout if index == 0 else secondary_timeout,
            max_bytes=max_bytes,
        )
        if not provider.is_configured():
            logger.warning(
                "Image provider %r has no API key configured; it will be skipped. "
                "Set its credentials before the event if it is expected to serve traffic.",
                name,
            )
            continue
        providers.append(provider)

    if not providers:
        logger.error(
            "No image providers are configured (IMAGE_PROVIDERS=%s). Set the key "
            "for at least one: DEEPINFRA_API_KEY, REPLICATE_API_TOKEN, or HF_TOKEN "
            "for the demo provider. Image generation will fail and rounds score 0.",
            ",".join(order) or "<empty>",
        )
    else:
        logger.info("Image provider chain: %s", " -> ".join(p.name for p in providers))

    demo = [p.name for p in providers if p.name in DEMO_ONLY_PROVIDERS]
    if demo:
        logger.warning(
            "=" * 70
            + "\nDEMO PROVIDER ACTIVE: %s\nThis chain is NOT event-ready. Demo "
              "providers are free or unmetered\nservices with no published "
              "concurrency guarantee; 150 teams generating\nin the same few "
              "seconds is precisely the traffic they shed.\nRestore "
              "IMAGE_PROVIDERS=deepinfra,replicate before the event.\n"
            + "=" * 70,
            ", ".join(demo),
        )

    return ProviderChain(providers)
