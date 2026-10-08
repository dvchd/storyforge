"""Vong lap worker: nhan job, chay adapter, gui ket qua.

storyforge-worker --config worker.toml
storyforge-worker --server http://127.0.0.1:8765 --token XXX --mock
"""
from __future__ import annotations

import argparse
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

from .adapters import Adapter, JobContext, RetryableError, build
from .client import WorkerClient

log = logging.getLogger("storyforge.worker")

MOCK_ADAPTERS = [{"kind": k, "type": "mock"} for k in ("llm.chat", "image.generate", "tts.synthesize", "image.remove_bg")]


class Worker:
    def __init__(self, client: WorkerClient, adapters: list[Adapter], name: str = "",
                 memory_mode: str = "sequential", cache_dir: Path | None = None, heartbeat_every: float = 20.0) -> None:
        self.client = client
        self.adapters = adapters
        self.name = name or platform.node()
        self.memory_mode = memory_mode       # sequential: chi giu mot model nang trong RAM
        self.cache_dir = cache_dir or Path(tempfile.gettempdir()) / "storyforge-worker-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_every = heartbeat_every
        self._stop = threading.Event()

    # ----------------------------------------------------------- helpers
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

    def fetch(self, asset_id: int, sha: str) -> Path:
        name = f"{sha or asset_id}"
        hits = list(self.cache_dir.glob(name + ".*")) if sha else []
        if hits:
            return hits[0]
        tmp = self.cache_dir / f"dl_{uuid.uuid4().hex}"
        self.client.fetch_asset(asset_id, tmp)
        ext = _sniff_ext(tmp)
        final = self.cache_dir / (name + ext)
        tmp.replace(final)
        return final

    # ----------------------------------------------------------- loop
    def run_once(self) -> bool:
        req = ClaimRequest(worker_id=self.client.worker_id, name=self.name, capabilities=self.capabilities(),
                           loaded_models=self.loaded_models(), model_ids=self.model_ids())
        job = self.client.claim(req)
        if job is None:
            return False
        adapter = self.pick(job.kind, job.model_hint)
        if self.memory_mode == "sequential":
            for a in self.adapters:
                if a is not adapter and a.loaded() and a.type_name not in ("openai", "mock", "edge_tts", "command"):
                    log.info("Giải phóng %s", a.model)
                    a.unload()
        work = Path(tempfile.mkdtemp(prefix=f"sf_job{job.id}_"))
        cancelled = threading.Event()
        stop_hb = threading.Event()

        def hb() -> None:
            while not stop_hb.wait(self.heartbeat_every):
                try:
                    if self.client.heartbeat(job.id):
                        cancelled.set()
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=hb, daemon=True)
        t.start()
        t0 = time.time()
        log.info("Job #%s %s -> %s (%s)", job.id, job.kind, adapter.type_name, adapter.model)
        try:
            ctx = JobContext(workdir=work, fetch_asset=self.fetch, is_cancelled=cancelled.is_set)
            res = adapter.run(job, ctx)
            for f in res.files:
                self.client.upload(job.id, f)
            self.client.complete(job.id, CompleteRequest(output=res.output, model_id=res.model_id or adapter.model,
                                                         elapsed=time.time() - t0))
            log.info("Job #%s xong sau %.1fs", job.id, time.time() - t0)
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

    def _safe_fail(self, job_id: int, err: str, retryable: bool) -> None:
        try:
            self.client.fail(job_id, FailRequest(error=err, retryable=retryable))
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


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="storyforge-worker")
    ap.add_argument("--config", help="File worker.toml")
    ap.add_argument("--server", help="Địa chỉ app, ví dụ http://127.0.0.1:8765")
    ap.add_argument("--token", help="Worker token (xem trang /workers hoặc storyforge-app token)")
    ap.add_argument("--id", help="ID worker (mặc định tên máy)")
    ap.add_argument("--mock", action="store_true", help="Dùng toàn bộ adapter giả lập")
    ap.add_argument("--once", action="store_true", help="Chạy hết job đang có rồi thoát")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    srv = cfg.get("server", {})
    wk = cfg.get("worker", {})
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
               heartbeat_every=float(wk.get("heartbeat_every", 20)))
    try:
        client.ping()
    except httpx.HTTPError as e:
        log.error("Không kết nối được app tại %s: %s", url, e)
    if args.once:
        while w.run_once():
            pass
        return
    w.run_forever(float(wk.get("poll_interval", 2)))


if __name__ == "__main__":
    main()
