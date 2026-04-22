"""
Tests for tools.llm_benchmark_compare response parsing.

Run: python3 tests/test_llm_benchmark.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.llm_benchmark_compare import extract_pick, extract_confidence


# ---------------------------------------------------------------------------
# Extract pick — various LLM response shapes
# ---------------------------------------------------------------------------

def test_extract_standard_format():
    r = """Recommended variant: AVIF, quality 50
Justification: Smallest file while SSIM remains high.
Confidence: high, because the trade-off is clear."""
    pick = extract_pick(r)
    assert pick == {"format": "avif", "encoder_quality_param": 50}, pick
    print("  extract_standard_format: OK")


def test_extract_q_shorthand():
    r = "After analysis, I recommend AVIF q50."
    pick = extract_pick(r)
    assert pick == {"format": "avif", "encoder_quality_param": 50}
    print("  extract_q_shorthand: OK")


def test_extract_quality_n():
    r = "The best option is WebP at quality 80."
    pick = extract_pick(r)
    assert pick == {"format": "webp", "encoder_quality_param": 80}
    print("  extract_quality_n: OK")


def test_extract_comma_quality():
    r = "My pick: JPEG, quality 90."
    pick = extract_pick(r)
    assert pick == {"format": "jpeg", "encoder_quality_param": 90}
    print("  extract_comma_quality: OK")


def test_extract_with_parens():
    r = "Recommended variant: WebP (quality 80)."
    pick = extract_pick(r)
    assert pick == {"format": "webp", "encoder_quality_param": 80}
    print("  extract_with_parens: OK")


def test_extract_jpg_aliased_to_jpeg():
    r = "Recommended variant: JPG quality 70."
    pick = extract_pick(r)
    assert pick == {"format": "jpeg", "encoder_quality_param": 70}
    print("  extract_jpg_aliased_to_jpeg: OK")


def test_extract_picks_first_match():
    """
    When multiple format references appear (e.g., discussing alternatives),
    the parser picks the one that matches the primary 'Recommended variant'
    pattern.
    """
    r = """Alternative if quality matters: JPEG, quality 90
Recommended variant: AVIF, quality 50
Alternative if size matters: WebP, quality 60"""
    pick = extract_pick(r)
    # The primary "Recommended variant:" pattern wins.
    assert pick == {"format": "avif", "encoder_quality_param": 50}, pick
    print("  extract_picks_first_match: OK")


def test_extract_returns_none_on_unparseable():
    r = "I cannot decide without more information."
    pick = extract_pick(r)
    assert pick is None
    print("  extract_returns_none_on_unparseable: OK")


# ---------------------------------------------------------------------------
# Confidence extraction
# ---------------------------------------------------------------------------

def test_extract_confidence_high():
    r = "Confidence: high, because the margin is large."
    assert extract_confidence(r) == "high"
    print("  extract_confidence_high: OK")


def test_extract_confidence_low():
    r = """Confidence: low, because the Pareto front has near-identical
choices."""
    assert extract_confidence(r) == "low"
    print("  extract_confidence_low: OK")


def test_extract_confidence_missing():
    r = "I recommend AVIF q50."
    assert extract_confidence(r) == ""
    print("  extract_confidence_missing: OK")


def main():
    print("=== extract_pick ===")
    test_extract_standard_format()
    test_extract_q_shorthand()
    test_extract_quality_n()
    test_extract_comma_quality()
    test_extract_with_parens()
    test_extract_jpg_aliased_to_jpeg()
    test_extract_picks_first_match()
    test_extract_returns_none_on_unparseable()
    print()
    print("=== extract_confidence ===")
    test_extract_confidence_high()
    test_extract_confidence_low()
    test_extract_confidence_missing()
    print()
    print("ALL LLM BENCHMARK PARSER TESTS PASSED")


if __name__ == "__main__":
    main()
