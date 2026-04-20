"""
COCO Y0 export service.

Purpose
-------
Generates a solver-ready ranked Object-Attribute Matrix that the user can
paste into the external MIAU COCO Y0 form at:

    https://miau.my-x.hu/myx-free/coco/beker_y0.php

External-tool boundary
----------------------
This module makes NO network requests. The MIAU COCO Y0 site is treated as
a *manual external solver*: this service produces text that the user copies
by hand into the form on miau.my-x.hu. There is no scraping, no automated
submission, no API integration. Keeping the boundary manual matches the
thesis methodology and avoids any coupling to a third-party site that we
do not control.

Ranking method
--------------
For each attribute column, raw values are replaced by ranks (1 = best),
with direction applied:
    compressed_size_kb   ↓  smallest  → rank 1
    psnr                 ↑  largest   → rank 1
    ssim                 ↑  largest   → rank 1
    size_reduction_pct   ↑  largest   → rank 1
Ties receive the same rank ("dense" ranking), so e.g. values [10, 20, 20, 30]
yield ranks [1, 2, 2, 3] (when minimizing).

Optional `step_count` collapses ranks into a fixed number of bins via
quantile binning. Useful for COCO Y0 runs that prefer a small ordinal scale
(e.g., step_count=5 → ranks in {1..5}).

Output format
-------------
Tab-separated text. First row is the header (object-column label + attribute
names). Each subsequent row is one object_id followed by its ranks. This is
the de-facto OAM format used in MY-X / MIAU tooling. If the COCO Y0 form
expects a different separator, change `_render_matrix` — it's the single
point of change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Literal, Tuple

from backend.services import storage

# Mirrors export_service.OAM_MINIMAL / OAM_EXTENDED. Kept local to avoid a
# circular dep — these lists are short and stable.
OAM_MINIMAL_ATTRS: List[str] = ["compressed_size_kb", "psnr", "ssim"]
OAM_EXTENDED_ATTRS: List[str] = [
    "compressed_size_kb", "size_reduction_pct", "psnr", "ssim",
]

# Attribute → "minimize" or "maximize". Affects rank direction.
ATTRIBUTE_DIRECTION: Dict[str, str] = {
    "compressed_size_kb": "min",
    "size_reduction_pct": "max",
    "psnr":               "max",
    "ssim":               "max",
}

OAMVariant = Literal["minimal", "extended"]


class CocoExportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect_objects() -> List[Dict]:
    """
    Pull every variant from every persisted result document. One row per
    variant = one object in the OAM. Sorted deterministically.
    """
    docs = storage.iter_all_results()
    if not docs:
        raise CocoExportError(
            "No processed images found. Upload at least one image before "
            "exporting COCO input."
        )

    rows: List[Dict] = []
    for doc in docs:
        for v in doc.get("variants", []):
            row = {
                "object_id": v["object_id"],
                "compressed_size_kb": v["compressed_size_kb"],
                "size_reduction_pct": v.get("size_reduction_pct"),
                "psnr": v["psnr"],
                "ssim": v["ssim"],
            }
            rows.append(row)

    rows.sort(key=lambda r: r["object_id"])
    return rows


def _attribute_columns(variant: OAMVariant) -> List[str]:
    if variant == "minimal":
        return OAM_MINIMAL_ATTRS
    if variant == "extended":
        return OAM_EXTENDED_ATTRS
    raise CocoExportError(f"Unknown OAM variant: {variant}")


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _dense_rank(values: List[float], direction: str) -> List[int]:
    """
    Dense ranking with direction.
        direction="min": smallest → 1
        direction="max": largest → 1
    Ties share a rank.
    """
    if not values:
        return []
    reverse = (direction == "max")
    sorted_unique = sorted(set(values), reverse=reverse)
    rank_of = {v: i + 1 for i, v in enumerate(sorted_unique)}
    return [rank_of[v] for v in values]


def _quantile_bin(ranks: List[int], step_count: int) -> List[int]:
    """
    Collapse a 1..N ranking into a 1..step_count ordinal scale via quantile
    binning. Preserves order; ranks within the same quantile receive the
    same bin number.
    """
    if step_count <= 0:
        raise CocoExportError("step_count must be a positive integer.")
    if not ranks:
        return []

    n = len(ranks)
    # Edge case: fewer items than steps → return ranks as-is (no compression
    # possible without inflating distinctness).
    if n <= step_count:
        return ranks

    sorted_pairs = sorted(enumerate(ranks), key=lambda p: p[1])
    binned = [0] * n
    for new_pos, (orig_idx, _) in enumerate(sorted_pairs):
        # bin index in [0, step_count-1], then +1 for 1-based
        bin_idx = (new_pos * step_count) // n
        binned[orig_idx] = bin_idx + 1
    return binned


def build_ranked_matrix(
    variant: OAMVariant = "minimal",
    step_count: int = 0,
) -> Dict:
    """
    Returns a structured payload:

        {
          "object_ids":  ["img001_avif_q50", ...],
          "attributes":  ["compressed_size_kb", "psnr", "ssim"],
          "directions":  ["min", "max", "max"],
          "ranked_matrix": [[1, 3, 4], ...],   # rows = objects, cols = attrs
          "step_count":  0,                     # 0 = no quantile binning
          "n_objects":   N,
          "n_attributes": M,
        }

    step_count = 0 (default) means full dense ranking (no quantile binning).
    """
    rows = _collect_objects()
    attrs = _attribute_columns(variant)

    # Required completeness: every (object, attribute) must have a value.
    missing = []
    for r in rows:
        for a in attrs:
            if r.get(a) is None:
                missing.append((r["object_id"], a))
    if missing:
        raise CocoExportError(
            f"Missing values in {len(missing)} cells "
            f"(first: {missing[0]}). Cannot build a complete ranked matrix."
        )

    # Per-attribute ranking, then optional binning, then transpose into
    # row-major matrix (rows = objects).
    per_attr_ranks: List[List[int]] = []
    for a in attrs:
        raw_vals = [float(r[a]) for r in rows]
        ranks = _dense_rank(raw_vals, ATTRIBUTE_DIRECTION[a])
        if step_count and step_count > 0:
            ranks = _quantile_bin(ranks, step_count)
        per_attr_ranks.append(ranks)

    n_obj = len(rows)
    matrix = [
        [per_attr_ranks[a_i][o_i] for a_i in range(len(attrs))]
        for o_i in range(n_obj)
    ]

    return {
        "object_ids":  [r["object_id"] for r in rows],
        "attributes":  attrs,
        "directions":  [ATTRIBUTE_DIRECTION[a] for a in attrs],
        "ranked_matrix": matrix,
        "step_count":  step_count or 0,
        "n_objects":   n_obj,
        "n_attributes": len(attrs),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Single point of change if the MIAU COCO Y0 form ever expects a different
# separator (e.g., ";" or " "). Keeping it as a constant rather than
# hardcoding makes that swap a one-line edit.
COCO_SEPARATOR = "\t"
OBJECT_COLUMN_HEADER = "object_id"


def _render_matrix(payload: Dict) -> str:
    """
    Tab-separated, paste-ready:

        object_id<TAB>compressed_size_kb<TAB>psnr<TAB>ssim
        img001_avif_q50<TAB>1<TAB>3<TAB>4
        ...
    """
    sep = COCO_SEPARATOR
    lines = []
    header = sep.join([OBJECT_COLUMN_HEADER] + payload["attributes"])
    lines.append(header)
    for oid, row in zip(payload["object_ids"], payload["ranked_matrix"]):
        lines.append(sep.join([oid] + [str(v) for v in row]))
    return "\n".join(lines) + "\n"


def render_object_list(payload: Dict) -> str:
    """One object_id per line. Useful for pasting into a separate field."""
    return "\n".join(payload["object_ids"]) + "\n"


def render_attribute_list(payload: Dict) -> str:
    """One attribute per line."""
    return "\n".join(payload["attributes"]) + "\n"


def render_matrix(payload: Dict) -> str:
    return _render_matrix(payload)


def render_full_text(payload: Dict) -> str:
    """
    Combined text for the .txt download. Includes a short header comment so
    the file is self-describing for the thesis appendix and for anyone who
    opens it later without context.
    """
    header_lines = [
        "# COCO Y0 input — generated by Image Compression Recommender",
        f"# attributes:    {', '.join(payload['attributes'])}",
        f"# directions:    {', '.join(payload['directions'])}  (1 = best)",
        f"# n_objects:     {payload['n_objects']}",
        f"# n_attributes:  {payload['n_attributes']}",
        f"# step_count:    {payload['step_count']}  (0 = full dense ranking)",
        "# external solver: https://miau.my-x.hu/myx-free/coco/beker_y0.php",
        "# (paste the matrix below — this app does NOT submit automatically)",
        "",
    ]
    return "\n".join(header_lines) + _render_matrix(payload)


# ---------------------------------------------------------------------------
# Public entrypoint used by the route layer
# ---------------------------------------------------------------------------

def coco_export_payload(variant: OAMVariant = "minimal",
                        step_count: int = 0) -> Dict:
    """
    Build the structured payload + every rendered string. The HTTP layer
    returns this directly to the UI so all four "Copy" buttons work without
    further round-trips.
    """
    payload = build_ranked_matrix(variant=variant, step_count=step_count)
    return {
        **payload,
        "matrix_text":     render_matrix(payload),
        "object_list_text": render_object_list(payload),
        "attribute_list_text": render_attribute_list(payload),
        "full_text":       render_full_text(payload),
        "external_solver_url": "https://miau.my-x.hu/myx-free/coco/beker_y0.php",
    }


def write_coco_input_file(variant: OAMVariant = "minimal",
                          step_count: int = 0,
                          out_dir: Path = None) -> Path:
    """Write the .txt to data/exports/coco_input.txt and return the path."""
    out_dir = out_dir or storage.EXPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = coco_export_payload(variant=variant, step_count=step_count)
    path = out_dir / "coco_input.txt"
    path.write_text(payload["full_text"], encoding="utf-8")
    return path
