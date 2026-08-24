"""Pollinations: a gentle ramp, to see what the demo provider actually does.

    python tools/loadtest_pollinations.py

Pollinations is the DEMO provider (services/providers.py says so, loudly, on
every boot). This script exists because it is currently what .env points at,
and "we know it is not event-ready" is a weaker position than "we know it is
not event-ready, and here is the concurrency at which it stops coping".

Why this run is much smaller than the DeepInfra one
---------------------------------------------------
The DeepInfra script fires 4 bursts of 175 because DeepInfra is a paid service
that sells concurrency, and 175-at-once is the traffic it is being bought for.

Pollinations is a free public service with no contract, no published rate
limit, and no account to attach usage to. Firing 700 requests at 175
concurrency from one IP is not a measurement -- it is abuse, and the likely
result is a block, which teaches you that you got blocked rather than how the
service performs. It would also be a poor way to treat something being offered
for free.

So this ramps gently and stops early:

    25 requests at  5 concurrent
    25 requests at 10 concurrent
    25 requests at 20 concurrent
    25 requests at 40 concurrent      = 100 images total

That is enough to answer the only question worth asking about a demo provider:
does it degrade before it gets anywhere near event scale? If p95 is already
past the 20-second primary budget at 20 concurrent, you have your answer
without needing to go to 175 -- and 175 was never going to work anyway.

If you want to push further after seeing the first result, the steps are a
flag, not an edit:

    python tools/loadtest_pollinations.py --sweep 5,10,20,40,80 --per-step 25

Expect a NOT EVENT READY verdict. That is the correct answer for this
provider, and seeing the harness produce it against a real service -- rather
than against the synthetic one in tools/selftest_loadtest.py -- is worth
having before you trust its verdict on DeepInfra.

No API key, no cost, no account. That is the entire reason this provider is in
the tree, and it is why this is the one load test you can run today.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.image_load_test import main as engine_main  # noqa: E402

if __name__ == "__main__":
    defaults = [
        "--provider", "pollinations",
        "--pattern", "sweep",
        "--sweep", "5,10,20,40",
        "--per-step", "25",
        # Longer than the other scripts use. A free service under load can take
        # a very long time rather than failing fast, and the whole point of
        # measuring at a generous timeout is to see *how* long instead of
        # recording an undifferentiated "timeout".
        "--timeout", "180",
        # Room for rate-limit buckets to refill between steps, so each step
        # measures itself rather than the residue of the one before it.
        "--gap", "30",
        "--out", "loadtest-pollinations",
    ]
    supplied = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    merged = []
    for i in range(0, len(defaults), 2):
        if defaults[i] not in supplied:
            merged.extend(defaults[i:i + 2])
    sys.argv = [sys.argv[0]] + merged + sys.argv[1:]
    sys.exit(engine_main())
