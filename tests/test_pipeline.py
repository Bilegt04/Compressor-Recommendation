"""
End-to-end integration test for the OAM-ready pipeline.

Validates:
    1. Deterministic IDs (image_id = img001..., object_id = imgNNN_fmt_qQ)
    2. Every variant has the full attribute set (no missing fields)
    3. Rounding rules applied (sizes/ratios/psnr/ssim)
    4. Pareto set non-empty, recommended ∈ Pareto
    5. Recommendation metadata present (rule_used, reason, threshold_unmet)
    6. All four exports produced: raw_results, attribute_dictionary, oam, analysis
    7. Exports are corpus-wide (both test images contribute rows)
    8. OAM variants: minimal (3 attrs) and extended (4 attrs)
    9. Required-non-null validation rejects incomplete data

Encoders are stubbed so this test runs without cwebp/avifenc.

Run: python3 tests/test_pipeline.py
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import (  # noqa: E402
    compression, export_service, metrics, pareto, recommendation,
    storage, topsis as topsis_mod,
)
from backend.services.ids import build_object_id  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — clean slate + synthetic images
# ---------------------------------------------------------------------------

def clean_data_dirs():
    for d in (storage.IMAGES_DIR, storage.VARIANTS_DIR,
              storage.RESULTS_DIR, storage.EXPORTS_DIR):
        if d.exists():
            for child in d.iterdir():
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)


def make_synthetic_image(seed: int, size: int = 256) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.zeros((size, size, 3), dtype=np.uint8)
    for y in range(size):
        base[y, :, 0] = y % 256
        base[y, :, 1] = (255 - y) % 256
    base[size // 4:3 * size // 4, size // 4:3 * size // 4, 2] = 200
    noise = rng.integers(-8, 8, size=base.shape, dtype=np.int16)
    base = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return base


def patch_compression_without_encoders():
    """Pillow-only compression so we don't need cwebp/avifenc for the test."""
    from backend.services.compression import Strategy

    def fake_compress(src: Path, strategy: Strategy, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / strategy.output_filename()
        with Image.open(src) as im:
            im = im.convert("RGB")
            if strategy.format == "jpeg":
                im.save(dst, format="JPEG",
                        quality=strategy.quality, optimize=True)
            elif strategy.format == "webp":
                im.save(dst, format="WEBP", quality=strategy.quality)
            elif strategy.format == "avif":
                try:
                    im.save(dst, format="AVIF", quality=strategy.quality)
                except Exception:
                    # Fallback if AVIF plugin absent — still yields a file
                    # so the rest of the pipeline can be validated.
                    im.save(dst, format="JPEG", quality=strategy.quality)
        assert dst.exists() and dst.stat().st_size > 0
        return dst

    def fake_compress_all(src: Path, out_dir: Path):
        return [
            {"strategy": s, "path": fake_compress(src, s, out_dir)}
            for s in compression.STRATEGIES
        ]

    compression.compress = fake_compress
    compression.compress_all = fake_compress_all


# ---------------------------------------------------------------------------
# Mirror of backend.main.run_pipeline without FastAPI dependency
# ---------------------------------------------------------------------------

def run_pipeline_direct(image_id: str, original_path: Path,
                        source_filename: str,
                        image_category: str = "") -> dict:
    from backend.services.ids import build_object_id as boid
    out_dir = storage.variants_dir(image_id)
    produced = compression.compress_all(original_path, out_dir)

    variants = []
    for rec in produced:
        mvals = metrics.compute_metrics(original_path, rec["path"])
        s = rec["strategy"]
        v = {
            "object_id": boid(image_id, s.format, s.quality),
            "format": s.format,
            "encoder_quality_param": s.quality,
            **mvals,
        }
        variants.append(v)

    front = pareto.get_pareto_front(variants)
    front_sigs = {(v["format"], v["encoder_quality_param"]) for v in front}

    recommended = recommendation.recommend(front)
    explanation = recommendation.explain(front, recommended)
    topsis_ranking = topsis_mod.topsis_rank(front)
    recommended_topsis = topsis_mod.recommend_topsis(front)
    rule_comparison = topsis_mod.compare_decision_rules(
        recommended, recommended_topsis, topsis_ranking
    )

    rec_sig = (recommended["format"], recommended["encoder_quality_param"])
    for v in variants:
        sig = (v["format"], v["encoder_quality_param"])
        v["is_pareto"] = sig in front_sigs
        v["is_recommended"] = sig == rec_sig

    payload = {
        "image_id": image_id,
        "source_filename": source_filename,
        "image_category": image_category,
        "original_path": str(original_path.relative_to(storage.REPO_ROOT)),
        "variants": variants,
        "pareto_front": front,
        "recommended": recommended,
        "recommended_key":
            f"{recommended['format']}_q{recommended['encoder_quality_param']}",
        "explanation": explanation,
        "topsis": {"ranking": topsis_ranking,
                   "recommended": recommended_topsis},
        "decision_rule_comparison": rule_comparison,
    }
    storage.write_result(image_id, payload)
    return payload


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

REQUIRED_VARIANT_FIELDS = [
    "object_id", "format", "encoder_quality_param",
    "original_size_kb", "compressed_size_kb",
    "compression_ratio", "size_reduction_pct",
    "psnr", "ssim", "width_px", "height_px",
    "is_pareto", "is_recommended",
]

REQUIRED_RECOMMENDED_FIELDS = [
    "object_id", "format", "encoder_quality_param",
    "compressed_size_kb", "psnr", "ssim",
    "ssim_threshold", "threshold_unmet",
    "recommendation_rule_used", "recommendation_reason",
]


def assert_payload_contract(payload: dict, expected_category: str):
    assert payload["image_id"].startswith("img"), payload["image_id"]
    assert payload["image_category"] == expected_category

    assert len(payload["variants"]) == 5, "5 mandatory strategies required"
    for v in payload["variants"]:
        for f in REQUIRED_VARIANT_FIELDS:
            assert f in v, f"variant missing {f}: {v}"
        # object_id determinism
        assert v["object_id"] == build_object_id(
            payload["image_id"], v["format"], v["encoder_quality_param"]
        )
        # non-null numerics
        assert v["compressed_size_kb"] > 0
        assert -1.0 <= v["ssim"] <= 1.0
        # rounding contract (metrics.py rounds before return)
        assert _decimals(v["compressed_size_kb"]) <= 2
        assert _decimals(v["compression_ratio"]) <= 2
        assert _decimals(v["size_reduction_pct"]) <= 2
        assert _decimals(v["psnr"]) <= 3
        assert _decimals(v["ssim"]) <= 4

    # Pareto non-empty + recommended ∈ Pareto
    assert len(payload["pareto_front"]) >= 1
    rec = payload["recommended"]
    for f in REQUIRED_RECOMMENDED_FIELDS:
        assert f in rec, f"recommended missing {f}"
    rec_sig = (rec["format"], rec["encoder_quality_param"])
    pareto_sigs = {(v["format"], v["encoder_quality_param"])
                   for v in payload["pareto_front"]}
    assert rec_sig in pareto_sigs
    assert isinstance(rec["threshold_unmet"], bool)
    assert rec["recommendation_rule_used"] in (
        "pareto_ssim>=0.95_min_size", "pareto_fallback_max_ssim"
    )

    # Exactly one variant flagged is_recommended
    flagged = [v for v in payload["variants"] if v["is_recommended"]]
    assert len(flagged) == 1, f"exactly one recommended, got {len(flagged)}"


def _decimals(x: float) -> int:
    s = f"{x:.12f}".rstrip("0").rstrip(".")
    if "." not in s:
        return 0
    return len(s.split(".")[1])


def assert_raw_results_csv(path: Path, n_images: int):
    rows = _read_csv(path)
    assert len(rows) == 5 * n_images, \
        f"expected {5 * n_images} rows, got {len(rows)}"
    for r in rows:
        for col in export_service.RAW_COLUMNS:
            assert col in r, f"raw_results missing column: {col}"
        # Required-non-null fields
        for col in ("object_id", "image_id", "compressed_size_kb", "psnr", "ssim"):
            assert r[col] not in ("", None), \
                f"null {col} in raw_results row: {r}"


def assert_attribute_dictionary_csv(path: Path):
    rows = _read_csv(path)
    expected_cols = ["attribute_id", "attribute_name", "direction",
                     "unit", "description"]
    for r in rows:
        for c in expected_cols:
            assert c in r and r[c] != ""
    names = {r["attribute_name"] for r in rows}
    for must in ("compressed_size_kb", "psnr", "ssim", "size_reduction_pct"):
        assert must in names, f"attribute_dictionary missing {must}"


def assert_oam_csv(path: Path, expected_cols: list, n_images: int):
    rows = _read_csv(path)
    assert len(rows) == 5 * n_images
    # Header assertion — read raw first line
    with open(path, encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == expected_cols, \
        f"expected columns {expected_cols}, got {header}"


def assert_analysis_csv(path: Path, n_images: int):
    rows = _read_csv(path)
    assert len(rows) == 5 * n_images
    with open(path, encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == export_service.ANALYSIS_COLUMNS
    # Exactly one is_recommended=True per image
    by_image = {}
    for r in rows:
        by_image.setdefault(r["image_id"], []).append(r)
    for image_id, rs in by_image.items():
        recs = [r for r in rs if r["is_recommended"].lower() == "true"]
        assert len(recs) == 1, \
            f"image {image_id} has {len(recs)} recommended rows (expected 1)"


def _read_csv(path: Path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    clean_data_dirs()
    patch_compression_without_encoders()

    # Process two images so we can verify corpus-wide export behavior.
    arr1 = make_synthetic_image(seed=42)
    arr2 = make_synthetic_image(seed=7)

    id1 = storage.new_image_id()
    p1 = storage.IMAGES_DIR / f"{id1}.png"
    Image.fromarray(arr1, "RGB").save(p1, format="PNG")
    payload1 = run_pipeline_direct(id1, p1,
                                   source_filename="photo_a.png",
                                   image_category="landscape")

    id2 = storage.new_image_id()
    p2 = storage.IMAGES_DIR / f"{id2}.png"
    Image.fromarray(arr2, "RGB").save(p2, format="PNG")
    payload2 = run_pipeline_direct(id2, p2,
                                   source_filename="photo_b.png",
                                   image_category="portrait")

    # Deterministic IDs
    assert id1 == "img001", f"expected img001, got {id1}"
    assert id2 == "img002", f"expected img002, got {id2}"

    assert_payload_contract(payload1, "landscape")
    assert_payload_contract(payload2, "portrait")

    # Run exports
    paths = export_service.export_all(oam_variant="minimal")
    raw_p = Path(paths["raw_results"])
    adict_p = Path(paths["attribute_dictionary"])
    oam_p = Path(paths["oam"])
    anal_p = Path(paths["analysis"])

    assert_raw_results_csv(raw_p, n_images=2)
    assert_attribute_dictionary_csv(adict_p)
    assert_oam_csv(oam_p,
                   ["object_id", "compressed_size_kb", "psnr", "ssim"],
                   n_images=2)
    assert_analysis_csv(anal_p, n_images=2)

    # Also verify extended OAM
    ext_path = export_service.export_oam(variant="extended")
    assert_oam_csv(ext_path,
                   ["object_id", "compressed_size_kb",
                    "size_reduction_pct", "psnr", "ssim"],
                   n_images=2)

    # Verify partial-data rejection: break one result file and re-run export.
    bad_file = storage.results_path(id1)
    original_text = bad_file.read_text(encoding="utf-8")
    broken = json.loads(original_text)
    # Null a required field in one variant.
    broken["variants"][0]["psnr"] = None
    bad_file.write_text(json.dumps(broken), encoding="utf-8")
    try:
        export_service.export_raw_results()
    except export_service.ExportError as e:
        print(f"Partial-data rejection works: {e}")
    else:
        raise AssertionError("export_raw_results should have rejected null psnr")
    # Restore the file so subsequent runs of the test aren't poisoned.
    bad_file.write_text(original_text, encoding="utf-8")

    # ---- reports ----
    print("=== PASS ===")
    print(f"img001.recommended : {payload1['recommended']['format']} "
          f"q{payload1['recommended']['encoder_quality_param']}  "
          f"rule={payload1['recommended']['recommendation_rule_used']}  "
          f"threshold_unmet={payload1['recommended']['threshold_unmet']}")
    print(f"img002.recommended : {payload2['recommended']['format']} "
          f"q{payload2['recommended']['encoder_quality_param']}  "
          f"rule={payload2['recommended']['recommendation_rule_used']}  "
          f"threshold_unmet={payload2['recommended']['threshold_unmet']}")
    print()
    print("Exports written:")
    for k, v in paths.items():
        size = Path(v).stat().st_size
        print(f"  {k:22s} → {v}  ({size} bytes)")
    print(f"  oam (extended)         → {ext_path}  ({ext_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
