"""
SSIM threshold sensitivity analysis CLI.

Runs the recommendation rule across a sweep of SSIM thresholds on the
corpus already persisted in data/results/ (populated by /upload or by
tools/batch_run.py).

Writes:
    data/exports/sensitivity_per_image.csv     — per-image × per-threshold
    data/exports/sensitivity_per_threshold.csv — stability summary per threshold

Usage:
    python -m tools.sensitivity
    python -m tools.sensitivity --min 0.90 --max 0.99 --step 0.005
    python -m tools.sensitivity --thresholds 0.90,0.93,0.95,0.97

The corpus-level summary is printed to stdout for quick inspection.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import sensitivity, storage  # noqa: E402


def _build_thresholds(min_t: float, max_t: float, step: float) -> List[float]:
    if step <= 0:
        raise SystemExit("--step must be > 0.")
    if min_t > max_t:
        raise SystemExit("--min must be <= --max.")
    out = []
    t = min_t
    # Accumulate with rounding to 4 decimals to avoid float drift
    while t <= max_t + 1e-9:
        out.append(round(t, 4))
        t += step
    return out


def _parse_threshold_list(s: str) -> List[float]:
    return sorted({round(float(x.strip()), 4) for x in s.split(",") if x.strip()})


def _write_per_image(path: Path, rows: list) -> None:
    cols = [
        "image_id", "threshold", "object_id", "format",
        "encoder_quality_param", "compressed_size_kb", "psnr", "ssim",
        "rule_fired",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_per_threshold(path: Path, rows: list) -> None:
    cols = [
        "threshold", "n_images", "n_fallback",
        "agreement_with_reference_count",
        "agreement_with_reference_rate_pct",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sensitivity",
        description="SSIM threshold sensitivity analysis over the persisted corpus.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--thresholds",
                   help="Comma-separated list (e.g., 0.90,0.93,0.95,0.97).")
    p.add_argument("--min", type=float, default=0.80)
    p.add_argument("--max", type=float, default=0.99)
    p.add_argument("--step", type=float, default=0.01)
    p.add_argument("--per-image-csv", type=Path,
                   default=storage.EXPORTS_DIR / "sensitivity_per_image.csv")
    p.add_argument("--per-threshold-csv", type=Path,
                   default=storage.EXPORTS_DIR / "sensitivity_per_threshold.csv")
    args = p.parse_args(argv)

    if args.thresholds:
        thresholds = _parse_threshold_list(args.thresholds)
    else:
        thresholds = _build_thresholds(args.min, args.max, args.step)

    if not thresholds:
        raise SystemExit("No thresholds produced. Check --min/--max/--step.")

    try:
        result = sensitivity.run_sweep(thresholds=thresholds)
    except ValueError as e:
        raise SystemExit(str(e))

    _write_per_image(args.per_image_csv, result["per_image_rows"])
    _write_per_threshold(args.per_threshold_csv, result["per_threshold_summary"])

    cs = result["corpus_summary"]
    print("Sensitivity sweep complete.")
    print(f"  thresholds scanned       : {len(thresholds)} "
          f"({thresholds[0]} .. {thresholds[-1]})")
    print(f"  reference threshold      : "
          f"{result['reference_threshold']}")
    print(f"  images in corpus         : {cs['n_images']}")
    print(f"  images with stable pick  : {cs['n_images_stable']} "
          f"({cs['stability_rate_pct']}%)")
    print(f"  mean distinct picks/img  : {cs['mean_distinct_picks_per_image']}")
    print(f"  fallback at ref thresh.  : {cs['ref_threshold_fallback_count']} "
          f"of {cs['n_images']}")
    print()
    print("Per-threshold summary:")
    print(f"  {'T':>6}  {'n_fallback':>10}  {'agree@ref':>10}  "
          f"{'agree_rate_pct':>14}")
    for row in result["per_threshold_summary"]:
        print(f"  {row['threshold']:>6.3f}  "
              f"{row['n_fallback']:>10}  "
              f"{row['agreement_with_reference_count']:>10}  "
              f"{row['agreement_with_reference_rate_pct']:>14.2f}")
    print()
    print(f"Wrote: {args.per_image_csv}")
    print(f"Wrote: {args.per_threshold_csv}")

    # Thesis-facing interpretation heuristic.
    print()
    _interpret(result)
    return 0


def _interpret(result: dict) -> None:
    """Print a short interpretation of the sweep for the thesis write-up."""
    cs = result["corpus_summary"]
    stable_rate = cs["stability_rate_pct"]

    print("Interpretation (for the thesis write-up):")
    if stable_rate >= 80.0:
        print(
            f"  {stable_rate}% of images give the SAME recommendation across "
            f"the entire threshold sweep. The 0.95 threshold is ROBUST — "
            f"the decision does not depend sensitively on the exact cutoff, "
            f"which neutralizes the 'why 0.95?' criticism."
        )
    elif stable_rate >= 50.0:
        print(
            f"  {stable_rate}% of images are stable across the sweep. The "
            f"threshold matters for the other {100.0 - stable_rate:.1f}%. "
            f"Investigate which image types flip — this is a finding."
        )
    else:
        print(
            f"  Only {stable_rate}% of images are stable. The threshold is "
            f"LOAD-BEARING. The thesis must justify the 0.95 choice with "
            f"external evidence (a calibration study, a reference benchmark, "
            f"or a sensitivity-weighted aggregation)."
        )
    # Fallback frequency at reference
    fb = cs["ref_threshold_fallback_count"]
    if fb > 0:
        rate = 100.0 * fb / max(cs["n_images"], 1)
        print(
            f"  {fb}/{cs['n_images']} images ({rate:.1f}%) hit the FALLBACK "
            f"branch at T=0.95 (no Pareto variant met the threshold). For "
            f"these, the recommendation is 'highest-SSIM Pareto variant,' "
            f"which is a different rule. Report this explicitly."
        )


if __name__ == "__main__":
    sys.exit(main())
