"""
Attribute computation for each variant.

Attribute directions:
    compressed_size_kb    : minimize
    psnr                  : maximize
    ssim                  : maximize
    size_reduction_pct    : maximize
    compression_ratio     : maximize

All values are computed — a variant is never stored with a partial metric set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import numpy as np
from PIL import Image
from skimage.metrics import peak_signal_noise_ratio, structural_similarity


class MetricError(RuntimeError):
    pass


# Spec §data-quality rounding.
ROUND_SIZE = 2        # KB
ROUND_RATIO = 2       # compression_ratio, size_reduction_pct
ROUND_PSNR = 3
ROUND_SSIM = 4


def _load_rgb_array(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def _file_size_kb(path: Path) -> float:
    return path.stat().st_size / 1024.0


def _image_dimensions(path: Path) -> Tuple[int, int]:
    with Image.open(path) as im:
        return int(im.width), int(im.height)


def compute_metrics(original_path: Path, variant_path: Path) -> Dict[str, float]:
    """
    Compare variant against original. Returns the full attribute set required
    by the OAM export, pre-rounded to thesis-presentation precision.
    """
    if not original_path.exists():
        raise MetricError(f"Original missing: {original_path}")
    if not variant_path.exists():
        raise MetricError(f"Variant missing: {variant_path}")

    orig = _load_rgb_array(original_path)
    comp = _load_rgb_array(variant_path)

    if orig.shape != comp.shape:
        raise MetricError(
            f"Shape mismatch: orig={orig.shape} vs variant={comp.shape}. "
            f"Encoder must preserve resolution."
        )

    psnr = peak_signal_noise_ratio(orig, comp, data_range=255)
    ssim = structural_similarity(
        orig, comp,
        channel_axis=-1,
        data_range=255,
        win_size=7,
    )

    psnr_val = 99.0 if np.isinf(psnr) else float(psnr)

    original_size_kb = _file_size_kb(original_path)
    compressed_size_kb = _file_size_kb(variant_path)

    if compressed_size_kb <= 0:
        raise MetricError(f"Variant has zero size: {variant_path}")

    compression_ratio = original_size_kb / compressed_size_kb
    size_reduction_pct = (
        (original_size_kb - compressed_size_kb) / original_size_kb * 100.0
        if original_size_kb > 0 else 0.0
    )
    width_px, height_px = _image_dimensions(variant_path)

    return {
        "original_size_kb":   round(float(original_size_kb),   ROUND_SIZE),
        "compressed_size_kb": round(float(compressed_size_kb), ROUND_SIZE),
        "compression_ratio":  round(float(compression_ratio),  ROUND_RATIO),
        "size_reduction_pct": round(float(size_reduction_pct), ROUND_RATIO),
        "psnr":               round(float(psnr_val),           ROUND_PSNR),
        "ssim":               round(float(ssim),               ROUND_SSIM),
        "width_px":           width_px,
        "height_px":          height_px,
    }
