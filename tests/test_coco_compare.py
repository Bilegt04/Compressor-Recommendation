"""
Tests for backend.services.coco_compare.

Run: python3 tests/test_coco_compare.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import coco_compare, storage  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def clean_corpus():
    for d in (storage.IMAGES_DIR, storage.VARIANTS_DIR,
              storage.RESULTS_DIR, storage.EXPORTS_DIR):
        if d.exists():
            for c in d.iterdir():
                if c.is_file(): c.unlink()
                elif c.is_dir(): shutil.rmtree(c)


def seed_two_image_corpus():
    """Persist two synthetic image results with known app/topsis picks."""
    def make_doc(image_id, app_pick_quality, topsis_pick_quality):
        variants = []
        for fmt, q in [("jpeg", 90), ("jpeg", 70), ("webp", 80),
                       ("webp", 60), ("avif", 50)]:
            oid = f"{image_id}_{fmt}_q{q}"
            variants.append({
                "object_id": oid, "format": fmt, "encoder_quality_param": q,
                "compressed_size_kb": 50.0 - q * 0.3,
                "psnr": 30.0 + q * 0.1, "ssim": 0.90 + q * 0.001,
                "is_pareto": True, "is_recommended": False,
                "original_size_kb": 100.0,
                "compression_ratio": 2.0, "size_reduction_pct": 50.0,
                "width_px": 256, "height_px": 256,
            })
        app_oid = f"{image_id}_jpeg_q{app_pick_quality}"
        topsis_oid = f"{image_id}_avif_q{topsis_pick_quality}"
        for v in variants:
            v["is_recommended"] = (v["object_id"] == app_oid)
        return {
            "image_id": image_id,
            "source_filename": f"{image_id}.png",
            "image_category": "",
            "variants": variants,
            "pareto_front": variants,
            "recommended": {
                **next(v for v in variants if v["object_id"] == app_oid),
                "recommendation_rule_used": "pareto_ssim>=0.95_min_size",
                "recommendation_reason": "test",
                "threshold_unmet": False, "ssim_threshold": 0.95,
            },
            "topsis": {
                "ranking": [],
                "recommended": {
                    **next(v for v in variants if v["object_id"] == topsis_oid),
                    "topsis_score": 0.9,
                },
            },
        }
    storage.write_result("img001", make_doc("img001", 90, 50))
    storage.write_result("img002", make_doc("img002", 70, 50))


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

def test_parser_scored_format():
    text = """
    img001_avif_q50    0.973
    img001_jpeg_q90    0.541
    img001_webp_q80    0.420
    """
    out = coco_compare.parse_coco_paste(text)
    assert out["format_detected"] == "scored"
    assert out["ranking"] == [
        "img001_avif_q50", "img001_jpeg_q90", "img001_webp_q80",
    ]
    assert out["scores"]["img001_avif_q50"] == 0.973
    print("  parser_scored_format: OK")


def test_parser_ranked_list_format():
    text = """
    1. img001_avif_q50
    2. img001_jpeg_q90
    3. img001_webp_q80
    """
    out = coco_compare.parse_coco_paste(text)
    assert out["format_detected"] == "ranked_list"
    assert out["ranking"][0] == "img001_avif_q50"
    print("  parser_ranked_list_format: OK")


def test_parser_mixed_separators():
    """Comma-separated, tab-separated, and irregular whitespace all work."""
    text = (
        "img001_jpeg_q90,0.5\n"
        "img002_avif_q50\t0.9\n"
        "  img003_webp_q80   0.7  \n"
    )
    out = coco_compare.parse_coco_paste(text)
    assert out["format_detected"] == "scored"
    # img002 has highest score → first
    assert out["ranking"][0] == "img002_avif_q50"
    print("  parser_mixed_separators: OK")


def test_parser_ignores_comments():
    text = "# header line\n# another comment\nimg001_jpeg_q90  0.5\n"
    out = coco_compare.parse_coco_paste(text)
    assert out["ranking"] == ["img001_jpeg_q90"]
    print("  parser_ignores_comments: OK")


def test_parser_rejects_empty():
    try:
        coco_compare.parse_coco_paste("")
    except coco_compare.CocoCompareError:
        print("  parser_rejects_empty: OK")
    else:
        raise AssertionError("expected CocoCompareError on empty input")


def test_parser_rejects_no_object_ids():
    try:
        coco_compare.parse_coco_paste("here is some text without any ids\n42")
    except coco_compare.CocoCompareError as e:
        assert "object IDs" in str(e)
        print("  parser_rejects_no_object_ids: OK")
    else:
        raise AssertionError("expected CocoCompareError")


def test_parser_does_not_grab_q_number_as_score():
    """Earlier versions of the regex risked matching the q-number inside
    the object id. Make sure the score parser strips the id first."""
    out = coco_compare.parse_coco_paste("img001_jpeg_q90\n")
    # No score on the line → falls back to ranked_list
    assert out["format_detected"] == "ranked_list"
    print("  parser_does_not_grab_q_number_as_score: OK")


# ---------------------------------------------------------------------------
# build_comparison tests
# ---------------------------------------------------------------------------

def test_full_agreement_when_coco_matches_app():
    clean_corpus()
    seed_two_image_corpus()
    paste = (
        "img001_jpeg_q90  0.99\n"   # app picked img001_jpeg_q90
        "img001_avif_q50  0.50\n"
        "img002_jpeg_q70  0.99\n"   # app picked img002_jpeg_q70
        "img002_avif_q50  0.50\n"
    )
    cmp = coco_compare.build_comparison(paste)
    s = cmp["summary"]
    assert s["n_images_compared"] == 2
    assert s["agree_app_vs_coco"]["count"] == 2
    assert s["agree_app_vs_coco"]["rate_pct"] == 100.0
    print("  full_agreement_when_coco_matches_app: OK")


def test_zero_agreement_when_coco_differs():
    clean_corpus()
    seed_two_image_corpus()
    # COCO picks WebP for both — neither is the app pick
    paste = (
        "img001_webp_q80  0.99\n"
        "img002_webp_q80  0.99\n"
    )
    cmp = coco_compare.build_comparison(paste)
    s = cmp["summary"]
    assert s["agree_app_vs_coco"]["count"] == 0
    assert s["agree_app_vs_coco"]["rate_pct"] == 0.0
    # TOPSIS picks AVIF for both, COCO picks WebP — also disagrees
    assert s["agree_topsis_vs_coco"]["count"] == 0
    print("  zero_agreement_when_coco_differs: OK")


def test_partial_corpus_overlap_warns():
    clean_corpus()
    seed_two_image_corpus()
    # Only img001 in paste; img002 should be flagged missing
    paste = "img001_jpeg_q90  0.99\n"
    cmp = coco_compare.build_comparison(paste)
    s = cmp["summary"]
    assert s["n_images_compared"] == 1
    assert any("img002" in w for w in cmp["warnings"]), cmp["warnings"]
    print("  partial_corpus_overlap_warns: OK")


def test_coco_picks_outside_corpus_warns():
    clean_corpus()
    seed_two_image_corpus()
    paste = (
        "img001_jpeg_q90  0.99\n"
        "img002_jpeg_q70  0.99\n"
        "img999_avif_q50  0.99\n"   # not in corpus
    )
    cmp = coco_compare.build_comparison(paste)
    assert "img999_avif_q50" in cmp["summary"]["coco_picks_outside_corpus"]
    assert any("not in this app" in w for w in cmp["warnings"])
    print("  coco_picks_outside_corpus_warns: OK")


def test_csv_export_shape():
    clean_corpus()
    seed_two_image_corpus()
    paste = "img001_jpeg_q90  0.99\nimg002_jpeg_q70  0.99\n"
    cmp = coco_compare.build_comparison(paste)
    csv_text = coco_compare.render_comparison_csv(cmp)
    lines = csv_text.strip().split("\n")
    assert lines[0].split(",")[0] == "image_id"
    assert "app_vs_coco_agree" in lines[0]
    assert len(lines) == 3  # header + 2 rows
    print("  csv_export_shape: OK")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    print("=== parser ===")
    test_parser_scored_format()
    test_parser_ranked_list_format()
    test_parser_mixed_separators()
    test_parser_ignores_comments()
    test_parser_rejects_empty()
    test_parser_rejects_no_object_ids()
    test_parser_does_not_grab_q_number_as_score()
    print()
    print("=== build_comparison ===")
    test_full_agreement_when_coco_matches_app()
    test_zero_agreement_when_coco_differs()
    test_partial_corpus_overlap_warns()
    test_coco_picks_outside_corpus_warns()
    test_csv_export_shape()
    print()
    print("ALL COCO COMPARE TESTS PASSED")


if __name__ == "__main__":
    main()
