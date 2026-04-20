"""
Identity helpers. IDs must be stable and deterministic so that re-running
the pipeline on the same corpus produces the same OAM rows.

Schema:
    image_id  = img{NNN}                  # zero-padded corpus-sequential
    object_id = {image_id}_{format}_q{encoder_quality_param}

Example:
    image_id  = img001
    object_id = img001_webp_q80
"""

from __future__ import annotations

import re
from pathlib import Path


_IMAGE_ID_RE = re.compile(r"^img(\d+)$")


def next_image_id(images_dir: Path) -> str:
    """
    Returns the next image_id based on existing originals in images_dir.
    Zero-padded to 3 digits. Deterministic: scans filesystem, picks
    max(existing) + 1.
    """
    max_n = 0
    if images_dir.exists():
        for p in images_dir.iterdir():
            if not p.is_file():
                continue
            m = _IMAGE_ID_RE.match(p.stem)
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
    return f"img{max_n + 1:03d}"


def build_object_id(image_id: str, fmt: str, encoder_quality_param: int) -> str:
    return f"{image_id}_{fmt}_q{encoder_quality_param}"
