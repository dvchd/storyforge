"""Vòng lặp worker: nhận job, chạy adapter, gửi kết quả.

storyforge-worker --config worker.toml
storyforge-worker --server http://127.0.0.1:8765 --token XXX --mock
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import platform
import shutil
import tempfile
import threading
import time
import tomllib
import traceback
import uuid
from pathlib import Path

import httpx

from storyforge.protocol import ClaimRequest, CompleteRequest, FailRequest

from .adapters import Adapter, Cancelled, JobContext, RetryableError, build
from .client import WorkerClient

log = logging.getLogger("storyforge.worker")

MOCK_ADAPTERS = [{"kind": k, "type": "mock"} for k in ("llm.chat", "image.generate", "tts.synthesize", "image.remove_bg")]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sniff_ext(path: Path) -> str:
    head = path.read_bytes()[:12]
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF":
        return ".wav"
    return ".bin"


class AssetCache:
    """Cache ảnh tham chiếu theo sha256, kiểm tra toàn vẹn, giới hạn dung lượng (xóa file ít dùng nhất)."""

    def __init__(self, client: WorkerClient, root: Path, max_mb: float = 2048) -> None:
        self.client, self.root, self.max_bytes = client, root, int(max_mb * 1024 * 1024)
        root.mkdir(parents=True, exist_ok=True)

    def get(self, asset_id: int, sha: str) -> Path:
        if sha:
            hit = next(iter(self.root.glob(sha + ".*")), None)
            if hit:
                hit.touch()
                return hit
        tmp = self.root / f"dl_{uuid.uuid4().hex}"
        declared = self.client.fetch_asset(asset_id, tmp)
        actual = _sha256(tmp)
        expect = sha or declared
        if expect and actual != expect:
            tmp.unlink(missing_ok=True)
            raise RetryableError(f"Ảnh tham chiếu {asset_id} bị hỏng khi tải (sha256 không khớp)")
        final = self.root / (actual + _sniff_ext(tmp))
        tmp.replace(final)
        self.evict()
        return final

    def evict(self) -> None:
        files = [f for f in self.root.iterdir() if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        if total <= self.max_bytes:
            return
        for f in sorted(files, key=lambda f: f.stat().st_mtime):
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
            if total <= self.max_bytes * 0.8:
                break


class Worker:
    def __init__(self, client: WorkerClient, adapters: list[Adapter], name: str = "",
                 memory_mode: str = "sequential", cache_dir: Path | None = None, heartbeat_every: float = 20.0,
                 cache_mb: float = 2048) -> None:
        self.client = client
        self.adapters = adapters
        self.name = name or platform.node()
        self.memory_mode = memory_mode       # sequential: chỉ giữ một model nặng trong RAM
        self.cache = AssetCache(client, cache_dir or Path(tempfile.gettempdir()) / "storyforge-worker-cache", cache_mb)
        self.heartbeat_every = heartbeat_every
        self._stop = threading.Event()

    def capabilities(self) -> list[str]:
        return sorted({a.kind for a in self.adapters})

    def loaded_models(self) -> list[str]:
        return [m for a in self.adapters if a.loaded() for m in a.model_ids()]

    def model_ids(self) -> list[str]:
        return [m for a in self.adapters for m in a.model_ids()]

    def pick(self, kind: str, hint: str) -> Adapter:
        cands = [a for a in self.adapters if a.kind == kind]
        for a in cands:
            if hint and hint in a.model_ids():
                return a
        for a in cands:
            if a.loaded():
                return a
        return cands[0]

    def run_once(self) -> bool:
        req = ClaimRequest(worker_id=self.client.worker_id, name=self.name, capabilities=self.capabilities(),
                           loaded_models=self.loaded_models(), model_ids=self.model_ids())
        job = self.client.claim(req)
        if job is None:
            return False
        adapter = self.pick(job.kind, job.model_hint)
        if self.memory_mode == "sequential" and adapter.heavy:
            for a in self.adapters:
                if a is not adapter and a.heavy and a.loaded():
                    log.info("Giải phóng %s để tải %s", a.model, adapter.model)
                    a.unload()
        work = Path(tempfile.mkdtemp(prefix=f"sf_job{job.id}_"))
        cancelled, stop_hb = threading.Event(), threading.Event()
        prog = {"p": None, "m": "", "dirty": False}
        interval = max(2.0, min(self.heartbeat_every, job.lease_seconds / 3))

        def report(frac: float, msg: str = "") -> None:
            prog.update(p=frac, m=msg, dirty=True)

        def hb() -> None:
            last = time.time()
            while not stop_hb.wait(1.0):
                due = time.time() - last >= interval
                if not due and not (prog["dirty"] and time.time() - last >= 1.5):
                    continue
                try:
                    prog["dirty"] = False
                    if self.client.heartbeat(job.id, prog["p"], prog["m"]):
                        cancelled.set()
                    last = time.time()
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=hb, daemon=True)
        t.start()
        t0 = time.time()
        log.info("Job #%s %s -> %s (%s)", job.id, job.kind, adapter.type_name, adapter.model)
        try:
            ctx = JobContext(workdir=work, fetch_asset=self.cache.get, is_cancelled=cancelled.is_set, report=report)
            res = adapter.run(job, ctx)
            ctx.check_cancel()
            for f in res.files:
                self.client.upload(job.id, f)
            self.client.complete(job.id, CompleteRequest(output=res.output, model_id=res.model_id or adapter.model,
                                                         elapsed=time.time() - t0))
            log.info("Job #%s xong sau %.1fs", job.id, time.time() - t0)
        except Cancelled:
            log.info("Job #%s đã hủy", job.id)
            self._safe_fail(job.id, "Đã hủy theo yêu cầu", False, True)
        except RetryableError as e:
            log.warning("Job #%s lỗi tạm thời: %s", job.id, e)
            self._safe_fail(job.id, str(e), True)
        except Exception as e:  # noqa: BLE001
            log.error("Job #%s lỗi: %s", job.id, e)
            self._safe_fail(job.id, f"{e}\n{traceback.format_exc()[-1500:]}", False)
        finally:
            stop_hb.set()
            shutil.rmtree(work, ignore_errors=True)
        return True

    def _safe_fail(self, job_id: int, err: str, retryable: bool, cancelled: bool = False) -> None:
        try:
            self.client.fail(job_id, FailRequest(error=err, retryable=retryable, cancelled=cancelled))
        except Exception:  # noqa: BLE001
            log.exception("Không gửi được lỗi job %s", job_id)

    def run_forever(self, poll: float = 2.0) -> None:
        log.info("Worker %s sẵn sàng: %s", self.client.worker_id, ", ".join(self.capabilities()))
        backoff = poll
        while not self._stop.is_set():
            try:
                did = self.run_once()
                backoff = poll
                if not did:
                    time.sleep(poll)
            except httpx.HTTPError as e:
                log.warning("Mất kết nối app: %s. Thử lại sau %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except KeyboardInterrupt:
                break

    def stop(self) -> None:
        self._stop.set()


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="storyforge-worker")
    ap.add_argument("--config", help="File worker.toml")
    ap.add_argument("--server", help="Địa chỉ app, ví dụ http://127.0.0.1:8765")
    ap.add_argument("--token", help="Worker token (trang /workers hoặc: storyforge-app token)")
    ap.add_argument("--id", help="ID worker (mặc định tên máy)")
    ap.add_argument("--mock", action="store_true", help="Dùng toàn bộ adapter giả lập")
    ap.add_argument("--once", action="store_true", help="Chạy hết job đang có rồi thoát")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    srv, wk = cfg.get("server", {}), cfg.get("worker", {})
    url = args.server or srv.get("url", "http://127.0.0.1:8765")
    token = args.token or srv.get("token", "")
    wid = args.id or wk.get("id") or f"{platform.node()}-{uuid.uuid4().hex[:4]}"
    adapters_cfg = MOCK_ADAPTERS if args.mock else cfg.get("adapters", [])
    if not adapters_cfg:
        ap.error("Không có adapter nào. Dùng --mock hoặc khai báo [[adapters]] trong worker.toml")
    adapters = [build(a) for a in adapters_cfg]
    client = WorkerClient(url, token, wid, timeout=float(srv.get("timeout", 120)))
    w = Worker(client, adapters, name=wk.get("name", ""), memory_mode=wk.get("memory_mode", "sequential"),
               cache_dir=Path(wk["cache_dir"]).expanduser() if wk.get("cache_dir") else None,
               heartbeat_every=float(wk.get("heartbeat_every", 20)), cache_mb=float(wk.get("cache_mb", 2048)))
    try:
        info = client.ping()
        log.info("Đã kết nối app %s (giao thức v%s)", url, info.get("schema_version"))
    except httpx.HTTPError as e:
        log.error("Không kết nối được app tại %s: %s", url, e)
    if args.once:
        while w.run_once():
            pass
        return
    w.run_forever(float(wk.get("poll_interval", 2)))


if __name__ == "__main__":
    main()
