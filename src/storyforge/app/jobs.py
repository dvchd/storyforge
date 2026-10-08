"""Hang doi job tren SQLite.

App chi tao job va xu ly ket qua; worker nhan job qua HTTP. Nhan job dung mot
cau UPDATE ... RETURNING nen an toan khi co nhieu worker cung luc. Job het han
thue (lease) ma khong co heartbeat se tu quay lai hang doi.
"""
from __future__ import annotations

import logging
import shutil
from typing import Any

from .config import get_settings
from .db import db, jd, jl, now

log = logging.getLogger("storyforge.jobs")


def enqueue(project_id: int | None, kind: str, payload: dict, owner_type: str, owner_id: int,
            input_hash: str = "", model_hint: str = "", priority: int = 0, max_attempts: int = 3) -> int:
    existing = db.one(
        "SELECT id FROM job WHERE owner_type=? AND owner_id=? AND kind=? AND status IN ('queued','running')",
        owner_type, owner_id, kind)
    if existing:
        return existing["id"]
    return db.insert("job", project_id=project_id, kind=kind, payload_json=jd(payload), model_hint=model_hint or "",
                     priority=priority, status="queued", max_attempts=max_attempts, owner_type=owner_type,
                     owner_id=owner_id, input_hash=input_hash, created_at=now())


def claim(worker_id: str, caps: list[str], loaded: list[str], lease: int | None = None) -> dict | None:
    if not caps:
        return None
    lease = lease or get_settings().lease_seconds
    t = now()
    cq = ",".join("?" * len(caps))
    lq = ",".join("?" * len(loaded)) if loaded else "''"
    sql = f"""
    UPDATE job SET status='running', worker_id=?, attempts=attempts+1, lease_until=?, started_at=?
    WHERE id = (
      SELECT id FROM job
      WHERE kind IN ({cq})
        AND (status='queued' OR (status='running' AND lease_until < ?))
        AND cancel_requested=0
      ORDER BY priority DESC, (model_hint IN ({lq})) DESC, id
      LIMIT 1)
    RETURNING *"""
    params: list[Any] = [worker_id, t + lease, t, *caps, t, *loaded]
    with db.tx() as c:
        row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def heartbeat(job_id: int, worker_id: str, lease: int | None = None) -> dict:
    lease = lease or get_settings().lease_seconds
    j = db.get("job", job_id)
    if not j or j["worker_id"] != worker_id or j["status"] != "running":
        return {"ok": False, "cancel": True}
    db.update("job", job_id, lease_until=now() + lease)
    return {"ok": True, "cancel": bool(j["cancel_requested"])}


def artifact_dir(job_id: int):
    d = get_settings().tmp_dir / f"job_{job_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def complete(job_id: int, output: dict, model_id: str = "", elapsed: float = 0.0) -> None:
    from . import engine  # tranh vong import

    j = db.get("job", job_id)
    if not j or j["status"] not in ("running", "queued"):
        return
    db.update("job", job_id, status="done", output_json=jd(output), model_id=model_id or "",
              finished_at=now(), elapsed=elapsed, error="")
    j = db.get("job", job_id)
    try:
        engine.handle_job_done(j, output)
    except Exception as e:  # loi xu ly ket qua khong duoc lam treo hang doi
        log.exception("Xu ly ket qua job %s loi", job_id)
        db.update("job", job_id, status="failed", error=f"handler: {e}")
        engine.handle_job_failed(db.get("job", job_id))
    finally:
        shutil.rmtree(artifact_dir(job_id), ignore_errors=True)


def fail(job_id: int, error: str, retryable: bool = True) -> None:
    from . import engine

    j = db.get("job", job_id)
    if not j:
        return
    if retryable and j["attempts"] < j["max_attempts"] and not j["cancel_requested"]:
        db.update("job", job_id, status="queued", worker_id="", lease_until=None, error=error[:4000])
        return
    db.update("job", job_id, status="failed", error=error[:4000], finished_at=now())
    engine.handle_job_failed(db.get("job", job_id))


def retry_with_feedback(job: dict, bad_text: str, error: str) -> bool:
    """LLM tra sai schema: dua loi vao hoi thoai va cho chay lai."""
    if job["attempts"] >= job["max_attempts"]:
        return False
    payload = jl(job["payload_json"], {})
    msgs = payload.get("messages", [])
    msgs = msgs + [
        {"role": "assistant", "content": (bad_text or "")[:6000]},
        {"role": "user", "content": f"Kết quả không hợp lệ: {error[:1500]}\nHãy trả lại đúng MỘT đối tượng JSON theo schema, không thêm chữ nào khác."},
    ]
    payload["messages"] = msgs
    db.update("job", job["id"], status="queued", worker_id="", lease_until=None, payload_json=jd(payload),
              error=f"retry: {error[:500]}")
    return True


def retry(job_id: int) -> None:
    db.update("job", job_id, status="queued", attempts=0, worker_id="", lease_until=None, cancel_requested=0, error="")


def cancel(job_id: int) -> None:
    j = db.get("job", job_id)
    if not j:
        return
    if j["status"] == "queued":
        db.update("job", job_id, status="cancelled", finished_at=now())
    elif j["status"] == "running":
        db.update("job", job_id, cancel_requested=1)


def touch_worker(worker_id: str, name: str, caps: list[str], loaded: list[str], models: list[str]) -> None:
    db.ex("""INSERT INTO worker(id,name,capabilities_json,loaded_json,models_json,last_seen) VALUES(?,?,?,?,?,?)
             ON CONFLICT(id) DO UPDATE SET name=excluded.name, capabilities_json=excluded.capabilities_json,
             loaded_json=excluded.loaded_json, models_json=excluded.models_json, last_seen=excluded.last_seen""",
          worker_id, name, jd(caps), jd(loaded), jd(models), now())


def counts(project_id: int | None = None) -> dict[str, int]:
    if project_id is None:
        rows = db.q("SELECT status, COUNT(*) n FROM job GROUP BY status")
    else:
        rows = db.q("SELECT status, COUNT(*) n FROM job WHERE project_id=? GROUP BY status", project_id)
    return {r["status"]: r["n"] for r in rows}
