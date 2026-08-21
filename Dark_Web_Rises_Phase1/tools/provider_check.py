"""Provider check: make one real call and confirm the adapter parses it.

The adapters in services/providers.py were written from vendor documentation,
not from observed responses. Nothing has ever confirmed that DeepInfra and
Replicate actually return the shapes the parsing code expects. A mismatch
does not crash -- ProviderChain catches ProviderError, trips the breaker and
moves on -- so on event day it would look like a provider outage rather than
a bug in our own code, and the failover would quietly send every image
request to the secondary.

This tool makes that failure visible while it is still cheap to fix.

    # everything configured in .env
    python tools/provider_check.py

    # one provider (Replicate's free-tier runs cost nothing)
    python tools/provider_check.py --provider replicate

    # keep the image and the raw response for inspection
    python tools/provider_check.py --save out.png --dump-json raw.json

Exit code is 0 only if every checked provider returned a usable image, so it
can gate a deploy.

Two distinct failures, deliberately separated
---------------------------------------------
A wrong model slug and an unparseable response both surface as ProviderError
in normal operation, but they need completely different fixes:

    slug wrong      -> HTTP 404, generate() raises before any parsing runs.
                       The parser is still unverified. Reporting this as
                       "shape check failed" would be actively misleading.
    shape wrong     -> HTTP 200, but no recognisable image field.
                       This is the one we are actually hunting.

So the checks run in that order and report separately. A 404 is reported as
"parser NOT verified", never as a parser failure.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401  -- importing this loads .env, so the tool picks up
              # the provider keys from the file without needing shell exports

import httpx

from services.providers import PROVIDER_TYPES, ProviderError

DEFAULT_PROMPT = (
    "a red bicycle leaning against a blue wall, bright daylight, photograph"
)

# Kept in one place so --model rebuilds the endpoint the same way the adapter
# constructs it, rather than string-patching a URL the adapter already built.
URL_TEMPLATES = {
    "pollinations": os.getenv("POLLINATIONS_BASE", "https://image.pollinations.ai/p"),
    "deepinfra": "https://api.deepinfra.com/v1/inference/{model}",
    "replicate": "https://api.replicate.com/v1/models/{model}/predictions",
    "huggingface": (
        os.getenv("HF_API_BASE", "https://router.huggingface.co/hf-inference/models").rstrip("/")
        + "/{model}"
    ),
}

# Long enough that a slow cold start is not mistaken for a broken adapter.
# The production timeouts (20s primary / 8s secondary) are a turn-budget
# decision, not a statement about how long the provider can take; checking
# the response *shape* should not be aborted by a tight deadline.
CHECK_TIMEOUT = 120.0

# Set by check_provider when a failure was diagnosed as transient upstream
# capacity, so main() knows a retry is worth making. A module-level flag is
# blunt, but check_provider's boolean return is deliberately a simple
# verified/not-verified answer and widening it would complicate every caller
# for one edge case.
LAST_WAS_TRANSIENT = {"value": False}


# ----------------------------------------------------------------------
# Structural description
# ----------------------------------------------------------------------
def describe(value, indent=2, key=None, depth=0):
    """Print the shape of a JSON response without dumping megabytes of base64.

    Deliberately generic rather than mirroring the adapter's extraction
    logic. If it duplicated the adapter it would drift from it, and would
    agree with a broken parser for exactly the same wrong reason.
    """
    pad = " " * indent
    label = f"{key}: " if key else ""

    if isinstance(value, dict):
        print(f"{pad}{label}object with keys {list(value)}")
        for child_key, child in value.items():
            describe(child, indent + 2, child_key, depth + 1)
        return

    if isinstance(value, list):
        print(f"{pad}{label}array, length {len(value)}")
        if value:
            describe(value[0], indent + 2, "[0]", depth + 1)
        return

    if isinstance(value, str):
        kind = ""
        if value.startswith(("http://", "https://")):
            kind = "  <- looks like a URL"
        elif value.startswith("data:") or _looks_like_base64(value):
            kind = "  <- looks like base64 image data"
        print(f"{pad}{label}string, {len(value)} chars: {_truncate(value)}{kind}")
        return

    print(f"{pad}{label}{type(value).__name__}: {value!r}")


def _truncate(text, limit=70):
    text = text.replace("\n", "")
    return text if len(text) <= limit else f"{text[:limit]}..."


def _looks_like_base64(value):
    if len(value) < 256:
        return False
    sample = value[:256]
    return all(character.isalnum() or character in "+/=_-" for character in sample)


def _redact_blobs(value):
    """Replace long base64 strings with a placeholder so --dump-json produces
    a file small enough to read and to paste into a bug report."""
    if isinstance(value, dict):
        return {key: _redact_blobs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_blobs(child) for child in value]
    if isinstance(value, str) and _looks_like_base64(value) and not value.startswith("http"):
        return f"<base64 blob, {len(value)} chars, elided>"
    return value


def image_dimensions(payload: bytes):
    """(width, height) of the returned image, or None.

    Uses Pillow, which is already a hard dependency of the app (ai_handling
    opens every image with it), so this adds nothing to install. Returns None
    rather than raising -- an unreadable image is already reported by
    identify_image, and failing here would turn a cosmetic extra into a broken
    check.
    """
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(payload)) as img:
            return img.size
    except Exception:
        return None


def identify_image(payload: bytes) -> str:
    """Report the real format from magic bytes.

    Worth checking because the adapter returns whatever it downloaded. If a
    provider ever serves an HTML error page or a JSON error body with HTTP
    200, the bytes flow all the way through to CLIP, which fails much later
    and much less clearly.
    """
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if payload.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "WEBP"
    if payload.startswith(b"GIF8"):
        return "GIF"
    head = payload[:64].lstrip()
    if head.startswith(b"<") or head.startswith(b"{"):
        return "NOT AN IMAGE (looks like HTML or JSON)"
    return "unrecognised"


# ----------------------------------------------------------------------
# Slug preflight
# ----------------------------------------------------------------------
async def preflight_replicate(client, provider):
    """Confirm the model exists before spending one of the free-tier runs.

    Replicate publishes a model-metadata endpoint, so the slug can be checked
    without running a prediction.
    """
    url = f"https://api.replicate.com/v1/models/{provider.model}"
    try:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            timeout=30.0,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"could not reach Replicate: {exc}"

    if response.status_code == 200:
        body = response.json()
        latest = (body.get("latest_version") or {}).get("id", "unknown")
        return True, f"model exists (latest version {latest[:12]})"
    if response.status_code in (401, 403):
        return False, "REPLICATE_API_TOKEN rejected (401/403)"
    if response.status_code == 404:
        return False, f"no such model {provider.model!r} (404) -- check REPLICATE_MODEL"
    return False, f"unexpected HTTP {response.status_code}"


async def preflight_deepinfra(client, provider):
    """DeepInfra has no documented per-model metadata endpoint that is
    guaranteed to be readable with an inference key, so the slug is validated
    from the status code of the real call instead. classify_http() below does
    the interpreting; this only reports what will be attempted."""
    return None, f"no preflight available; slug validated from the call itself"


PREFLIGHTS = {
    "replicate": preflight_replicate,
    "deepinfra": preflight_deepinfra,
}


# Substrings that mean "the upstream ran out of capacity", regardless of the
# status code attached to them. Observed for real: hf-inference returns
# HTTP 400 with a CUDA OOM body, which reads as "your request is malformed"
# if you trust the status code alone. The body is the better evidence, so it
# is checked first.
CAPACITY_MARKERS = (
    "out of memory",
    "cuda",
    "currently loading",
    "model is loading",
    "overloaded",
    "no available workers",
    "capacity",
    "try again later",
)


def diagnose(status, body):
    """Explain a failure using the body where it contradicts the status code.

    Returns (explanation, request_shape_was_accepted).

    The second value matters: if the upstream got far enough to attempt
    inference, then the route, the model slug and the request payload were
    all accepted. That is most of what this tool exists to establish, and it
    should not be thrown away just because the generation itself failed.
    """
    lowered = (body or "").lower()
    if any(marker in lowered for marker in CAPACITY_MARKERS):
        return (
            "UPSTREAM CAPACITY, not our request. The call reached the model and "
            "failed\n         while generating, which means the route, the model "
            "slug and the\n         request payload were all ACCEPTED. Retry, or "
            "pick a smaller/warmer model.",
            True,
        )
    return classify_http(status), False


def classify_http(status):
    """Map a status code to the fix it implies."""
    return {
        400: "bad request -- the payload shape we send is wrong, not the response",
        401: "API key rejected",
        402: "payment required -- no credit on the account",
        403: "forbidden -- key lacks access to this model",
        404: "NO SUCH MODEL -- fix the model slug; the parser is still unverified",
        # Seen for real: hf-inference returns 410 for models it has retired.
        # Distinct from 404 -- the model exists on the Hub, it is just not
        # served by *this* provider route, so the fix is the route or the
        # model, never the parser.
        410: ("MODEL RETIRED ON THIS PROVIDER -- the model exists but this "
              "route no longer serves it. Pick a model the provider still "
              "supports, or point HF_API_BASE at the provider that does. "
              "The parser is still unverified"),
        422: "unprocessable -- input schema mismatch (e.g. wrong field name for the prompt)",
        429: "rate limited or out of free-tier runs",
    }.get(status)


# ----------------------------------------------------------------------
# The check
# ----------------------------------------------------------------------
async def check_provider(name, args):
    print("=" * 72)
    print(f"{name.upper()}")
    print("=" * 72)

    cls = PROVIDER_TYPES.get(name)
    if cls is None:
        print(f"  Unknown provider {name!r}. Known: {', '.join(PROVIDER_TYPES)}")
        return False

    provider = cls(timeout=CHECK_TIMEOUT, max_bytes=int(
        os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024))
    ))

    if args.model:
        # Overriding the model is how you verify the parser without paying.
        # On Replicate the response envelope -- status, urls.get, output -- is
        # identical for every model, so a free-tier image model exercises the
        # same parsing path, polling fallback and URL download as the model
        # you will actually run. It does not prove that the production model
        # puts a *list* in `output` rather than a bare string, but the adapter
        # accepts both, so what is left unverified is small and known.
        provider.model = args.model
        provider.url = URL_TEMPLATES[name].format(model=args.model)
        print(f"  NOTE     --model override in use: this verifies the response")
        print(f"           envelope, not the production model itself.")

    print(f"  model    {provider.model}")
    print(f"  endpoint {provider.url}")

    LAST_WAS_TRANSIENT["value"] = False

    if not provider.is_configured():
        env_var = {
            "deepinfra": "DEEPINFRA_API_KEY",
            "replicate": "REPLICATE_API_TOKEN",
            "huggingface": "HF_TOKEN",
            "pollinations": "(none required)",
        }[name]
        print(f"  SKIPPED: {env_var} is not set.\n")
        return False
    # getattr, not provider.api_key: a provider that needs no credentials
    # legitimately has no such attribute, and "every provider authenticates"
    # is precisely the assumption this tool exists to stop making.
    api_key = getattr(provider, "api_key", None)
    print(f"  key      set ({len(api_key)} chars)" if api_key
          else "  key      none required")
    print()

    async with httpx.AsyncClient() as client:
        # --- step 1: does the model slug resolve? ----------------------
        preflight = PREFLIGHTS.get(name)
        if preflight:
            ok, message = await preflight(client, provider)
            marker = {True: "OK  ", False: "FAIL", None: "--  "}[ok]
            print(f"  [{marker}] slug check: {message}")
            if ok is False:
                print("\n  Stopping: the parser was never reached, so it remains "
                      "UNVERIFIED.\n")
                return False

        # --- step 2: what does the raw response actually look like? ----
        print(f"  [....] calling the API (prompt: {args.prompt[:44]!r})")
        raw, status, body = await raw_call(client, name, provider, args.prompt)

        if status is not None and status != 200:
            explanation, shape_accepted = diagnose(status, body)
            print(f"  [FAIL] HTTP {status}"
                  + (f" -- {explanation}" if explanation else ""))

            LAST_WAS_TRANSIENT["value"] = shape_accepted
            if shape_accepted:
                # Worth stating plainly. The request contract is the part we
                # control and the part that was unverified; upstream capacity
                # is neither. Reporting this as a flat failure would discard
                # the one thing the call did establish.
                print("\n  Request contract VERIFIED: endpoint, model slug and "
                      "payload shape\n  are all accepted by the provider. Only "
                      "the response parser is still\n  unverified, because no "
                      "response was produced to parse.")
                print("  This is a transient upstream condition -- retry.")
            elif status in (404, 410):
                print("\n  The call never reached the parsing code, so the "
                      "response parser is still UNVERIFIED.")
                if status == 410 and name == "huggingface":
                    print("  hf-inference retired most image models; its "
                          "documented text-to-image\n  model is "
                          "stabilityai/stable-diffusion-3-medium-diffusers. "
                          "FLUX runs\n  on other providers (fal-ai, replicate), "
                          "which use different request\n  shapes -- not a "
                          "drop-in HF_API_BASE change.")
            print()
            return False

        if raw is None:
            print("  [FAIL] no response body to inspect\n")
            return False

        if isinstance(raw, dict) and raw.get("__raw_image__"):
            print(f"  [OK  ] HTTP 200, raw image body (no JSON envelope)")
            print("\n  --- observed response shape ---")
            print(f"    content-type: {raw['content_type']}")
            print(f"    body:         {raw['bytes']:,} bytes")
            print(f"    magic bytes:  {raw['magic']}")
            print()
        else:
            print(f"  [OK  ] HTTP 200, response parsed as JSON")
            print("\n  --- observed response shape ---")
            describe(raw)
            print()

        if args.dump_json:
            with open(args.dump_json, "w") as handle:
                json.dump(_redact_blobs(raw), handle, indent=2)
            print(f"  Raw response (base64 elided) written to {args.dump_json}\n")

        # --- step 3: does the real adapter cope with it? ---------------
        # This calls the shipping code path, not a copy of it. That is the
        # whole point: agreement between this tool and the adapter proves
        # nothing unless the adapter itself is what ran.
        print("  --- running the real adapter against a fresh call ---")
        try:
            payload = await provider.generate(client, args.prompt)
        except ProviderError as exc:
            print(f"  [FAIL] the adapter rejected the response: {exc}")
            print("\n  The response shape above does not match what "
                  "services/providers.py expects.")
            print("  Fix the extraction logic in "
                  f"{cls.__name__}.generate to match the shape printed above.\n")
            return False
        except Exception as exc:
            print(f"  [FAIL] the adapter raised unexpectedly: {type(exc).__name__}: {exc}\n")
            return False

        fmt = identify_image(payload)
        dims = image_dimensions(payload)
        print(f"  [OK  ] adapter returned {len(payload):,} bytes, format {fmt}"
              + (f", {dims[0]}x{dims[1]} px" if dims else ""))

        # Dimensions are reported because on DeepInfra they ARE the price.
        # FLUX-2-klein-4b bills $0.014 x (w/1024) x (h/1024), so a request whose
        # width/height were silently ignored costs 4x what the same image costs
        # at 512 -- and nothing else in the output would show it. Confirming the
        # pixels is confirming the invoice.
        if dims and name == "deepinfra":
            requested = getattr(provider, "size", 0)
            billed = 0.014 * (dims[0] / 1024) * (dims[1] / 1024)
            print(f"         DeepInfra bills by area: ~${billed:.4f} for this image "
                  f"at {dims[0]}x{dims[1]}")
            if requested and (dims[0] != requested or dims[1] != requested):
                print(f"  [WARN] DEEPINFRA_SIZE={requested} but the image came back "
                      f"{dims[0]}x{dims[1]}.")
                print(f"         The size parameter was ignored, so you are paying "
                      f"${billed:.4f}/image\n         instead of "
                      f"${0.014 * (requested/1024)**2:.4f}. Check whether this model "
                      f"accepts width/height.")
            elif requested:
                print(f"         DEEPINFRA_SIZE={requested} took effect. "
                      f"Use --cost-per-image {billed:.4f} for the load tests.")

        if fmt.startswith("NOT AN IMAGE") or fmt == "unrecognised":
            print("\n  The adapter returned bytes, but they are not a recognisable "
                  "image.\n  CLIP would fail on these much later and much less "
                  "clearly.\n")
            return False

        if args.save:
            with open(args.save, "wb") as handle:
                handle.write(payload)
            print(f"  Image written to {args.save} -- open it and confirm it "
                  "matches the prompt.")

        print(f"\n  {name} VERIFIED: slug resolves, response parses, image is valid.\n")
        return True


async def raw_call(client, name, provider, prompt):
    """Make the same request the adapter makes, but return the undecoded JSON
    so the observed shape can be printed even when the adapter chokes on it.

    Kept deliberately thin -- it mirrors only the *request*, which is stable
    and documented, never the response handling, which is the thing under
    test.
    """
    try:
        if name == "deepinfra":
            # provider._payload(), not a literal body. The adapter now sends
            # width/height (DeepInfra bills FLUX by area, and the 1024 default
            # costs 4x what 512 does for detail CLIP downsamples away). If this
            # tool kept its own copy of the request body, the two would drift
            # and the check would verify a request the app never makes -- which
            # is the one thing this tool exists not to do.
            response = await client.post(
                provider.url,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json=provider._payload(prompt),
                timeout=CHECK_TIMEOUT,
            )
        elif name == "pollinations":
            # GET, prompt in the path, no auth header -- unlike every other
            # provider here. Built by the adapter so this cannot drift.
            import urllib.parse as _up
            url = (
                f"{provider.base}/{_up.quote(prompt, safe='')}"
                f"?model={_up.quote(provider.model, safe='')}"
                f"&width={provider.size}&height={provider.size}&nologo=true"
            )
            response = await client.get(url, timeout=CHECK_TIMEOUT)
        elif name == "huggingface":
            # `inputs`, not `prompt` -- see the HF text-to-image API spec.
            response = await client.post(
                provider.url,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json={"inputs": prompt},
                timeout=CHECK_TIMEOUT,
            )
        else:
            response = await client.post(
                provider.url,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Prefer": "wait",
                },
                json={"input": {"prompt": prompt}},
                timeout=CHECK_TIMEOUT,
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        print(f"  [FAIL] transport error: {exc}")
        return None, None, ""

    if response.status_code not in (200, 201):
        # Show the body: vendor error messages are usually specific and
        # save a round of guessing. It is also returned, because the body
        # frequently contradicts the status code (see diagnose()).
        body = response.text[:400]
        if body:
            print(f"         body: {body}")
        return None, response.status_code, body

    # Hugging Face returns the image itself, with no JSON envelope, so a
    # non-JSON body is a documented success here rather than a failure.
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("image/"):
        return {
            "__raw_image__": True,
            "content_type": content_type,
            "bytes": len(response.content),
            "magic": identify_image(response.content),
        }, 200, ""

    try:
        return response.json(), 200, ""
    except ValueError:
        print(f"  [FAIL] response was neither an image nor JSON "
              f"(content-type {content_type!r}). First 200 bytes:")
        print(f"         {response.content[:200]!r}")
        return None, None, ""


def main():
    parser = argparse.ArgumentParser(
        description="Confirm each image provider's response parses correctly."
    )
    parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_TYPES),
                        help="check only this provider (repeatable). "
                             "Default: everything in IMAGE_PROVIDERS.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="prompt to generate with")
    parser.add_argument("--model", metavar="SLUG",
                        help="override the model, e.g. a free-tier model on "
                             "Replicate. Verifies the response envelope without "
                             "spending credit; see the note printed when used.")
    parser.add_argument("--save", metavar="PATH",
                        help="write the returned image here")
    parser.add_argument("--dump-json", metavar="PATH",
                        help="write the raw response (base64 elided) here")
    parser.add_argument("--retries", type=int, default=3, metavar="N",
                        help="attempts when the provider reports a transient "
                             "capacity error such as CUDA OOM (default 3). "
                             "Only transient failures are retried; a bad slug "
                             "or a shape mismatch fails immediately.")
    parser.add_argument("--retry-delay", type=float, default=10.0, metavar="SECONDS",
                        help="wait between retries (default 10)")
    args = parser.parse_args()

    names = args.provider or [
        name.strip().lower()
        for name in os.getenv("IMAGE_PROVIDERS", "deepinfra,replicate").split(",")
        if name.strip()
    ]

    print(f"\nChecking: {', '.join(names)}")
    print(f"Prompt:   {args.prompt!r}\n")

    results = {}
    for name in names:
        # Retries belong in this tool but NOT in the adapter: inside a live
        # turn a retry burns the player's clock, which is why failover
        # replaced retrying (see services/providers.py). Here there is no
        # player waiting, and a transient capacity blip should not be
        # mistaken for an unverified parser.
        for attempt in range(1, args.retries + 1):
            if attempt > 1:
                print(f"\n--- retry {attempt - 1} of {args.retries - 1} "
                      f"(previous attempt hit a transient upstream error) ---\n")
                time.sleep(args.retry_delay)
            results[name] = asyncio.run(check_provider(name, args))
            if results[name] or not LAST_WAS_TRANSIENT["value"]:
                break

    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for name, ok in results.items():
        print(f"  {name:<14}{'VERIFIED' if ok else 'NOT VERIFIED'}")

    if not all(results.values()):
        print("\nAt least one provider is unverified. On event day this would "
              "look\nlike a provider outage -- the breaker trips and traffic "
              "silently moves\nto the secondary -- rather than a bug in our "
              "own parsing.\n")
        return 1

    print("\nAll checked providers verified.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
