"""
Sensitivity analysis for the SSIM threshold.

Purpose
-------
The primary decision rule (backend/services/recommendation.py) filters the
Pareto front by SSIM >= T, then picks minimum compressed_size_kb. T is
currently 0.95 — a design choice, not a finding. This module treats T as a
free parameter and asks: how does the chosen variant change as T sweeps?

Two outputs per corpus:
    1. per-image timeline      [(image_id, threshold, recommended_object_id,
                                 rule_fired)]
    2. stability summary       For each threshold:
                                 - n_images
                                 - n_using_fallback
                                 - n_distinct_picks_per_image_mean
                                 - agreement rate with T=0.95 (the deployed rule)

Interpretation
--------------
If the recommendation barely changes across a wide range of T, the current
threshold is defensible — the decision is robust to the exact cutoff.
If the recommendation thrashes, the threshold is load-bearing and the 0.95
choice requires external justification.

Both findings are publishable. The point is to surface which one you have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from backend.services import pareto, recommendation, storage


# Default sweep: 20 points from 0.80 to 0.99 inclusive.
DEFAULT_THRESHOLDS: List[float] = [round(0.80 + 0.01 * i, 2) for i in range(20)]

# The threshold currently deployed in recommendation.py. Agreement rates are
# computed against this reference. Keep this in sync if recommendation.py
# ever changes.
REFERENCE_THRESHOLD: float = 0.95


@dataclass(frozen=True)
class SweepPoint:
    image_id: str
    threshold: float
    object_id: str
    format: str
    encoder_quality_param: int
    compressed_size_kb: float
    psnr: float
    ssim: float
    rule_fired: str                # "primary" | "fallback"


def _object_id(image_id: str, fmt: str, q: int) -> str:
    return f"{image_id}_{fmt}_q{q}"


def sweep_thresholds_for_variants(
    image_id: str,
    variants: List[Dict],
    thresholds: List[float] = None,
) -> List[SweepPoint]:
    """
    For a single image's variant set, produce one SweepPoint per threshold.

    Re-runs the same recommendation logic (compute Pareto front, apply
    SSIM >= T, pick min size; fall back to max-SSIM Pareto variant if no
    variant qualifies) for each T in `thresholds`.

    Pure function — does not read or write storage.
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    if not variants:
        return []

    front = pareto.get_pareto_front(variants)
    out: List[SweepPoint] = []

    for T in thresholds:
        rec = recommendation.recommend(front, ssim_threshold=T)
        rule_fired = "primary" if rec.get("threshold_met") else "fallback"
        out.append(SweepPoint(
            image_id=image_id,
            threshold=T,
            object_id=_object_id(image_id, rec["format"],
                                 rec["encoder_quality_param"]),
            format=rec["format"],
            encoder_quality_param=rec["encoder_quality_param"],
            compressed_size_kb=rec["compressed_size_kb"],
            psnr=rec["psnr"],
            ssim=rec["ssim"],
            rule_fired=rule_fired,
        ))
    return out


def _persisted_variant_sets() -> List[Tuple[str, List[Dict]]]:
    """Load every persisted image's variant list from data/results/."""
    out: List[Tuple[str, List[Dict]]] = []
    for doc in storage.iter_all_results():
        variants = doc.get("variants", [])
        if variants:
            out.append((doc["image_id"], variants))
    return out


def run_sweep(
    thresholds: List[float] = None,
) -> Dict:
    """
    Corpus-level sweep.

    Returns:
        {
          "thresholds": [float, ...],
          "reference_threshold": 0.95,
          "per_image_rows": [SweepPoint dicts...],
          "per_threshold_summary": [
              {
                "threshold": float,
                "n_images": int,
                "n_fallback": int,
                "agreement_with_reference_count": int,
                "agreement_with_reference_rate_pct": float,
              }, ...
          ],
          "per_image_summary": [
              {
                "image_id": str,
                "n_distinct_picks": int,
                "picks_across_sweep": [object_id, ...],   # same length as thresholds
                "is_stable": bool,   # True iff n_distinct_picks == 1
              }, ...
          ],
          "corpus_summary": {
              "n_images": int,
              "n_images_stable": int,
              "stability_rate_pct": float,
              "mean_distinct_picks_per_image": float,
              "ref_threshold_fallback_count": int,
          },
        }

    Raises ValueError if no persisted images are found.
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    variant_sets = _persisted_variant_sets()
    if not variant_sets:
        raise ValueError(
            "No persisted images found. Run the pipeline on at least one "
            "image first (via /upload or tools/batch_run.py)."
        )

    # Per-image sweep
    per_image_rows: List[Dict] = []
    picks_by_image: Dict[str, List[str]] = {}
    fallback_by_image_and_threshold: Dict[Tuple[str, float], bool] = {}

    for image_id, variants in variant_sets:
        points = sweep_thresholds_for_variants(image_id, variants, thresholds)
        picks_by_image[image_id] = [p.object_id for p in points]
        for p in points:
            per_image_rows.append({
                "image_id": p.image_id,
                "threshold": p.threshold,
                "object_id": p.object_id,
                "format": p.format,
                "encoder_quality_param": p.encoder_quality_param,
                "compressed_size_kb": p.compressed_size_kb,
                "psnr": p.psnr,
                "ssim": p.ssim,
                "rule_fired": p.rule_fired,
            })
            fallback_by_image_and_threshold[(p.image_id, p.threshold)] = (
                p.rule_fired == "fallback"
            )

    # Reference picks (at T = REFERENCE_THRESHOLD)
    ref_picks: Dict[str, str] = {}
    if REFERENCE_THRESHOLD in thresholds:
        ref_index = thresholds.index(REFERENCE_THRESHOLD)
        for image_id, picks in picks_by_image.items():
            ref_picks[image_id] = picks[ref_index]
    else:
        # Compute reference picks separately if the reference wasn't in the sweep.
        for image_id, variants in variant_sets:
            pts = sweep_thresholds_for_variants(
                image_id, variants, [REFERENCE_THRESHOLD]
            )
            ref_picks[image_id] = pts[0].object_id

    # Per-threshold summary
    per_threshold_summary: List[Dict] = []
    for i, T in enumerate(thresholds):
        picks_at_T = {img: picks[i] for img, picks in picks_by_image.items()}
        agree = sum(1 for img, pick in picks_at_T.items()
                    if ref_picks.get(img) == pick)
        fallback_count = sum(
            1 for (img, t), is_fb in fallback_by_image_and_threshold.items()
            if t == T and is_fb
        )
        per_threshold_summary.append({
            "threshold": T,
            "n_images": len(picks_at_T),
            "n_fallback": fallback_count,
            "agreement_with_reference_count": agree,
            "agreement_with_reference_rate_pct": round(
                100.0 * agree / max(len(picks_at_T), 1), 2
            ),
        })

    # Per-image summary
    per_image_summary: List[Dict] = []
    for image_id, picks in picks_by_image.items():
        distinct = list(dict.fromkeys(picks))   # preserves order, dedups
        per_image_summary.append({
            "image_id": image_id,
            "n_distinct_picks": len(distinct),
            "picks_across_sweep": picks,
            "is_stable": len(distinct) == 1,
        })

    # Corpus summary
    n_stable = sum(1 for s in per_image_summary if s["is_stable"])
    mean_distinct = (
        sum(s["n_distinct_picks"] for s in per_image_summary)
        / max(len(per_image_summary), 1)
    )
    ref_fallback = sum(
        1 for (img, t), is_fb in fallback_by_image_and_threshold.items()
        if t == REFERENCE_THRESHOLD and is_fb
    )

    return {
        "thresholds": thresholds,
        "reference_threshold": REFERENCE_THRESHOLD,
        "per_image_rows": per_image_rows,
        "per_threshold_summary": per_threshold_summary,
        "per_image_summary": per_image_summary,
        "corpus_summary": {
            "n_images": len(variant_sets),
            "n_images_stable": n_stable,
            "stability_rate_pct": round(
                100.0 * n_stable / max(len(variant_sets), 1), 2
            ),
            "mean_distinct_picks_per_image": round(mean_distinct, 3),
            "ref_threshold_fallback_count": ref_fallback,
        },
    }
