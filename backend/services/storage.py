"""
Filesystem layout:
    /data/images/{image_id}{.ext}
    /data/variants/{image_id}/{format}_q{encoder_quality_param}.{format}
    /data/results/{image_id}.json
    /data/exports/{raw_results,attribute_dictionary,oam,analysis}.csv

Atomic writes for JSON results — never leave a partial file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from backend.services.ids import next_image_id

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
IMAGES_DIR = DATA_ROOT / "images"
VARIANTS_DIR = DATA_ROOT / "variants"
RESULTS_DIR = DATA_ROOT / "results"
EXPORTS_DIR = DATA_ROOT / "exports"

for _d in (IMAGES_DIR, VARIANTS_DIR, RESULTS_DIR, EXPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def new_image_id() -> str:
    return next_image_id(IMAGES_DIR)


def save_original(image_id: str, data: bytes, original_name: str) -> Path:
    ext = Path(original_name).suffix.lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}:
        ext = ".png"
    dst = IMAGES_DIR / f"{image_id}{ext}"
    dst.write_bytes(data)
    return dst


def variants_dir(image_id: str) -> Path:
    d = VARIANTS_DIR / image_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_path(image_id: str) -> Path:
    return RESULTS_DIR / f"{image_id}.json"


def write_result(image_id: str, payload: Dict[str, Any]) -> Path:
    """Atomic write — spec forbids partial results."""
    payload = dict(payload)
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    path = results_path(image_id)
    fd, tmp = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp.json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_result(image_id: str) -> Optional[Dict[str, Any]]:
    p = results_path(image_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_results() -> List[Dict[str, Any]]:
    out = []
    for p in sorted(RESULTS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "image_id": data.get("image_id", p.stem),
                "created_at": data.get("created_at"),
                "recommended": data.get("recommended"),
                "n_variants": len(data.get("variants", [])),
            })
        except Exception:
            continue
    return out


def iter_all_results() -> List[Dict[str, Any]]:
    """Return every persisted result document. Sorted by image_id for
    deterministic export ordering."""
    docs = []
    for p in sorted(RESULTS_DIR.glob("*.json")):
        try:
            docs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return docs
