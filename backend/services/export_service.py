"""
Export service. Produces four thesis-ready CSVs from the persisted result
documents in data/results/.

Outputs (written to data/exports/):
    raw_results.csv            One row per variant with all experimental fields.
    attribute_dictionary.csv   Attribute metadata (id, name, direction, unit, desc).
    oam.csv                    Object-Attribute Matrix. Rows = objects,
                               columns = selected OAM attributes. object_id first.
    analysis.csv               Per-object pareto/recommended/threshold/rule/reason.

Design rules:
    - Every export is atomic (tempfile + os.replace).
    - Field names are canonical and consistent across all four files.
    - Numeric rounding follows metrics.ROUND_* (already applied in stored JSON).
    - Missing required fields abort the export — no partial files.
    - OAM variant is configurable: "minimal" or "extended".
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Literal, Tuple

from backend.services import storage

OAMVariant = Literal["minimal", "extended"]


# ---------------------------------------------------------------------------
# Canonical schemas
# ---------------------------------------------------------------------------

RAW_COLUMNS: List[str] = [
    "object_id",
    "image_id",
    "source_filename",
    "image_category",
    "format",
    "encoder_quality_param",
    "original_size_kb",
    "compressed_size_kb",
    "compression_ratio",
    "size_reduction_pct",
    "psnr",
    "ssim",
    "width_px",
    "height_px",
    "is_pareto",
    "is_recommended",
    "threshold_unmet",
    "recommendation_rule_used",
    "recommendation_reason",
]

ANALYSIS_COLUMNS: List[str] = [
    "object_id",
    "image_id",
    "is_pareto",
    "is_recommended",
    "threshold_unmet",
    "recommendation_rule_used",
    "recommendation_reason",
]

OAM_MINIMAL: List[str] = ["compressed_size_kb", "psnr", "ssim"]
OAM_EXTENDED: List[str] = [
    "compressed_size_kb", "size_reduction_pct", "psnr", "ssim",
]

# attribute_dictionary.csv rows. One row per OAM attribute, plus rows for
# auxiliary measured attributes that may be used in thesis analysis.
ATTRIBUTE_DICTIONARY: List[Dict[str, str]] = [
    {"attribute_id": "A1", "attribute_name": "compressed_size_kb",
     "direction": "down", "unit": "KB",
     "description": "Smaller compressed file size is preferred."},
    {"attribute_id": "A2", "attribute_name": "psnr",
     "direction": "up", "unit": "dB",
     "description": "Peak signal-to-noise ratio vs. original; higher is better."},
    {"attribute_id": "A3", "attribute_name": "ssim",
     "direction": "up", "unit": "dimensionless",
     "description": "Structural similarity to original in [-1,1]; higher is better."},
    {"attribute_id": "A4", "attribute_name": "size_reduction_pct",
     "direction": "up", "unit": "percent",
     "description": "Percent reduction vs. original file size; higher is better."},
    {"attribute_id": "A5", "attribute_name": "compression_ratio",
     "direction": "up", "unit": "ratio",
     "description": "original_size_kb / compressed_size_kb; higher is better."},
]


class ExportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Required-field validation (spec §data-quality)
# ---------------------------------------------------------------------------

REQUIRED_NON_NULL = (
    "object_id", "image_id", "compressed_size_kb", "psnr", "ssim",
)


def _assert_complete(row: Dict[str, Any], row_index: int) -> None:
    for field in REQUIRED_NON_NULL:
        if field not in row or row[field] is None or row[field] == "":
            raise ExportError(
                f"Row {row_index}: required field '{field}' missing or empty. "
                f"Export aborted — no partial files per spec."
            )


# ---------------------------------------------------------------------------
# Row materialization from stored JSON documents
# ---------------------------------------------------------------------------

def _variant_rows_from_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten one result document into variant-level rows with every field
    the exports need. This is the single source of truth used by all CSVs.
    """
    image_id = doc["image_id"]
    source_filename = doc.get("source_filename", "")
    image_category = doc.get("image_category", "")

    recommended = doc.get("recommended", {})
    rec_sig = (recommended.get("format"),
               recommended.get("encoder_quality_param"))
    rule_used = recommended.get("recommendation_rule_used", "")
    reason = recommended.get("recommendation_reason", "")
    threshold_unmet_flag = bool(recommended.get("threshold_unmet", False))

    rows: List[Dict[str, Any]] = []
    for v in doc["variants"]:
        v_sig = (v["format"], v["encoder_quality_param"])
        is_recommended = (v_sig == rec_sig)

        # threshold_unmet is only meaningful on the recommended row per spec.
        # Non-recommended rows carry False to keep the column non-null.
        row_threshold_unmet = threshold_unmet_flag if is_recommended else False

        row = {
            "object_id":                v["object_id"],
            "image_id":                 image_id,
            "source_filename":          source_filename,
            "image_category":           image_category,
            "format":                   v["format"],
            "encoder_quality_param":    v["encoder_quality_param"],
            "original_size_kb":         v["original_size_kb"],
            "compressed_size_kb":       v["compressed_size_kb"],
            "compression_ratio":        v["compression_ratio"],
            "size_reduction_pct":       v["size_reduction_pct"],
            "psnr":                     v["psnr"],
            "ssim":                     v["ssim"],
            "width_px":                 v["width_px"],
            "height_px":                v["height_px"],
            "is_pareto":                bool(v.get("is_pareto", False)),
            "is_recommended":           is_recommended,
            "threshold_unmet":          row_threshold_unmet,
            "recommendation_rule_used": rule_used if is_recommended else "",
            "recommendation_reason":    reason if is_recommended else "",
        }
        rows.append(row)
    return rows


def collect_all_rows() -> List[Dict[str, Any]]:
    docs = storage.iter_all_results()
    if not docs:
        raise ExportError("No processed images found in data/results/.")

    all_rows: List[Dict[str, Any]] = []
    for doc in docs:
        for row in _variant_rows_from_document(doc):
            all_rows.append(row)
    # Deterministic ordering: by image_id, then by format, then quality desc.
    all_rows.sort(key=lambda r: (
        r["image_id"], r["format"], -int(r["encoder_quality_param"])
    ))
    return all_rows


# ---------------------------------------------------------------------------
# Atomic CSV writer
# ---------------------------------------------------------------------------

def _atomic_write_csv(path: Path, columns: List[str],
                      rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp.csv", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=columns, extrasaction="ignore"
            )
            writer.writeheader()
            for r in rows:
                writer.writerow({c: r.get(c, "") for c in columns})
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Public export functions
# ---------------------------------------------------------------------------

def export_raw_results(out_dir: Path = None) -> Path:
    out_dir = out_dir or storage.EXPORTS_DIR
    rows = collect_all_rows()
    for i, r in enumerate(rows):
        _assert_complete(r, i)
    path = out_dir / "raw_results.csv"
    _atomic_write_csv(path, RAW_COLUMNS, rows)
    return path


def export_attribute_dictionary(out_dir: Path = None) -> Path:
    out_dir = out_dir or storage.EXPORTS_DIR
    path = out_dir / "attribute_dictionary.csv"
    _atomic_write_csv(
        path,
        ["attribute_id", "attribute_name", "direction", "unit", "description"],
        ATTRIBUTE_DICTIONARY,
    )
    return path


def _oam_columns(variant: OAMVariant) -> List[str]:
    if variant == "minimal":
        return ["object_id"] + OAM_MINIMAL
    if variant == "extended":
        return ["object_id"] + OAM_EXTENDED
    raise ExportError(f"Unknown OAM variant: {variant}")


def export_oam(variant: OAMVariant = "minimal",
               out_dir: Path = None) -> Path:
    out_dir = out_dir or storage.EXPORTS_DIR
    rows = collect_all_rows()
    for i, r in enumerate(rows):
        _assert_complete(r, i)
    cols = _oam_columns(variant)
    path = out_dir / "oam.csv"
    _atomic_write_csv(path, cols, rows)
    return path


def export_analysis(out_dir: Path = None) -> Path:
    out_dir = out_dir or storage.EXPORTS_DIR
    rows = collect_all_rows()
    for i, r in enumerate(rows):
        _assert_complete(r, i)
    path = out_dir / "analysis.csv"
    _atomic_write_csv(path, ANALYSIS_COLUMNS, rows)
    return path


def export_all(oam_variant: OAMVariant = "minimal",
               out_dir: Path = None) -> Dict[str, str]:
    """
    Runs all four exports. Fails the whole operation if any validation
    fails (data-quality §"no partial export files").
    """
    out_dir = out_dir or storage.EXPORTS_DIR
    paths = {
        "raw_results":          str(export_raw_results(out_dir)),
        "attribute_dictionary": str(export_attribute_dictionary(out_dir)),
        "oam":                  str(export_oam(oam_variant, out_dir)),
        "analysis":             str(export_analysis(out_dir)),
    }
    return paths
