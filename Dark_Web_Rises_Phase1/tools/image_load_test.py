"""Provider load test: does this provider survive the traffic Phase 1 actually makes?

`tools/provider_check.py` answers a different question -- "does one call parse?".
It makes a single request and stops. That is necessary and not sufficient: an
adapter that parses one response perfectly tells you nothing about what the
provider does when 175 teams hit it in the same second, which is the only
traffic shape this event ever produces.

The traffic shape, precisely
----------------------------
Rounds are synchronised. Every team is on the same turn at the same time, so
image requests arrive as a *burst*, not a stream:

    175 teams  ->  175 simultaneous requests, once per turn
    4 turns per round                  ->  700 images per round
    5 rounds                           -> 3500 images per event

700 is therefore not an arbitrary test size. It is exactly one round, and it
is the smallest sample that contains a full four-burst sequence -- enough to
show whether a provider degrades across consecutive bursts (rate-limit
buckets refilling, autoscaler catching up, queue depth accumulating) rather
than just how it handles the first one.

Why the timeout here is 120s and not 20s
----------------------------------------
Production uses IMAGE_GEN_TIMEOUT_SECONDS=20 for the primary provider. If
this harness used 20s too, every slow request would be recorded as a bare
"timeout" and the actual latency distribution above 20s would be invisible --
you would learn that requests failed but not whether they failed at 21s or
at 300s. Those imply completely different fixes.

So requests run to a long timeout, the true latency is measured, and the
report then derives what the production budget *would* have done to that
distribution. Same data, both questions answered, one run.

Usage
-----
    # one full round of DeepInfra traffic (4 bursts x 175) = 700 images
    python tools/image_load_test.py --provider deepinfra --pattern burst \
        --burst-size 175 --bursts 4

    # find where Replicate's concurrency ceiling is: 400 images across 4 steps
    python tools/image_load_test.py --provider replicate --pattern sweep \
        --sweep 25,50,100,175 --per-step 100

    # resume an interrupted run (re-reads results.jsonl, skips what finished)
    python tools/image_load_test.py --provider deepinfra --resume

Exit code is 0 only if the run meets the event thresholds (--min-success-rate
and the derived production-timeout check), so this can gate go/no-go.

Nothing here re-implements a provider. Requests go through the shipping
adapters in services/providers.py, because a harness that agrees with a copy
of the adapter proves nothing about the adapter.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401  -- importing this loads .env, so provider keys are
              # picked up from the file without shell exports

import httpx

from services.providers import PROVIDER_TYPES, ProviderError

# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------
# Generated rather than hardcoded, and deliberately varied. A single repeated
# prompt is the wrong test: every provider in this space caches on the prompt
# hash, so 700 identical prompts measures a cache, not a GPU. These are built
# from the same shape a player's prompt takes -- a short scene description
# that passes classify_prompt() -- with enough combinatorial spread that no
# two requests in a 700-image run collide.
_SUBJECTS = [
    "a rusted motorcycle", "a glass tower", "a wooden fishing boat",
    "an abandoned lighthouse", "a red telephone box", "a marble staircase",
    "a cracked stone bridge", "a neon vending machine", "a copper diving helmet",
    "a snow covered bicycle", "a hot air balloon", "a derelict tram",
    "a brass telescope", "a painted wooden door", "a stack of old books",
]
_SETTINGS = [
    "on an empty beach", "in a flooded warehouse", "beside a mountain road",
    "under a motorway bridge", "in an overgrown courtyard", "on a frozen lake",
    "inside a glass greenhouse", "at the edge of a pine forest",
    "in a narrow cobbled alley", "on a windswept cliff",
]
_LIGHTING = [
    "in bright morning light", "under heavy grey cloud", "at golden hour",
    "lit by a single street lamp", "in dense fog", "during a rainstorm",
    "under a clear night sky", "in harsh midday sun",
]
_STYLES = [
    "photograph", "cinematic still", "watercolour painting",
    "oil painting", "pencil sketch", "35mm film photograph",
]


def build_prompts(count: int, seed: int) -> list:
    """Deterministic, non-repeating prompts. Seeded so a rerun compares like
    for like -- a latency change between two runs should mean the provider
    changed, not that the prompts did."""
    rng = random.Random(seed)
    combos = set()
    prompts = []
    while len(prompts) < count:
        combo = (
            rng.randrange(len(_SUBJECTS)), rng.randrange(len(_SETTINGS)),
            rng.randrange(len(_LIGHTING)), rng.randrange(len(_STYLES)),
        )
        if combo in combos:
            continue
        combos.add(combo)
        s, se, li, st = combo
        prompts.append(
            f"{_SUBJECTS[s]} {_SETTINGS[se]} {_LIGHTING[li]}, {_STYLES[st]}"
        )
    return prompts


# ----------------------------------------------------------------------
# Error taxonomy
# ----------------------------------------------------------------------
# Every failure is bucketed, because "12% failed" is not actionable and
# "12% failed, all of them 429" is. The buckets map to different fixes:
#
#   rate_limit    -> lower MAX_CONCURRENT_IMAGE_REQUESTS, or buy more quota
#   timeout       -> the model is too slow for a 90s turn; pick a smaller one
#   capacity      -> provider-side, retry or move the primary
#   auth/billing  -> fix before the event, nothing to tune
#   shape         -> our parser is wrong; this is the one we can fix ourselves
#   transport     -> network between us and them
FAILURE_BUCKETS = [
    ("auth", ("http 401", "http 403", "rejected", "unauthor")),
    ("billing", ("http 402", "payment", "insufficient", "credit", "quota exceeded", "billing")),
    ("rate_limit", ("http 429", "rate limit", "too many requests")),
    ("bad_model", ("http 404", "http 410", "no such model")),
    ("bad_request", ("http 400", "http 422", "unprocessable")),
    ("capacity", ("http 500", "http 502", "http 503", "http 504", "out of memory",
                  "cuda", "overloaded", "no available workers", "currently loading",
                  "try again later", "capacity")),
    ("timeout", ("timeout", "timed out", "did not finish within")),
    ("shape", ("no recognisable image", "was not an image url", "non-json",
               "neither an image nor json", "over the", "exceeded")),
    ("transport", ("transport error", "connection", "download failed", "empty")),
]


def classify_failure(message: str) -> str:
    lowered = (message or "").lower()
    for bucket, markers in FAILURE_BUCKETS:
        if any(marker in lowered for marker in markers):
            return bucket
    return "other"


def identify_image(payload: bytes) -> str:
    """Real format from magic bytes.

    Checked on every single response, not just a sample. A provider under load
    is exactly when it starts returning HTML error pages with HTTP 200, and
    those bytes would otherwise flow through the adapter, past the size check,
    into CLIP, and fail there -- at which point the round is already lost and
    the log says "image comparison failed" instead of "the provider is down".
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
        return "NOT_AN_IMAGE"
    return "unrecognised"


# ----------------------------------------------------------------------
# Client-side headroom
# ----------------------------------------------------------------------
# Everything in this section exists for one reason: if OUR side cannot hold N
# requests open at once, the run measures our bottleneck and reports it as the
# provider's. That failure is invisible -- the numbers look plausible, the
# verdict looks authoritative, and it is wrong. Worse, at a paid provider you
# have spent the images to learn nothing.
#
# tools/check_client_concurrency.py proves these settings work, offline and
# for free, before any of them matter.

def tune_runtime_for_concurrency(peak: int, verbose: bool = True) -> list:
    """Raise the client-side ceilings that would otherwise cap us below `peak`.

    Returns a list of human-readable notes about what was found and changed.
    """
    notes = []

    # 1. anyio's thread limiter, default 40.
    #
    # httpx's async transport resolves hostnames with getaddrinfo, which is a
    # blocking call, so anyio runs it on a thread from a pool bounded by this
    # limiter. At 200 simultaneous connection attempts the name resolutions
    # queue 40 at a time. DNS is usually cached after the first lookup so the
    # effect is modest -- but "modest" is doing real work in that sentence,
    # and it lands entirely on the first burst, which is the one whose numbers
    # get read most carefully. Raising it costs nothing; the threads are only
    # created on demand.
    try:
        import anyio.to_thread
        limiter = anyio.to_thread.current_default_thread_limiter()
        before = limiter.total_tokens
        wanted = max(before, peak + 32)
        if wanted > before:
            limiter.total_tokens = wanted
            notes.append(f"anyio thread limiter {before} -> {wanted} "
                         f"(DNS resolution would have queued in batches of {before})")
        else:
            notes.append(f"anyio thread limiter already {before}, no change needed")
    except Exception as exc:  # pragma: no cover - anyio internals
        notes.append(f"could not raise the anyio thread limiter: {exc}")

    # 2. File descriptors. Each concurrent HTTPS request needs its own socket
    # (HTTP/1.1, deliberately -- see build_client). Hitting the limit surfaces
    # as OSError EMFILE partway through a burst, which the harness would
    # faithfully record as a transport failure *of the provider*.
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        needed = peak * 2 + 128   # sockets, plus the files everything else holds
        if soft < needed:
            target = min(hard, max(needed, soft))
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                notes.append(f"RLIMIT_NOFILE {soft} -> {target} (needed ~{needed})")
            except (ValueError, OSError):
                notes.append(
                    f"WARNING: RLIMIT_NOFILE is {soft}, this run wants ~{needed}, "
                    f"and it could not be raised. Run `ulimit -n {needed}` first "
                    f"or expect spurious transport errors."
                )
        else:
            notes.append(f"RLIMIT_NOFILE {soft} is sufficient (needs ~{needed})")
    except ImportError:
        # Windows. There is no per-process fd rlimit in the POSIX sense, and
        # the practical socket ceiling is far above anything here. The one
        # Windows-specific concern -- SelectorEventLoop's 512-handle limit --
        # does not apply, because Python 3.8+ defaults to the Proactor loop.
        notes.append("RLIMIT_NOFILE: not applicable on Windows")

    if verbose:
        for note in notes:
            marker = "  !! " if note.startswith("WARNING") else "  -- "
            print(f"{marker}{note}")
    return notes


def build_client(peak: int) -> httpx.AsyncClient:
    """The HTTP client, sized so the pool is never the constraint.

    Deliberately HTTP/1.1, not HTTP/2. Multiplexing 200 requests over one
    connection sounds better and would measure something else: HTTP/2 servers
    advertise SETTINGS_MAX_CONCURRENT_STREAMS, commonly 100, and requests past
    it queue *inside* the connection where this harness cannot see them. A
    silent cap at 100 while the report says 200 is exactly the class of lie
    this file is built to avoid. One socket per in-flight request is more
    expensive and unambiguous.
    """
    return httpx.AsyncClient(
        limits=httpx.Limits(
            # Headroom above `peak` so that a redirect or an image download
            # (Replicate returns a URL that then has to be fetched) never has
            # to wait for a slot behind a generation request.
            max_connections=peak * 2 + 20,
            max_keepalive_connections=peak * 2 + 20,
            # Long enough to span the gap between bursts, so burst 2 reuses
            # burst 1's connections instead of re-handshaking 200 TLS sessions
            # and charging that to the provider's latency.
            keepalive_expiry=120.0,
        ),
        follow_redirects=True,
        # httpx's default is 5s for everything including connect. Every
        # request here passes its own timeout, but the pool timeout is not
        # overridable per-request -- leave it generous or a burst that briefly
        # queues would fail with PoolTimeout and be recorded as the provider
        # failing.
        timeout=httpx.Timeout(None, connect=30.0, pool=60.0),
    )


async def warm_connections(client: httpx.AsyncClient, provider, count: int) -> str:
    """Open `count` pooled connections before the timed burst starts.

    Free -- these are unauthenticated GETs to the API root, not generations.
    Whatever they return (401, 403, 404) is irrelevant; the point is the side
    effect of having resolved DNS and completed a TLS handshake.

    Without this, the first burst's latency includes 200 simultaneous DNS
    lookups and 200 TLS handshakes, which is client-side setup cost being
    attributed to the provider's generation time. That inflates burst 1
    relative to burst 2 and invites exactly the wrong conclusion -- "it warms
    up after the first round" -- about a provider that did nothing of the sort.
    """
    url = getattr(provider, "url", "") or getattr(provider, "base", "")
    if not url:
        return "no endpoint to warm"
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}/"

    async def ping():
        try:
            await client.get(root, timeout=20.0)
        except Exception:
            pass   # the handshake is the point; the response is not

    started = time.perf_counter()
    await asyncio.gather(*(ping() for _ in range(count)))
    return (f"opened {count} connection(s) to {parts.netloc} in "
            f"{time.perf_counter() - started:.1f}s")


@dataclass
class Result:
    index: int
    step: str                 # burst/sweep step this belonged to
    concurrency: int          # how many were in flight for this step
    started_at: float
    latency: float
    ok: bool
    bytes: int = 0
    image_format: str = ""
    failure_bucket: str = ""
    error: str = ""
    prompt: str = ""
    # Seconds spent waiting for an admission slot before the request was even
    # sent, and the sum of that plus the request itself. `latency` is what the
    # provider took; `total_wait` is what the player experienced.
    queue_wait: float = 0.0
    total_wait: float = 0.0


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
class LoadTest:
    def __init__(self, args):
        self.args = args
        self.results: list = []
        self.done_indices: set = set()
        self.out_dir = os.path.abspath(args.out)
        self.jsonl_path = os.path.join(self.out_dir, "results.jsonl")
        self.samples_dir = os.path.join(self.out_dir, "samples")
        self._jsonl = None
        self._samples_saved = {}
        self.peak_inflight = {}

    # -- provider ------------------------------------------------------
    def build_provider(self):
        cls = PROVIDER_TYPES.get(self.args.provider)
        if cls is None:
            raise SystemExit(
                f"Unknown provider {self.args.provider!r}. "
                f"Known: {', '.join(sorted(PROVIDER_TYPES))}"
            )
        provider = cls(
            # Long, deliberately. See the module docstring: measuring the true
            # latency distribution and then deriving what the 20s production
            # budget does to it beats truncating the data at 20s.
            timeout=self.args.timeout,
            max_bytes=int(os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024))),
        )
        if self.args.model:
            provider.model = self.args.model
            if self.args.provider == "deepinfra":
                provider.url = f"https://api.deepinfra.com/v1/inference/{self.args.model}"
            elif self.args.provider == "replicate":
                provider.url = (
                    f"https://api.replicate.com/v1/models/{self.args.model}/predictions"
                )
        if not provider.is_configured():
            env_var = {
                "deepinfra": "DEEPINFRA_API_KEY",
                "replicate": "REPLICATE_API_TOKEN",
                "huggingface": "HF_TOKEN",
                "pollinations": "(none required)",
            }.get(self.args.provider, "its API key")
            message = (
                f"{self.args.provider} is not configured: {env_var} is not set "
                f"in .env or the environment."
            )
            # A --dry-run is for seeing the plan and the projected bill before
            # committing, which is exactly when you are most likely not to have
            # the key exported yet. Refusing to print the plan for want of a
            # credential no request will use would defeat the flag.
            if self.args.dry_run:
                print(f"NOTE: {message}\n      The plan below is still accurate; "
                      f"set it before running for real.\n")
            else:
                raise SystemExit(message)
        # The breaker exists to protect *players* from a failing provider. Here
        # a failing provider is the finding, so opening the breaker halfway
        # through would replace real measurements with a wall of skips and
        # hide exactly the data the run is for.
        provider.breaker.threshold = 10 ** 9
        return provider

    # -- persistence ---------------------------------------------------
    def open_output(self):
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.samples_dir, exist_ok=True)
        if self.args.resume and os.path.exists(self.jsonl_path):
            with open(self.jsonl_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.results.append(Result(**record))
                    self.done_indices.add(record["index"])
            print(f"Resuming: {len(self.done_indices)} request(s) already recorded.")
        # Append on resume, truncate otherwise. Written per-request rather
        # than at the end: a 700-image run against a slow provider takes tens
        # of minutes, and losing all of it to a Ctrl-C or a dropped VPN would
        # make the run something nobody repeats.
        self._jsonl = open(
            self.jsonl_path, "a" if self.args.resume else "w", encoding="utf-8"
        )

    def record(self, result: Result):
        self.results.append(result)
        self._jsonl.write(json.dumps(asdict(result)) + "\n")
        self._jsonl.flush()

    def maybe_save_sample(self, step: str, index: int, payload: bytes, fmt: str):
        """Write generated images to disk.

        Two modes, because the right answer depends on what an image costs.

        --save-all keeps every one. These are paid images: at 512x512 JPEG
        they are roughly 60 KB each, so a 700-image run is about 40 MB. That
        is nothing next to having paid for them and thrown them away, and a
        provider that degrades under load -- grey squares, repeated frames,
        the same seed over and over -- is visible in a contact sheet of 200
        and invisible in any latency number, because every one of those
        responses is a success by every metric this file records.

        The sampled default exists for the case where images are large or the
        run is huge. Filenames carry the request index, so either mode joins
        back to results.jsonl and the prompt that produced each image.
        """
        if not self.args.save_all:
            saved = self._samples_saved.get(step, 0)
            if saved >= self.args.samples_per_step:
                return
            self._samples_saved[step] = saved + 1
        ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(fmt, "bin")
        name = f"{self.args.provider}_{step}_{index:04d}.{ext}"
        with open(os.path.join(self.samples_dir, name), "wb") as fh:
            fh.write(payload)

    # -- one request ---------------------------------------------------
    async def one(self, client, provider, index, step, concurrency, prompt):
        started = time.time()
        clock = time.perf_counter()
        try:
            payload = await provider.generate(client, prompt)
        except ProviderError as exc:
            latency = time.perf_counter() - clock
            return Result(
                index=index, step=step, concurrency=concurrency, started_at=started,
                latency=round(latency, 3), ok=False,
                failure_bucket=classify_failure(str(exc)), error=str(exc)[:300],
                prompt=prompt,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            latency = time.perf_counter() - clock
            return Result(
                index=index, step=step, concurrency=concurrency, started_at=started,
                latency=round(latency, 3), ok=False,
                failure_bucket="adapter_crash",
                error=f"{type(exc).__name__}: {exc}"[:300], prompt=prompt,
            )

        latency = time.perf_counter() - clock
        fmt = identify_image(payload)
        # Bytes that are not an image are a failure even though the adapter
        # returned them. This is the HTML-error-page-with-HTTP-200 case.
        ok = fmt not in ("NOT_AN_IMAGE", "unrecognised")
        if ok:
            self.maybe_save_sample(step, index, payload, fmt)
        return Result(
            index=index, step=step, concurrency=concurrency, started_at=started,
            latency=round(latency, 3), ok=ok, bytes=len(payload), image_format=fmt,
            failure_bucket="" if ok else "shape",
            error="" if ok else f"adapter returned {len(payload)} bytes of {fmt}",
            prompt=prompt,
        )

    # -- schedules -----------------------------------------------------
    def plan(self):
        """Return [(step_name, concurrency, count), ...].

        Kept separate from execution so `--dry-run` can print exactly what a
        run will cost before it spends anything.
        """
        a = self.args
        if a.pattern == "burst":
            return [
                (f"burst{i + 1}", a.burst_size, a.burst_size) for i in range(a.bursts)
            ]
        if a.pattern == "sweep":
            steps = [int(s) for s in a.sweep.split(",") if s.strip()]
            # Names are suffixed when a concurrency repeats. Repeating one is
            # the correct way to model consecutive turns -- `--sweep 40,40,40,40
            # --per-step 175` is four turns of 175 at the app's real
            # concurrency, each queue draining before the next begins. Without
            # the suffix all four steps would share the key "c40" and quietly
            # overwrite each other in the per-step table.
            duplicated = len(set(steps)) != len(steps)
            return [
                (f"c{c}_{i + 1}" if duplicated else f"c{c}", c, a.per_step)
                for i, c in enumerate(steps)
            ]
        return [("steady", a.concurrency, a.count)]

    async def run_step(self, client, provider, step, concurrency, count, offset, prompts):
        """Fire `count` requests with at most `concurrency` in flight.

        For a burst step concurrency == count, so all of them start together --
        which is the point. A semaphore rather than chunked gathers keeps the
        in-flight number constant for the steady/sweep patterns instead of
        letting it sawtooth down to zero at every chunk boundary, which would
        understate the load the provider actually sees.
        """
        semaphore = asyncio.Semaphore(concurrency)
        completed = {"n": 0}
        # Measured, not assumed. The semaphore permits `concurrency`, but a
        # too-small connection pool, an fd limit or a thread-limiter stall
        # would mean far fewer were ever actually in flight -- and the report
        # would still say "175 concurrent". This is the receipt.
        inflight = {"now": 0, "peak": 0}
        total_to_do = sum(
            1 for i in range(offset, offset + count) if i not in self.done_indices
        )

        async def worker(index):
            if index in self.done_indices:
                return
            # Clock starts HERE, before the semaphore, not after it.
            #
            # It used to start inside, which silently excluded the wait for an
            # admission slot. At concurrency 40 with 175 requests that is the
            # difference between reporting 5.6s and 21.8s -- and the player is
            # waiting for the second number. A harness that measures only the
            # part after its own queue is measuring the provider's behaviour
            # while the question is the player's experience.
            enqueued = time.perf_counter()
            async with semaphore:
                admitted = time.perf_counter()
                inflight["now"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["now"])
                try:
                    result = await self.one(
                        client, provider, index, step, concurrency, prompts[index]
                    )
                finally:
                    inflight["now"] -= 1
            result.queue_wait = round(admitted - enqueued, 3)
            result.total_wait = round(result.queue_wait + result.latency, 3)
            self.record(result)
            completed["n"] += 1
            mark = "." if result.ok else "x"
            sys.stdout.write(mark)
            if completed["n"] % 50 == 0:
                sys.stdout.write(f" {completed['n']}/{total_to_do}\n")
            sys.stdout.flush()

        step_clock = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(offset, offset + count)))
        elapsed = time.perf_counter() - step_clock
        sys.stdout.write("\n")
        self.peak_inflight[step] = inflight["peak"]
        if inflight["peak"] < concurrency * 0.9 and total_to_do >= concurrency:
            print(f"    !! peak in flight was only {inflight['peak']} of "
                  f"{concurrency} requested.\n"
                  f"       This step measured OUR ceiling, not the provider's. "
                  f"Run\n"
                  f"       tools/check_client_concurrency.py --concurrency "
                  f"{concurrency} to find it.")
        else:
            print(f"    peak in flight: {inflight['peak']}/{concurrency}")
        return elapsed

    async def run(self):
        a = self.args
        plan = self.plan()
        total = sum(count for _, _, count in plan)
        prompts = build_prompts(total, a.seed)

        provider = self.build_provider()
        print(f"provider   {a.provider}")
        print(f"model      {provider.model}")
        print(f"endpoint   {getattr(provider, 'url', '(per-request)')}")
        print(f"timeout    {a.timeout}s (production primary is "
              f"{os.getenv('IMAGE_GEN_TIMEOUT_SECONDS', '20')}s -- see the report)")
        print(f"plan       {', '.join(f'{s}: {c} req @ {k} concurrent' for s, k, c in plan)}")
        print(f"total      {total} images")
        if a.cost_per_image:
            print(f"est. cost  ${total * a.cost_per_image:.2f} "
                  f"at ${a.cost_per_image}/image")
        print()

        if a.dry_run:
            print("--dry-run: nothing was sent.")
            return

        self.open_output()

        # Raise our own ceilings before anything is sent. If the client cannot
        # hold `peak` requests open, this run measures the client and blames
        # the provider -- and at a paid provider it does so having spent the
        # images. tools/check_client_concurrency.py proves these work offline.
        peak = max(k for _, k, _ in plan)
        print(f"client headroom for {peak} concurrent:")
        tune_runtime_for_concurrency(peak)
        print()

        step_timings = []
        self.provider = provider
        async with build_client(peak) as client:
            offset = 0
            for step, concurrency, count in plan:
                print(f"--- {step}: {count} requests at {concurrency} concurrent ---")
                if a.warmup:
                    print(f"    warming: {await warm_connections(client, provider, concurrency)}")
                elapsed = await self.run_step(
                    client, provider, step, concurrency, count, offset, prompts
                )
                step_timings.append((step, concurrency, count, elapsed))
                print(f"    {elapsed:.1f}s wall, "
                      f"{count / elapsed if elapsed else 0:.1f} images/s\n")
                offset += count
                if a.gap and (step, concurrency, count) != plan[-1]:
                    print(f"    waiting {a.gap}s before the next step "
                          f"(models the gap between turns)\n")
                    await asyncio.sleep(a.gap)

        if self._jsonl:
            self._jsonl.close()
        return self.report(step_timings)

    # -- reporting -----------------------------------------------------
    def report(self, step_timings):
        a = self.args
        results = self.results
        if not results:
            print("No results recorded.")
            return 1

        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        latencies = sorted(r.latency for r in ok)

        def pct(p):
            if not latencies:
                return 0.0
            k = max(0, min(len(latencies) - 1, int(round(p / 100 * (len(latencies) - 1)))))
            return latencies[k]

        prod_timeout = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "20"))
        turn_budget = float(os.getenv("TIME_PER_ROUND", "90"))
        # The number this whole exercise exists to produce. A request that
        # succeeded here in 34s is a request the live game would have killed
        # at 20s and failed over -- so the honest success rate for the event
        # is not the raw one.
        would_survive = [r for r in ok if r.latency <= prod_timeout]
        effective_rate = len(would_survive) / len(results) if results else 0.0
        raw_rate = len(ok) / len(results) if results else 0.0

        buckets = {}
        for r in bad:
            buckets[r.failure_bucket or "other"] = buckets.get(r.failure_bucket or "other", 0) + 1

        per_step = {}
        for r in results:
            entry = per_step.setdefault(
                r.step, {"n": 0, "ok": 0, "lat": [], "concurrency": r.concurrency}
            )
            entry["n"] += 1
            if r.ok:
                entry["ok"] += 1
                entry["lat"].append(r.latency)

        lines = []
        w = lines.append
        w("=" * 74)
        w(f"LOAD TEST REPORT -- {a.provider}")
        w("=" * 74)
        w(f"generated      {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        w(f"pattern        {a.pattern}")
        w(f"requests       {len(results)}")
        w("")
        w("-- outcome ---------------------------------------------------------")
        w(f"succeeded              {len(ok):>6}   ({raw_rate * 100:.1f}%)")
        w(f"failed                 {len(bad):>6}   ({(1 - raw_rate) * 100:.1f}%)")
        w(f"succeeded within {prod_timeout:g}s   {len(would_survive):>6}   "
          f"({effective_rate * 100:.1f}%)  <- what the live game would have got")
        if len(ok) != len(would_survive):
            w(f"                       {len(ok) - len(would_survive):>6}   succeeded here "
              f"but would be killed by IMAGE_GEN_TIMEOUT_SECONDS and failed over")
        w("")
        if latencies:
            w("-- latency of successful requests (seconds) ------------------------")
            w(f"min    {latencies[0]:>7.2f}")
            w(f"p50    {pct(50):>7.2f}")
            w(f"p90    {pct(90):>7.2f}")
            w(f"p95    {pct(95):>7.2f}")
            w(f"p99    {pct(99):>7.2f}")
            w(f"max    {latencies[-1]:>7.2f}")
            w(f"mean   {statistics.fmean(latencies):>7.2f}")
            w("")
        queued = [r for r in ok if r.total_wait and r.queue_wait > 0.05]
        if queued:
            totals = sorted(r.total_wait for r in ok if r.total_wait)
            waits = sorted(r.queue_wait for r in ok)

            def tp(p_, seq):
                k = max(0, min(len(seq) - 1, int(round(p_ / 100 * (len(seq) - 1)))))
                return seq[k]

            w("-- what a PLAYER actually waited -----------------------------------")
            w("  `latency` above is the provider's part only. When this harness")
            w("  admits fewer requests than it was given (--concurrency below the")
            w("  count), the rest wait for a slot first. The player waits for both.")
            w("")
            # The queue depth is stated because it is the number most likely to
            # be wrong. These figures describe ONE queue of `per_step` requests.
            # If the real system re-queues periodically -- Phase 1 admits one
            # request per team per turn, then starts fresh next turn -- then a
            # run whose step size exceeds a single turn is measuring a backlog
            # that never occurs, and the wait figures are pessimistic by the
            # ratio between the two.
            deepest = max((e["n"] for e in per_step.values()), default=0)
            w(f"  Measured against a queue {deepest} deep, admitted "
              f"{max((e['concurrency'] for e in per_step.values()), default=0)} "
              f"at a time.")
            w(f"  Phase 1 admits one request per team per TURN, so the real queue "
              f"is the team")
            w(f"  count. If {deepest} is larger than that, these waits are "
              f"pessimistic -- model")
            w(f"  consecutive turns with --pattern sweep --sweep 40,40,40,40 "
              f"--per-step <teams>.")
            w("")
            w(f"{'':<18}{'p50':>9}{'p95':>9}{'max':>9}")
            w(f"{'waiting for a slot':<18}{tp(50, waits):>9.2f}"
              f"{tp(95, waits):>9.2f}{waits[-1]:>9.2f}")
            w(f"{'provider request':<18}{pct(50):>9.2f}{pct(95):>9.2f}"
              f"{latencies[-1]:>9.2f}")
            w(f"{'PLAYER TOTAL':<18}{tp(50, totals):>9.2f}"
              f"{tp(95, totals):>9.2f}{totals[-1]:>9.2f}")
            w("")
            # The distinction that decides whether this passes: the production
            # timeout applies to the HTTP request only. ai_handling holds the
            # semaphore OUTSIDE the request, so time spent waiting for a slot
            # cannot trip IMAGE_GEN_TIMEOUT_SECONDS. What bounds it is the turn.
            w(f"  The {prod_timeout:g}s timeout applies to the request only -- "
              f"services/ai_handling.py")
            w(f"  holds the semaphore around the call, so queue time cannot trip it.")
            w(f"  What bounds the total is the turn: "
              f"{totals[-1]:.1f}s worst case against a "
              f"{turn_budget:g}s turn.")
            if totals[-1] > turn_budget:
                w(f"  THAT DOES NOT FIT.")
            else:
                w(f"  That fits, with {turn_budget - totals[-1]:.0f}s to spare.")
            w("")

        if buckets:
            w("-- failures by cause -----------------------------------------------")
            for bucket, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
                w(f"{bucket:<16}{n:>6}   {FIX_HINTS.get(bucket, '')}")
            w("")
            w("  sample messages:")
            seen = set()
            for r in bad:
                key = r.failure_bucket
                if key in seen:
                    continue
                seen.add(key)
                w(f"    [{key}] {r.error[:150]}")
            w("")
        w("-- per step --------------------------------------------------------")
        w(f"{'step':<10}{'conc':>6}{'n':>7}{'ok%':>8}{'p50':>8}{'p95':>8}{'max':>8}")
        for step, _, _, elapsed in step_timings or [(s, 0, 0, 0) for s in per_step]:
            e = per_step.get(step)
            if not e:
                continue
            lat = sorted(e["lat"])

            def sp(p):
                if not lat:
                    return 0.0
                k = max(0, min(len(lat) - 1, int(round(p / 100 * (len(lat) - 1)))))
                return lat[k]

            w(f"{step:<10}{e['concurrency']:>6}{e['n']:>7}"
              f"{(e['ok'] / e['n'] * 100 if e['n'] else 0):>7.1f}%"
              f"{sp(50):>8.2f}{sp(95):>8.2f}{(lat[-1] if lat else 0):>8.2f}")
        w("")
        if step_timings:
            w("-- throughput ------------------------------------------------------")
            for step, concurrency, count, elapsed in step_timings:
                w(f"{step:<10}{count:>5} images in {elapsed:>7.1f}s   "
                  f"{count / elapsed if elapsed else 0:>6.2f} images/s")
            w("")
        if ok:
            sizes = [r.bytes for r in ok]
            formats = {}
            for r in ok:
                formats[r.image_format] = formats.get(r.image_format, 0) + 1
            w("-- payload ---------------------------------------------------------")
            w(f"formats        {', '.join(f'{k} x{v}' for k, v in formats.items())}")
            w(f"size           min {min(sizes) / 1024:.1f} KB, "
              f"median {statistics.median(sizes) / 1024:.1f} KB, "
              f"max {max(sizes) / 1024:.1f} KB")
            # Egress matters: at IMAGE_DELIVERY=url these bytes land on the
            # game host's disk and are then served to players.
            per_event = statistics.median(sizes) * 3500
            w(f"projected      {per_event / 1024 / 1024 / 1024:.2f} GB written to "
              f"static/generated over a full 3500-image event")
            w("")
        # Actual spend, when the provider reports it. DeepInfra returns
        # inference_status.cost per request, which beats an estimate: an
        # estimate is a price list multiplied by a count, and both halves can
        # be wrong (the wrong tier, or requests that were retried internally).
        provider = getattr(self, "provider", None)
        observed_cost = getattr(provider, "observed_cost", 0.0)
        observed_n = getattr(provider, "observed_requests", 0)
        runtimes = sorted(getattr(provider, "observed_runtime_ms", []) or [])

        if observed_n:
            per_image = observed_cost / observed_n
            w("-- cost, as billed by the provider ----------------------------------")
            w(f"reported by    {observed_n} response(s) carrying inference_status.cost")
            w(f"this run       ${observed_cost:.4f}")
            w(f"per image      ${per_image:.4f}")
            w(f"full event     ${per_image * 3500:.2f}  (3500 images)")
            if a.cost_per_image:
                drift = abs(per_image - a.cost_per_image)
                if drift > a.cost_per_image * 0.1:
                    w(f"NOTE           --cost-per-image was ${a.cost_per_image:.4f}, "
                      f"but the provider billed ${per_image:.4f}.")
                    w(f"               Trust the provider. Rerun with "
                      f"--cost-per-image {per_image:.4f}.")
            w("")
        elif a.cost_per_image:
            w("-- cost (estimated) -------------------------------------------------")
            w(f"this run       ${len(results) * a.cost_per_image:.2f}")
            w(f"full event     ${3500 * a.cost_per_image:.2f}  (3500 images)")
            w("")

        # The split that decides which knob to turn. Provider-side inference
        # time comes from the response; wall latency is measured here. The
        # difference is queueing plus network -- ours and theirs.
        if runtimes and latencies:
            def rp(p_):
                k = max(0, min(len(runtimes) - 1,
                               int(round(p_ / 100 * (len(runtimes) - 1)))))
                return runtimes[k] / 1000.0
            w("-- where the time actually went ------------------------------------")
            w(f"{'':<16}{'p50':>9}{'p95':>9}")
            w(f"{'provider says':<16}{rp(50):>9.2f}{rp(95):>9.2f}   "
              f"(inference_status.runtime_ms)")
            w(f"{'we measured':<16}{pct(50):>9.2f}{pct(95):>9.2f}   (wall clock)")
            overhead50 = max(0.0, pct(50) - rp(50))
            w(f"{'queue + network':<16}{overhead50:>9.2f}"
              f"{max(0.0, pct(95) - rp(95)):>9.2f}")
            w("")
            if rp(50) > 0 and pct(50) > rp(50) * 4:
                w(f"  Most of the latency is NOT the model: it generates in "
                  f"{rp(50):.2f}s but takes")
                w(f"  {pct(50):.2f}s to come back. That is queueing at the provider.")
                w("")
                # Earlier versions asserted here that lowering concurrency
                # would make things worse. That is true of THROUGHPUT and false
                # of LATENCY, and measurement showed the difference: 175
                # requests at 40-concurrent drained in the same wall time as
                # 175 fired at once, but each request waited 5.6s instead of
                # 23s, because it queued behind 40 rather than 175. Admitting
                # fewer at a time does not make the provider faster -- it stops
                # our requests ageing in the provider's queue where the HTTP
                # timeout can reach them.
                w(f"  Throughput is set by the provider and admitting fewer at "
                  f"once will not raise it.")
                w(f"  Per-request latency is different: a shorter admission "
                  f"window means each")
                w(f"  request queues behind fewer others, and time spent waiting "
                  f"for one of OUR")
                w(f"  slots is not exposed to IMAGE_GEN_TIMEOUT_SECONDS the way "
                  f"time spent inside")
                w(f"  an open HTTP request is. Compare a burst run against a "
                  f"--pattern steady run")
                w(f"  at MAX_CONCURRENT_IMAGE_REQUESTS to see the effect on your "
                  f"own account.")
                w("")
            elif rp(50) > 0:
                w(f"  Latency is dominated by the model itself "
                  f"({rp(50):.2f}s of {pct(50):.2f}s), so there is")
                w(f"  little queueing at this concurrency.")
                w("")

        # --- verdict ---
        w("-- verdict ---------------------------------------------------------")
        failures = []
        if effective_rate < a.min_success_rate:
            failures.append(
                f"effective success rate {effective_rate * 100:.1f}% is below the "
                f"required {a.min_success_rate * 100:.0f}%"
            )
        if latencies and pct(95) > prod_timeout:
            failures.append(
                f"p95 latency {pct(95):.1f}s exceeds the {prod_timeout:g}s production "
                f"timeout -- more than 5% of turns would fail over"
            )
        if latencies and pct(99) > turn_budget:
            failures.append(
                f"p99 latency {pct(99):.1f}s exceeds the {turn_budget:g}s turn budget"
            )
        for bucket in ("auth", "billing", "bad_model", "bad_request"):
            if buckets.get(bucket):
                failures.append(
                    f"{buckets[bucket]} {bucket} failure(s) -- configuration, not load; "
                    f"fix before drawing any conclusion from this run"
                )
        if failures:
            w("NOT EVENT READY")
            for f in failures:
                w(f"  - {f}")
        else:
            w("PASS -- this provider sustained the event's traffic shape within budget.")
        w("=" * 74)

        text = "\n".join(lines)
        print()
        print(text)

        report_path = os.path.join(self.out_dir, "report.txt")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

        summary = {
            "provider": a.provider,
            "model": a.model or os.getenv(
                f"{a.provider.upper()}_MODEL", ""
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pattern": a.pattern,
            "requests": len(results),
            "succeeded": len(ok),
            "failed": len(bad),
            "raw_success_rate": round(raw_rate, 4),
            "effective_success_rate": round(effective_rate, 4),
            "production_timeout_seconds": prod_timeout,
            "latency": {
                "p50": pct(50), "p90": pct(90), "p95": pct(95),
                "p99": pct(99), "max": latencies[-1] if latencies else 0,
            },
            "failure_buckets": buckets,
            "per_step": {
                step: {
                    "concurrency": e["concurrency"],
                    "n": e["n"],
                    "ok": e["ok"],
                    "p95": (sorted(e["lat"])[int(0.95 * (len(e["lat"]) - 1))]
                            if e["lat"] else 0),
                }
                for step, e in per_step.items()
            },
            "observed_cost_usd": round(observed_cost, 4) if observed_n else None,
            "observed_cost_per_image": round(observed_cost / observed_n, 6) if observed_n else None,
            "provider_runtime_ms_p50": runtimes[len(runtimes) // 2] if runtimes else None,
            "peak_inflight_by_step": self.peak_inflight,
            "verdict": "PASS" if not failures else "NOT_EVENT_READY",
            "verdict_reasons": failures,
        }
        with open(os.path.join(self.out_dir, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")

        saved_files = []
        try:
            saved_files = os.listdir(self.samples_dir)
        except OSError:
            pass
        if saved_files:
            saved_bytes = sum(
                os.path.getsize(os.path.join(self.samples_dir, f))
                for f in saved_files
            )
            print(f"\nimages saved     {len(saved_files)} of {len(results)} "
                  f"({saved_bytes / 1024 / 1024:.1f} MB)"
                  + ("" if a.save_all else "  -- rerun with --save-all to keep them all"))
        print(f"\nper-request log  {self.jsonl_path}")
        print(f"summary          {os.path.join(self.out_dir, 'summary.json')}")
        print(f"report           {report_path}")
        print(f"sample images    {self.samples_dir}")
        return 0 if not failures else 1


FIX_HINTS = {
    "rate_limit": "lower MAX_CONCURRENT_IMAGE_REQUESTS or raise the account quota",
    "timeout": "model too slow for this concurrency; try a smaller/faster model",
    "capacity": "provider-side; promote the secondary or reduce burst size",
    "auth": "fix the API key before the event",
    "billing": "top up the account before the event",
    "bad_model": "the model slug is wrong",
    "bad_request": "our request payload shape is wrong",
    "shape": "our response parser is wrong -- services/providers.py",
    "transport": "network between the game host and the provider",
    "adapter_crash": "a bug in the adapter, not the provider",
}


# Documented defaults, per provider, matching how each is actually used.
PRESETS = {
    # Primary: model one whole round of real traffic -- 4 turns x 175 teams.
    "deepinfra": dict(pattern="burst", burst_size=175, bursts=4, gap=15.0),
    # Secondary: it only ever sees failover traffic, so what matters is not
    # "can it do 175" but "at what concurrency does it stop coping", which a
    # sweep answers and a flat burst does not.
    "replicate": dict(pattern="sweep", sweep="25,50,100,175", per_step=100, gap=20.0),
}


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--provider", required=True, choices=sorted(PROVIDER_TYPES))
    p.add_argument("--pattern", choices=("burst", "sweep", "steady"),
                   help="burst: N simultaneous bursts (models turn boundaries). "
                        "sweep: ramp concurrency to find the ceiling. "
                        "steady: fixed concurrency. Default: the provider preset.")
    p.add_argument("--burst-size", type=int, help="requests per burst (default 175 "
                                                  "= one per team)")
    p.add_argument("--bursts", type=int, help="number of bursts (default 4 = one round)")
    p.add_argument("--sweep", help="comma-separated concurrency steps, e.g. 25,50,100,175")
    p.add_argument("--per-step", type=int, help="requests per sweep step")
    p.add_argument("--count", type=int, default=100, help="total requests (steady only)")
    p.add_argument("--concurrency", type=int, default=40, help="in flight (steady only)")
    p.add_argument("--gap", type=float, help="seconds to wait between steps")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-request timeout for MEASUREMENT (default 120). Kept well "
                        "above the production 20s on purpose -- see the module docstring.")
    p.add_argument("--model", help="override the model slug")
    p.add_argument("--out", help="output directory (default loadtest-<provider>/)")
    p.add_argument("--resume", action="store_true",
                   help="continue a previous run, skipping requests already logged")
    p.add_argument("--seed", type=int, default=20260818, help="prompt RNG seed")
    p.add_argument("--samples-per-step", type=int, default=5,
                   help="images to keep per step for visual inspection "
                        "(ignored when --save-all is set)")
    p.add_argument("--save-all", action="store_true",
                   help="write EVERY generated image, not just a sample. You "
                        "paid for them; at 512x512 JPEG a 700-image run is "
                        "~40 MB. Filenames carry the request index so they "
                        "join to results.jsonl.")
    p.add_argument("--cost-per-image", type=float, default=0.0,
                   help="USD per image, for the cost projection")
    p.add_argument("--min-success-rate", type=float, default=0.97,
                   help="effective success rate required to pass (default 0.97)")
    p.add_argument("--no-warmup", dest="warmup", action="store_false", default=True,
                   help="skip opening pooled connections before each step. The "
                        "warm-up is free (unauthenticated GETs to the API root, "
                        "not generations) and keeps 200 simultaneous DNS lookups "
                        "and TLS handshakes out of the measured latency.")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and the cost estimate, send nothing")
    args = p.parse_args()

    preset = PRESETS.get(args.provider, dict(pattern="steady"))
    for key, value in preset.items():
        if getattr(args, key, None) in (None,):
            setattr(args, key, value)
    # Fill anything still unset so the namespace is complete for every pattern.
    args.pattern = args.pattern or "steady"
    args.burst_size = args.burst_size or 175
    args.bursts = args.bursts or 4
    args.sweep = args.sweep or "25,50,100,175"
    args.per_step = args.per_step or 100
    args.gap = 0.0 if args.gap is None else args.gap
    args.out = args.out or f"loadtest-{args.provider}"

    test = LoadTest(args)
    try:
        return asyncio.run(test.run()) or 0
    except KeyboardInterrupt:
        print("\n\nInterrupted. Progress is in "
              f"{test.jsonl_path} -- rerun with --resume to continue.")
        with contextlib.suppress(Exception):
            test._jsonl.close()
        return 130


if __name__ == "__main__":
    sys.exit(main())
