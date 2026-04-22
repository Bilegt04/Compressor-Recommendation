"""
Tests for backend.services.coco_local.

Run: python3 tests/test_coco_local.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.coco_local import (  # noqa: E402
    local_coco_y0, top_pick, _dense_rank,
)
import numpy as np


def mk(oid_suffix, fmt, q, size, psnr, ssim):
    return {
        "object_id": f"img001_{fmt}_q{q}" if oid_suffix is None else oid_suffix,
        "format": fmt,
        "encoder_quality_param": q,
        "compressed_size_kb": size,
        "psnr": psnr,
        "ssim": ssim,
    }


# ---------------------------------------------------------------------------
# Dense ranking
# ---------------------------------------------------------------------------

def test_dense_rank_min():
    vals = np.array([30.0, 10.0, 20.0, 10.0])
    ranks = _dense_rank(vals, "min")
    # 10 → 1 (tied), 20 → 2, 30 → 3
    assert list(ranks) == [3, 1, 2, 1], list(ranks)
    print("  dense_rank_min: OK")


def test_dense_rank_max():
    vals = np.array([30.0, 10.0, 20.0, 30.0])
    ranks = _dense_rank(vals, "max")
    # 30 → 1 (tied), 20 → 2, 10 → 3
    assert list(ranks) == [1, 3, 2, 1], list(ranks)
    print("  dense_rank_max: OK")


# ---------------------------------------------------------------------------
# local_coco_y0 — dominated variants should rank last
# ---------------------------------------------------------------------------

def test_dominated_variant_ranks_last():
    """
    One variant is worse on every attribute. COCO must not pick it as #1.
    """
    variants = [
        mk(None, "avif", 50,  40.0, 42.0, 0.99),  # best on everything
        mk(None, "jpeg", 90, 200.0, 40.0, 0.96),  # worse size, worse psnr,
                                                   # worse ssim
        mk(None, "webp", 80,  80.0, 41.0, 0.98),  # middle on everything
    ]
    results = local_coco_y0(variants)
    assert results[0].format == "avif", results
    assert results[-1].format == "jpeg", results
    print("  dominated_variant_ranks_last: OK")


def test_single_variant():
    variants = [mk(None, "jpeg", 90, 100.0, 40.0, 0.98)]
    results = local_coco_y0(variants)
    assert len(results) == 1
    assert results[0].rank == 1
    print("  single_variant: OK")


def test_empty_input():
    assert local_coco_y0([]) == []
    print("  empty_input: OK")


def test_results_sorted_by_rank():
    variants = [
        mk(None, "avif", 50,  40.0, 36.0, 0.93),
        mk(None, "jpeg", 90, 200.0, 42.0, 0.99),
        mk(None, "webp", 80,  80.0, 40.0, 0.97),
    ]
    results = local_coco_y0(variants)
    ranks = [r.rank for r in results]
    assert ranks == sorted(ranks), ranks
    # Becslés monotone decreasing with rank
    becsles = [r.becsles for r in results]
    for i in range(len(becsles) - 1):
        assert becsles[i] >= becsles[i + 1], becsles
    print("  results_sorted_by_rank: OK")


def test_top_pick_returns_rank_1():
    variants = [
        mk(None, "avif", 50,  40.0, 36.0, 0.93),
        mk(None, "jpeg", 90, 200.0, 42.0, 0.99),
    ]
    results = local_coco_y0(variants)
    best = top_pick(results)
    assert best.rank == 1
    assert best.object_id == results[0].object_id
    print("  top_pick_returns_rank_1: OK")


def test_all_variants_identical():
    """If every variant has identical attributes, the output is stable
    (each object gets the same Becslés, sort is stable)."""
    variants = [
        mk(None, "jpeg", 90, 100.0, 40.0, 0.98),
        mk(None, "webp", 80, 100.0, 40.0, 0.98),
        mk(None, "avif", 50, 100.0, 40.0, 0.98),
    ]
    results = local_coco_y0(variants)
    # All Becslés should be equal
    becs = {r.becsles for r in results}
    assert len(becs) == 1, becs
    # Input order preserved on ties
    assert results[0].format == "jpeg"
    print("  all_variants_identical: OK")


def test_extended_attribute_set():
    """Using the extended OAM (4 attrs) still produces a valid ranking."""
    variants = [
        {**mk(None, "avif", 50,  40.0, 36.0, 0.93), "size_reduction_pct": 80.0},
        {**mk(None, "jpeg", 90, 200.0, 42.0, 0.99), "size_reduction_pct": 10.0},
        {**mk(None, "webp", 80,  80.0, 40.0, 0.97), "size_reduction_pct": 60.0},
    ]
    results = local_coco_y0(
        variants,
        attributes=["compressed_size_kb", "size_reduction_pct", "psnr", "ssim"],
    )
    assert len(results) == 3
    # Should still produce a strict rank ordering
    assert {r.rank for r in results} == {1, 2, 3}
    print("  extended_attribute_set: OK")


def main():
    print("=== dense rank ===")
    test_dense_rank_min()
    test_dense_rank_max()
    print()
    print("=== local_coco_y0 ===")
    test_dominated_variant_ranks_last()
    test_single_variant()
    test_empty_input()
    test_results_sorted_by_rank()
    test_top_pick_returns_rank_1()
    test_all_variants_identical()
    test_extended_attribute_set()
    print()
    print("ALL COCO LOCAL TESTS PASSED")


if __name__ == "__main__":
    main()
