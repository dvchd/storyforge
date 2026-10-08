"""Tao ung dung FastAPI."""
from __future__ import annotations

import base64
import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import engine
from .config import Settings, load_settings, set_settings
from .db import db

log = logging.getLogger("storyforge")
HERE = Path(__file__).parent


class LocalRunner:
    """Chay job khong can AI (ffmpeg, ghep trang) trong tien trinh app."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, name="local-runner", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not engine.local_tick():
                    time.sleep(1.0)
            except Exception:  # noqa: BLE001
                log.exception("local runner")
                time.sleep(2.0)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    set_settings(settings)
    db.init(settings.db_path)
    runner = LocalRunner()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.start_local_runner:
            runner.start()
        yield
        runner.stop()

    app = FastAPI(title="StoryForge", lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        pw = settings.ui_password
        if pw and not request.url.path.startswith("/api/worker") and not request.url.path.startswith("/static"):
            h = request.headers.get("authorization", "")
            ok = False
            if h.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(h[6:]).decode().partition(":")
                    ok = hmac.compare_digest(given, pw)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="StoryForge"'})
        return await call_next(request)

    from . import routes_web, routes_worker

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(routes_worker.router)
    app.include_router(routes_web.router)
    return app
