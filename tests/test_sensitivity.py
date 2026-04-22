"""
Tests for backend.services.sensitivity.

Verifies:
    - Per-variant sweep returns one point per threshold.
    - Stable case: all thresholds pick the same variant → reported as stable.
    - Flip case: higher threshold forces a different variant → reported flip.
    - Fallback case: when no variant meets the threshold, rule_fired =
      "fallback" and the highest-SSIM Pareto variant is returned.
    - Corpus summary computes agreement with the reference threshold correctly.

Run: python3 tests/test_sensitivity.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import sensitivity, storage  # noqa: E402


def clean_corpus():
    for d in (storage.IMAGES_DIR, storage.VARIANTS_DIR,
              storage.RESULTS_DIR, storage.EXPORTS_DIR):
        if d.exists():
            for c in d.iterdir():
                if c.is_file(): c.unlink()
                elif c.is_dir(): shutil.rmtree(c)


def _persist_variants(image_id: str, variants):
    doc = {
        "image_id": image_id,
        "variants": variants,
    }
    storage.write_result(image_id, doc)


def _mkvar(fmt, q, size, psnr, ssim):
    return {
        "format": fmt,
        "encoder_quality_param": q,
        "compressed_size_kb": size,
        "psnr": psnr,
        "ssim": ssim,
        "object_id": f"stub_{fmt}_q{q}",
    }


# ---------------------------------------------------------------------------
# Per-variant sweep
# ---------------------------------------------------------------------------

def test_returns_one_point_per_threshold():
    variants = [
        _mkvar("jpeg", 90, 200.0, 42.0, 0.99),
        _mkvar("jpeg", 70, 100.0, 38.0, 0.97),
        _mkvar("webp", 80,  80.0, 40.0, 0.985),
        _mkvar("webp", 60,  50.0, 36.0, 0.95),
        _mkvar("avif", 50,  40.0, 35.0, 0.93),
    ]
    thresholds = [0.80, 0.85, 0.90, 0.95, 0.99]
    points = sensitivity.sweep_thresholds_for_variants("img001", variants, thresholds)
    assert len(points) == 5, len(points)
    # Points carry threshold and rule_fired metadata
    for p, T in zip(points, thresholds):
        assert p.threshold == T
        assert p.rule_fired in ("primary", "fallback")
    print("  returns_one_point_per_threshold: OK")


def test_stable_when_smallest_variant_is_highest_ssim():
    """
    Degenerate case: one variant dominates on size AND quality (not realistic,
    but validates the mechanics). All thresholds pick the same thing.
    """
    variants = [
        _mkvar("avif", 50, 40.0, 42.0, 0.99),
        _mkvar("jpeg", 90, 200.0, 40.0, 0.97),
    ]
    points = sensitivity.sweep_thresholds_for_variants(
        "img001", variants, [0.80, 0.90, 0.95, 0.99]
    )
    picks = {p.object_id for p in points}
    assert len(picks) == 1, picks
    print("  stable_when_smallest_variant_is_highest_ssim: OK")


def test_pick_flips_at_ssim_boundary():
    """
    Realistic case: smaller variant has lower SSIM. Tightening the threshold
    should eliminate it and force the larger-but-better variant to win.
    """
    variants = [
        _mkvar("avif", 50,  40.0, 36.0, 0.93),  # smallest but lowest SSIM
        _mkvar("webp", 80,  80.0, 40.0, 0.97),  # middle
        _mkvar("jpeg", 90, 200.0, 42.0, 0.99),  # largest but highest SSIM
    ]
    # At T=0.90 all three qualify → min size picks avif.
    # At T=0.95 avif is eliminated → min size picks webp (size=80).
    # At T=0.98 webp is also eliminated → picks jpeg.
    points = sensitivity.sweep_thresholds_for_variants(
        "img001", variants, [0.90, 0.95, 0.98]
    )
    assert points[0].format == "avif", points[0]
    assert points[1].format == "webp", points[1]
    assert points[2].format == "jpeg", points[2]
    # All three should fire the PRIMARY rule (each has a qualifying variant)
    assert all(p.rule_fired == "primary" for p in points)
    print("  pick_flips_at_ssim_boundary: OK")


def test_fallback_when_no_variant_meets_threshold():
    variants = [
        _mkvar("jpeg", 70, 100.0, 30.0, 0.85),
        _mkvar("webp", 60,  50.0, 28.0, 0.80),
    ]
    points = sensitivity.sweep_thresholds_for_variants(
        "img001", variants, [0.95]
    )
    assert points[0].rule_fired == "fallback"
    # Fallback picks highest-SSIM Pareto variant → jpeg q70
    assert points[0].format == "jpeg"
    print("  fallback_when_no_variant_meets_threshold: OK")


# ---------------------------------------------------------------------------
# Corpus-level sweep
# ---------------------------------------------------------------------------

def test_run_sweep_rejects_empty_corpus():
    clean_corpus()
    try:
        sensitivity.run_sweep([0.95])
    except ValueError as e:
        assert "No persisted images" in str(e)
        print("  run_sweep_rejects_empty_corpus: OK")
    else:
        raise AssertionError("expected ValueError on empty corpus")


def test_run_sweep_summary_math():
    clean_corpus()
    # img001 — stable (all thresholds pick the same variant)
    _persist_variants("img001", [
        _mkvar("avif", 50, 40.0, 42.0, 0.99),   # dominates on both size & SSIM
        _mkvar("jpeg", 90, 200.0, 38.0, 0.96),
    ])
    # img002 — flips at 0.95
    _persist_variants("img002", [
        _mkvar("avif", 50,  40.0, 36.0, 0.90),
        _mkvar("webp", 80,  80.0, 40.0, 0.96),
        _mkvar("jpeg", 90, 200.0, 42.0, 0.99),
    ])

    thresholds = [0.85, 0.90, 0.95, 0.99]
    result = sensitivity.run_sweep(thresholds=thresholds)

    # corpus_summary
    cs = result["corpus_summary"]
    assert cs["n_images"] == 2
    assert cs["n_images_stable"] == 1, cs
    assert cs["stability_rate_pct"] == 50.0

    # per-threshold summary shape
    assert len(result["per_threshold_summary"]) == 4
    # At T=0.95 (the reference), the agreement with itself must be 100%
    ref_row = next(r for r in result["per_threshold_summary"]
                   if r["threshold"] == 0.95)
    assert ref_row["agreement_with_reference_rate_pct"] == 100.0
    assert ref_row["agreement_with_reference_count"] == 2

    # per-image summary
    img1_sum = next(s for s in result["per_image_summary"]
                    if s["image_id"] == "img001")
    assert img1_sum["is_stable"] is True
    assert img1_sum["n_distinct_picks"] == 1

    img2_sum = next(s for s in result["per_image_summary"]
                    if s["image_id"] == "img002")
    assert img2_sum["is_stable"] is False
    # At T=0.85 and 0.90, avif q50 should qualify (ssim=0.90 meets 0.90 but
    # NOT 0.95). At 0.95, avif is eliminated — webp q80 wins. At 0.99,
    # both avif and webp are eliminated — jpeg q90 wins.
    picks = img2_sum["picks_across_sweep"]
    assert picks[0].endswith("avif_q50"), picks
    assert picks[1].endswith("avif_q50"), picks
    assert picks[2].endswith("webp_q80"), picks
    assert picks[3].endswith("jpeg_q90"), picks
    assert img2_sum["n_distinct_picks"] == 3

    print("  run_sweep_summary_math: OK")


def test_reference_outside_sweep_still_computes_agreement():
    """
    If the user specifies a sweep that doesn't include 0.95, the reference
    picks are computed separately and agreement is still reported.
    """
    clean_corpus()
    _persist_variants("img001", [
        _mkvar("avif", 50,  40.0, 36.0, 0.90),
        _mkvar("webp", 80,  80.0, 40.0, 0.96),
        _mkvar("jpeg", 90, 200.0, 42.0, 0.99),
    ])
    # Sweep doesn't include 0.95 — reference threshold itself.
    result = sensitivity.run_sweep(thresholds=[0.80, 0.93, 0.97])
    # Agreement numbers should still be populated
    for row in result["per_threshold_summary"]:
        assert "agreement_with_reference_rate_pct" in row
        # Each image produces a pick at 0.95; either the sweep row matches
        # or it doesn't — just check the field is present and sane.
        assert 0.0 <= row["agreement_with_reference_rate_pct"] <= 100.0
    print("  reference_outside_sweep_still_computes_agreement: OK")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    print("=== per-variant sweep ===")
    test_returns_one_point_per_threshold()
    test_stable_when_smallest_variant_is_highest_ssim()
    test_pick_flips_at_ssim_boundary()
    test_fallback_when_no_variant_meets_threshold()
    print()
    print("=== corpus sweep ===")
    test_run_sweep_rejects_empty_corpus()
    test_run_sweep_summary_math()
    test_reference_outside_sweep_still_computes_agreement()
    print()
    print("ALL SENSITIVITY TESTS PASSED")


if __name__ == "__main__":
    main()
