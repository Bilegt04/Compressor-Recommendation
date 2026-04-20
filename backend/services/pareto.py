"""
Pareto front computation.

Dominance (A dominates B) requires, on the three attributes with their
direction-signs applied:
    A.file_size_kb <= B.file_size_kb
    A.psnr         >= B.psnr
    A.ssim         >= B.ssim
AND at least one strict inequality.

A variant is Pareto-optimal iff no other variant dominates it.
"""

from __future__ import annotations

from typing import List, Dict, Any


FLOAT_EPS = 1e-9


def _dominates(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    # Direction-adjusted: compressed_size_kb minimized, psnr/ssim maximized.
    le_size = a["compressed_size_kb"] <= b["compressed_size_kb"] + FLOAT_EPS
    ge_psnr = a["psnr"]                >= b["psnr"] - FLOAT_EPS
    ge_ssim = a["ssim"]                >= b["ssim"] - FLOAT_EPS

    if not (le_size and ge_psnr and ge_ssim):
        return False

    strict = (
        a["compressed_size_kb"] < b["compressed_size_kb"] - FLOAT_EPS
        or a["psnr"]            > b["psnr"] + FLOAT_EPS
        or a["ssim"]            > b["ssim"] + FLOAT_EPS
    )
    return strict


def get_pareto_front(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return the non-dominated subset. Preserves input order for stability.
    Guarantees non-empty output when input is non-empty.
    """
    if not variants:
        return []

    front: List[Dict[str, Any]] = []
    for i, candidate in enumerate(variants):
        dominated = False
        for j, other in enumerate(variants):
            if i == j:
                continue
            if _dominates(other, candidate):
                dominated = True
                break
        if not dominated:
            front.append(candidate)

    # Safety net — spec requires non-empty Pareto set.
    if not front:
        front = [min(variants, key=lambda v: v["compressed_size_kb"])]
    return front
