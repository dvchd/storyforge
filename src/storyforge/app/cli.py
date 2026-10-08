"""Dong lenh cua app: storyforge-app serve | demo | token."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="storyforge-app")
    ap.add_argument("--data", default=None, help="Thư mục dữ liệu (mặc định ./data hoặc STORYFORGE_DATA)")
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", help="Chạy web UI và API cho worker")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--reload", action="store_true")
    dm = sub.add_parser("demo", help="Tạo dự án mẫu từ examples/sample_story")
    dm.add_argument("--mode", default="both", choices=["video", "comic", "both"])
    dm.add_argument("--level", default="2")
    dm.add_argument("--dir", default=None, help="Thư mục chứa các file chương .txt")
    sub.add_parser("token", help="In worker token")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(args.data)

    if args.cmd == "token":
        print(s.worker_token)
        return
    if args.cmd == "demo":
        from . import engine, policy
        from .db import db, jd, now
        from .main import create_app
        from .routes_web import add_chapters, split_chapters

        s.start_local_runner = False
        create_app(s)
        folder = Path(args.dir) if args.dir else Path(__file__).resolve().parents[3] / "examples" / "sample_story"
        pid = db.insert("project", name="Truyện mẫu", mode=args.mode,
                        style_prompt="Semi-realistic Asian fantasy illustration, soft painterly colors, cinematic lighting",
                        policy_json=jd({**policy.DEFAULT_POLICY, "level": args.level}), created_at=now())
        items = []
        for f in sorted(folder.glob("*.txt")):
            items += split_chapters(f.read_text(encoding="utf-8"), f.stem)
        add_chapters(pid, items)
        engine.advance_project(pid)
        print(f"Đã tạo dự án #{pid} với {len(items)} chương. Mở web UI và bấm 'Chạy tất cả chương'.")
        return

    import uvicorn

    from .main import create_app

    host = getattr(args, "host", None) or s.host
    port = getattr(args, "port", None) or s.port
    app = create_app(s)
    print(f"StoryForge: http://{host}:{port}  |  worker token: {s.worker_token}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
