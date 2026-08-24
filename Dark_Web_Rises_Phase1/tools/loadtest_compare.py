"""Turn two load-test runs into the one decision they exist to inform:
which provider should be primary, and is the chain event-ready at all?

    python tools/loadtest_deepinfra.py
    python tools/loadtest_replicate.py
    python tools/loadtest_compare.py

Reads the summary.json each run writes and prints a side-by-side plus a
recommended IMAGE_PROVIDERS line.

The comparison is not "whose p95 is lower"
------------------------------------------
The two runs are deliberately not the same test -- one is a flat 175-burst,
the other a ramp -- so their headline numbers are not directly comparable and
ranking them on p95 alone would be meaningless. What *is* comparable, and
what actually decides the ordering, is each provider's behaviour at the
concurrency the event produces (175) measured against the budget it would run
under in that position:

    as primary    20s (IMAGE_GEN_TIMEOUT_SECONDS)
    as secondary   8s (IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS)

A provider can be a fine primary and a poor secondary: 12 seconds is
comfortable inside a 20s budget and useless inside an 8s one. So each
provider is scored twice, once in each seat, and the ordering falls out of
that rather than out of a single number.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401,E402  -- loads .env for the budget values

PRIMARY_BUDGET = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "20"))
SECONDARY_BUDGET = float(os.getenv("IMAGE_GEN_FALLBACK_TIMEOUT_SECONDS", "8"))
# The concurrency the event actually generates: one request per team, all at
# once. Anything measured below this is not evidence about event day.
EVENT_CONCURRENCY = int(os.getenv("EXPECTED_TEAMS", "175"))


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def step_at_event_concurrency(summary):
    """The step whose concurrency is closest to (but not below) 175.

    A burst run has every step at 175; a sweep run has exactly one. Picking
    the closest-at-or-above rather than the maximum avoids silently reporting
    a 25-concurrent step as though it said something about event day.
    """
    steps = summary.get("per_step") or {}
    candidates = [
        (name, data) for name, data in steps.items()
        if data.get("concurrency", 0) >= EVENT_CONCURRENCY
    ]
    if not candidates:
        # Nothing reached event concurrency -- report the highest that ran, and
        # the caller flags it, because extrapolating upward from 100 to 175 is
        # exactly the assumption this whole exercise exists to stop making.
        if not steps:
            return None, None
        name, data = max(steps.items(), key=lambda kv: kv[1].get("concurrency", 0))
        return name, data
    # Worst-performing qualifying step, not the best: consecutive bursts
    # degrade, and the last burst of a round is the one a player experiences.
    return min(candidates, key=lambda kv: kv[1].get("ok", 0) / max(1, kv[1].get("n", 1)))


def assess(summary, budget, seat):
    """Would this provider hold up in `seat` (primary/secondary)?"""
    name, step = step_at_event_concurrency(summary)
    if step is None:
        return None
    ok_rate = step.get("ok", 0) / max(1, step.get("n", 1))
    p95 = step.get("p95", 0)
    return {
        "seat": seat,
        "step": name,
        "concurrency": step.get("concurrency"),
        "reached_event_concurrency": step.get("concurrency", 0) >= EVENT_CONCURRENCY,
        "success_rate": ok_rate,
        "p95": p95,
        "within_budget": p95 <= budget,
        # The number that matters: requests that both succeeded and did so
        # fast enough to count in that seat.
        "usable_rate": ok_rate if p95 <= budget else ok_rate * 0.0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deepinfra", default="loadtest-pollinations/summary.json")
    #ap.add_argument("--replicate", default="loadtest-replicate/summary.json")
    args = ap.parse_args()

    runs = {"deepinfra": load(args.deepinfra), "replicate": load(args.replicate)}
    missing = [name for name, data in runs.items() if data is None]
    if missing:
        raise SystemExit(
            f"No results for: {', '.join(missing)}.\n"
            "Run tools/loadtest_deepinfra.py and tools/loadtest_replicate.py first."
        )

    print("=" * 74)
    print("PROVIDER COMPARISON")
    print("=" * 74)
    print(f"event concurrency  {EVENT_CONCURRENCY} simultaneous requests "
          f"(one per team, per turn)")
    print(f"primary budget     {PRIMARY_BUDGET:g}s")
    print(f"secondary budget   {SECONDARY_BUDGET:g}s")
    print()
    print(f"{'':<14}{'requests':>10}{'raw ok%':>10}{'effective%':>12}"
          f"{'p95':>9}{'verdict':>18}")
    for name, data in runs.items():
        print(f"{name:<14}{data['requests']:>10}"
              f"{data['raw_success_rate'] * 100:>9.1f}%"
              f"{data['effective_success_rate'] * 100:>11.1f}%"
              f"{data['latency']['p95']:>9.2f}"
              f"{data['verdict']:>18}")
    print()

    print("-- behaviour at event concurrency ------------------------------------")
    seats = {}
    for name, data in runs.items():
        as_primary = assess(data, PRIMARY_BUDGET, "primary")
        as_secondary = assess(data, SECONDARY_BUDGET, "secondary")
        seats[name] = (as_primary, as_secondary)
        if as_primary is None:
            print(f"{name:<14} no per-step data")
            continue
        flag = "" if as_primary["reached_event_concurrency"] else \
            f"  (!! only reached {as_primary['concurrency']} concurrent -- " \
            f"this run does not cover event day)"
        print(f"{name:<14} step {as_primary['step']} @ "
              f"{as_primary['concurrency']} concurrent: "
              f"{as_primary['success_rate'] * 100:.1f}% ok, p95 {as_primary['p95']:.2f}s")
        print(f"{'':<14}   as primary   ({PRIMARY_BUDGET:g}s): "
              f"{'OK' if as_primary['within_budget'] else 'TOO SLOW'}")
        print(f"{'':<14}   as secondary ({SECONDARY_BUDGET:g}s): "
              f"{'OK' if as_secondary['within_budget'] else 'TOO SLOW'}")
        if flag:
            print(flag)
    print()

    # --- recommendation ---
    print("-- recommendation ----------------------------------------------------")
    viable_primary = [
        name for name, (p, _) in seats.items()
        if p and p["within_budget"] and p["success_rate"] >= 0.95
    ]
    viable_secondary = [
        name for name, (_, s) in seats.items()
        if s and s["within_budget"] and s["success_rate"] >= 0.80
    ]

    if not viable_primary:
        print("NO VIABLE PRIMARY.")
        print("  Neither provider served 175 simultaneous requests inside "
              f"{PRIMARY_BUDGET:g}s at a 95% success rate.")
        print("  Options, in order of how much they cost you:")
        print("    1. Pick a faster model (fewer steps / smaller model) and rerun.")
        print("    2. Lower MAX_CONCURRENT_IMAGE_REQUESTS so the burst is spread")
        print("       across the turn instead of arriving at once -- costs latency")
        print("       for the teams at the back of the queue, not correctness.")
        print("    3. Raise IMAGE_GEN_TIMEOUT_SECONDS, but only as far as")
        print(f"       {PRIMARY_BUDGET:g} + {SECONDARY_BUDGET:g} stays comfortably "
              "inside TIME_PER_ROUND.")
        return 1

    # Best primary = highest success rate at event concurrency.
    order = sorted(
        viable_primary, key=lambda n: -seats[n][0]["success_rate"]
    )
    rest = [n for n in seats if n not in order]
    chain = order + [n for n in rest if n in viable_secondary] + \
            [n for n in rest if n not in viable_secondary]

    print(f"IMAGE_PROVIDERS={','.join(chain)}")
    print()
    if chain[0] != "deepinfra":
        print(f"  NOTE: this is a change from the documented default. {chain[0]} "
              f"outperformed deepinfra at event concurrency.")
        print("  Reordering IMAGE_PROVIDERS in .env is a configuration change,")
        print("  not a code change -- that is what the chain is for.")
    if len(chain) > 1 and chain[1] not in viable_secondary:
        print(f"  WARNING: {chain[1]} did not meet the secondary budget of "
              f"{SECONDARY_BUDGET:g}s at event concurrency.")
        print("  Failover exists but would mostly time out, so treat a primary")
        print("  outage as an event-level incident rather than something the")
        print("  chain absorbs. Say so in the runbook.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
