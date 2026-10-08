"""Hàng đợi job trên SQLite.

- Nhận job bằng một câu UPDATE ... RETURNING: an toàn khi nhiều worker.
- Ưu tiên hiệu lực = priority + điểm chờ (chống bỏ đói), sau đó ưu tiên job dùng model worker đang tải.
- Thời gian thuê theo loại job; hết hạn mà không có heartbeat thì job tự quay lại hàng đợi.
- Không tin tuyệt đối vào worker: tiến độ chỉ được ghi nhận khi TĂNG; job không tiến triển quá
  ngưỡng "đứng yên" của loại job thì bị thu hồi (watchdog), kể cả khi worker vẫn gửi heartbeat.
"""
from __future__ import annotations

import logging
import shutil
from typing import Any

from storyforge.protocol import DEFAULT_LEASE

from .config import get_settings
from .db import db, jd, jl, now

log = logging.getLogger("storyforge.jobs")

# Ngưỡng đứng yên (giây). LLM qua API thường không báo tiến độ trong lúc sinh chữ: tắt (0), dựa vào lease.
DEFAULT_STALL = {"image.generate": 600, "tts.synthesize": 600, "image.remove_bg": 600, "llm.chat": 0}
STALL_WARN = 120     # giao diện đánh dấu "đứng yên" sau chừng này giây


def lease_for(kind: str) -> int:
    return int((get_settings().lease or {}).get(kind) or DEFAULT_LEASE.get(kind, 600))


def stall_for(kind: str) -> int:
    o = get_settings().stall or {}
    return int(o[kind]) if kind in o else int(DEFAULT_STALL.get(kind, 0))


def enqueue(project_id: int | None, kind: str, payload: dict, owner_type: str, owner_id: int,
            input_hash: str = "", model_hint: str = "", priority: int = 0, max_attempts: int = 3) -> int:
    existing = db.one(
        "SELECT id FROM job WHERE owner_type=? AND owner_id=? AND kind=? AND status IN ('queued','running')",
        owner_type, owner_id, kind)
    if existing:
        return existing["id"]
    return db.insert("job", project_id=project_id, kind=kind, payload_json=jd(payload), model_hint=model_hint or "",
                     priority=priority, status="queued", max_attempts=max_attempts, owner_type=owner_type,
                     owner_id=owner_id, input_hash=input_hash, lease_seconds=lease_for(kind), created_at=now())


def _eff_priority_sql() -> str:
    s = get_settings()
    return (f"(priority + MIN({int(s.aging_cap)}, "
            f"CAST((:now - COALESCE(created_at, :now)) / {max(1, int(s.aging_seconds))} AS INTEGER)))")


def claim(worker_id: str, caps: list[str], loaded: list[str]) -> dict | None:
    if not caps:
        return None
    params: dict[str, Any] = {"w": worker_id, "now": now()}
    cq = ",".join(f":c{i}" for i in range(len(caps)))
    params.update({f"c{i}": c for i, c in enumerate(caps)})
    if loaded:
        lq = ",".join(f":l{i}" for i in range(len(loaded)))
        params.update({f"l{i}": m for i, m in enumerate(loaded)})
    else:
        lq = "''"
    sql = f"""
    UPDATE job SET status='running', worker_id=:w, attempts=attempts+1, lease_until=:now + lease_seconds,
                   started_at=:now, heartbeat_at=:now, last_progress_at=:now, progress=NULL, progress_msg=''
    WHERE id = (
      SELECT id FROM job
      WHERE kind IN ({cq})
        AND (status='queued' OR (status='running' AND lease_until < :now))
        AND cancel_requested=0
      ORDER BY {_eff_priority_sql()} DESC, (model_hint IN ({lq})) DESC, id
      LIMIT 1)
    RETURNING *"""
    with db.tx() as c:
        row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def record_progress(job_id: int, progress: float | None, message: str = "") -> None:
    """Ghi tiến độ, chỉ khi tăng (đơn điệu). Thông điệp mới không được tính là tiến triển."""
    j = db.get("job", job_id)
    if not j:
        return
    t = now()
    cols: dict[str, Any] = {"heartbeat_at": t}
    if progress is not None:
        p = max(0.0, min(1.0, float(progress)))
        if p > (j["progress"] or 0.0) + 1e-6:
            cols.update(progress=p, last_progress_at=t)
    if message:
        cols["progress_msg"] = message[:200]
    db.update("job", job_id, **cols)


def heartbeat(job_id: int, worker_id: str, progress: float | None = None, message: str = "") -> dict:
    j = db.get("job", job_id)
    if not j or j["worker_id"] != worker_id or j["status"] != "running":
        return {"ok": False, "cancel": True}
    db.update("job", job_id, lease_until=now() + j["lease_seconds"])
    record_progress(job_id, progress, message)
    return {"ok": True, "cancel": bool(j["cancel_requested"])}


def watchdog() -> int:
    """Thu hồi job AI đang chạy mà không tiến triển quá ngưỡng. Trả số job đã xử lý."""
    t, n = now(), 0
    for j in db.q("SELECT * FROM job WHERE status='running' AND kind NOT LIKE 'local.%'"):
        limit = stall_for(j["kind"])
        if not limit:
            continue
        last = j["last_progress_at"] or j["started_at"] or t
        if t - last <= limit:
            continue
        pct = int((j["progress"] or 0) * 100)
        msg = (f"Đứng yên quá {limit // 60} phút ở {pct}% (worker {j['worker_id'] or '?'}): đã thu hồi. "
               "Worker có thể bị treo hoặc báo tiến độ sai.")
        log.warning("Job %s: %s", j["id"], msg)
        db.update("job", j["id"], stalls=j["stalls"] + 1)
        fail(j["id"], msg, retryable=True)
        n += 1
    return n


def artifact_dir(job_id: int):
    d = get_settings().tmp_dir / f"job_{job_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def complete(job_id: int, output: dict, model_id: str = "", elapsed: float = 0.0) -> None:
    from . import engine

    j = db.get("job", job_id)
    if not j or j["status"] not in ("running", "queued"):
        return
    usage = output.get("usage") or {}
    db.update("job", job_id, status="done", output_json=jd(output), model_id=model_id or "", progress=1.0,
              finished_at=now(), elapsed=elapsed, error="",
              tokens_in=int(usage.get("prompt_tokens") or 0), tokens_out=int(usage.get("completion_tokens") or 0))
    j = db.get("job", job_id)
    try:
        engine.handle_job_done(j, output)
    except Exception as e:
        log.exception("Xử lý kết quả job %s lỗi", job_id)
        db.update("job", job_id, status="failed", error=f"handler: {e}")
        engine.handle_job_failed(db.get("job", job_id))
    finally:
        shutil.rmtree(artifact_dir(job_id), ignore_errors=True)


def fail(job_id: int, error: str, retryable: bool = True, cancelled: bool = False) -> None:
    from . import engine

    j = db.get("job", job_id)
    if not j:
        return
    if cancelled or j["cancel_requested"]:
        db.update("job", job_id, status="cancelled", error=error[:4000] or "đã hủy", finished_at=now())
        engine.handle_job_failed(db.get("job", job_id))
        return
    if retryable and j["attempts"] < j["max_attempts"]:
        db.update("job", job_id, status="queued", worker_id="", lease_until=None, error=error[:4000])
        return
    db.update("job", job_id, status="failed", error=error[:4000], finished_at=now())
    engine.handle_job_failed(db.get("job", job_id))


def retry_with_feedback(job: dict, bad_text: str, error: str, fallback_model: str = "",
                        meta_update: dict | None = None) -> bool:
    """LLM trả sai: prompt gốc + câu trả lời sai GẦN NHẤT + lời nhắc sửa (không nối dồn), tăng temperature,
    lần cuối chuyển model dự phòng."""
    if job["attempts"] >= job["max_attempts"]:
        return False
    payload = jl(job["payload_json"], {})
    meta = payload.setdefault("meta", {})
    meta.update(meta_update or {})
    base_n = int(meta.get("base_messages", 2))
    base_t = float(meta.setdefault("base_temperature", payload.get("temperature", 0.2)))
    msgs = payload.get("messages", [])[:base_n]
    msgs += [
        {"role": "assistant", "content": (bad_text or "")[:2500]},
        {"role": "user", "content": f"Kết quả trên không hợp lệ:\n{error[:1500]}\n"
                                    "Hãy trả lại đúng MỘT đối tượng JSON theo schema, sửa các lỗi trên, không thêm chữ nào khác."},
    ]
    payload["messages"] = msgs
    payload["temperature"] = round(min(0.7, base_t + 0.15 * job["attempts"]), 2)
    cols: dict[str, Any] = {"status": "queued", "worker_id": "", "lease_until": None, "payload_json": jd(payload),
                            "error": f"thử lại: {error[:500]}"}
    if fallback_model and job["attempts"] + 1 >= job["max_attempts"]:
        cols["model_hint"] = fallback_model
    db.update("job", job["id"], **cols)
    return True


def retry(job_id: int) -> None:
    db.update("job", job_id, status="queued", attempts=0, worker_id="", lease_until=None, cancel_requested=0,
              error="", progress=None, created_at=now())


def cancel(job_id: int) -> None:
    j = db.get("job", job_id)
    if not j:
        return
    if j["status"] == "queued":
        from . import engine

        db.update("job", job_id, status="cancelled", finished_at=now())
        engine.handle_job_failed(db.get("job", job_id))
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


def online_capabilities(window: float = 60.0) -> set[str]:
    caps: set[str] = set()
    for w in db.q("SELECT capabilities_json FROM worker WHERE last_seen > ?", now() - window):
        caps.update(jl(w["capabilities_json"], []))
    return caps


def missing_workers(project_id: int | None = None) -> list[str]:
    sql = "SELECT DISTINCT kind FROM job WHERE status='queued' AND kind NOT LIKE 'local.%'"
    args: list[Any] = []
    if project_id is not None:
        sql += " AND project_id=?"
        args.append(project_id)
    return sorted({r["kind"] for r in db.q(sql, *args)} - online_capabilities())


def stats(project_id: int | None = None) -> dict:
    where, args = ("WHERE project_id=?", [project_id]) if project_id is not None else ("", [])
    out = {}
    for k in [r["kind"] for r in db.q(f"SELECT DISTINCT kind FROM job {where}", *args)]:
        a = [project_id, k] if project_id is not None else [k]
        w = "WHERE project_id=? AND kind=?" if project_id is not None else "WHERE kind=?"
        done = db.q(f"SELECT elapsed FROM job {w} AND status='done' ORDER BY id DESC LIMIT 50", *a)
        avg = sum(d["elapsed"] for d in done) / len(done) if done else 0.0
        queued = db.val(f"SELECT COUNT(*) FROM job {w} AND status IN ('queued','running')", *a) or 0
        tok = db.one(f"SELECT SUM(tokens_in) i, SUM(tokens_out) o FROM job {w}", *a)
        out[k] = {"avg": avg, "pending": queued, "eta": avg * queued, "done": len(done),
                  "tokens_in": tok["i"] or 0, "tokens_out": tok["o"] or 0}
    return out


def queue_positions(rows: list[dict]) -> dict[int, int]:
    s = get_settings()
    t = now()

    def eff(j: dict) -> float:
        return j["priority"] + min(s.aging_cap, int((t - (j["created_at"] or t)) / max(1, s.aging_seconds)))

    by_kind: dict[str, list[dict]] = {}
    for j in db.q("SELECT id, kind, priority, created_at FROM job WHERE status='queued'"):
        by_kind.setdefault(j["kind"], []).append(j)
    pos: dict[int, int] = {}
    for lst in by_kind.values():
        lst.sort(key=lambda j: (-eff(j), j["id"]))
        for i, j in enumerate(lst, 1):
            pos[j["id"]] = i
    return {r["id"]: pos[r["id"]] for r in rows if r["id"] in pos}
