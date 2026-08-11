"""Benchmark CLIP comparator models on THIS machine, for speed and separation.

Two questions have to be answered together, and answering only the first is
how you end up with a fast scorer that ranks teams badly:

    1. How long does one comparison take?
    2. Does the model still separate a matching pair from a mismatched one?

A model that scores every pair at 0.62 is instant and useless -- the
leaderboard becomes noise. So this reports latency *and* the gap between
"same image", "related image" and "unrelated image".

    # defaults: compares RN50 against ViT-B-32
    python tools/clip_bench.py

    # your own candidates and your own images
    python tools/clip_bench.py --models RN50 ViT-B-32 RN50x4 \
        --reference static/images/some.jpg --generated static/generated/x.jpg

Scores from different models are NOT comparable to each other. Switching the
comparator changes the whole score distribution, so any threshold, ranking
expectation or "good score" intuition built on RN50 has to be rebuilt.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401  -- loads .env

# Candidate pretrained tags. open_clip names the weights separately from the
# architecture, and a wrong pairing raises rather than silently downloading
# something else.
#
# The -quickgelu variants are the correct pairing for OpenAI weights: every
# OpenAI CLIP model was trained with QuickGELU activations, so plain "RN50"
# or "ViT-B-32" with pretrained="openai" loads them into a model using
# standard GELU. open_clip only warns, so the mismatch produces
# plausible-looking but wrong embeddings -- for the benchmark that means
# comparing separation figures that neither model would actually produce.
DEFAULT_MODELS = [("RN50-quickgelu", "openai"), ("ViT-B-32-quickgelu", "openai")]


def parse_model(spec):
    """Accept "RN50" or "RN50:openai"."""
    if ":" in spec:
        arch, tag = spec.split(":", 1)
        return arch, tag
    return spec, "openai"


def make_images(reference, generated):
    """Return (name, PIL image) pairs to score against the reference."""
    from PIL import Image
    ref = Image.open(reference).convert("RGB")

    pairs = [("identical", ref.copy())]

    if generated and os.path.exists(generated):
        pairs.append(("real generated", Image.open(generated).convert("RGB")))

    # A degraded copy stands in for "related but not the same" without
    # needing a second real image: heavy blur plus a crop keeps the subject
    # but destroys detail, which is roughly what four description hops do.
    blurred = ref.copy().resize((max(1, ref.width // 8), max(1, ref.height // 8)))
    blurred = blurred.resize(ref.size)
    pairs.append(("degraded copy", blurred))

    # Structured noise: must score LOW, or the model is not discriminating.
    noise = Image.effect_noise(ref.size, 64).convert("RGB")
    pairs.append(("noise", noise))

    return ref, pairs


def bench(arch, tag, reference, generated, runs):
    import torch
    import open_clip

    print(f"\n{'=' * 68}\n{arch} ({tag})\n{'=' * 68}")

    load_started = time.perf_counter()
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(arch, pretrained=tag)
    except Exception as exc:
        print(f"  could not load: {exc}")
        return None
    model.eval()
    load_seconds = time.perf_counter() - load_started
    params = sum(p.numel() for p in model.visual.parameters())
    print(f"  load {load_seconds:6.1f}s   image-encoder params {params / 1e6:.0f}M")

    ref, pairs = make_images(reference, generated)

    def embed(image):
        tensor = preprocess(image).unsqueeze(0)
        # No autocast -- matches services/ai_handling._encode exactly. A
        # benchmark that does not use the shipping code path measures
        # something nobody runs. This one used to include autocast and
        # therefore reported ~7000 ms per encode, which made the model look
        # like the bottleneck when it never was.
        with torch.no_grad():
            features = model.encode_image(tensor)
            return features / features.norm(dim=-1, keepdim=True)

    # Warm up: the first pass through any torch model pays one-off
    # allocation and kernel-selection costs that are not representative.
    embed(ref)

    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        embed(ref)
        timings.append(time.perf_counter() - started)

    per_encode = statistics.median(timings)
    print(f"  encode: median {per_encode * 1000:6.1f} ms   "
          f"min {min(timings) * 1000:.1f}   max {max(timings) * 1000:.1f}")
    print(f"  -> one comparison  = {per_encode * 2 * 1000:6.1f} ms (two encodes)")
    print(f"  -> with ref cached = {per_encode * 1000:6.1f} ms")
    print(f"  -> 150 teams, cached, 1 thread = {per_encode * 150:.1f}s of CPU\n")

    ref_features = embed(ref)
    scores = {}
    for name, image in pairs:
        similarity = (ref_features @ embed(image).T).item()
        scores[name] = max(0.0, min(similarity, 1.0)) * 100
        print(f"    {name:<16} {scores[name]:6.2f}")

    spread = scores.get("identical", 0) - scores.get("noise", 0)
    print(f"\n  separation (identical - noise): {spread:.1f} points")
    if spread < 30:
        print("  WARNING: weak separation. A narrow range compresses the "
              "leaderboard\n  and makes rankings noise-dominated.")
    return {"model": f"{arch}:{tag}", "encode_ms": per_encode * 1000,
            "params_m": params / 1e6, "spread": spread}


def diagnose(reference, runs):
    """Find out WHERE the time goes, before anyone changes the model.

    A CLIP RN50 encode at 224x224 is tens of milliseconds on a modern CPU.
    If a measurement comes back in seconds, the model is not the problem and
    swapping it will not help. The usual suspects, isolated here:

      autocast     torch.amp.autocast('cpu') casts to bfloat16. On a CPU
                   without AMX or AVX512-BF16 the bf16 kernels are emulated,
                   and for a conv-heavy model that can be dramatically
                   SLOWER than plain fp32. This is in the shipping path
                   (_compare_image_sync), so it is the first thing to rule
                   out.
      threads      torch.set_num_threads(1) is the default here.
      preprocess   PIL resize of a 4K screenshot is not free, and is charged
                   to every comparison.
      quickgelu    OpenAI weights were trained with QuickGELU. Loading them
                   into a non-quickgelu model is a correctness bug, and the
                   two variants are worth timing side by side anyway.
    """
    import torch
    import open_clip
    from PIL import Image

    print(f"\n{'=' * 68}\nDIAGNOSTIC -- where does the time actually go?\n{'=' * 68}")
    print(f"  torch {torch.__version__}, {os.cpu_count()} logical CPUs")
    print(f"  torch.get_num_threads() = {torch.get_num_threads()}")

    raw = Image.open(reference).convert("RGB")
    print(f"  reference image is {raw.width}x{raw.height}")

    def time_it(label, fn, n=None):
        n = n or runs
        fn()  # warm up
        started = time.perf_counter()
        for _ in range(n):
            fn()
        each = (time.perf_counter() - started) / n
        print(f"    {label:<44}{each * 1000:9.1f} ms")
        return each

    for arch in ("RN50", "RN50-quickgelu"):
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                arch, pretrained="openai")
        except Exception as exc:
            print(f"\n  {arch}: could not load ({exc})")
            continue
        model.eval()
        print(f"\n  --- {arch} ---")

        time_it("preprocess only (PIL resize + normalise)",
                lambda: preprocess(raw).unsqueeze(0))

        tensor = preprocess(raw).unsqueeze(0)

        def encode_autocast():
            with torch.no_grad(), torch.amp.autocast("cpu"):
                return model.encode_image(tensor)

        def encode_plain():
            with torch.no_grad():
                return model.encode_image(tensor)

        # Labelled "old path": autocast was REMOVED from the shipping code
        # on 7 Aug after this diagnostic measured it 228x slower. It is still
        # timed here so the finding stays reproducible on new hardware --
        # bf16 is genuinely faster on a CPU with AMX, so this is a
        # per-machine question, not a settled one.
        with_ac = time_it("encode WITH autocast('cpu')  [OLD path, removed]", encode_autocast)
        without = time_it("encode WITHOUT autocast      [fp32, current]", encode_plain)

        if with_ac > without * 1.3:
            print(f"    ^^ autocast is {with_ac / without:.1f}x SLOWER here. "
                  f"Correctly absent from the shipping code.")
        elif without > with_ac * 1.3:
            print(f"    ^^ autocast is {without / with_ac:.1f}x FASTER on this "
                  f"CPU (it has usable bf16).\n       Worth reconsidering for "
                  f"THIS hardware only -- see services/ai_handling._encode.")
        else:
            print("    ^^ autocast makes little difference on this CPU.")

        for threads in sorted({1, 2, 4, os.cpu_count() or 4}):
            torch.set_num_threads(threads)
            time_it(f"encode, fp32, torch threads = {threads}", encode_plain, n=max(3, runs // 2))
        torch.set_num_threads(1)

    print("\n  Read this before changing the model: if fp32 or more threads\n"
          "  moves the number by an order of magnitude, the comparator was\n"
          "  never the bottleneck.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true",
                        help="isolate WHERE the time goes (autocast, threads, "
                             "preprocess) instead of comparing models. Run this "
                             "first if an encode takes more than ~200 ms.")
    parser.add_argument("--models", nargs="+", default=None,
                        help='architectures, e.g. RN50 ViT-B-32 or RN50:openai')
    parser.add_argument("--reference", default=None,
                        help="reference image (default: first in static/images)")
    parser.add_argument("--generated", default=None,
                        help="a real generated image, to score a true pair")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    reference = args.reference
    if not reference:
        folder = paths.REFERENCE_IMAGE_DIR
        candidates = [f for f in sorted(os.listdir(folder))
                      if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        if not candidates:
            print(f"No reference images in {folder}. Pass --reference.")
            return 1
        reference = os.path.join(folder, candidates[0])

    if args.diagnose:
        return diagnose(reference, args.runs)

    print(f"reference: {reference}")
    print(f"threads:   torch will use {os.getenv('TORCH_THREADS_PER_WORKER', '1')} "
          f"(TORCH_THREADS_PER_WORKER)")

    specs = [parse_model(s) for s in args.models] if args.models else DEFAULT_MODELS
    results = [r for r in
               (bench(arch, tag, reference, args.generated, args.runs) for arch, tag in specs)
               if r]

    if len(results) > 1:
        print(f"\n{'=' * 68}\nSUMMARY\n{'=' * 68}")
        print(f"  {'model':<22}{'encode ms':>11}{'params M':>10}{'separation':>12}")
        for r in sorted(results, key=lambda r: r["encode_ms"]):
            print(f"  {r['model']:<22}{r['encode_ms']:>11.1f}{r['params_m']:>10.0f}"
                  f"{r['spread']:>12.1f}")
        print("\n  Scores from different models are NOT comparable. Changing the\n"
              "  comparator changes the score distribution, so any expectation\n"
              "  of what a 'good' score looks like has to be rebuilt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
