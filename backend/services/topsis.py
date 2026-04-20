"""
TOPSIS scalarization over the Pareto set. Field names aligned with the
OAM export schema (compressed_size_kb, encoder_quality_param).
"""

from __future__ import annotations

import math
from typing import Dict, Any, List, Tuple

DEFAULT_WEIGHTS: Tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)

RULE_TOPSIS = "topsis_equal_weights"


def _normalize_column(col: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in col))
    if norm == 0.0:
        return [0.0 for _ in col]
    return [x / norm for x in col]


def topsis_rank(pareto_front: List[Dict[str, Any]],
                weights: Tuple[float, float, float] = DEFAULT_WEIGHTS
                ) -> List[Dict[str, Any]]:
    if not pareto_front:
        return []

    if len(pareto_front) == 1:
        only = dict(pareto_front[0])
        only["topsis_score"] = 1.0
        return [only]

    if abs(sum(weights) - 1.0) > 1e-9:
        raise ValueError(f"Weights must sum to 1.0, got {sum(weights)}.")

    sizes = [v["compressed_size_kb"] for v in pareto_front]
    psnrs = [v["psnr"]               for v in pareto_front]
    ssims = [v["ssim"]               for v in pareto_front]

    sizes_n = _normalize_column(sizes)
    psnrs_n = _normalize_column(psnrs)
    ssims_n = _normalize_column(ssims)

    w_size, w_psnr, w_ssim = weights
    sizes_w = [x * w_size for x in sizes_n]
    psnrs_w = [x * w_psnr for x in psnrs_n]
    ssims_w = [x * w_ssim for x in ssims_n]

    size_ideal, size_anti = min(sizes_w), max(sizes_w)   # size minimized
    psnr_ideal, psnr_anti = max(psnrs_w), min(psnrs_w)   # psnr maximized
    ssim_ideal, ssim_anti = max(ssims_w), min(ssims_w)   # ssim maximized

    ranked: List[Dict[str, Any]] = []
    for i, v in enumerate(pareto_front):
        dp = math.sqrt(
            (sizes_w[i] - size_ideal) ** 2
            + (psnrs_w[i] - psnr_ideal) ** 2
            + (ssims_w[i] - ssim_ideal) ** 2
        )
        dn = math.sqrt(
            (sizes_w[i] - size_anti) ** 2
            + (psnrs_w[i] - psnr_anti) ** 2
            + (ssims_w[i] - ssim_anti) ** 2
        )
        denom = dp + dn
        score = 0.0 if denom == 0.0 else dn / denom
        annotated = dict(v)
        annotated["topsis_score"] = round(score, 6)
        ranked.append(annotated)

    ranked.sort(key=lambda x: x["topsis_score"], reverse=True)
    return ranked


def recommend_topsis(pareto_front: List[Dict[str, Any]],
                     weights: Tuple[float, float, float] = DEFAULT_WEIGHTS
                     ) -> Dict[str, Any]:
    if not pareto_front:
        raise ValueError("Pareto front is empty — cannot recommend.")

    ranked = topsis_rank(pareto_front, weights)
    top = ranked[0]
    return {
        **top,
        "recommendation_rule_used": RULE_TOPSIS,
        "weights": {
            "compressed_size_kb": weights[0],
            "psnr": weights[1],
            "ssim": weights[2],
        },
        "recommendation_reason": (
            f"Highest TOPSIS closeness coefficient "
            f"(C={top['topsis_score']:.4f}) on the Pareto front "
            f"with weights size={weights[0]:.3f}, "
            f"psnr={weights[1]:.3f}, ssim={weights[2]:.3f}."
        ),
    }


def compare_decision_rules(primary: Dict[str, Any],
                           topsis: Dict[str, Any],
                           topsis_ranking: List[Dict[str, Any]]) -> Dict[str, Any]:
    same = (primary["format"] == topsis["format"]
            and primary["encoder_quality_param"] == topsis["encoder_quality_param"])
    primary_score = next(
        (t["topsis_score"] for t in topsis_ranking
         if t["format"] == primary["format"]
         and t["encoder_quality_param"] == primary["encoder_quality_param"]),
        None,
    )
    return {
        "agree": same,
        "primary": {"format": primary["format"],
                    "encoder_quality_param": primary["encoder_quality_param"]},
        "topsis":  {"format": topsis["format"],
                    "encoder_quality_param": topsis["encoder_quality_param"]},
        "topsis_score_of_primary": primary_score,
        "topsis_score_of_topsis_pick": topsis["topsis_score"],
    }
