"""Phiên bản schema, sao lưu và nâng cấp dữ liệu.

- DB mới: tạo schema hiện tại.
- DB cũ: sao lưu vào data/backups/ rồi chạy lần lượt các bước nâng cấp.
Thêm bước mới: viết hàm _vN_to_vN1, đăng ký vào STEPS, tăng db.SCHEMA_VERSION.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Callable

log = logging.getLogger("storyforge.migrations")
KEEP_BACKUPS = 20


def backup(db_path: str | Path, backup_dir: Path, label: str = "manual") -> Path | None:
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backup_dir / f"storyforge-{stamp}-{label}.db"
    src, dst = sqlite3.connect(str(db_path)), sqlite3.connect(str(out))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for f in sorted(backup_dir.glob("storyforge-*.db"))[:-KEEP_BACKUPS]:
        f.unlink(missing_ok=True)
    log.info("Đã sao lưu DB vào %s", out)
    return out


def current_version(c: sqlite3.Connection) -> int:
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in tables:
        r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if r:
            return int(r[0])
    return 1 if "project" in tables else 0


def ensure(db, backup_dir: Path) -> None:
    from .db import SCHEMA, SCHEMA_VERSION

    c = db.conn()
    v = current_version(c)
    if v == 0:
        c.executescript(SCHEMA)
        _set_version(c, SCHEMA_VERSION)
        return
    if v > SCHEMA_VERSION:
        raise RuntimeError(f"DB ở phiên bản {v}, mới hơn app ({SCHEMA_VERSION}). Hãy cập nhật StoryForge.")
    if v < SCHEMA_VERSION:
        backup(db.path, backup_dir, f"before-v{v}-to-v{SCHEMA_VERSION}")
        for step in range(v, SCHEMA_VERSION):
            log.info("Nâng cấp schema v%s -> v%s", step, step + 1)
            STEPS[step](c)
    c.executescript(SCHEMA)
    _set_version(c, SCHEMA_VERSION)


def _set_version(c: sqlite3.Connection, v: int) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(v),))


def _cols(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _v1_to_v2(c: sqlite3.Connection) -> None:
    """v1 -> v2: giữ dự án, chương, nhân vật, sự kiện, ảnh tham chiếu nhân vật, tài nguyên, nhật ký.
    Gộp bối cảnh theo tên; bỏ dữ liệu dẫn xuất (nhịp, cảnh, trang, khung, job) để tạo lại."""
    from .db import SCHEMA

    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("BEGIN")
    try:
        old_locs = [dict(zip(_cols(c, "location"), r)) for r in c.execute("SELECT * FROM location ORDER BY id")]
        old_events = [dict(zip(_cols(c, "state_event"), r)) for r in c.execute("SELECT * FROM state_event ORDER BY id")]
        for t in ("balloon", "panel", "page", "segment", "beat", "state_event", "location", "job"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        if "notes" not in _cols(c, "character"):
            c.execute("ALTER TABLE character ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
        if "first_chapter" not in _cols(c, "ref"):
            c.execute("ALTER TABLE ref ADD COLUMN first_chapter INTEGER NOT NULL DEFAULT 1")
        if "size" not in _cols(c, "asset"):
            c.execute("ALTER TABLE asset ADD COLUMN size INTEGER NOT NULL DEFAULT 0")
        c.execute("DELETE FROM ref WHERE owner_type='location'")
        c.execute("COMMIT")
        c.executescript(SCHEMA)
        c.execute("BEGIN")
        seen: set[tuple] = set()
        for l in old_locs:
            key = (l["project_id"], l["name"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            c.execute("INSERT INTO location(id,project_id,name,description,first_chapter,status,flags_json,reviewer,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (l["id"], l["project_id"], l["name"], l.get("description", ""), l.get("first_chapter", 1),
                       l.get("status", "pending"), l.get("flags_json", "[]"), l.get("reviewer", ""), l.get("created_at")))
        for e in old_events:
            field = "mark" if e["field"] == "injury" and e.get("permanent") else e["field"]
            c.execute("INSERT INTO state_event(id,project_id,subject_type,subject_id,chapter_id,chapter_idx,field,value,"
                      "permanent,until_chapter,evidence,status,flags_json,reviewer,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (e["id"], e["project_id"], "character", e["character_id"], e["chapter_id"], e["chapter_idx"], field,
                       e["value"], e["permanent"], e["until_chapter"], e["evidence"], e["status"], e["flags_json"],
                       e["reviewer"], e["created_at"]))
        c.execute("UPDATE chapter SET status='state_ready' WHERE status IN ('beating','review_beats','producing','done')")
        c.execute("UPDATE chapter SET status='new' WHERE status='extracting'")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.execute("PRAGMA foreign_keys=ON")


def _v2_to_v3(c: sqlite3.Connection) -> None:
    """v2 -> v3: theo dõi heartbeat và tiến độ của job để phát hiện job đứng yên."""
    cols = _cols(c, "job")
    for name, ddl in (("heartbeat_at", "REAL"), ("last_progress_at", "REAL"), ("stalls", "INTEGER NOT NULL DEFAULT 0")):
        if name not in cols:
            c.execute(f"ALTER TABLE job ADD COLUMN {name} {ddl}")


STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _v1_to_v2, 2: _v2_to_v3}
