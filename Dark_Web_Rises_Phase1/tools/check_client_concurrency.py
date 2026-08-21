"""Prove THIS machine can hold N requests open at once -- before spending money.

    python tools/check_client_concurrency.py --concurrency 200

No network, no API key, no cost. Runs the load harness's real HTTP client
against a local server that counts how many requests are genuinely in flight
at the same moment, then reports whether the number the client achieved
matches the number it was asked for.

Why this is worth a separate tool
---------------------------------
If our side cannot sustain 200 concurrent requests, a paid run does not fail
loudly. It succeeds, reports plausible latency figures, and attributes our
queueing to the provider. You would conclude "DeepInfra is slow at 200" from a
measurement of your own laptop -- and you would have paid 200 images for it.

The specific ceilings, all of which are below 200 by default somewhere:

  anyio thread limiter (40)   httpx resolves DNS via a blocking getaddrinfo
                              on an anyio worker thread. The default pool is
                              40, so 200 connection attempts resolve names in
                              batches of 40.
  httpx connection pool       Defaults are generous in modern httpx, but an
                              explicitly-set pool smaller than the burst turns
                              concurrency into a queue *inside* the client,
                              where no timing in the report can see it.
  RLIMIT_NOFILE               One socket per in-flight HTTP/1.1 request. Hit
                              the limit and you get OSError partway through,
                              recorded as a provider transport failure.
  event loop                  On Windows, SelectorEventLoop caps at 512
                              handles. Python 3.8+ defaults to Proactor, which
                              does not -- but a project that sets the policy
                              explicitly could reintroduce it.

This tool exercises the same `tune_runtime_for_concurrency()` and
`build_client()` the real run uses, so a pass here is evidence about the real
run rather than about a similar-looking copy.

Exit code is 0 only if the achieved concurrency is within tolerance of what
was asked for, so it can gate the paid run.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401,E402  -- loads .env

from tools.image_load_test import (  # noqa: E402
    build_client, tune_runtime_for_concurrency,
)

# A real 1x1 PNG, so the response is something the harness would accept as an
# image rather than something it would correctly reject.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class CountingServer:
    """A local HTTP/1.1 server that holds each request open and counts overlap.

    Written directly on asyncio streams rather than with a framework, for two
    reasons: no extra dependency for something that has to run on a moderator's
    machine, and total control over when the response is written -- which is
    the entire measurement.
    """

    def __init__(self, hold_seconds: float):
        self.hold = hold_seconds
        self.now = 0
        self.peak = 0
        self.total = 0
        self.server = None
        self.port = None

    async def handle(self, reader, writer):
        try:
            # Read the request head. We do not care what it says; we only need
            # to consume it so the client's write completes.
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    asyncio.TimeoutError):
                return

            self.now += 1
            self.total += 1
            self.peak = max(self.peak, self.now)
            try:
                # Holding here is what makes overlap observable. Without it,
                # requests would complete fast enough that a client with a
                # concurrency of 1 could look identical to one with 200.
                await asyncio.sleep(self.hold)
            finally:
                self.now -= 1

            body = TINY_PNG
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: image/png\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: keep-alive\r\n\r\n" + body
            )
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass


async def run_check(concurrency: int, hold: float, tolerance: float):
    print("=" * 74)
    print(f"CLIENT CONCURRENCY CHECK -- can this machine hold {concurrency} "
          f"requests open?")
    print("=" * 74)
    print(f"python        {sys.version.split()[0]}")
    print(f"event loop    {type(asyncio.get_running_loop()).__name__}")
    try:
        import httpx
        print(f"httpx         {httpx.__version__}")
    except Exception:
        pass
    print()

    print("tuning client-side ceilings (the same call the real run makes):")
    tune_runtime_for_concurrency(concurrency)
    print()

    server = CountingServer(hold_seconds=hold)
    port = await server.start()
    url = f"http://127.0.0.1:{port}/generate"
    print(f"local server  {url}  (holds each request {hold:g}s)")
    print(f"firing        {concurrency} requests, all at once\n")

    # The same client the paid run uses. If this were a fresh
    # httpx.AsyncClient() with default settings, a pass here would say nothing
    # about the real run.
    async with build_client(concurrency) as client:
        client_inflight = {"now": 0, "peak": 0}

        async def one(i):
            client_inflight["now"] += 1
            client_inflight["peak"] = max(client_inflight["peak"],
                                          client_inflight["now"])
            try:
                return await client.post(url, json={"prompt": f"probe {i}"},
                                         timeout=120.0)
            except Exception as exc:
                return exc
            finally:
                client_inflight["now"] -= 1

        started = time.perf_counter()
        results = await asyncio.gather(*(one(i) for i in range(concurrency)))
        elapsed = time.perf_counter() - started

    await server.stop()

    failures = [r for r in results if isinstance(r, Exception)]
    ok = [r for r in results if not isinstance(r, Exception)
          and getattr(r, "status_code", 0) == 200]

    print("-- results ----------------------------------------------------------")
    print(f"requests sent                {concurrency}")
    print(f"succeeded                    {len(ok)}")
    print(f"failed                       {len(failures)}")
    print(f"wall time                    {elapsed:.2f}s")
    print(f"peak concurrent, client side {client_inflight['peak']}")
    print(f"peak concurrent, SERVER side {server.peak}   <- the one that counts")
    print()

    # The server's count is authoritative. The client-side counter increments
    # before the request is handed to httpx, so it would read `concurrency`
    # even if httpx then queued every one of them internally -- which is
    # precisely the failure being hunted.
    achieved = server.peak
    required = int(concurrency * tolerance)
    problems = []

    if achieved < required:
        problems.append(
            f"only {achieved} of {concurrency} requests were ever in flight "
            f"together (needed at least {required})"
        )
    if failures:
        kinds = {}
        for f in failures:
            kinds[type(f).__name__] = kinds.get(type(f).__name__, 0) + 1
        problems.append(f"{len(failures)} request(s) failed: {kinds}")
        for f in failures[:3]:
            print(f"  sample failure: {type(f).__name__}: {str(f)[:160]}")

    # If the client really ran them in parallel, wall time is about one hold
    # period. If it queued them in batches of B, wall time is
    # ceil(concurrency / B) hold periods -- so the ratio names the batch size.
    expected_parallel = hold * 1.5 + 5.0
    if elapsed > expected_parallel and achieved >= required:
        batches = max(1, round(elapsed / hold))
        problems.append(
            f"wall time {elapsed:.1f}s suggests roughly {batches} sequential "
            f"batches rather than one parallel wave, even though peak overlap "
            f"looked correct -- connection setup is being serialised somewhere"
        )

    print("-- verdict ----------------------------------------------------------")
    if problems:
        print(f"NOT READY for {concurrency} concurrent requests")
        for p in problems:
            print(f"  - {p}")
        print()
        print("  What to try, in order:")
        print("    1. Raise the file-descriptor limit (POSIX):")
        print(f"         ulimit -n {concurrency * 2 + 128}")
        print("    2. Check nothing else is holding the anyio thread pool down;")
        print("       tune_runtime_for_concurrency() raises it to "
              f"{concurrency + 32} at start-up.")
        print("    3. If failures are ConnectError/TimeoutException, the local")
        print("       loopback is saturating -- unlikely, but it would mean the")
        print("       machine cannot sustain this many sockets at all.")
        print("    4. Fall back to a lower burst size and say so in the report:")
        print(f"         --burst-size {max(25, achieved - 10)}")
        print()
        print("  Do NOT run the paid test until this passes. It would measure")
        print("  this machine and bill you for the privilege.")
        return 1

    print(f"READY -- {achieved} requests were genuinely in flight together, "
          f"in {elapsed:.1f}s.")
    print(f"  A paid burst of {concurrency} will measure the provider, not us.")
    print("=" * 74)
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--concurrency", type=int, default=200,
                   help="how many simultaneous requests to prove (default 200)")
    p.add_argument("--hold", type=float, default=3.0,
                   help="seconds the local server holds each request open. Must "
                        "be long enough that overlap is unambiguous (default 3)")
    p.add_argument("--tolerance", type=float, default=0.95,
                   help="fraction of --concurrency that must be reached to pass "
                        "(default 0.95)")
    args = p.parse_args()
    return asyncio.run(run_check(args.concurrency, args.hold, args.tolerance))


if __name__ == "__main__":
    sys.exit(main())
