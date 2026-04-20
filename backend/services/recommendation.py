"""
Single-pick recommendation from the Pareto set.

Primary rule (lexicographic, spec §8):
    1. Filter Pareto front by SSIM >= SSIM_THRESHOLD.
    2. Pick minimum compressed_size_kb.
    3. Tie-break: max SSIM, then max PSNR.

Fallback rule (when no Pareto variant meets the SSIM threshold):
    Return the Pareto variant with highest SSIM.
    Flag threshold_unmet=True in the exported row.

Both rules are identified by `recommendation_rule_used` in the output so the
thesis can audit which variants were picked under which regime.
"""

from __future__ import annotations

from typing import Dict, Any, List, Tuple

SSIM_THRESHOLD = 0.95

RULE_PRIMARY = "pareto_ssim>=0.95_min_size"
RULE_FALLBACK = "pareto_fallback_max_ssim"


def _primary_sort_key(v: Dict[str, Any]) -> Tuple[float, float, float]:
    # Minimize size; tie-break: max ssim, then max psnr.
    return (v["compressed_size_kb"], -v["ssim"], -v["psnr"])


def _fallback_sort_key(v: Dict[str, Any]) -> Tuple[float, float, float]:
    # Maximize ssim; tie-break: max psnr, then min size.
    return (-v["ssim"], -v["psnr"], v["compressed_size_kb"])


def recommend(pareto_front: List[Dict[str, Any]],
              ssim_threshold: float = SSIM_THRESHOLD) -> Dict[str, Any]:
    if not pareto_front:
        raise ValueError("Pareto front is empty — cannot recommend.")

    qualified = [v for v in pareto_front if v["ssim"] >= ssim_threshold]
    threshold_met = bool(qualified)

    if threshold_met:
        chosen = sorted(qualified, key=_primary_sort_key)[0]
        rule = RULE_PRIMARY
        reason = (
            f"Minimum compressed_size_kb among Pareto variants with "
            f"SSIM >= {ssim_threshold}."
        )
    else:
        chosen = sorted(pareto_front, key=_fallback_sort_key)[0]
        rule = RULE_FALLBACK
        reason = (
            f"No Pareto variant met SSIM >= {ssim_threshold}; "
            f"selected Pareto variant with highest SSIM."
        )

    return {
        **chosen,
        "ssim_threshold": ssim_threshold,
        "threshold_met": threshold_met,
        "threshold_unmet": not threshold_met,
        "recommendation_rule_used": rule,
        "recommendation_reason": reason,
    }


def explain(pareto_front: List[Dict[str, Any]],
            recommended: Dict[str, Any]) -> str:
    higher = [v for v in pareto_front if v["ssim"] > recommended["ssim"]]
    if not higher:
        return (
            f"Recommended: {recommended['format']} "
            f"q{recommended['encoder_quality_param']} — "
            f"SSIM={recommended['ssim']:.4f}, "
            f"size={recommended['compressed_size_kb']} KB. "
            f"No Pareto alternative offers higher SSIM."
        )
    alt = max(higher, key=lambda v: v["ssim"])
    size_delta = (
        (alt["compressed_size_kb"] - recommended["compressed_size_kb"])
        / max(alt["compressed_size_kb"], 1e-9)
    )
    ssim_delta = alt["ssim"] - recommended["ssim"]
    return (
        f"Recommended: {recommended['format']} "
        f"q{recommended['encoder_quality_param']}. "
        f"Moving to {alt['format']} q{alt['encoder_quality_param']} "
        f"would raise SSIM by {ssim_delta:.4f} at a file-size cost of "
        f"{size_delta*100:.1f}%."
    )
