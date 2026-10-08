"""Dòng lệnh của app: serve | demo | token | backup | gc | vendor | doctor."""
from __future__ import annotations

import argparse
import logging
import sys
import urllib.request
from pathlib import Path

from .config import load_settings, set_settings

VENDOR = {
    "htmx.min.js": "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js",
    "alpine.min.js": "https://unpkg.com/alpinejs@3.14.8/dist/cdn.min.js",
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="storyforge-app")
    ap.add_argument("--data", default=None, help="Thư mục dữ liệu (mặc định ./data hoặc STORYFORGE_DATA)")
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", help="Chạy web UI và API cho worker")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    dm = sub.add_parser("demo", help="Tạo dự án mẫu từ examples/")
    dm.add_argument("--story", default="sample_story", help="sample_story | modern_story | đường dẫn thư mục")
    dm.add_argument("--mode", default="both", choices=["video", "comic", "both"])
    dm.add_argument("--format", default="page", choices=["page", "webtoon"])
    dm.add_argument("--level", default="2")
    sub.add_parser("token", help="In worker token")
    sub.add_parser("backup", help="Sao lưu cơ sở dữ liệu vào data/backups/")
    gc = sub.add_parser("gc", help="Dọn tài nguyên không còn dùng")
    gc.add_argument("--hours", type=float, default=24.0)
    gc.add_argument("--dry-run", action="store_true")
    sub.add_parser("vendor", help="Tải htmx và Alpine về static/vendor để dùng offline")
    sub.add_parser("doctor", help="Kiểm tra môi trường (ffmpeg, font, thư mục dữ liệu)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(args.data)
    set_settings(s)
    from . import system

    if args.cmd == "token":
        print(s.worker_token)
        return
    if args.cmd == "doctor":
        rows = system.doctor_report()
        bad = 0
        for r in rows:
            print(("✓ " if r["ok"] else "✗ ") + r["text"])
            bad += not r["ok"]
        print("Môi trường ổn." if not bad else f"Có {bad} mục cần xử lý (xem trên).")
        sys.exit(1 if bad else 0)
    if args.cmd == "vendor":
        out = Path(__file__).parent / "static" / "vendor"
        out.mkdir(parents=True, exist_ok=True)
        for name, url in VENDOR.items():
            print(f"Tải {url}")
            urllib.request.urlretrieve(url, out / name)
        print(f"Xong. Giao diện sẽ dùng file trong {out}")
        return
    if args.cmd == "backup":
        from . import migrations

        print(migrations.backup(s.db_path, s.backup_dir, "manual") or "Chưa có cơ sở dữ liệu")
        return

    from .main import create_app

    if args.cmd == "gc":
        s.start_local_runner = False
        create_app(s)
        from . import assets

        r = assets.gc(args.hours, args.dry_run)
        print(f"{'(thử) ' if r['dry_run'] else ''}Xóa {r['rows']} bản ghi, {r['files']} file, "
              f"giải phóng {r['bytes'] / 1e6:.1f} MB")
        return
    report = system.startup_report()
    if args.cmd == "demo":
        from . import engine, migrations, policy
        from .db import db, jd, now
        from .routes_web import add_chapters, split_chapters

        migrations.backup(s.db_path, s.backup_dir, "before-demo")
        s.start_local_runner = False
        create_app(s)
        root = Path(__file__).resolve().parents[3] / "examples"
        folder = Path(args.story) if Path(args.story).is_dir() else root / args.story
        style = (folder / "style.txt").read_text(encoding="utf-8").strip() if (folder / "style.txt").exists() else \
            "Semi-realistic illustration, soft painterly colors, cinematic lighting"
        pid = db.insert("project", name=f"Mẫu: {folder.name}", mode=args.mode, style_prompt=style,
                        settings_json=jd({"comic_format": args.format}),
                        policy_json=jd({**policy.DEFAULT_POLICY, "level": args.level}), created_at=now())
        items = []
        for f in sorted(folder.glob("*.txt")):
            if f.name != "style.txt":
                items += split_chapters(f.read_text(encoding="utf-8"), f.stem)
        add_chapters(pid, items)
        engine.advance_project(pid)
        print(f"Đã tạo dự án #{pid} với {len(items)} chương. Mở web UI và bấm 'Chạy tất cả chương'.")
        if report and args.mode in ("video", "both"):
            print(report)
        return

    import uvicorn

    host = getattr(args, "host", None) or s.host
    port = getattr(args, "port", None) or s.port
    if not system.port_free(host, port):
        print(f"✗ Cổng {host}:{port} đang bận (app khác đang chạy?). Đổi cổng: storyforge-app serve --port {port + 1}")
    app = create_app(s)
    print(f"StoryForge: http://{host}:{port}  |  worker token: {s.worker_token}")
    if report:
        print(report)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
