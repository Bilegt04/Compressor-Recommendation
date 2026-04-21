"""
Batch corpus runner.

Walks a folder of images, runs each through the compression pipeline using
the existing services, and emits a single corpus-level CSV intended as
thesis evidence.

Usage:
    python -m tools.batch_run /path/to/images
    python -m tools.batch_run /path/to/images --output corpus_2026Q2.csv
    python -m tools.batch_run /path/to/images --category landscape

The CSV is the unit of analysis for the thesis chapter on recommendation
evidence: each row is one (image, variant) pair with its full attribute
set plus three decision-method columns (app, TOPSIS, COCO placeholder)
and the corresponding agreement flags.

This script is standalone — it does not require the FastAPI server to be
running. It calls services directly and writes through the same storage
layer the HTTP app uses, so existing per-image JSONs and OAM exports
remain consistent with the corpus CSV.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make the repo root importable when run as `python -m tools.batch_run` or
# `python tools/batch_run.py` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import (  # noqa: E402
    compression, metrics, pareto, recommendation, storage,
    topsis as topsis_mod,
)
from backend.services.compression import CompressionError
from backend.services.ids import build_object_id
from backend.services.metrics import MetricError


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


# --- CSV schema (frozen for thesis reproducibility) -----------------------

CORPUS_COLUMNS: List[str] = [
    # Identity
    "run_id",
    "image_id",
    "object_id",
    "source_filename",
    "image_category",
    # Strategy
    "format",
    "encoder_quality_param",
    # Measured attributes
    "original_size_kb",
    "compressed_size_kb",
    "compression_ratio",
    "size_reduction_pct",
    "psnr",
    "ssim",
    "width_px",
    "height_px",
    # Decision flags
    "is_pareto",
    "is_recommended",          # primary lex rule
    "is_topsis_recommended",   # TOPSIS pick
    # Cross-method comparison fields
    "app_pick_object_id",      # repeated per-row for easy filtering in pivots
    "topsis_pick_object_id",
    "app_vs_topsis_agree",     # bool
    # Provenance
    "primary_rule_used",
    "topsis_score",            # this object's TOPSIS score (if on Pareto)
    "processed_at_utc",
]


def _variant_sig(v: Dict) -> Tuple[str, int]:
    return (v["format"], v["encoder_quality_param"])


def _process_one_image(image_path: Path,
                       category: str,
                       run_id: str,
                       processed_at: str) -> List[Dict]:
    """
    Run the full pipeline on one file and return the list of CSV rows
    for that image. Mirrors backend.main.run_pipeline but bypasses the
    FastAPI layer.
    """
    image_id = storage.new_image_id()

    # Save original via the same storage helper used by /upload, so
    # downstream OAM exports see this image in data/results/ too.
    data = image_path.read_bytes()
    saved_path = storage.save_original(image_id, data, image_path.name)

    out_dir = storage.variants_dir(image_id)
    produced = compression.compress_all(saved_path, out_dir)

    variants: List[Dict] = []
    for rec in produced:
        mvals = metrics.compute_metrics(saved_path, rec["path"])
        s = rec["strategy"]
        variants.append({
            "object_id": build_object_id(image_id, s.format, s.quality),
            "format": s.format,
            "encoder_quality_param": s.quality,
            **mvals,
        })

    front = pareto.get_pareto_front(variants)
    front_sigs = {_variant_sig(v) for v in front}

    primary = recommendation.recommend(front)
    primary_sig = _variant_sig(primary)

    topsis_ranking = topsis_mod.topsis_rank(front)
    topsis_pick = topsis_mod.recommend_topsis(front)
    topsis_sig = _variant_sig(topsis_pick)
    # Score lookup for non-Pareto variants → "" (not on Pareto front).
    score_by_sig = {
        (t["format"], t["encoder_quality_param"]): t["topsis_score"]
        for t in topsis_ranking
    }

    app_pick_oid = build_object_id(image_id, primary["format"],
                                   primary["encoder_quality_param"])
    topsis_pick_oid = build_object_id(image_id, topsis_pick["format"],
                                      topsis_pick["encoder_quality_param"])
    rules_agree = (primary_sig == topsis_sig)

    # Persist per-image JSON so the existing OAM exports continue to work.
    payload = {
        "image_id": image_id,
        "source_filename": image_path.name,
        "image_category": category,
        "original_path": str(saved_path.relative_to(storage.REPO_ROOT)),
        "variants": [
            {**v,
             "is_pareto": _variant_sig(v) in front_sigs,
             "is_recommended": _variant_sig(v) == primary_sig}
            for v in variants
        ],
        "pareto_front": front,
        "recommended": primary,
        "recommended_key": f"{primary['format']}_q{primary['encoder_quality_param']}",
        "explanation": recommendation.explain(front, primary),
        "topsis": {"ranking": topsis_ranking, "recommended": topsis_pick},
        "decision_rule_comparison":
            topsis_mod.compare_decision_rules(primary, topsis_pick, topsis_ranking),
    }
    storage.write_result(image_id, payload)

    # Build CSV rows.
    rows: List[Dict] = []
    for v in variants:
        sig = _variant_sig(v)
        rows.append({
            "run_id": run_id,
            "image_id": image_id,
            "object_id": v["object_id"],
            "source_filename": image_path.name,
            "image_category": category,
            "format": v["format"],
            "encoder_quality_param": v["encoder_quality_param"],
            "original_size_kb": v["original_size_kb"],
            "compressed_size_kb": v["compressed_size_kb"],
            "compression_ratio": v["compression_ratio"],
            "size_reduction_pct": v["size_reduction_pct"],
            "psnr": v["psnr"],
            "ssim": v["ssim"],
            "width_px": v["width_px"],
            "height_px": v["height_px"],
            "is_pareto": sig in front_sigs,
            "is_recommended": sig == primary_sig,
            "is_topsis_recommended": sig == topsis_sig,
            "app_pick_object_id": app_pick_oid,
            "topsis_pick_object_id": topsis_pick_oid,
            "app_vs_topsis_agree": rules_agree,
            "primary_rule_used": primary["recommendation_rule_used"],
            "topsis_score": score_by_sig.get(sig, ""),
            "processed_at_utc": processed_at,
        })
    return rows


def _iter_input_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)
    if not files:
        raise SystemExit(
            f"No supported images in {folder} "
            f"(extensions: {sorted(SUPPORTED_EXTS)})"
        )
    return files


def _write_csv(path: Path, rows: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CORPUS_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CORPUS_COLUMNS})


def _summarize(rows: List[Dict], elapsed: float) -> str:
    """Per-run text summary printed to stdout."""
    by_image = {}
    for r in rows:
        by_image.setdefault(r["image_id"], []).append(r)

    n_images = len(by_image)
    n_variants = len(rows)
    agree_lex_topsis = sum(
        1 for img_rows in by_image.values() if img_rows[0]["app_vs_topsis_agree"]
    )
    fallback_count = sum(
        1 for img_rows in by_image.values()
        if img_rows[0]["primary_rule_used"] == "pareto_fallback_max_ssim"
    )

    return (
        f"\nCorpus summary\n"
        f"  images processed         : {n_images}\n"
        f"  variants generated       : {n_variants}\n"
        f"  app vs TOPSIS agreement  : {agree_lex_topsis}/{n_images} "
        f"({100*agree_lex_topsis/max(n_images,1):.1f}%)\n"
        f"  primary rule = fallback  : {fallback_count}/{n_images} "
        f"(no Pareto variant met SSIM>=0.95)\n"
        f"  elapsed                  : {elapsed:.1f}s\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="batch_run",
        description="Run the compression pipeline on every image in a folder.",
    )
    parser.add_argument("folder", type=Path,
                        help="Folder containing input images.")
    parser.add_argument("--output", "-o", type=Path,
                        default=storage.EXPORTS_DIR / "corpus_results.csv",
                        help="Output CSV path. "
                             "Default: data/exports/corpus_results.csv")
    parser.add_argument("--category", "-c", default="",
                        help="Optional category label applied to all images "
                             "in this run (e.g., 'photo', 'screenshot').")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="Skip files that fail instead of aborting.")
    args = parser.parse_args(argv)

    files = _iter_input_files(args.folder)
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    processed_at = datetime.now(timezone.utc).isoformat()

    print(f"Run ID: {run_id}")
    print(f"Found {len(files)} image(s) in {args.folder}")
    print(f"Output: {args.output}\n")

    all_rows: List[Dict] = []
    failures: List[Tuple[Path, str]] = []
    t_start = time.time()

    for i, f in enumerate(files, 1):
        sys.stdout.write(f"[{i:>3}/{len(files)}] {f.name} … ")
        sys.stdout.flush()
        try:
            rows = _process_one_image(
                f, category=args.category,
                run_id=run_id, processed_at=processed_at,
            )
            all_rows.extend(rows)
            sys.stdout.write(f"OK ({rows[0]['image_id']})\n")
        except (CompressionError, MetricError, OSError, ValueError) as e:
            sys.stdout.write(f"FAILED ({e})\n")
            failures.append((f, str(e)))
            if not args.continue_on_error:
                print("\nAborting. Use --continue-on-error to skip failures.")
                return 1

    elapsed = time.time() - t_start
    if not all_rows:
        print("No rows produced. CSV not written.")
        return 1

    _write_csv(args.output, all_rows)
    print(f"\nWrote {len(all_rows)} rows to {args.output}")
    print(_summarize(all_rows, elapsed))

    if failures:
        print(f"Failures ({len(failures)}):")
        for path, err in failures:
            print(f"  - {path.name}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
