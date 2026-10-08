"""Lop du lieu mong tren sqlite3 cua thu vien chuan.

Khong dung ORM de giam phu thuoc va chay giong nhau tren Windows, macOS, Linux.
Moi luong dung mot ket noi rieng, bat WAL de app va API worker doc ghi dong thoi.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS project(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'video',
  style_prompt TEXT NOT NULL DEFAULT '',
  settings_json TEXT NOT NULL DEFAULT '{}',
  policy_json TEXT NOT NULL DEFAULT '{}',
  autorun INTEGER NOT NULL DEFAULT 0,
  paused INTEGER NOT NULL DEFAULT 0,
  pause_reason TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS chapter(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  run_requested INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  policy_json TEXT NOT NULL DEFAULT '{}',
  needs_recheck INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  video_asset_id INTEGER,
  srt_asset_id INTEGER,
  comic_cbz_asset_id INTEGER,
  comic_pdf_asset_id INTEGER,
  created_at REAL,
  UNIQUE(project_id, idx)
);
CREATE TABLE IF NOT EXISTS character(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  appearance TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  first_chapter INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS state_event(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
  chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
  chapter_idx INTEGER NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  permanent INTEGER NOT NULL DEFAULT 0,
  until_chapter INTEGER,
  evidence TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS location(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT 'default',
  description TEXT NOT NULL DEFAULT '',
  first_chapter INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS ref(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  state_key TEXT NOT NULL DEFAULT 'base',
  label TEXT NOT NULL DEFAULT '',
  prompt TEXT NOT NULL DEFAULT '',
  refs_json TEXT NOT NULL DEFAULT '[]',
  width INTEGER NOT NULL DEFAULT 1024,
  height INTEGER NOT NULL DEFAULT 1024,
  seed INTEGER NOT NULL DEFAULT 0,
  input_hash TEXT NOT NULL DEFAULT '',
  asset_id INTEGER,
  status TEXT NOT NULL DEFAULT 'missing',
  flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT '',
  regen_count INTEGER NOT NULL DEFAULT 0,
  locked INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  UNIQUE(owner_type, owner_id, state_key)
);
CREATE TABLE IF NOT EXISTS beat(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  para_start INTEGER NOT NULL,
  para_end INTEGER NOT NULL,
  narration TEXT NOT NULL DEFAULT '',
  location_id INTEGER,
  cast_json TEXT NOT NULL DEFAULT '[]',
  shot TEXT NOT NULL DEFAULT 'medium',
  action TEXT NOT NULL DEFAULT '',
  mood TEXT NOT NULL DEFAULT '',
  dialogue_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending',
  flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS segment(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
  beat_id INTEGER NOT NULL REFERENCES beat(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  narration TEXT NOT NULL DEFAULT '',
  seed INTEGER NOT NULL DEFAULT 0,
  image_asset_id INTEGER,
  image_hash TEXT NOT NULL DEFAULT '',
  image_status TEXT NOT NULL DEFAULT 'missing',
  image_flags TEXT NOT NULL DEFAULT '[]',
  image_regen INTEGER NOT NULL DEFAULT 0,
  audio_asset_id INTEGER,
  audio_hash TEXT NOT NULL DEFAULT '',
  audio_status TEXT NOT NULL DEFAULT 'missing',
  audio_flags TEXT NOT NULL DEFAULT '[]',
  audio_regen INTEGER NOT NULL DEFAULT 0,
  duration REAL NOT NULL DEFAULT 0,
  sentences_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS page(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  layout TEXT NOT NULL,
  asset_id INTEGER
);
CREATE TABLE IF NOT EXISTS panel(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
  page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  beat_id INTEGER NOT NULL REFERENCES beat(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  seed INTEGER NOT NULL DEFAULT 0,
  image_asset_id INTEGER,
  image_hash TEXT NOT NULL DEFAULT '',
  image_status TEXT NOT NULL DEFAULT 'missing',
  image_flags TEXT NOT NULL DEFAULT '[]',
  image_regen INTEGER NOT NULL DEFAULT 0,
  reviewer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS balloon(
  id INTEGER PRIMARY KEY,
  panel_id INTEGER NOT NULL REFERENCES panel(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'speech',
  character_id INTEGER,
  text TEXT NOT NULL,
  x REAL NOT NULL,
  y REAL NOT NULL,
  w REAL NOT NULL,
  tail_x REAL,
  tail_y REAL
);
CREATE TABLE IF NOT EXISTS asset(
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL,
  input_hash TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_asset_hash ON asset(input_hash);
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  model_hint TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  worker_id TEXT NOT NULL DEFAULT '',
  lease_until REAL,
  output_json TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  input_hash TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  started_at REAL,
  finished_at REAL,
  elapsed REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_job_status ON job(status, kind, priority);
CREATE INDEX IF NOT EXISTS ix_job_owner ON job(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  entity TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  reviewer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_audit_project ON audit(project_id, created_at);
CREATE TABLE IF NOT EXISTS worker(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  loaded_json TEXT NOT NULL DEFAULT '[]',
  models_json TEXT NOT NULL DEFAULT '[]',
  last_seen REAL,
  jobs_done INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> float:
    return time.time()


def jl(s: Any, default: Any = None) -> Any:
    """json.loads an toan."""
    if s is None or s == "":
        return default
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def jd(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


class Database:
    def __init__(self) -> None:
        self.path: str | None = None
        self._local = threading.local()

    def init(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        if self.path is None:
            raise RuntimeError("Database chua init")
        c = getattr(self._local, "c", None)
        if c is None or getattr(self._local, "path", None) != self.path:
            c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
            self._local.path = self.path
            self._local.depth = 0
        return c

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            c.execute("BEGIN IMMEDIATE")
        self._local.depth = depth + 1
        try:
            yield c
        except BaseException:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("ROLLBACK")
            raise
        else:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("COMMIT")

    # ---------------------------------------------------------- helpers
    def q(self, sql: str, *p: Any) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, p).fetchall()]

    def one(self, sql: str, *p: Any) -> dict | None:
        r = self.conn().execute(sql, p).fetchone()
        return dict(r) if r else None

    def val(self, sql: str, *p: Any) -> Any:
        r = self.conn().execute(sql, p).fetchone()
        return r[0] if r else None

    def ex(self, sql: str, *p: Any) -> sqlite3.Cursor:
        return self.conn().execute(sql, p)

    def get(self, table: str, id_: Any) -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE id=?", id_)

    def insert(self, table: str, **cols: Any) -> int:
        keys = list(cols)
        sql = f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})"
        cur = self.conn().execute(sql, [cols[k] for k in keys])
        return int(cur.lastrowid)

    def update(self, table: str, id_: Any, **cols: Any) -> None:
        if not cols:
            return
        sets = ",".join(f"{k}=?" for k in cols)
        self.conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*cols.values(), id_])

    def delete(self, table: str, id_: Any) -> None:
        self.conn().execute(f"DELETE FROM {table} WHERE id=?", (id_,))


db = Database()
