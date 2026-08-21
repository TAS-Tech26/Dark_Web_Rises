"""Prove the load harness measures correctly, without spending a penny.

    python tools/selftest_loadtest.py

Runs `tools/image_load_test.py` end to end against a synthetic provider whose
latency distribution and failure mix are *known in advance*, then checks that
the report the harness produced matches what was injected.

Why this exists
---------------
A load test is a measuring instrument, and an uncalibrated instrument is worse
than none -- it produces confident numbers that are wrong, and you act on
them. The specific ways this harness could lie are all plausible:

  * concurrency not actually reached, because httpx's connection pool is
    smaller than the burst, so "175 concurrent" is really 10 concurrent and
    the latency figures are of a queue rather than of the provider;
  * latency measured around the semaphore wait rather than around the
    request, inflating every number under load;
  * failures silently swallowed, so the success rate flatters the provider;
  * the p95 helper off by one at small n.

Each of those produces a plausible-looking report. This script makes the
instrument disagree with reality out loud instead.

It also doubles as a way to see what the real output looks like before
committing to a 700-image billed run.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401,E402

from services.providers import ImageProvider, ProviderError, PROVIDER_TYPES  # noqa: E402
import tools.image_load_test as engine  # noqa: E402

# A real 1x1 PNG. Using genuine bytes rather than b"fake" matters: the harness
# identifies formats from magic bytes and would correctly reject a placeholder,
# so a fake payload would test the rejection path instead of the success path.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

# What we inject, and therefore what the report must recover.
#
# These are *counts*, not probabilities, and that distinction is the point.
# An earlier version of this file rolled a seeded RNG per request and asserted
# the measured rate landed within a few points of the nominal one. It did not,
# and the check failed -- correctly, but uselessly: with n=120 a nominal 10%
# lands anywhere from 5% to 17% by chance, so the assertion was really
# measuring the seed. A calibration harness that is itself statistical cannot
# tell you whether the instrument or the sample is at fault, which defeats the
# purpose. So the outcome mix is now exact by construction: a fixed multiset,
# shuffled for ordering only, one outcome consumed per request. Any deviation
# in the report is then unambiguously the harness.
TOTAL = 120
N_FAIL = 12                # exactly 10%
N_SLOW = 6                 # exactly  5% -- succeed, but past the prod timeout
N_NORMAL = TOTAL - N_FAIL - N_SLOW
FAILURE_RATE = N_FAIL / TOTAL
SLOW_TAIL_RATE = N_SLOW / TOTAL
SLOW_TAIL_LATENCY = 25.0
LATENCY_MEAN = 2.0
LATENCY_SPREAD = 0.6

_peak_in_flight = {"now": 0, "max": 0}


class SyntheticProvider(ImageProvider):
    """Known latency, known failure mix, no network.

    Deliberately a real ImageProvider subclass rather than a mock object: the
    harness calls `provider.generate(client, prompt)` and reads
    `provider.breaker`, `provider.model` and `provider.is_configured()`, so a
    duck-typed stand-in could pass here and the real adapters still fail.
    """

    def __init__(self, timeout, max_bytes):
        super().__init__(name="synthetic", timeout=timeout, max_bytes=max_bytes)
        self.model = "synthetic/known-distribution"
        self.url = "memory://synthetic"

        rng = random.Random(7)
        # Exact mix, shuffled so the outcomes are spread across both bursts
        # rather than all the failures landing in the first one.
        outcomes = (["slow"] * N_SLOW + ["fail"] * N_FAIL + ["ok"] * N_NORMAL)
        rng.shuffle(outcomes)
        self._outcomes = outcomes
        # Latencies are pre-drawn too, so the p50 assertion is against a
        # distribution that is fixed rather than re-rolled each run.
        self._latencies = [
            max(0.05, rng.gauss(LATENCY_MEAN, LATENCY_SPREAD)) for _ in range(TOTAL)
        ]
        self._next = 0

    def is_configured(self):
        return True

    def _take(self):
        """Claim the next outcome.

        Safe without a lock: this runs on a single event loop and there is no
        await between the read and the write, so no other coroutine can
        interleave. (It would not be safe across threads, which is why this
        is stated rather than assumed.)
        """
        index = self._next
        self._next += 1
        return index, self._outcomes[index % TOTAL], self._latencies[index % TOTAL]

    async def generate(self, client, prompt):
        index, outcome, latency = self._take()
        _peak_in_flight["now"] += 1
        _peak_in_flight["max"] = max(_peak_in_flight["max"], _peak_in_flight["now"])
        try:
            if outcome == "slow":
                # Succeeds, but slower than the production timeout. This is the
                # case the harness exists to distinguish from an outright
                # failure, so it must survive to the report as a success that
                # would nonetheless have been killed live.
                await asyncio.sleep(SLOW_TAIL_LATENCY)
                return TINY_PNG
            if outcome == "fail":
                await asyncio.sleep(0.2)
                raise ProviderError("synthetic returned HTTP 429")
            await asyncio.sleep(latency)
            return TINY_PNG
        finally:
            _peak_in_flight["now"] -= 1


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


def main():
    # Registered rather than special-cased inside the engine: the engine has no
    # test mode and should not grow one, because a code path that only runs in
    # tests is a code path that is not the one shipping.
    PROVIDER_TYPES["synthetic"] = SyntheticProvider

    out_dir = tempfile.mkdtemp(prefix="dwr-selftest-")
    bursts = 2
    burst_size = TOTAL // bursts
    total = TOTAL

    # The engine reads the production budget from the environment; pin it so
    # the assertions below are about the harness, not about whatever .env says.
    os.environ["IMAGE_GEN_TIMEOUT_SECONDS"] = "20"
    os.environ["TIME_PER_ROUND"] = "90"

    print(f"Injecting: {total} requests, {FAILURE_RATE:.0%} hard failures, "
          f"{SLOW_TAIL_RATE:.0%} slow-tail successes at {SLOW_TAIL_LATENCY:g}s,\n"
          f"           the rest ~N({LATENCY_MEAN}, {LATENCY_SPREAD}) seconds, "
          f"in {bursts} bursts of {burst_size}.\n")

    sys.argv = [
        "image_load_test.py",
        "--provider", "synthetic",
        "--pattern", "burst",
        "--burst-size", str(burst_size),
        "--bursts", str(bursts),
        "--gap", "0",
        "--timeout", "60",
        "--out", out_dir,
        "--samples-per-step", "2",
    ]
    started = time.perf_counter()
    engine.main()
    wall = time.perf_counter() - started

    with open(os.path.join(out_dir, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)

    print("\n" + "=" * 74)
    print("CALIBRATION CHECKS")
    print("=" * 74)
    ok = True

    ok &= check(
        "every request accounted for",
        summary["requests"] == total,
        f"reported {summary['requests']}, expected {total}",
    )

    # The one that catches a too-small connection pool. If the harness only
    # ever had 10 requests in flight, everything else it reports is a
    # measurement of its own queue.
    ok &= check(
        "burst concurrency actually reached the provider",
        _peak_in_flight["max"] >= burst_size * 0.9,
        f"peak in flight was {_peak_in_flight['max']}, expected ~{burst_size}",
    )

    # Exact, not approximate: the injected mix is a fixed multiset (see the
    # comment on N_FAIL), so anything other than the exact count is the
    # harness losing or inventing a result.
    ok &= check(
        "failure count recovered exactly",
        summary["failed"] == N_FAIL,
        f"measured {summary['failed']}, injected {N_FAIL} "
        f"({summary['failed'] / summary['requests']:.1%} vs {FAILURE_RATE:.1%})",
    )

    ok &= check(
        "rate-limit failures bucketed correctly",
        summary["failure_buckets"].get("rate_limit", 0) == summary["failed"],
        f"buckets: {summary['failure_buckets']}",
    )

    # Latency must be of the request, not of the request plus its wait for a
    # semaphore slot. In a pure burst there is no wait, so p50 should land on
    # the injected mean; if it lands well above, the timer is in the wrong place.
    p50 = summary["latency"]["p50"]
    ok &= check(
        "p50 latency matches the injected distribution",
        abs(p50 - LATENCY_MEAN) < 0.5,
        f"measured p50 {p50:.2f}s, injected mean {LATENCY_MEAN:.2f}s",
    )

    # The headline distinction: slow successes are successes here and
    # failures in production. The gap between the two rates must be exactly
    # the slow-tail count.
    raw = summary["raw_success_rate"]
    effective = summary["effective_success_rate"]
    gap_requests = round((raw - effective) * summary["requests"])
    ok &= check(
        "slow-tail successes counted raw but excluded from effective",
        gap_requests == N_SLOW,
        f"raw {raw:.1%}, effective {effective:.1%}, "
        f"gap {gap_requests} request(s) vs injected {N_SLOW}",
    )

    ok &= check(
        "verdict rejects a provider this unreliable",
        summary["verdict"] == "NOT_EVENT_READY",
        f"verdict was {summary['verdict']}",
    )

    # Bursts run concurrently, so wall time should be roughly the slow tail
    # once per burst -- not the sum of every request's latency. A serialised
    # harness would take minutes here.
    serial_estimate = total * LATENCY_MEAN
    ok &= check(
        "requests ran concurrently, not serially",
        wall < serial_estimate * 0.35,
        f"{wall:.1f}s wall vs {serial_estimate:.0f}s if serialised",
    )

    per_request = sum(
        1 for _ in open(os.path.join(out_dir, "results.jsonl"), encoding="utf-8")
    )
    ok &= check(
        "per-request log is complete and resumable",
        per_request == total,
        f"{per_request} lines, expected {total}",
    )

    samples = os.listdir(os.path.join(out_dir, "samples"))
    ok &= check(
        "sample images written per step",
        len(samples) == 2 * bursts,
        f"{len(samples)} files: {samples}",
    )

    print("=" * 74)
    if ok:
        print("Harness calibrated: the report matches the injected reality.\n")
    else:
        print("HARNESS IS LYING -- do not trust a real run until this passes.\n")
    shutil.rmtree(out_dir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
