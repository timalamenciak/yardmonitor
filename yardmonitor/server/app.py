"""FastAPI server application — handles file uploads, AI job dispatch, and web UI."""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import Database
from .jobs import JobQueue
from .models_registry import ModelsRegistry
from .pipeline_runner import ServerPipelineRunner

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


def _load_config() -> dict:
    path = os.environ.get("YARDMONITOR_CONFIG", "config/server.yaml")
    models_path = os.environ.get("YARDMONITOR_MODELS", "config/models.yaml")
    config: dict = {}
    if Path(path).exists():
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    config["_models_path"] = models_path
    return config


# ── Application lifespan ──────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = _load_config()
    data_dir = Path(config.get("server", {}).get("data_dir", "data"))
    workers = int(os.environ.get("YARDMONITOR_WORKERS", "0")) or config.get("server", {}).get("pipeline_workers", 1)

    db = Database(data_dir)
    db.init()

    models = ModelsRegistry(config.get("_models_path", "config/models.yaml"))
    runner = ServerPipelineRunner(config, db, models, data_dir)
    queue = JobQueue(db, runner, max_workers=workers)

    app.state.config = config
    app.state.db = db
    app.state.models = models
    app.state.queue = queue
    app.state.data_dir = data_dir

    logger.info("YardMonitor server ready — data_dir=%s", data_dir)
    yield
    queue.shutdown(wait=False)


# ── App init ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="YardMonitor",
    description="Ecological monitoring server — camera trap + AudioMoth processing",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _db(request: Request) -> Database:
    return request.app.state.db


def _queue(request: Request) -> JobQueue:
    return request.app.state.queue


def _models(request: Request) -> ModelsRegistry:
    return request.app.state.models


def _data_dir(request: Request) -> Path:
    return request.app.state.data_dir


def _dep_dir(data_dir: Path, dep_id: str) -> Path:
    return data_dir / "deployments" / dep_id


# ── Media serving ─────────────────────────────────────────────────────────────


@app.get("/media/{dep_id}/{filename:path}")
async def serve_media(dep_id: str, filename: str, request: Request):
    """Serve a raw media file (image or audio) from a deployment."""
    path = _dep_dir(_data_dir(request), dep_id) / "media" / filename
    if not path.is_file():
        raise HTTPException(404, "Media file not found")
    return FileResponse(path)


@app.get("/thumb/{dep_id}/{filename:path}")
async def serve_thumb(dep_id: str, filename: str, request: Request):
    """Serve a 400×300 JPEG thumbnail, generating and caching it on first access."""
    media_path = _dep_dir(_data_dir(request), dep_id) / "media" / filename
    if not media_path.is_file():
        raise HTTPException(404, "Media file not found")

    thumb_dir = _dep_dir(_data_dir(request), dep_id) / "thumbs"
    thumb_path = thumb_dir / (Path(filename).stem + ".jpg")

    if not thumb_path.exists():
        try:
            from PIL import Image
            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(media_path) as img:
                img.thumbnail((400, 300))
                img.convert("RGB").save(thumb_path, "JPEG", quality=80)
        except Exception:
            return FileResponse(media_path)

    return FileResponse(thumb_path)


# ── REST API ──────────────────────────────────────────────────────────────────


@app.post("/api/deployments")
async def api_create_deployment(
    request: Request,
    sensor_type: str = Form(...),
    location_name: str = Form(""),
    sensor_id: str = Form(""),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    deployment_id: Optional[str] = Form(None),
):
    """Register a new deployment and get its ID back."""
    if sensor_type not in ("camera_trap", "audiomoth"):
        raise HTTPException(422, "sensor_type must be 'camera_trap' or 'audiomoth'")

    dep_id = _db(request).create_deployment(
        sensor_type=sensor_type,
        sensor_id=sensor_id,
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        deployment_id=deployment_id,
    )
    dep_dir = _dep_dir(_data_dir(request), dep_id)
    (dep_dir / "media").mkdir(parents=True, exist_ok=True)
    return {"deployment_id": dep_id, "status": "uploading"}


@app.post("/api/deployments/{dep_id}/upload")
async def api_upload_file(
    dep_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    """Upload a single media file to a deployment's media directory."""
    dep = _db(request).get_deployment(dep_id)
    if dep is None:
        raise HTTPException(404, "Deployment not found")

    media_dir = _dep_dir(_data_dir(request), dep_id) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file.filename).name
    dest = media_dir / safe_name

    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _write_upload(file.file, dest))

    _db(request).increment_file_count(dep_id)
    return {"filename": safe_name, "size": dest.stat().st_size}


def _write_upload(src, dest: Path) -> None:
    with open(dest, "wb") as f:
        shutil.copyfileobj(src, f)


@app.get("/api/deployments/{dep_id}/files")
async def api_list_uploaded_files(dep_id: str, request: Request):
    """Return a list of filenames already present in this deployment's media directory."""
    dep = _db(request).get_deployment(dep_id)
    if dep is None:
        raise HTTPException(404, "Deployment not found")
    media_dir = _dep_dir(_data_dir(request), dep_id) / "media"
    if not media_dir.exists():
        return []
    return sorted(p.name for p in media_dir.iterdir() if p.is_file())


@app.post("/api/deployments/{dep_id}/process")
async def api_trigger_processing(dep_id: str, request: Request):
    """Submit a deployment for AI processing."""
    dep = _db(request).get_deployment(dep_id)
    if dep is None:
        raise HTTPException(404, "Deployment not found")
    _db(request).update_deployment(dep_id, status="queued")
    job_id = _queue(request).submit(dep_id)
    return {"job_id": job_id, "deployment_id": dep_id}


@app.get("/api/deployments")
async def api_list_deployments(
    request: Request,
    sensor_type: Optional[str] = None,
    status: Optional[str] = None,
):
    return _db(request).list_deployments(sensor_type=sensor_type, status=status)


@app.get("/api/deployments/{dep_id}")
async def api_get_deployment(dep_id: str, request: Request):
    dep = _db(request).get_deployment(dep_id)
    if dep is None:
        raise HTTPException(404)
    stats = _db(request).get_deployment_stats(dep_id)
    return {**dep, **stats}


@app.get("/api/observations")
async def api_observations(
    request: Request,
    sensor_type: Optional[str] = None,
    deployment_id: Optional[str] = None,
    scientific_name: Optional[str] = None,
    detection_type: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    min_confidence: Optional[float] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    rows, total = _db(request).query_observations(
        sensor_type=sensor_type,
        deployment_id=deployment_id,
        scientific_name=scientific_name,
        detection_type=detection_type,
        from_date=from_date,
        to_date=to_date,
        min_confidence=min_confidence,
        page=page,
        per_page=per_page,
    )
    return {"total": total, "page": page, "per_page": per_page, "results": rows}


@app.get("/api/species")
async def api_species(request: Request):
    return _db(request).get_species_summary()


@app.get("/api/jobs")
async def api_list_jobs(request: Request):
    return _db(request).list_jobs()


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str, request: Request):
    job = _db(request).get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return job


@app.get("/api/models")
async def api_models(request: Request):
    return _models(request).all()


# ── Download endpoints ────────────────────────────────────────────────────────


@app.get("/download/{dep_id}/camtrap-dp")
async def download_camtrap_dp(dep_id: str, request: Request):
    """Stream a ZIP of the deployment's Camtrap DP package."""
    import zipfile, io as _io
    dep_dir = _dep_dir(_data_dir(request), dep_id)
    files = list(dep_dir.glob("*.csv")) + list(dep_dir.glob("*.json"))
    if not files:
        raise HTTPException(404, "No Camtrap DP files found for this deployment")

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    buf.seek(0)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{dep_id}_camtrap_dp.zip"'},
    )


@app.get("/download/{dep_id}/darwin-core")
async def download_darwin_core(dep_id: str, request: Request):
    """Stream the Darwin Core Archive ZIP for an AudioMoth deployment."""
    dwca_path = _dep_dir(_data_dir(request), dep_id) / f"{dep_id}_dwca.zip"
    if not dwca_path.is_file():
        raise HTTPException(404, "Darwin Core Archive not found for this deployment")
    return FileResponse(
        dwca_path,
        media_type="application/zip",
        filename=f"{dep_id}_darwin_core.zip",
    )


# ── Web UI — HTML routes ──────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def ui_dashboard(request: Request):
    stats = _db(request).get_dashboard_stats()
    jobs = _db(request).list_jobs(limit=5)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "recent_jobs": jobs},
    )


@app.get("/deployments", response_class=HTMLResponse)
async def ui_deployments(
    request: Request,
    sensor_type: Optional[str] = None,
    status: Optional[str] = None,
):
    deps = _db(request).list_deployments(sensor_type=sensor_type, status=status)
    return templates.TemplateResponse(
        request,
        "deployments.html",
        {
            "deployments": deps,
            "filter_sensor": sensor_type or "",
            "filter_status": status or "",
        },
    )


@app.get("/deployments/{dep_id}", response_class=HTMLResponse)
async def ui_deployment_detail(dep_id: str, request: Request):
    dep = _db(request).get_deployment(dep_id)
    if dep is None:
        raise HTTPException(404)
    stats = _db(request).get_deployment_stats(dep_id)
    media = _db(request).list_media(dep_id)
    jobs = [j for j in _db(request).list_jobs() if j["deployment_id"] == dep_id]
    return templates.TemplateResponse(
        request,
        "deployment_detail.html",
        {
            "dep": dep,
            "stats": stats,
            "media": media,
            "jobs": jobs,
        },
    )


@app.get("/observations", response_class=HTMLResponse)
async def ui_observations(
    request: Request,
    sensor_type: Optional[str] = None,
    deployment_id: Optional[str] = None,
    q: Optional[str] = None,
    detection_type: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    min_confidence: Optional[float] = None,
    page: int = Query(1, ge=1),
):
    per_page = 48
    # Form sends 0-100; DB stores 0-1
    db_min_conf = (min_confidence / 100.0) if min_confidence is not None and min_confidence > 1 else min_confidence
    rows, total = _db(request).query_observations(
        sensor_type=sensor_type,
        deployment_id=deployment_id,
        scientific_name=q,
        detection_type=detection_type,
        from_date=from_date,
        to_date=to_date,
        min_confidence=db_min_conf,
        page=page,
        per_page=per_page,
    )
    deps = _db(request).list_deployments()
    pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "observations.html",
        {
            "observations": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "deployments": deps,
            "filters": {
                "sensor_type": sensor_type or "",
                "deployment_id": deployment_id or "",
                "q": q or "",
                "detection_type": detection_type or "",
                "from_date": from_date or "",
                "to_date": to_date or "",
                "min_confidence": min_confidence if min_confidence is not None else "",
            },
        },
    )


@app.get("/api/timeline")
async def api_timeline(
    request: Request,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    deployment_id: Optional[str] = None,
    location_name: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    return _db(request).get_timeline_data(
        from_date=from_date,
        to_date=to_date,
        deployment_id=deployment_id,
        location_name=location_name,
        limit=limit,
    )


@app.get("/timeline", response_class=HTMLResponse)
async def ui_timeline(
    request: Request,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    deployment_id: Optional[str] = None,
    location_name: Optional[str] = None,
):
    deps = _db(request).list_deployments()
    locations = sorted({d["location_name"] for d in deps if d.get("location_name")})
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "deployments": deps,
            "locations": locations,
            "filters": {
                "from_date": from_date or "",
                "to_date": to_date or "",
                "deployment_id": deployment_id or "",
                "location_name": location_name or "",
            },
        },
    )


@app.get("/jobs", response_class=HTMLResponse)
async def ui_jobs(request: Request):
    jobs = _db(request).list_jobs()
    return templates.TemplateResponse(
        request, "jobs.html", {"jobs": jobs}
    )


@app.get("/models", response_class=HTMLResponse)
async def ui_models(request: Request):
    return templates.TemplateResponse(
        request,
        "models.html",
        {"models": _models(request).all()},
    )
