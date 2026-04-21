"""
FastAPI entrypoint.

Modes:
    - user-test mode (default): upload → variants → metrics → friendly
      recommendation. OAM/export/TOPSIS routes are not registered.
    - thesis mode (ENABLE_OAM_FEATURES=true): every route above is registered,
      including /exports/* and /images/{id}/topsis.

The pipeline itself is identical in both modes — only the exposed surface
differs. This lets the same deployed app be flipped to thesis mode via env
var without a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.config import CORS_ORIGINS, ENABLE_OAM_FEATURES, MAX_UPLOAD_BYTES
from backend.services import (
    compression, metrics, pareto, presentation, recommendation, storage,
    topsis as topsis_mod,
)
from backend.services.compression import CompressionError
from backend.services.ids import build_object_id
from backend.services.metrics import MetricError

# Export service is imported lazily inside OAM routes so the module still
# loads cleanly in user-test mode — but the file stays in the repo.
from backend.services import export_service  # noqa: F401 (kept intentionally)
from backend.services import coco_export      # noqa: F401 (kept intentionally)
from backend.services import coco_compare     # noqa: F401 (kept intentionally)


app = FastAPI(
    title="Image Compression Recommender",
    version="2.1.0",
    description="Upload an image, get the best compression recommendation.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=True),
              name="ui")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _build_variant_record(strategy, image_id: str,
                          metric_values: Dict[str, float]) -> Dict[str, Any]:
    return {
        "object_id": build_object_id(image_id, strategy.format, strategy.quality),
        "format": strategy.format,
        "encoder_quality_param": strategy.quality,
        **metric_values,
    }


def _variant_sig(v: Dict[str, Any]):
    return (v["format"], v["encoder_quality_param"])


def run_pipeline(image_id: str, original_path: Path,
                 source_filename: str,
                 image_category: str = "") -> Dict[str, Any]:
    out_dir = storage.variants_dir(image_id)

    try:
        produced = compression.compress_all(original_path, out_dir)
    except CompressionError as e:
        # User-visible failure — keep wording human-friendly.
        raise HTTPException(
            status_code=500,
            detail=f"One of the image encoders failed. Please try a "
                   f"different image or contact the site owner. ({e})",
        )

    variants: List[Dict[str, Any]] = []
    for rec in produced:
        try:
            mvals = metrics.compute_metrics(original_path, rec["path"])
        except MetricError as e:
            raise HTTPException(status_code=500,
                                detail=f"Could not measure image quality: {e}")
        variants.append(_build_variant_record(rec["strategy"], image_id, mvals))

    front = pareto.get_pareto_front(variants)
    front_sigs = {_variant_sig(v) for v in front}

    recommended = recommendation.recommend(front)
    explanation = recommendation.explain(front, recommended)

    # TOPSIS runs in both modes — its small cost buys thesis-mode data.
    topsis_ranking = topsis_mod.topsis_rank(front)
    recommended_topsis = topsis_mod.recommend_topsis(front)
    rule_comparison = topsis_mod.compare_decision_rules(
        recommended, recommended_topsis, topsis_ranking
    )

    rec_sig = _variant_sig(recommended)
    for v in variants:
        sig = _variant_sig(v)
        v["is_pareto"] = sig in front_sigs
        v["is_recommended"] = sig == rec_sig

    payload = {
        "image_id": image_id,
        "source_filename": source_filename,
        "image_category": image_category,
        "original_path": str(original_path.relative_to(storage.REPO_ROOT)),
        "variants": variants,
        "pareto_front": front,
        "recommended": recommended,
        "recommended_key":
            f"{recommended['format']}_q{recommended['encoder_quality_param']}",
        "explanation": explanation,
        "topsis": {"ranking": topsis_ranking,
                   "recommended": recommended_topsis},
        "decision_rule_comparison": rule_comparison,
    }
    storage.write_result(image_id, payload)
    return payload


def _user_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compose the user-friendly response. Internal fields are preserved
    in storage but not surfaced here."""
    original_kb = payload["recommended"].get("original_size_kb", 0.0)
    return {
        "image_id": payload["image_id"],
        "original_size_kb": original_kb,
        "width_px": payload["recommended"].get("width_px"),
        "height_px": payload["recommended"].get("height_px"),
        "recommendation": presentation.build_friendly_recommendation(payload),
        "variants": [
            presentation.build_friendly_variant(v, original_kb)
            for v in payload["variants"]
        ],
        "recommended_variant_key": payload["recommended_key"],
    }


# ---------------------------------------------------------------------------
# Routes — always available
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "image-compression-recommender",
        "version": "2.1.0",
        "mode": "thesis" if ENABLE_OAM_FEATURES else "user-test",
        "ui": "/ui/",
    }


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/config")
def config():
    """Frontend queries this at load time to decide which panels to show."""
    return {
        "oam_features_enabled": ENABLE_OAM_FEATURES,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


@app.get("/health")
def health():
    """Deployment health probe."""
    import shutil
    return {
        "status": "ok",
        "encoders": {
            "cwebp": shutil.which("cwebp") is not None,
            "avifenc": shutil.which("avifenc") is not None,
        },
    }


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    image_category: Optional[str] = Form(default=""),
):
    if not file.filename:
        raise HTTPException(status_code=400,
                            detail="Please choose an image file.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400,
                            detail="The uploaded file was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (limit: "
                   f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    image_id = storage.new_image_id()
    try:
        original_path = storage.save_original(image_id, data, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Could not save the image: {e}")

    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(original_path) as im:
            im.verify()
    except (UnidentifiedImageError, Exception):
        original_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="That file is not a supported image format. "
                   "Try JPEG, PNG, or WebP.",
        )

    result = run_pipeline(
        image_id, original_path,
        source_filename=file.filename,
        image_category=image_category or "",
    )
    return JSONResponse(_user_view(result))


@app.get("/images/{image_id}")
def get_image(image_id: str):
    """User-facing per-image view — friendly fields only."""
    data = storage.read_result(image_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Image not found.")
    return _user_view(data)


@app.get("/images/{image_id}/preview")
def get_image_preview(image_id: str):
    """Serves the original for in-page preview."""
    for p in storage.IMAGES_DIR.iterdir():
        if p.stem == image_id:
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="Preview not available.")


@app.get("/images/{image_id}/variant/{variant_key}")
def get_variant_file(image_id: str, variant_key: str):
    vdir = storage.variants_dir(image_id)
    for p in vdir.iterdir():
        if p.stem == variant_key:
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="Variant not found.")


# ---------------------------------------------------------------------------
# COCO Y0 export + comparison — ALWAYS available (not gated).
#
# Produces text the user pastes by hand into the external MIAU solver at
# https://miau.my-x.hu/myx-free/coco/beker_y0.php and accepts the pasted
# COCO output back for cross-method comparison. The app does NOT call MIAU.
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402  (kept near consumer for clarity)


class CocoCompareRequest(BaseModel):
    paste: str


@app.get("/coco/preview")
def coco_preview(
    oam_variant: str = Query("minimal", pattern="^(minimal|extended)$"),
    step_count: int = Query(0, ge=0, le=20),
):
    """
    Returns the structured payload + every rendered string. The frontend
    uses these to populate the COCO Y0 panel and to power the Copy /
    Download actions.
    """
    try:
        return coco_export.coco_export_payload(
            variant=oam_variant, step_count=step_count
        )
    except coco_export.CocoExportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/coco/download")
def coco_download(
    oam_variant: str = Query("minimal", pattern="^(minimal|extended)$"),
    step_count: int = Query(0, ge=0, le=20),
):
    """Writes data/exports/coco_input.txt and returns it as attachment."""
    try:
        p = coco_export.write_coco_input_file(
            variant=oam_variant, step_count=step_count
        )
    except coco_export.CocoExportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(str(p), media_type="text/plain",
                        filename="coco_input.txt")


@app.post("/coco/compare")
def coco_compare_endpoint(req: CocoCompareRequest):
    """
    Accept pasted COCO Y0 output and return per-image comparison rows
    plus a corpus-level agreement summary. No external request is made.
    """
    try:
        return coco_compare.build_comparison(req.paste)
    except coco_compare.CocoCompareError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/coco/compare.csv")
def coco_compare_csv_endpoint(req: CocoCompareRequest):
    """CSV form of the comparison, for thesis appendix."""
    try:
        comparison = coco_compare.build_comparison(req.paste)
    except coco_compare.CocoCompareError as e:
        raise HTTPException(status_code=400, detail=str(e))
    csv_text = coco_compare.render_comparison_csv(comparison)
    out_path = storage.EXPORTS_DIR / "coco_comparison.csv"
    out_path.write_text(csv_text, encoding="utf-8")
    return FileResponse(str(out_path), media_type="text/csv",
                        filename="coco_comparison.csv")


# ---------------------------------------------------------------------------
# OAM / thesis routes — registered only when feature flag is on
# ---------------------------------------------------------------------------

if ENABLE_OAM_FEATURES:

    @app.get("/images")
    def list_images():
        return storage.list_results()

    @app.get("/images/{image_id}/raw")
    def get_image_raw(image_id: str):
        """Thesis-mode: full internal dataset with rule tags and TOPSIS."""
        data = storage.read_result(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Unknown image_id.")
        return data

    @app.get("/images/{image_id}/analysis")
    def get_analysis(image_id: str):
        data = storage.read_result(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Unknown image_id.")
        return {
            "image_id": image_id,
            "pareto_front": data["pareto_front"],
            "recommended": data["recommended"],
            "explanation": data["explanation"],
            "ssim_threshold": data["recommended"].get("ssim_threshold"),
            "threshold_unmet": data["recommended"].get("threshold_unmet"),
            "recommendation_rule_used":
                data["recommended"].get("recommendation_rule_used"),
        }

    @app.get("/images/{image_id}/topsis")
    def get_topsis(image_id: str):
        data = storage.read_result(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Unknown image_id.")
        return {
            "ranking": data["topsis"]["ranking"],
            "recommended": data["topsis"]["recommended"],
            "comparison": data["decision_rule_comparison"],
        }

    def _download(path: Path, filename: str) -> FileResponse:
        return FileResponse(str(path), media_type="text/csv",
                            filename=filename)

    @app.post("/exports/all")
    def exports_all(oam_variant: str = Query(
            "minimal", pattern="^(minimal|extended)$")):
        try:
            paths = export_service.export_all(oam_variant=oam_variant)
        except export_service.ExportError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"written": paths, "oam_variant": oam_variant}

    @app.get("/exports/raw_results.csv")
    def exports_raw_results():
        try:
            p = export_service.export_raw_results()
        except export_service.ExportError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _download(p, "raw_results.csv")

    @app.get("/exports/attribute_dictionary.csv")
    def exports_attribute_dictionary():
        p = export_service.export_attribute_dictionary()
        return _download(p, "attribute_dictionary.csv")

    @app.get("/exports/oam.csv")
    def exports_oam(oam_variant: str = Query(
            "minimal", pattern="^(minimal|extended)$")):
        try:
            p = export_service.export_oam(variant=oam_variant)
        except export_service.ExportError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _download(p, "oam.csv")

    @app.get("/exports/analysis.csv")
    def exports_analysis():
        try:
            p = export_service.export_analysis()
        except export_service.ExportError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _download(p, "analysis.csv")
