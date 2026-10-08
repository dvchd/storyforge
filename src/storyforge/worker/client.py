"""Client HTTP của worker. Chỉ dùng API /api/worker của app."""
from __future__ import annotations

from pathlib import Path

import httpx

from storyforge.protocol import (SCHEMA_VERSION, ClaimedJob, ClaimRequest, CompleteRequest, FailRequest,
                                 HeartbeatRequest)


class WorkerClient:
    def __init__(self, base_url: str, token: str, worker_id: str, http: httpx.Client | None = None,
                 timeout: float = 60.0) -> None:
        self.worker_id = worker_id
        headers = {"X-Worker-Token": token, "X-Worker-Id": worker_id, "X-Schema-Version": str(SCHEMA_VERSION)}
        if http is None:
            self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
        else:
            self.http = http
            self.http.headers.update(headers)

    def ping(self) -> dict:
        r = self.http.get("/api/worker/ping")
        r.raise_for_status()
        return r.json()

    def claim(self, req: ClaimRequest) -> ClaimedJob | None:
        r = self.http.post("/api/worker/claim", json=req.model_dump())
        if r.status_code == 204:
            return None
        if r.status_code == 409:
            raise RuntimeError(r.json().get("detail", "Khác phiên bản giao thức"))
        r.raise_for_status()
        return ClaimedJob(**r.json())

    def heartbeat(self, job_id: int, progress: float | None = None, message: str = "") -> bool:
        """Gia hạn thuê job, gửi tiến độ. Trả True nếu app yêu cầu hủy."""
        r = self.http.post(f"/api/worker/jobs/{job_id}/heartbeat",
                           json=HeartbeatRequest(progress=progress, message=message).model_dump())
        r.raise_for_status()
        return bool(r.json().get("cancel"))

    def fetch_asset(self, asset_id: int, dest: Path) -> str:
        """Tải file, trả sha256 app khai báo (để worker kiểm tra toàn vẹn)."""
        with self.http.stream("GET", f"/api/worker/assets/{asset_id}") as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
            return r.headers.get("x-asset-sha256", "")

    def upload(self, job_id: int, path: Path) -> str:
        r = self.http.post(f"/api/worker/jobs/{job_id}/artifact", params={"name": path.name},
                           content=path.read_bytes(), headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()
        return r.json()["artifact"]

    def complete(self, job_id: int, req: CompleteRequest) -> None:
        r = self.http.post(f"/api/worker/jobs/{job_id}/complete", json=req.model_dump())
        r.raise_for_status()

    def fail(self, job_id: int, req: FailRequest) -> None:
        r = self.http.post(f"/api/worker/jobs/{job_id}/fail", json=req.model_dump())
        r.raise_for_status()
