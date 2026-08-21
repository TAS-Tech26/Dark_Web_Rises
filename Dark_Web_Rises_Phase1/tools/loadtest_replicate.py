"""Replicate: 400 images, ramped, to find where the fallback stops coping.

    python tools/loadtest_replicate.py

Why this run is shaped differently from the DeepInfra one
---------------------------------------------------------
Replicate is the secondary in IMAGE_PROVIDERS=deepinfra,replicate. It never
sees the full 175-per-turn burst in normal operation -- it only sees whatever
DeepInfra dropped. So the useful question is not "can it do 175 at once",
which is the wrong question for a fallback; it is "up to what concurrency
does it stay inside its 8-second fallback budget, and what happens past that".

A ramp answers that and a flat burst does not. 400 images across four steps:

    100 requests at  25 concurrent   <- a mild DeepInfra wobble
    100 requests at  50 concurrent   <- a bad minute
    100 requests at 100 concurrent   <- DeepInfra badly degraded
    100 requests at 175 concurrent   <- DeepInfra completely down

The last step is the one that decides the failover story. If Replicate holds
at 175, a total DeepInfra outage is survivable and the event continues on the
secondary. If it collapses at 100, then a DeepInfra outage is an event-level
incident and the mitigation has to be something other than "the chain will
handle it" -- lower MAX_CONCURRENT_IMAGE_REQUESTS so the burst is spread, or
accept degraded rounds and say so in the runbook.

The 20-second gap between steps lets rate-limit buckets refill, so each step
measures that step rather than the residue of the previous one.

The budget Replicate is judged against
--------------------------------------
As the secondary its timeout is IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS (8s), not
the 20s the primary gets -- the two together have to fit in a turn. The
engine's default check uses the primary timeout, so this script overrides the
threshold to the one Replicate actually runs under.

`Prefer: wait` note
-------------------
The adapter sends `Prefer: wait`, which asks Replicate to hold the connection
until the prediction finishes instead of returning a pending object to poll.
Under high concurrency that is exactly the behaviour most likely to change --
a loaded API is more likely to give up waiting and hand back a pending
prediction, at which point the adapter's polling fallback runs inside the
player's turn. This run exercises that path for real; if you see latencies
clustered just under the timeout at the higher steps, that is what you are
looking at.

Cost
----
Replicate bills per prediction-second and the rate depends on the model, so
nothing is hardcoded. flux-schnell is cheap; pass the current rate to get a
projection:

    python tools/loadtest_replicate.py --cost-per-image 0.003
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.image_load_test import main as engine_main  # noqa: E402

if __name__ == "__main__":
    fallback_timeout = os.getenv("IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS", "8")
    defaults = [
        "--provider", "replicate",
        "--pattern", "sweep",
        "--sweep", "25,50,100,175",
        "--per-step", "100",     # 4 x 100 = 400 images
        "--gap", "20",           # let rate-limit buckets refill between steps
        "--timeout", "120",      # measure the real distribution
        # A secondary that only serves failover traffic does not need the
        # primary's success rate -- it needs to be better than nothing. 0.90
        # is the level at which failover is still worth having.
        "--min-success-rate", "0.90",
        "--out", "loadtest-replicate",
    ]
    supplied = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    merged = []
    for i in range(0, len(defaults), 2):
        if defaults[i] not in supplied:
            merged.extend(defaults[i:i + 2])
    sys.argv = [sys.argv[0]] + merged + sys.argv[1:]

    # The engine derives its "would the live game have got this?" column from
    # IMAGE_GEN_TIMEOUT_SECONDS, which is the *primary's* budget. Replicate
    # runs on the fallback budget, so point the engine at that instead --
    # judging the secondary by the primary's 20s would flatter it by more
    # than a factor of two.
    os.environ["IMAGE_GEN_TIMEOUT_SECONDS"] = fallback_timeout
    print(f"Judging Replicate against its fallback budget of {fallback_timeout}s "
          f"(IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS), not the primary's 20s.\n")

    sys.exit(engine_main())
