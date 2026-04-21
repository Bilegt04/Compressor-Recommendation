"""
COCO Y0 comparison service — strict parser for FINAL COCO OUTPUT only.

Input contract
--------------
This module accepts exactly ONE of:

    (a) Ranked list  — one object_id per line, best to worst.
                       No numbers on the line (except inside the id itself,
                       e.g. the "90" in "img001_jpeg_q90"). Optional leading
                       rank prefix like "1." or "1)" is allowed.

    (b) Scored list  — one object_id per line followed by exactly ONE
                       numeric score. Higher score = better.

Both formats have EXACTLY ONE object_id per line and AT MOST ONE trailing
score value.

What we reject
--------------
Any line with multiple numeric values after the object_id is a RANKED MATRIX
ROW (solver input, not output). This is the exact bug reported: the user
pasted the ranked matrix into the comparison textarea and the old parser
silently accepted it by treating the first column's rank as a "score."
We now reject such input with a specific error:

    "This looks like COCO input (ranked matrix), not final COCO output."

Other rejected shapes:
    - attribute lists     (lines are attribute names, not object_ids)
    - object lists alone  — technically accepted as ranked-list if they are
                            sorted best-to-worst by the solver; indistinguishable
                            from the output shape so we can't reject without
                            more signal. The user's textarea helper text tells
                            them what to paste.
    - mixed/garbage       — reported via `rejected_lines` diagnostics.

External-tool boundary
----------------------
No network calls. This parses text the user pastes.
"""

from __future__ import annotations

import io
import csv
import re
from typing import Dict, List, Optional, Tuple

from backend.services import storage


class CocoCompareError(RuntimeError):
    """Parser/comparison failure. Carries an optional diagnostics dict."""

    def __init__(self, message: str, diagnostics: Optional[Dict] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


_OBJECT_ID_RE = re.compile(r"\b(img\d+_[a-z]+_q\d+)\b", re.IGNORECASE)

# Matches any number token on the line (int, float, scientific).
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")

# Leading "1.", "1)", "1 " rank prefixes the user may paste with a list.
# Must consume the whole prefix so the residual can't be mistaken for a score.
_RANK_PREFIX_RE = re.compile(r"^\s*\d+[\.\)]\s+")


# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------

def _classify_line(raw: str) -> Dict:
    """
    Return a dict describing one input line:
        {
          "raw": str,
          "stripped": str,
          "object_id": str | None,
          "trailing_numbers": [float, ...],   # numbers AFTER the object_id
          "kind": "empty" | "comment" | "no_id" |
                  "ranked_list" | "scored" | "matrix_row",
        }
    """
    stripped = raw.strip()
    if not stripped:
        return {"raw": raw, "stripped": "", "kind": "empty",
                "object_id": None, "trailing_numbers": []}
    if stripped.startswith("#"):
        return {"raw": raw, "stripped": stripped, "kind": "comment",
                "object_id": None, "trailing_numbers": []}

    # Strip a leading rank prefix ("1. ", "1) ", "1 ") if present — it is
    # NOT a score and must not influence classification.
    no_prefix = _RANK_PREFIX_RE.sub("", stripped, count=1)

    oid_match = _OBJECT_ID_RE.search(no_prefix)
    if not oid_match:
        return {"raw": raw, "stripped": stripped, "kind": "no_id",
                "object_id": None, "trailing_numbers": []}

    after = no_prefix[oid_match.end():]
    numbers = [float(m.group(0)) for m in _NUMBER_RE.finditer(after)]

    if len(numbers) == 0:
        kind = "ranked_list"
    elif len(numbers) == 1:
        kind = "scored"
    else:
        # Two or more numbers after the id → this is a ranked-matrix row.
        # Exactly the shape the old parser accepted by accident.
        kind = "matrix_row"

    return {
        "raw": raw,
        "stripped": stripped,
        "kind": kind,
        "object_id": oid_match.group(1),
        "trailing_numbers": numbers,
    }


# ---------------------------------------------------------------------------
# Public: parse
# ---------------------------------------------------------------------------

def parse_coco_paste(text: str) -> Dict:
    """
    Strict parser. Returns:

        {
          "format_detected": "scored" | "ranked_list",
          "ranking": [object_id, ...],     # best → worst
          "scores":  {object_id: score}    # scored format only
          "diagnostics": {
              "n_matched": int,
              "n_rejected": int,
              "rejected_lines": [{"line_no": int, "reason": str,
                                   "text": str}, ...],
          }
        }

    Raises CocoCompareError with .diagnostics populated on any of:
        - empty input
        - no object_ids anywhere
        - one or more lines look like a ranked-matrix row
        - mixed "scored" + "ranked_list" (ambiguous)
    """
    if not text or not text.strip():
        raise CocoCompareError("Pasted text is empty.",
                               {"n_matched": 0, "n_rejected": 0,
                                "rejected_lines": []})

    lines = [_classify_line(raw) for raw in text.splitlines()]
    rejected: List[Dict] = []
    matrix_rows: List[Dict] = []
    scored_rows: List[Dict] = []
    ranked_rows: List[Dict] = []

    for i, info in enumerate(lines, start=1):
        if info["kind"] in ("empty", "comment"):
            continue
        if info["kind"] == "no_id":
            rejected.append({
                "line_no": i,
                "reason": "no object_id (expected something like 'img001_jpeg_q90')",
                "text": info["stripped"][:120],
            })
            continue
        if info["kind"] == "matrix_row":
            matrix_rows.append(info)
            rejected.append({
                "line_no": i,
                "reason": f"looks like a ranked-matrix row "
                          f"({len(info['trailing_numbers'])} numeric columns)",
                "text": info["stripped"][:120],
            })
            continue
        if info["kind"] == "scored":
            scored_rows.append(info)
        elif info["kind"] == "ranked_list":
            ranked_rows.append(info)

    diagnostics = {
        "n_matched": len(scored_rows) + len(ranked_rows),
        "n_rejected": len(rejected),
        "rejected_lines": rejected,
    }

    # --- Hard rejections ---
    if matrix_rows:
        raise CocoCompareError(
            "This looks like COCO input (ranked matrix), not final COCO "
            "output. Each line should have one object_id, with at most one "
            "numeric score after it. Paste the result of the external solver, "
            "not the matrix you sent to it.",
            diagnostics,
        )

    if not scored_rows and not ranked_rows:
        raise CocoCompareError(
            "Could not find any valid object_id lines. Expected 'img...' "
            "on each non-comment line.",
            diagnostics,
        )

    if scored_rows and ranked_rows:
        # One format at a time. Mixing is almost always an accidental paste.
        raise CocoCompareError(
            f"Mixed line shapes detected "
            f"({len(scored_rows)} scored, {len(ranked_rows)} unscored). "
            f"Every non-comment line must follow the same format.",
            diagnostics,
        )

    # --- Build the validated ranking ---
    if scored_rows:
        pairs = [(r["object_id"], r["trailing_numbers"][0]) for r in scored_rows]
        pairs.sort(key=lambda p: p[1], reverse=True)  # higher = better
        return {
            "format_detected": "scored",
            "ranking": [oid for oid, _ in pairs],
            "scores": {oid: s for oid, s in pairs},
            "diagnostics": diagnostics,
        }

    # ranked_list path — preserve input order, dedup
    seen = set()
    ranking: List[str] = []
    for r in ranked_rows:
        oid = r["object_id"]
        if oid in seen:
            continue
        seen.add(oid)
        ranking.append(oid)
    return {
        "format_detected": "ranked_list",
        "ranking": ranking,
        "scores": {},
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _coco_pick_per_image(ranking: List[str]) -> Dict[str, str]:
    picks: Dict[str, str] = {}
    for oid in ranking:
        prefix = oid.split("_", 1)[0]
        if prefix not in picks:
            picks[prefix] = oid
    return picks


def _persisted_picks() -> Dict[str, Dict]:
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
    Validates the paste (strictly) and produces per-image + corpus-level
    comparison. Raises CocoCompareError with diagnostics if validation fails.
    """
    parsed = parse_coco_paste(coco_paste_text)
    coco_picks = _coco_pick_per_image(parsed["ranking"])
    persisted = _persisted_picks()

    if not persisted:
        raise CocoCompareError(
            "No processed images in this app instance. Upload at least one "
            "image before comparing.",
            parsed["diagnostics"],
        )

    warnings: List[str] = []

    outside = sorted(oid for img, oid in coco_picks.items() if img not in persisted)
    if outside:
        warnings.append(
            f"COCO output references {len(outside)} image_id(s) not in the "
            f"local corpus: {outside[:5]}{'...' if len(outside) > 5 else ''}."
        )

    missing = sorted(set(persisted) - set(coco_picks))
    if missing:
        warnings.append(
            f"{len(missing)} image(s) in the local corpus have no COCO pick "
            f"in the paste: {missing[:5]}{'...' if len(missing) > 5 else ''}."
        )

    rows: List[Dict] = []
    a_t = a_c = t_c = compared = 0
    for image_id in sorted(persisted):
        coco_pick = coco_picks.get(image_id, "")
        if not coco_pick:
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
            "app_vs_coco_agree": app_vs_coco,
            "topsis_vs_coco_agree": topsis_vs_coco,
            "primary_rule_used": persisted[image_id]["primary_rule_used"],
            "coco_pick_present_in_app_corpus": True,
        })

    def _rate(num: int) -> float:
        return round(100.0 * num / compared, 2) if compared else 0.0

    return {
        "format_detected": parsed["format_detected"],
        "diagnostics": parsed["diagnostics"],
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
# CSV export
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
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COMPARISON_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in comparison["rows"]:
        w.writerow({c: r.get(c, "") for c in COMPARISON_COLUMNS})
    return buf.getvalue()
