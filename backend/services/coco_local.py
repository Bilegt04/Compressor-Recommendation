"""
Local approximation of MIAU's COCO Y0 ranking, for in-app demonstration.

Method
------
COCO Y0 in its classical MY-X / MIAU formulation is an OLS-based estimator
over a ranked Object-Attribute Matrix:

    1. Rank each attribute column 1..N with direction applied
       (minimized attributes ranked ascending — rank 1 = smallest value;
        maximized attributes ranked descending — rank 1 = largest value).
    2. For each object, y_i = sum of its direction-adjusted ranks. This
       serves as the "ideal" response.
    3. Fit y = X * b + e, where X is the ranked matrix (objects × attributes)
       and y is as in step 2.
    4. The fitted value ŷ_i is the Becslés for object i.
    5. Objects are ranked by ŷ_i descending (higher = better).

This produces a ranking that matches MIAU's output on small problems under
the standard COCO convention. For larger / degenerate cases (e.g., all
attributes perfectly correlated), the OLS fit can become unstable; we fall
back to the direct rank-sum y_i in those cases.

IMPORTANT
---------
This is an APPROXIMATION. I have not calibrated against actual MIAU output.
Until the student pastes a real MIAU output and we verify Becslés values
match, this module is labeled "approximated" in every user-facing surface.
If calibration reveals a discrepancy, the fix point is `_fit_y0`.

No network calls. Pure computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


# Attribute direction: "min" means smaller is better, "max" means larger.
ATTRIBUTE_DIRECTION: Dict[str, str] = {
    "compressed_size_kb": "min",
    "size_reduction_pct": "max",
    "psnr": "max",
    "ssim": "max",
}

OAM_MINIMAL: List[str] = ["compressed_size_kb", "psnr", "ssim"]


@dataclass(frozen=True)
class CocoLocalResult:
    """One ranked variant with its Becslés estimate."""
    object_id: str
    format: str
    encoder_quality_param: int
    becsles: float
    rank: int  # 1 = best


def _dense_rank(values: np.ndarray, direction: str) -> np.ndarray:
    """
    Dense ranking with direction. Ties share a rank.
        direction="min": smallest → 1
        direction="max": largest → 1
    """
    if direction == "max":
        values = -values
    # argsort twice = ranks; +1 for 1-based. Stable sort preserves order.
    order = np.argsort(values, kind="stable")
    ranks = np.empty_like(order)
    # Dense ranks (ties get the same rank, no gaps).
    prev = None
    r = 0
    for idx in order:
        if prev is None or values[idx] != prev:
            r += 1
            prev = values[idx]
        ranks[idx] = r
    return ranks


def _fit_y0(X_ranks: np.ndarray, y: np.ndarray) -> np.ndarray:
    """
    Fit y = X * b via OLS, return fitted values ŷ.
    Falls back to y itself if the fit is degenerate (e.g., singular matrix).
    """
    try:
        # lstsq handles non-square / rank-deficient matrices.
        beta, *_ = np.linalg.lstsq(X_ranks, y, rcond=None)
        y_hat = X_ranks @ beta
        if not np.all(np.isfinite(y_hat)):
            return y.astype(float)
        return y_hat
    except (np.linalg.LinAlgError, ValueError):
        return y.astype(float)


def local_coco_y0(
    variants: List[Dict],
    attributes: List[str] = None,
) -> List[CocoLocalResult]:
    """
    Run local Y0 approximation on a set of variants for a single image.

    Args:
        variants: list of dicts with keys `object_id`, `format`,
            `encoder_quality_param`, and each attribute in `attributes`.
        attributes: which attributes to include. Defaults to OAM_MINIMAL
            (compressed_size_kb, psnr, ssim).

    Returns:
        List of CocoLocalResult, sorted by rank (1 = best).
    """
    attributes = attributes or OAM_MINIMAL
    if not variants:
        return []
    n_obj = len(variants)
    n_attr = len(attributes)

    # Build the raw attribute matrix.
    raw = np.zeros((n_obj, n_attr), dtype=float)
    for i, v in enumerate(variants):
        for j, a in enumerate(attributes):
            raw[i, j] = float(v[a])

    # Rank each column with direction (1 = best).
    X_ranks = np.zeros_like(raw)
    for j, a in enumerate(attributes):
        direction = ATTRIBUTE_DIRECTION.get(a, "max")
        X_ranks[:, j] = _dense_rank(raw[:, j], direction)

    # Convert ranks to "goodness scores" (higher = better) on a [1..n_obj]
    # scale. Both predictors and response now share polarity so the OLS
    # fit is stable and interpretable.
    X_goodness = (n_obj + 1) - X_ranks

    # Response vector: sum of goodness across attributes (higher = better).
    y = X_goodness.sum(axis=1).astype(float)

    # OLS fit. ŷ is the Becslés. Higher = better.
    y_hat = _fit_y0(X_goodness, y)

    # Sort by Becslés descending (higher = better). Stable to preserve
    # input order on ties.
    order = np.argsort(-y_hat, kind="stable")

    results: List[CocoLocalResult] = []
    for rank_idx, orig_idx in enumerate(order):
        v = variants[orig_idx]
        results.append(CocoLocalResult(
            object_id=v["object_id"],
            format=v["format"],
            encoder_quality_param=v["encoder_quality_param"],
            becsles=round(float(y_hat[orig_idx]), 4),
            rank=rank_idx + 1,
        ))
    return results


def top_pick(results: List[CocoLocalResult]) -> CocoLocalResult:
    """Return the rank-1 result. Raises if list is empty."""
    if not results:
        raise ValueError("No COCO Y0 results.")
    return min(results, key=lambda r: r.rank)
