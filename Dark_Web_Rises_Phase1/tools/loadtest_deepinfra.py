"""DeepInfra: 700 images, shaped like one real round of Phase 1.

    python tools/loadtest_deepinfra.py

That is the whole interface. It is a thin, opinionated front end over
`tools/image_load_test.py` so the run that matters is a single command with no
flags to get wrong at 2am the night before the event.

Why 700, and why in four bursts
-------------------------------
700 is not a round number picked for convenience. Phase 1 runs 175 teams of 4,
rounds are synchronised, and every team requests an image at the same moment
in each turn:

    175 teams x 4 turns = 700 images, arriving as 4 bursts of 175

Firing 700 requests at a steady 40-concurrent trickle would also produce
"700 images" and would tell you almost nothing, because the event never
produces that shape. The failure mode being hunted here is specifically what
happens to request #175 when requests #1-174 arrived in the same millisecond,
and then what happens to the *second* burst 15 seconds later once the
provider's rate-limit bucket has been drained by the first.

The 15-second gap is the realistic floor for a turn: a player has 90 seconds
but most submit early, and a shorter gap between bursts is a harsher test
than the event will produce, not a gentler one.

DeepInfra is the configured primary (IMAGE_PROVIDERS=deepinfra,replicate), so
the bar it has to clear is the higher one: it must serve essentially all of
this traffic inside the 20-second primary timeout, because every request it
misses becomes load on Replicate that Replicate was never sized for.

Cost
----
Pass --cost-per-image to get a projection for the full 3500-image event.
DeepInfra bills per image and the rate depends on the model, so it is not
hardcoded here -- a wrong constant is worse than no constant. Check the
current price for DEEPINFRA_MODEL and pass it:

    python tools/loadtest_deepinfra.py --cost-per-image 0.0009

Run --dry-run first if you want the plan and the bill before spending anything.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.image_load_test import main as engine_main  # noqa: E402

if __name__ == "__main__":
    # Defaults injected as argv so every one of them stays overridable on the
    # command line -- a preset you cannot argue with is a preset you end up
    # editing, and an edited test script is one nobody trusts.
    defaults = [
        "--provider", "deepinfra",
        "--pattern", "burst",
        "--burst-size", "175",   # one request per team
        "--bursts", "4",         # one full round
        "--gap", "15",           # realistic floor for the gap between turns
        "--timeout", "120",      # measure the real distribution, not a truncated one
        "--min-success-rate", "0.98",  # the primary carries the event
        "--out", "loadtest-deepinfra",
    ]
    supplied = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    merged = []
    for i in range(0, len(defaults), 2):
        if defaults[i] not in supplied:
            merged.extend(defaults[i:i + 2])
    sys.argv = [sys.argv[0]] + merged + sys.argv[1:]
    sys.exit(engine_main())
