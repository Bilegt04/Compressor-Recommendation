"""
LLM benchmark comparison tool.

Given an LLM's responses to Prompt 2 (one per image), parse each response,
extract the recommended variant, and compare against the app's persisted
recommendations. Emit an annex-ready CSV.

Input format (one JSON file at data/llm_benchmark/responses.jsonl):
    One JSON object per line, with fields:
        {"image_id": "img001", "llm_label": "claude-sonnet-4-5",
         "response": "full LLM response text"}

Usage:
    python -m tools.llm_benchmark_compare

Output:
    data/exports/llm_benchmark_comparison.csv
    Prints corpus-level agreement rates to stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import storage  # noqa: E402


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

# The prompt asks for "Recommended variant: FORMAT, quality N". We try a
# few variations because LLM output varies: "AVIF q50", "AVIF quality 50",
# "AVIF, quality 50", "AVIF at quality 50", "WebP (quality 80)", etc.
_FORMAT_WORD = r"(jpeg|jpg|webp|avif)"
_PATTERNS = [
    # "Recommended variant: AVIF, quality 50"
    re.compile(
        rf"Recommended\s+variant\s*:\s*{_FORMAT_WORD}\s*[,\s]+(?:quality|q|at\s+quality)?\s*(\d+)",
        re.IGNORECASE),
    # "AVIF q50" / "WebP q80"
    re.compile(rf"\b{_FORMAT_WORD}\s*[-_\s]*q\s*(\d+)\b", re.IGNORECASE),
    # "AVIF at quality 50" / "AVIF (quality 50)" / "AVIF, quality 50" /
    # "JPEG quality 90" / "is WebP at quality 80"
    # Allow up to ~20 chars of filler (punctuation, "at", "of", etc.)
    # between the format word and the "quality"/"q" marker, as long as
    # the marker is followed by a number.
    re.compile(rf"\b{_FORMAT_WORD}\b.{{0,20}}?(?:quality|q)\s*(\d+)\b",
               re.IGNORECASE),
]


def extract_pick(response_text: str) -> Optional[Dict[str, str]]:
    """
    Parse the LLM's chosen variant out of its response. Returns
    {"format": "avif", "encoder_quality_param": 50} or None.
    """
    for pattern in _PATTERNS:
        m = pattern.search(response_text)
        if m:
            fmt = m.group(1).lower()
            if fmt == "jpg":
                fmt = "jpeg"
            try:
                q = int(m.group(2))
            except ValueError:
                continue
            return {"format": fmt, "encoder_quality_param": q}
    return None


_CONFIDENCE_RE = re.compile(
    r"confidence\s*:\s*(low|medium|high)", re.IGNORECASE
)


def extract_confidence(response_text: str) -> str:
    m = _CONFIDENCE_RE.search(response_text)
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _persisted_app_picks() -> Dict[str, Dict]:
    out = {}
    for doc in storage.iter_all_results():
        rec = doc.get("recommended", {})
        out[doc["image_id"]] = {
            "format": rec.get("format"),
            "encoder_quality_param": rec.get("encoder_quality_param"),
            "object_id": rec.get("object_id",
                                 f"{doc['image_id']}_{rec.get('format')}"
                                 f"_q{rec.get('encoder_quality_param')}"),
        }
    return out


def build_comparison(responses_path: Path) -> List[Dict]:
    """Return a list of comparison rows."""
    if not responses_path.exists():
        raise SystemExit(f"Responses file not found: {responses_path}")

    app_picks = _persisted_app_picks()
    if not app_picks:
        raise SystemExit(
            "No persisted images in data/results/. Run the app on images "
            "before comparing."
        )

    rows: List[Dict] = []
    with open(responses_path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"WARN line {line_no}: invalid JSON — {e}",
                      file=sys.stderr)
                continue

            image_id = obj.get("image_id", "")
            llm_label = obj.get("llm_label", "")
            response = obj.get("response", "")

            app = app_picks.get(image_id)
            if app is None:
                rows.append({
                    "image_id": image_id,
                    "llm_label": llm_label,
                    "app_pick": "",
                    "llm_pick_format": "",
                    "llm_pick_quality": "",
                    "agree": "",
                    "llm_confidence": "",
                    "parse_status": "image not in app corpus",
                })
                continue

            pick = extract_pick(response)
            confidence = extract_confidence(response)

            if pick is None:
                rows.append({
                    "image_id": image_id,
                    "llm_label": llm_label,
                    "app_pick": f"{app['format']} q{app['encoder_quality_param']}",
                    "llm_pick_format": "",
                    "llm_pick_quality": "",
                    "agree": "",
                    "llm_confidence": confidence,
                    "parse_status": "could not parse LLM pick",
                })
                continue

            agree = (pick["format"] == app["format"]
                     and pick["encoder_quality_param"]
                     == app["encoder_quality_param"])
            rows.append({
                "image_id": image_id,
                "llm_label": llm_label,
                "app_pick": f"{app['format']} q{app['encoder_quality_param']}",
                "llm_pick_format": pick["format"],
                "llm_pick_quality": pick["encoder_quality_param"],
                "agree": agree,
                "llm_confidence": confidence,
                "parse_status": "ok",
            })

    return rows


def write_comparison_csv(rows: List[Dict], path: Path) -> None:
    cols = ["image_id", "llm_label", "app_pick", "llm_pick_format",
            "llm_pick_quality", "agree", "llm_confidence", "parse_status"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def print_summary(rows: List[Dict]) -> None:
    # Group by LLM
    by_llm: Dict[str, List[Dict]] = {}
    for r in rows:
        by_llm.setdefault(r["llm_label"] or "(unknown)", []).append(r)

    print("LLM benchmark comparison")
    print(f"  total response rows : {len(rows)}")
    for llm, group in by_llm.items():
        parsed = [r for r in group if r["parse_status"] == "ok"]
        agreements = sum(1 for r in parsed if r["agree"] is True)
        unparseable = sum(1 for r in group
                          if r["parse_status"] == "could not parse LLM pick")
        missing = sum(1 for r in group
                      if r["parse_status"] == "image not in app corpus")
        total = len(parsed)
        rate = 100.0 * agreements / max(total, 1)
        print(f"\n  {llm}:")
        print(f"    parsed picks        : {total}")
        print(f"    agreements with app : {agreements} ({rate:.1f}%)")
        print(f"    unparseable         : {unparseable}")
        print(f"    missing from corpus : {missing}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="llm_benchmark_compare",
        description="Compare LLM Prompt 2 responses against app recommendations.",
    )
    p.add_argument("--responses", type=Path,
                   default=Path("data/llm_benchmark/responses.jsonl"),
                   help="Path to JSONL file with LLM responses.")
    p.add_argument("--output", type=Path,
                   default=storage.EXPORTS_DIR / "llm_benchmark_comparison.csv")
    args = p.parse_args(argv)

    rows = build_comparison(args.responses)
    write_comparison_csv(rows, args.output)
    print_summary(rows)
    print(f"\nWrote: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
