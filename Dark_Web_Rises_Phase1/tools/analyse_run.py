"""Read a completed load test's results.jsonl and work out WHY it was slow.

    python tools/analyse_run.py loadtest-ceiling

Costs nothing -- it re-reads data you already paid for.

The question it answers
-----------------------
A load test says "p50 was 51 seconds". That is a symptom with at least three
different causes, and they need opposite responses:

  a FIFO queue          every request is accepted, then served one at a time.
                        Completions are evenly spaced. The provider has less
                        capacity allocated to you than you asked for. Sending
                        fewer at once changes nothing; you need more capacity.

  genuine slowness      requests run in parallel but each one is slow.
                        Completions cluster near the end, all at once. A
                        smaller/faster model fixes it.

  progressive collapse  early requests are fast, later ones get worse and
                        worse. The provider is degrading under the load. Back
                        off the concurrency.

Telling them apart needs the completion *timeline*, not the percentiles -- and
the percentiles are all the report prints. This reconstructs the timeline from
each request's start time and latency, which results.jsonl already records.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys


def load(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def bar(value, peak, width=44):
    if peak <= 0:
        return ""
    return "#" * max(0, min(width, round(value / peak * width)))


def analyse(rows, step=None):
    if step:
        rows = [r for r in rows if r.get("step") == step]
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        print("No successful requests to analyse.")
        return 1

    t0 = min(r["started_at"] for r in ok)
    # Reconstructed rather than recorded: results.jsonl stores when a request
    # started and how long it took, so the completion instant is the sum. That
    # is enough to rebuild both the completion timeline and the in-flight
    # count at any moment.
    finishes = sorted(r["started_at"] + r["latency"] - t0 for r in ok)
    starts = sorted(r["started_at"] - t0 for r in ok)
    span = finishes[-1] if finishes else 0.0

    print("=" * 70)
    print(f"COMPLETION TIMELINE -- {len(ok)} successful requests"
          + (f", step {step}" if step else ""))
    print("=" * 70)
    print(f"first request started    {starts[0]:.2f}s")
    print(f"last request started     {starts[-1]:.2f}s   "
          f"(all fired within {starts[-1] - starts[0]:.2f}s)")
    print(f"first completion         {finishes[0]:.2f}s")
    print(f"last completion          {finishes[-1]:.2f}s")
    print(f"throughput               {len(ok) / span:.2f}/s" if span else "")
    print()

    # --- the discriminator -------------------------------------------
    gaps = [b - a for a, b in zip(finishes, finishes[1:])]
    if len(gaps) >= 10:
        median_gap = statistics.median(gaps)
        # Coefficient of variation. A FIFO queue draining at a fixed service
        # rate produces near-identical gaps (CV close to 0); parallel work
        # finishing together produces a few big gaps and many near-zero ones
        # (CV well above 1).
        mean_gap = statistics.fmean(gaps)
        cv = (statistics.pstdev(gaps) / mean_gap) if mean_gap > 0 else 0.0
        print("-- how the completions were spaced ---------------------------------")
        print(f"median gap between completions   {median_gap * 1000:.0f} ms")
        print(f"mean gap                         {mean_gap * 1000:.0f} ms")
        print(f"variability (CV)                 {cv:.2f}")
        print()

        first_third = finishes[len(finishes) // 3]
        last_third = finishes[-len(finishes) // 3]
        early_rate = (len(finishes) // 3) / first_third if first_third else 0
        late_rate = (len(finishes) // 3) / (finishes[-1] - last_third) \
            if finishes[-1] > last_third else 0

        if cv < 0.9:
            verdict = "SERIAL FIFO QUEUE"
            detail = (
                f"Completions are evenly spaced at ~{median_gap*1000:.0f} ms.\n"
                f"  The provider accepted every request and then served them one\n"
                f"  after another at a fixed rate. This is a CAPACITY allocation,\n"
                f"  not a rate limit and not slowness -- there is no error to\n"
                f"  retry and no concurrency setting on our side that changes it.\n"
                f"  Sending fewer at once would not help; the queue is theirs."
            )
        elif late_rate < early_rate * 0.6:
            verdict = "PROGRESSIVE DEGRADATION"
            detail = (
                f"Early completions ran at ~{early_rate:.1f}/s, late ones at "
                f"~{late_rate:.1f}/s.\n"
                f"  The provider is getting worse as the burst goes on. Reduce\n"
                f"  the burst size and see if the rate holds."
            )
        else:
            verdict = "PARALLEL BUT SLOW"
            detail = (
                f"Completions cluster rather than spacing evenly, so requests\n"
                f"  did run concurrently -- each one is just slow. A smaller or\n"
                f"  faster model is the lever here."
            )
        print("-- diagnosis -------------------------------------------------------")
        print(f"{verdict}")
        print(f"  {detail}")
        print()

    # --- in-flight over time -----------------------------------------
    buckets = 24
    width = span / buckets if span else 1.0
    inflight = []
    for i in range(buckets):
        t = (i + 0.5) * width
        n = sum(1 for r in ok
                if (r["started_at"] - t0) <= t < (r["started_at"] - t0 + r["latency"]))
        inflight.append(n)
    peak = max(inflight) if inflight else 0

    print("-- requests in flight, over the run --------------------------------")
    for i, n in enumerate(inflight):
        print(f"  {i * width:>6.1f}s {n:>5}  {bar(n, peak)}")
    print()

    # --- completions per second --------------------------------------
    done = []
    for i in range(buckets):
        lo, hi = i * width, (i + 1) * width
        done.append(sum(1 for f in finishes if lo <= f < hi))
    dpeak = max(done) if done else 0
    print("-- completions per bucket ------------------------------------------")
    for i, n in enumerate(done):
        print(f"  {i * width:>6.1f}s {n:>5}  {bar(n, dpeak)}")
    print()

    # --- what this means for a turn ----------------------------------
    turn = float(os.getenv("TIME_PER_ROUND", "90"))
    primary = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "20"))
    rate = len(ok) / span if span else 0
    teams = int(os.getenv("EXPECTED_TEAMS", "175"))
    print("-- what that rate means for the event ------------------------------")
    print(f"observed sustained rate          {rate:.2f} images/s")
    if rate:
        print(f"time to serve {teams} teams          {teams / rate:.0f}s")
        print(f"served inside the {primary:g}s primary   "
              f"{min(teams, int(primary * rate))} of {teams} teams")
        print(f"served inside a {turn:g}s turn        "
              f"{min(teams, int(turn * rate))} of {teams} teams")
        print()
        print(f"to serve all {teams} inside {primary:g}s you need "
              f"{teams / primary:.1f} images/s "
              f"({teams / primary / rate:.1f}x this)")
    print("=" * 70)
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", help="a load-test output directory, "
                                   "e.g. loadtest-ceiling")
    p.add_argument("--step", help="only analyse this step (e.g. burst1, c100)")
    args = p.parse_args()

    path = args.run_dir
    if os.path.isdir(path):
        path = os.path.join(path, "results.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"No results.jsonl at {path}")
    return analyse(load(path), args.step)


if __name__ == "__main__":
    sys.exit(main())
