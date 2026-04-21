"""
COCO Y0 comparison service.

After the user pastes COCO Y0 output from
https://miau.my-x.hu/myx-free/coco/beker_y0.php into the UI, this module:

1. Parses the pasted text (two formats supported, see below).
2. Determines the COCO-selected variant per image_id.
3. Compares it against the app's primary recommendation and the TOPSIS
   pick (both already stored per-image in data/results/).
4. Produces:
   - per-image comparison rows (app vs TOPSIS vs COCO + agreement flags)
   - corpus-level summary (3 pairwise agreement rates)

External-tool boundary
----------------------
This module does NOT call MIAU. It only parses text the user pastes.
The COCO Y0 site remains a manual external solver in the workflow.

Supported paste formats
-----------------------
The MIAU output format is not standardized — the student pastes whatever
the site returns. This parser accepts two shapes and tries them in order:

  Format A — scored: each line has an object_id and a numeric score,
             separated by tab/whitespace/comma. Higher score = better.
             Example:
                 img001_avif_q50    0.973
                 img001_jpeg_q90    0.541
                 ...

  Format B — pre-ranked list: each line contains exactly one object_id
             (any leading rank number, separator chars, or surrounding
             whitespace are stripped). Order = best to worst.
             Example:
                 1. img001_avif_q50
                 2. img001_jpeg_q90
                 ...

Format A wins if any line has a parseable numeric score; otherwise B.

If the actual MIAU output uses a different format, change `parse_coco_paste`
— it's the only place that handles input shape.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from backend.services import storage


class CocoCompareError(RuntimeError):
    pass


# Object IDs from this app look like "img001_jpeg_q90". Allow letters,
# digits, underscores. Conservative pattern — won't match arbitrary text.
_OBJECT_ID_RE = re.compile(r"\b(img\d+_[a-z]+_q\d+)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _try_parse_scored(text: str) -> Optional[List[Tuple[str, float]]]:
    """
    Parse Format A. Returns [(object_id, score), ...] sorted by score
    descending, or None if no line had a numeric score.

    A line counts as "scored" only if a numeric value appears AFTER the
    object id. This avoids treating leading rank prefixes like "1." (in
    "1. img001_avif_q50") as scores.
    """
    pairs: List[Tuple[str, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        oid_match = _OBJECT_ID_RE.search(line)
        if not oid_match:
            continue
        # Look for a number on the SAME line, AFTER the object id ends.
        after = line[oid_match.end():]
        num_match = re.search(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?", after)
        if num_match:
            try:
                pairs.append((oid_match.group(1), float(num_match.group(0))))
            except ValueError:
                continue
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs


def _try_parse_ranked_list(text: str) -> List[str]:
    """
    Parse Format B. Returns ordered list of object_ids, best → worst.
    Each non-empty line contributes its first object_id match.
    """
    out: List[str] = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _OBJECT_ID_RE.search(line)
        if not m:
            continue
        oid = m.group(1)
        if oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def parse_coco_paste(text: str) -> Dict:
    """
    Try Format A first (scored), fall back to Format B (ranked list).

    Returns:
        {
          "format_detected": "scored" | "ranked_list",
          "ranking": [object_id, ...],     # best → worst
          "scores":  {object_id: score}    # only for scored format
        }

    Raises CocoCompareError on empty/unparseable input.
    """
    if not text or not text.strip():
        raise CocoCompareError("Pasted text is empty.")

    scored = _try_parse_scored(text)
    if scored:
        return {
            "format_detected": "scored",
            "ranking": [oid for oid, _ in scored],
            "scores": {oid: s for oid, s in scored},
        }

    ranked = _try_parse_ranked_list(text)
    if ranked:
        return {
            "format_detected": "ranked_list",
            "ranking": ranked,
            "scores": {},
        }

    raise CocoCompareError(
        "Could not find any object IDs in the pasted text. Object IDs "
        "look like 'img001_jpeg_q90'. Make sure you pasted the COCO Y0 "
        "output, not the input matrix."
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _coco_pick_per_image(coco_ranking: List[str]) -> Dict[str, str]:
    """
    For each image_id present in the ranking, return the highest-ranked
    object_id (= COCO pick). image_id is the prefix before the first
    underscore: "img001_avif_q50" → "img001".
    """
    picks: Dict[str, str] = {}
    for oid in coco_ranking:
        prefix = oid.split("_", 1)[0]
        if prefix not in picks:        # first occurrence wins (best rank)
            picks[prefix] = oid
    return picks


def _persisted_picks() -> Dict[str, Dict]:
    """
    Load every persisted result document and return:
        {image_id: {
            "app_pick": object_id,
            "topsis_pick": object_id,
            "primary_rule_used": str,
        }}
    """
    out: Dict[str, Dict] = {}
    for doc in storage.iter_all_results():
        image_id = doc["image_id"]
        rec = doc.get("recommended", {})
        topsis_rec = (doc.get("topsis") or {}).get("recommended", {})
        out[image_id] = {
            "app_pick": rec.get("object_id", ""),
            "topsis_pick": topsis_rec.get("object_id", ""),
            "primary_rule_used": rec.get("recommendation_rule_used", ""),
        }
    return out


def build_comparison(coco_paste_text: str) -> Dict:
    """
    Top-level entry point. Combines the parsed COCO output with the stored
    app + TOPSIS picks for every image and returns a structured payload.

    Returns:
        {
          "format_detected": "scored" | "ranked_list",
          "rows": [
            {
              "image_id": "img001",
              "app_pick": "img001_jpeg_q90",
              "topsis_pick": "img001_avif_q50",
              "coco_pick":  "img001_webp_q80",
              "app_vs_topsis_agree": False,
              "app_vs_coco_agree":   False,
              "topsis_vs_coco_agree": False,
              "primary_rule_used": "pareto_ssim>=0.95_min_size",
              "coco_pick_present_in_app_corpus": True,
            }, ...
          ],
          "summary": {
            "n_images_in_corpus":      N_corpus,
            "n_images_in_coco_paste":  N_coco,
            "n_images_compared":       N_compared,
            "agree_app_vs_topsis":     {"count": int, "rate": float},
            "agree_app_vs_coco":       {"count": int, "rate": float},
            "agree_topsis_vs_coco":    {"count": int, "rate": float},
            "coco_picks_outside_corpus": [object_id, ...],
          },
          "warnings": [str, ...],
        }
    """
    parsed = parse_coco_paste(coco_paste_text)
    coco_picks = _coco_pick_per_image(parsed["ranking"])
    persisted = _persisted_picks()

    if not persisted:
        raise CocoCompareError(
            "No processed images found in this app instance. Process at "
            "least one image before comparing COCO Y0 results."
        )

    warnings: List[str] = []

    # Sanity check: are any COCO picks for images that don't exist locally?
    outside = sorted(
        oid for img, oid in coco_picks.items() if img not in persisted
    )
    if outside:
        warnings.append(
            f"COCO output references {len(outside)} image_id(s) not in "
            f"this app's corpus: {outside[:5]}{'...' if len(outside) > 5 else ''}. "
            f"They are excluded from the comparison."
        )

    # And: are any local images missing from the COCO paste?
    missing_from_coco = sorted(set(persisted) - set(coco_picks))
    if missing_from_coco:
        warnings.append(
            f"{len(missing_from_coco)} image(s) in the local corpus have "
            f"no COCO pick in the paste: {missing_from_coco[:5]}"
            f"{'...' if len(missing_from_coco) > 5 else ''}."
        )

    rows: List[Dict] = []
    a_t = a_c = t_c = compared = 0
    for image_id in sorted(persisted):
        coco_pick = coco_picks.get(image_id, "")
        if not coco_pick:
            # Skip — image absent from paste; cannot compare COCO column.
            continue
        app_pick = persisted[image_id]["app_pick"]
        topsis_pick = persisted[image_id]["topsis_pick"]
        app_vs_topsis = (app_pick == topsis_pick)
        app_vs_coco = (app_pick == coco_pick)
        topsis_vs_coco = (topsis_pick == coco_pick)
        compared += 1
        a_t += int(app_vs_topsis)
        a_c += int(app_vs_coco)
        t_c += int(topsis_vs_coco)
        rows.append({
            "image_id": image_id,
            "app_pick": app_pick,
            "topsis_pick": topsis_pick,
            "coco_pick": coco_pick,
            "app_vs_topsis_agree": app_vs_topsis,
            "app_vs_coco_agree":   app_vs_coco,
            "topsis_vs_coco_agree": topsis_vs_coco,
            "primary_rule_used": persisted[image_id]["primary_rule_used"],
            "coco_pick_present_in_app_corpus": True,
        })

    def _rate(num: int) -> float:
        return round(100.0 * num / compared, 2) if compared else 0.0

    return {
        "format_detected": parsed["format_detected"],
        "rows": rows,
        "summary": {
            "n_images_in_corpus":     len(persisted),
            "n_images_in_coco_paste": len(coco_picks),
            "n_images_compared":      compared,
            "agree_app_vs_topsis":    {"count": a_t, "rate_pct": _rate(a_t)},
            "agree_app_vs_coco":      {"count": a_c, "rate_pct": _rate(a_c)},
            "agree_topsis_vs_coco":   {"count": t_c, "rate_pct": _rate(t_c)},
            "coco_picks_outside_corpus": outside,
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CSV export of the comparison
# ---------------------------------------------------------------------------

COMPARISON_COLUMNS: List[str] = [
    "image_id",
    "app_pick",
    "topsis_pick",
    "coco_pick",
    "app_vs_topsis_agree",
    "app_vs_coco_agree",
    "topsis_vs_coco_agree",
    "primary_rule_used",
]


def render_comparison_csv(comparison: Dict) -> str:
    """Render the rows as CSV text (header + per-image rows)."""
    import io, csv
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COMPARISON_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in comparison["rows"]:
        w.writerow({c: r.get(c, "") for c in COMPARISON_COLUMNS})
    return buf.getvalue()
