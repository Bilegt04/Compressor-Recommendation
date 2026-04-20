"""
Presentation layer. Translates internal rule tags and raw metric deltas into
plain-language copy that the user-facing UI can show without exposing thesis
jargon.

Inputs are the existing per-image payload fields — this module adds, never
mutates or removes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# Map internal rule tags → short labels + longer explanations.
_RULE_LABELS: Dict[str, Dict[str, str]] = {
    "pareto_ssim>=0.95_min_size": {
        "label": "Best balance",
        "why": "Smallest file among visually acceptable results "
               "(SSIM stays at or above 0.95).",
    },
    "pareto_fallback_max_ssim": {
        "label": "Highest quality available",
        "why": "No option met the preferred quality target, so the "
               "highest-quality efficient option was chosen.",
    },
    "topsis_equal_weights": {
        "label": "Balanced trade-off",
        "why": "Best overall trade-off across file size and image quality.",
    },
}

_DEFAULT_LABEL = {"label": "Recommended", "why": "Selected as the best option."}


def _friendly_format_name(fmt: str) -> str:
    return {"jpeg": "JPEG", "webp": "WebP", "avif": "AVIF"}.get(fmt.lower(),
                                                                 fmt.upper())


def _percent_saved(original_kb: float, compressed_kb: float) -> float:
    if original_kb <= 0:
        return 0.0
    return round((original_kb - compressed_kb) / original_kb * 100.0, 2)


def _percent_vs(reference_kb: float, candidate_kb: float) -> float:
    """Positive = candidate is smaller than reference."""
    if reference_kb <= 0:
        return 0.0
    return round((reference_kb - candidate_kb) / reference_kb * 100.0, 2)


def _jpeg_q90(variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for v in variants:
        if v["format"] == "jpeg" and v["encoder_quality_param"] == 90:
            return v
    return None


def build_friendly_recommendation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a compact, user-facing object:

        {
          "label": "Best balance",
          "format_name": "AVIF",
          "encoder_quality_param": 50,
          "headline": "Recommended: AVIF, quality 50",
          "why": "Smallest file among visually acceptable results.",
          "comparison": [
             "Saves 32.84% compared with the original.",
             "Keeps SSIM at 0.9767.",
             "Cuts file size by 50.5% vs. JPEG q90.",
          ],
          "quality_indicator": "high" | "acceptable" | "low",
        }

    Never raises — if data is missing, returns a minimal payload so the UI
    can still render something useful.
    """
    rec = payload.get("recommended") or {}
    variants = payload.get("variants") or []

    fmt = rec.get("format", "")
    q = rec.get("encoder_quality_param", "")
    ssim = rec.get("ssim")
    original_kb = rec.get("original_size_kb")
    compressed_kb = rec.get("compressed_size_kb")

    rule = rec.get("recommendation_rule_used", "")
    labels = _RULE_LABELS.get(rule, _DEFAULT_LABEL)

    # Quality indicator derived from SSIM — purely for UI coloring.
    quality_indicator = "acceptable"
    if isinstance(ssim, (int, float)):
        if ssim >= 0.95:
            quality_indicator = "high"
        elif ssim < 0.90:
            quality_indicator = "low"

    comparison: List[str] = []
    if (isinstance(original_kb, (int, float))
            and isinstance(compressed_kb, (int, float))):
        saved = _percent_saved(original_kb, compressed_kb)
        comparison.append(
            f"Saves {saved:.2f}% compared with the original "
            f"({compressed_kb:.2f} KB vs. {original_kb:.2f} KB)."
        )
    if isinstance(ssim, (int, float)):
        comparison.append(f"Keeps SSIM at {ssim:.4f}.")

    # Against JPEG q90 as the universal reference — skip if the recommendation
    # itself is JPEG q90.
    ref = _jpeg_q90(variants)
    if (ref and not (fmt == "jpeg" and q == 90)
            and isinstance(compressed_kb, (int, float))):
        delta = _percent_vs(ref["compressed_size_kb"], compressed_kb)
        if delta > 0:
            comparison.append(
                f"Cuts file size by {delta:.1f}% vs. JPEG q90 "
                f"with only a small quality trade-off."
            )
        elif delta < 0:
            comparison.append(
                f"Uses {abs(delta):.1f}% more space than JPEG q90 but "
                f"preserves image quality better."
            )

    return {
        "label": labels["label"],
        "format_name": _friendly_format_name(str(fmt)),
        "encoder_quality_param": q,
        "headline": f"Recommended: {_friendly_format_name(str(fmt))}, quality {q}",
        "why": labels["why"],
        "comparison": comparison,
        "quality_indicator": quality_indicator,
    }


def build_friendly_variant(v: Dict[str, Any],
                           original_kb: float) -> Dict[str, Any]:
    """Per-variant friendly view for the comparison cards/table."""
    compressed_kb = v.get("compressed_size_kb", 0.0)
    return {
        "format_name": _friendly_format_name(v.get("format", "")),
        "encoder_quality_param": v.get("encoder_quality_param"),
        "compressed_size_kb": compressed_kb,
        "percent_saved": _percent_saved(original_kb, compressed_kb),
        "psnr": v.get("psnr"),
        "ssim": v.get("ssim"),
        "is_recommended": bool(v.get("is_recommended", False)),
        "is_efficient": bool(v.get("is_pareto", False)),  # friendly rename
    }
