"""
COCO Y0 comparison service — MIAU-aware.

MIAU's COCO Y0 solver at
    https://miau.my-x.hu/myx-free/coco/beker_y0.php
does NOT emit object_id strings in its final result table. It emits:

    Rangsor         — a mapping of internal row labels (O1, O2, ...) to
                      whatever object names the user submitted. This is
                      where object_id strings live.
    Lépcsők(1)      — staircase diagnostic, unrelated to the final result.
    Lépcsők(2)      — staircase diagnostic, unrelated to the final result.
    COCO:Y0         — the final result table. Rows are labelled O1..ON with
                      a numeric "Becslés" (estimate) column.

To produce a correct comparison the user must paste enough of the MIAU page
that BOTH "Rangsor" and "COCO:Y0" are included. The parser then:
    1. Parses Rangsor into {O2: object_id, ...}
    2. Parses COCO:Y0 into [(O_label, becsles), ...]
    3. Joins the two — COCO's ordering, with real object_ids.

WINNER DIRECTION
----------------
Higher Becslés is treated as better. This is the default interpretation of
MY-X / COCO Y0 output in standard use. If the professor's workflow uses an
inverted interpretation, flip the single constant BECSLES_HIGHER_IS_BETTER
below. Ties are broken by original O-row order (stable), so two objects
with the same Becslés retain the row order MIAU printed.

External-tool boundary
----------------------
No network calls. Everything is parsed from pasted text.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional, Tuple

from backend.services import storage


# Single point of change if the professor's workflow treats lower Becslés
# as the winner.
BECSLES_HIGHER_IS_BETTER = True


class CocoCompareError(RuntimeError):
    """Parser/comparison failure. Carries a diagnostics dict."""

    def __init__(self, message: str, diagnostics: Optional[Dict] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


# ---------------------------------------------------------------------------
# Regex building blocks
# ---------------------------------------------------------------------------

_OBJECT_ID_RE = re.compile(r"\b(img\d+_[a-z]+_q\d+)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_O_LABEL_RE = re.compile(r"\b(O\d+)\b")

# Leading "1.", "1)", "1 " rank prefix (accepted and stripped).
_RANK_PREFIX_RE = re.compile(r"^\s*\d+[\.\)]\s+")

# Block header detection. MIAU's section labels appear on their own line or
# prefix a line; we look for them case-insensitively.
_BLOCK_HEADERS = {
    "rangsor":    re.compile(r"\bRangsor\b", re.IGNORECASE),
    "lepcsok1":   re.compile(r"L[eé]pcs[oő]k\s*\(\s*1\s*\)", re.IGNORECASE),
    "lepcsok2":   re.compile(r"L[eé]pcs[oő]k\s*\(\s*2\s*\)", re.IGNORECASE),
    "coco_y0":    re.compile(r"COCO\s*:?\s*Y0\b|\bY0\b(?!\w)", re.IGNORECASE),
}


# ---------------------------------------------------------------------------
# Block splitting
# ---------------------------------------------------------------------------

def _split_into_blocks(text: str) -> Dict[str, List[str]]:
    """
    Walk the text and group lines by the most recently seen block header.
    Returns a dict of block_name -> list of lines that followed it.

    Lines before any recognized header go under the key "_preamble".
    A line that IS a header (just a label) becomes that block's first
    line if it carries data on the same line (MIAU sometimes prints the
    label inline with the first row).
    """
    blocks: Dict[str, List[str]] = {
        "_preamble": [],
        "rangsor": [],
        "lepcsok1": [],
        "lepcsok2": [],
        "coco_y0": [],
    }
    current = "_preamble"

    for raw in text.splitlines():
        line = raw.rstrip()

        # Detect header on this line. Order matters: check the longer /
        # more specific patterns first so "Lépcsők(1)" doesn't leak into
        # "coco_y0" through the bare "Y0" fallback.
        detected = None
        for name in ("rangsor", "lepcsok1", "lepcsok2", "coco_y0"):
            if _BLOCK_HEADERS[name].search(line):
                detected = name
                break

        if detected:
            current = detected
            # Strip the header token off the line. Whatever remains may be
            # a data row that started on the same line as the label.
            residual = _BLOCK_HEADERS[detected].sub("", line, count=1).strip()
            if residual:
                blocks[current].append(residual)
            continue

        blocks[current].append(line)

    return blocks


# ---------------------------------------------------------------------------
# Rangsor: O-label -> object_id mapping
# ---------------------------------------------------------------------------

def _parse_rangsor(lines: List[str]) -> Dict[str, str]:
    """
    Each non-empty line should carry one O-label AND one object_id.
    Accepts any separator: tabs, multiple spaces, commas, colons.

    Examples accepted per line:
        O1    img001_jpeg_q90
        O2:   img001_jpeg_q70
        O3,   img001_webp_q80
        1.  O4    img001_webp_q60
    """
    mapping: Dict[str, str] = {}
    for raw in lines:
        line = _RANK_PREFIX_RE.sub("", raw.strip(), count=1)
        if not line or line.startswith("#"):
            continue
        o_match = _O_LABEL_RE.search(line)
        oid_match = _OBJECT_ID_RE.search(line)
        if o_match and oid_match:
            mapping[o_match.group(1).upper()] = oid_match.group(1)
    return mapping


# ---------------------------------------------------------------------------
# COCO:Y0: O-label + Becslés score
# ---------------------------------------------------------------------------

def _parse_coco_y0(lines: List[str]) -> List[Tuple[str, float]]:
    """
    Each non-empty data line in the COCO:Y0 block should start with an
    O-label and contain at least one numeric value (Becslés).

    We take the FIRST number after the O-label as Becslés. Other numeric
    columns (residual, rank, etc.) that MIAU may include to the right are
    ignored — the parser is tolerant to extra columns.

    Header rows containing the literal word "Becslés" (or just no number)
    are skipped.
    """
    pairs: List[Tuple[str, float]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Skip header rows. MIAU typically prints a header like
        # "Objektum  Becslés  ..." before the data rows.
        if re.search(r"\bBecsl[eé]s\b", line, re.IGNORECASE) and \
                not _O_LABEL_RE.search(line):
            continue

        o_match = _O_LABEL_RE.search(line)
        if not o_match:
            continue
        after = line[o_match.end():]
        num_match = _NUMBER_RE.search(after)
        if not num_match:
            continue
        try:
            # MIAU may use comma as decimal separator in Hungarian locale.
            score = float(num_match.group(0).replace(",", "."))
        except ValueError:
            continue
        pairs.append((o_match.group(1).upper(), score))
    return pairs


# ---------------------------------------------------------------------------
# Fallback: direct object_id + score paste (no MIAU blocks at all)
# ---------------------------------------------------------------------------

def _try_parse_direct(text: str) -> Optional[Dict]:
    """
    Accept the old hand-converted format: one object_id per line, optionally
    followed by exactly one numeric score. Useful when the user preprocessed
    MIAU output into object_id form themselves.

    Returns None if the paste looks nothing like a direct list; raises
    CocoCompareError if it looks LIKE a list but violates the rules (mixed
    shapes, ranked-matrix rows, etc).
    """
    from_lines: List[Dict] = []
    bad_matrix_rows: List[Dict] = []
    non_id_rejects: List[Dict] = []
    has_any_id = False

    for i, raw in enumerate(text.splitlines(), start=1):
        line = _RANK_PREFIX_RE.sub("", raw.strip(), count=1)
        if not line or line.startswith("#"):
            continue
        oid_match = _OBJECT_ID_RE.search(line)
        if not oid_match:
            non_id_rejects.append({"line_no": i, "text": line[:120]})
            continue
        has_any_id = True
        after = line[oid_match.end():]
        nums = [m.group(0) for m in _NUMBER_RE.finditer(after)]
        if len(nums) >= 2:
            bad_matrix_rows.append({"line_no": i, "text": line[:120]})
            continue
        from_lines.append({
            "line_no": i,
            "object_id": oid_match.group(1),
            "score": float(nums[0].replace(",", ".")) if nums else None,
        })

    if not has_any_id:
        return None

    if bad_matrix_rows:
        raise CocoCompareError(
            "Detected a ranked-matrix paste (multiple numbers per line). "
            "Paste the FINAL COCO result, not the ranked input matrix.",
            {"n_matched": 0,
             "n_rejected": len(bad_matrix_rows) + len(non_id_rejects),
             "rejected_lines": [
                 {"line_no": r["line_no"],
                  "reason": "ranked-matrix row",
                  "text": r["text"]} for r in bad_matrix_rows
             ] + [
                 {"line_no": r["line_no"],
                  "reason": "no object_id",
                  "text": r["text"]} for r in non_id_rejects
             ]},
        )

    scored = [r for r in from_lines if r["score"] is not None]
    unscored = [r for r in from_lines if r["score"] is None]

    if scored and unscored:
        raise CocoCompareError(
            f"Mixed line shapes in direct paste "
            f"({len(scored)} scored, {len(unscored)} unscored).",
            {"n_matched": len(from_lines),
             "n_rejected": len(non_id_rejects),
             "rejected_lines": [
                 {"line_no": r["line_no"],
                  "reason": "no object_id",
                  "text": r["text"]} for r in non_id_rejects
             ]},
        )

    if scored:
        scored.sort(key=lambda r: r["score"], reverse=BECSLES_HIGHER_IS_BETTER)
        return {
            "format_detected": "direct_scored",
            "ranking": [r["object_id"] for r in scored],
            "scores": {r["object_id"]: r["score"] for r in scored},
            "diagnostics": {
                "n_matched": len(scored),
                "n_rejected": len(non_id_rejects),
                "rejected_lines": [
                    {"line_no": r["line_no"],
                     "reason": "no object_id",
                     "text": r["text"]} for r in non_id_rejects
                ],
                "blocks_detected": ["direct_scored"],
            },
        }

    # Pure ranked list
    seen, ranking = set(), []
    for r in unscored:
        if r["object_id"] not in seen:
            seen.add(r["object_id"])
            ranking.append(r["object_id"])
    return {
        "format_detected": "direct_ranked_list",
        "ranking": ranking,
        "scores": {},
        "diagnostics": {
            "n_matched": len(ranking),
            "n_rejected": len(non_id_rejects),
            "rejected_lines": [
                {"line_no": r["line_no"],
                 "reason": "no object_id",
                 "text": r["text"]} for r in non_id_rejects
            ],
            "blocks_detected": ["direct_ranked_list"],
        },
    }


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------

def parse_coco_paste(text: str) -> Dict:
    """
    Top-level parser. Returns the same shape regardless of input format:

        {
          "format_detected": "miau_rangsor+y0" | "direct_scored" |
                             "direct_ranked_list",
          "ranking": [object_id, ...],         # best → worst
          "scores":  {object_id: becsles}      # scored formats only
          "o_to_object_id": {O-label: object_id}  # MIAU format only
          "diagnostics": {
              "blocks_detected": [str, ...],
              "n_matched": int,
              "n_rejected": int,
              "rejected_lines": [{line_no, reason, text}, ...],
              "winner_rule": str,
          }
        }
    """
    if not text or not text.strip():
        raise CocoCompareError("Pasted text is empty.")

    blocks = _split_into_blocks(text)
    detected = [name for name in ("rangsor", "lepcsok1", "lepcsok2", "coco_y0")
                if _has_content(blocks[name])]

    # Diagnostic: detect staircase block paste.
    if not blocks["rangsor"] and not blocks["coco_y0"] and \
            (_has_content(blocks["lepcsok1"]) or _has_content(blocks["lepcsok2"])):
        raise CocoCompareError(
            "This is a staircase diagnostic block (Lépcsők), not the "
            "final COCO object result. Paste the Rangsor block together "
            "with the COCO:Y0 block instead.",
            {"blocks_detected": detected,
             "n_matched": 0, "n_rejected": 0, "rejected_lines": []},
        )

    # MIAU format requires both Rangsor and COCO:Y0.
    if _has_content(blocks["rangsor"]) or _has_content(blocks["coco_y0"]):
        return _parse_miau_format(blocks, detected)

    # Fallback — maybe they pre-converted it themselves.
    direct = _try_parse_direct(text)
    if direct is not None:
        return direct

    # Nothing recognizable.
    raise CocoCompareError(
        "Could not find Rangsor or COCO:Y0 sections, and no object_id "
        "lines either. Paste the FINAL COCO result from the MIAU site, "
        "including both the Rangsor block and the COCO:Y0 block.",
        {"blocks_detected": detected,
         "n_matched": 0, "n_rejected": 0, "rejected_lines": []},
    )


def _has_content(lines: List[str]) -> bool:
    return any(line.strip() and not line.strip().startswith("#") for line in lines)


def _parse_miau_format(blocks: Dict[str, List[str]],
                       detected: List[str]) -> Dict:
    if not _has_content(blocks["rangsor"]):
        raise CocoCompareError(
            "COCO:Y0 rows use internal row labels (O1, O2, …). Please "
            "include the Rangsor block so the app can map them back to "
            "the original object IDs, or paste a converted object_id + "
            "score list instead.",
            {"blocks_detected": detected,
             "n_matched": 0, "n_rejected": 0, "rejected_lines": []},
        )
    if not _has_content(blocks["coco_y0"]):
        raise CocoCompareError(
            "Found the Rangsor block but no COCO:Y0 block. Paste the "
            "final COCO:Y0 result table as well.",
            {"blocks_detected": detected,
             "n_matched": 0, "n_rejected": 0, "rejected_lines": []},
        )

    o_to_oid = _parse_rangsor(blocks["rangsor"])
    y0_pairs = _parse_coco_y0(blocks["coco_y0"])

    rejected: List[Dict] = []
    missing_labels = set()

    # Join COCO:Y0 O-labels back to object_ids through the Rangsor mapping.
    joined: List[Tuple[str, float]] = []
    for o_label, score in y0_pairs:
        if o_label not in o_to_oid:
            missing_labels.add(o_label)
            rejected.append({
                "line_no": 0,
                "reason": f"O-label '{o_label}' not present in Rangsor",
                "text": f"{o_label} {score}",
            })
            continue
        joined.append((o_to_oid[o_label], score))

    if not joined:
        raise CocoCompareError(
            "Could not match any COCO:Y0 rows to Rangsor object IDs. "
            f"COCO rows: {len(y0_pairs)}; Rangsor entries: {len(o_to_oid)}; "
            f"unmatched O-labels: {sorted(missing_labels)[:5]}.",
            {"blocks_detected": detected,
             "n_matched": 0,
             "n_rejected": len(rejected),
             "rejected_lines": rejected},
        )

    # Sort. Stable — preserves MIAU row order for ties.
    joined.sort(key=lambda p: p[1], reverse=BECSLES_HIGHER_IS_BETTER)

    winner_rule = ("higher Becslés = better" if BECSLES_HIGHER_IS_BETTER
                   else "lower Becslés = better")

    return {
        "format_detected": "miau_rangsor+y0",
        "ranking": [oid for oid, _ in joined],
        "scores": {oid: s for oid, s in joined},
        "o_to_object_id": o_to_oid,
        "diagnostics": {
            "blocks_detected": detected,
            "n_matched": len(joined),
            "n_rejected": len(rejected),
            "rejected_lines": rejected,
            "winner_rule": winner_rule,
            "rangsor_count": len(o_to_oid),
            "y0_row_count": len(y0_pairs),
        },
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
    parsed = parse_coco_paste(coco_paste_text)
    coco_picks = _coco_pick_per_image(parsed["ranking"])
    persisted = _persisted_picks()

    if not persisted:
        raise CocoCompareError(
            "No processed images in this app instance. Upload at least one "
            "image before comparing.",
            parsed.get("diagnostics", {}),
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
            "app_vs_coco_agree":   app_vs_coco,
            "topsis_vs_coco_agree": topsis_vs_coco,
            "primary_rule_used": persisted[image_id]["primary_rule_used"],
            "coco_pick_present_in_app_corpus": True,
        })

    def _rate(num: int) -> float:
        return round(100.0 * num / compared, 2) if compared else 0.0

    return {
        "format_detected": parsed["format_detected"],
        "diagnostics": parsed["diagnostics"],
        "o_to_object_id": parsed.get("o_to_object_id", {}),
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
