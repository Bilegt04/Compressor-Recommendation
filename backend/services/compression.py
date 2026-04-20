"""
Compression strategies. Each strategy is an object with a stable identity
(format + quality) and is materialized as a file variant on disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from PIL import Image


# Mandatory strategy set defined by the spec.
STRATEGIES: List["Strategy"] = []


@dataclass(frozen=True)
class Strategy:
    format: str            # "jpeg" | "webp" | "avif"
    quality: int
    encoder: str           # "pillow" | "cwebp" | "avifenc"

    @property
    def key(self) -> str:
        return f"{self.format}_q{self.quality}"

    def output_filename(self) -> str:
        return f"{self.key}.{self.format}"


STRATEGIES = [
    Strategy("jpeg", 90, "pillow"),
    Strategy("jpeg", 70, "pillow"),
    Strategy("webp", 80, "cwebp"),
    Strategy("webp", 60, "cwebp"),
    Strategy("avif", 50, "avifenc"),
]


class CompressionError(RuntimeError):
    pass


def _ensure_encoder(binary: str) -> None:
    if shutil.which(binary) is None:
        raise CompressionError(
            f"Required encoder '{binary}' not found on PATH. "
            f"Install it before running the pipeline."
        )


def _compress_jpeg(src: Path, dst: Path, quality: int) -> None:
    with Image.open(src) as im:
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(dst, format="JPEG", quality=quality, optimize=True)


def _compress_cwebp(src: Path, dst: Path, quality: int) -> None:
    _ensure_encoder("cwebp")
    # -q sets lossy quality; -mt enables multithread.
    result = subprocess.run(
        ["cwebp", "-q", str(quality), "-mt", str(src), "-o", str(dst)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CompressionError(f"cwebp failed: {result.stderr.strip()}")


def _compress_avifenc(src: Path, dst: Path, quality: int) -> None:
    _ensure_encoder("avifenc")
    # avifenc uses --min/--max quantizers (0=lossless, 63=worst).
    # Map quality (0..100) to quantizer (63..0) linearly.
    q = max(0, min(63, round((100 - quality) * 63 / 100)))
    # avifenc requires a supported input (PNG/Y4M/JPEG). Convert via Pillow if needed.
    tmp_png = dst.with_suffix(".src.png")
    with Image.open(src) as im:
        im.convert("RGB").save(tmp_png, format="PNG")
    try:
        result = subprocess.run(
            ["avifenc", "--min", str(q), "--max", str(q),
             "-j", "all", str(tmp_png), str(dst)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise CompressionError(f"avifenc failed: {result.stderr.strip()}")
    finally:
        if tmp_png.exists():
            tmp_png.unlink()


def compress(src: Path, strategy: Strategy, out_dir: Path) -> Path:
    """
    Apply a single strategy. Returns the path of the produced variant.
    Never overwrites silently: caller controls out_dir per image_id.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / strategy.output_filename()

    if strategy.encoder == "pillow":
        _compress_jpeg(src, dst, strategy.quality)
    elif strategy.encoder == "cwebp":
        _compress_cwebp(src, dst, strategy.quality)
    elif strategy.encoder == "avifenc":
        _compress_avifenc(src, dst, strategy.quality)
    else:
        raise CompressionError(f"Unknown encoder: {strategy.encoder}")

    if not dst.exists() or dst.stat().st_size == 0:
        raise CompressionError(f"Variant not produced or empty: {dst}")
    return dst


def compress_all(src: Path, out_dir: Path) -> List[dict]:
    """
    Apply the full mandatory strategy set. Fails hard if any strategy fails —
    partial datasets are not allowed per spec.
    Returns a list of {strategy, path} records.
    """
    records = []
    for strategy in STRATEGIES:
        path = compress(src, strategy, out_dir)
        records.append({"strategy": strategy, "path": path})
    return records
