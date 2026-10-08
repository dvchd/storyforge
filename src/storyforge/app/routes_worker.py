"""API cho worker. Worker không truy cập DB trực tiếp, chỉ gọi các endpoint này."""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from storyforge.protocol import (AI_KINDS, SCHEMA_VERSION, ClaimedJob, ClaimRequest, CompleteRequest, FailRequest,
                                 HeartbeatRequest, HeartbeatResponse)

from . import assets, jobs
from .config import get_settings
from .db import db, jl

router = APIRouter(prefix="/api/worker", tags=["worker"])


def auth(x_worker_token: str = Header(default=""), x_worker_id: str = Header(default="")) -> str:
    if not hmac.compare_digest(x_worker_token or "", get_settings().worker_token):
        raise HTTPException(401, "Sai worker token")
    return x_worker_id


@router.get("/ping")
def ping(_: str = Depends(auth)) -> dict:
    return {"ok": True, "schema_version": SCHEMA_VERSION}


@router.post("/claim", response_model=None)
def claim(req: ClaimRequest, _: str = Depends(auth)):
    if req.schema_version != SCHEMA_VERSION:
        raise HTTPException(409, f"Worker dùng giao thức v{req.schema_version}, app dùng v{SCHEMA_VERSION}. "
                                 "Hãy cập nhật cùng phiên bản StoryForge.")
    caps = [c for c in req.capabilities if c in AI_KINDS]
    jobs.touch_worker(req.worker_id, req.name, caps, req.loaded_models, req.model_ids)
    j = jobs.claim(req.worker_id, caps, req.loaded_models)
    if not j:
        return Response(status_code=204)
    return ClaimedJob(id=j["id"], kind=j["kind"], payload=jl(j["payload_json"], {}), model_hint=j["model_hint"],
                      attempts=j["attempts"], lease_seconds=j["lease_seconds"]).model_dump()


@router.post("/jobs/{job_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(job_id: int, req: HeartbeatRequest | None = Body(default=None), worker_id: str = Depends(auth)):
    req = req or HeartbeatRequest()
    return HeartbeatResponse(**jobs.heartbeat(job_id, worker_id, req.progress, req.message))


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, _: str = Depends(auth)):
    a = db.get("asset", asset_id)
    if not a:
        raise HTTPException(404)
    return FileResponse(assets.abs_path(a), media_type=a["mime"], filename=Path(a["path"]).name,
                        headers={"X-Asset-Sha256": a["sha256"]})


@router.post("/jobs/{job_id}/artifact")
async def upload_artifact(job_id: int, name: str, request: Request, worker_id: str = Depends(auth)):
    j = db.get("job", job_id)
    if not j or j["status"] != "running" or j["worker_id"] != worker_id:
        raise HTTPException(409, "Job không còn thuộc worker này")
    safe = Path(name).name
    if not safe or safe.startswith("."):
        raise HTTPException(400, "Tên file không hợp lệ")
    dst = jobs.artifact_dir(job_id) / safe
    with dst.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"artifact": safe, "size": dst.stat().st_size}


@router.post("/jobs/{job_id}/complete")
def complete(job_id: int, req: CompleteRequest, worker_id: str = Depends(auth)):
    j = db.get("job", job_id)
    if not j or j["worker_id"] != worker_id:
        raise HTTPException(409, "Job không còn thuộc worker này")
    jobs.complete(job_id, req.output, req.model_id, req.elapsed)
    db.ex("UPDATE worker SET jobs_done=jobs_done+1 WHERE id=?", worker_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/fail")
def fail(job_id: int, req: FailRequest, worker_id: str = Depends(auth)):
    j = db.get("job", job_id)
    if not j or j["worker_id"] != worker_id:
        raise HTTPException(409, "Job không còn thuộc worker này")
    jobs.fail(job_id, req.error, req.retryable, req.cancelled)
    return {"ok": True}
