"""Tiện ích kiểm thử: app + worker giả lập trong cùng tiến trình."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from storyforge.app import engine, policy  # noqa: E402
from storyforge.app.config import load_settings  # noqa: E402
from storyforge.app.db import db, jd, now  # noqa: E402
from storyforge.app.main import create_app  # noqa: E402
from storyforge.app.routes_web import add_chapters, split_chapters  # noqa: E402
from storyforge.worker.adapters import build  # noqa: E402
from storyforge.worker.client import WorkerClient  # noqa: E402
from storyforge.worker.runner import MOCK_ADAPTERS, Worker  # noqa: E402

EXAMPLES = ROOT / "examples"
# kích thước nhỏ để kiểm thử nhanh
FAST = {"video_width": 320, "video_height": 180, "video_fps": 8, "video_loudnorm": False, "video_transition": 0.2,
        "comic_page_width": 600, "comic_page_height": 900, "ref_size": 256, "image_area": 65536,
        "webtoon_width": 400}


def make_env(tmp: Path, **settings):
    s = load_settings(tmp / "data", start_local_runner=False, **settings)
    app = create_app(s)
    client = TestClient(app)
    wc = WorkerClient("http://testserver", s.worker_token, "test-worker", http=client)
    worker = Worker(wc, [build(a) for a in MOCK_ADAPTERS], cache_dir=tmp / "cache", heartbeat_every=999)
    return s, client, worker


def new_project(mode: str = "both", level: str = "0", autorun: int = 1, story: str = "sample_story",
                chapters: int | None = None, **extra) -> int:
    pid = db.insert("project", name=f"T {mode} L{level}", mode=mode, style_prompt="test style",
                    policy_json=jd({**policy.DEFAULT_POLICY, "level": level, "checkpoint_every": 0}),
                    autorun=autorun, created_at=now(), settings_json=jd({**FAST, **extra}))
    items = []
    for f in sorted((EXAMPLES / story).glob("*.txt")):
        if f.name != "style.txt":
            items += split_chapters(f.read_text(encoding="utf-8"), f.stem)
    add_chapters(pid, items[:chapters] if chapters else items)
    return pid


def drain(worker: Worker, limit: int = 3000) -> int:
    n = 0
    while n < limit:
        did = worker.run_once()
        did_local = engine.local_tick()
        if not did and not did_local:
            break
        n += 1
    return n


def statuses(pid: int) -> list[str]:
    return [c["status"] for c in db.q("SELECT status FROM chapter WHERE project_id=? ORDER BY idx", pid)]


def char(pid: int, name: str) -> dict:
    return db.one("SELECT * FROM character WHERE project_id=? AND name=?", pid, name)
