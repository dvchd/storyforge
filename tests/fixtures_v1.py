"""Tạo cơ sở dữ liệu theo schema v1 (StoryForge 0.1) để kiểm thử nâng cấp."""
from __future__ import annotations

import sqlite3
from pathlib import Path

V1_SCHEMA = """
CREATE TABLE project(id INTEGER PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'video',
  style_prompt TEXT NOT NULL DEFAULT '', settings_json TEXT NOT NULL DEFAULT '{}', policy_json TEXT NOT NULL DEFAULT '{}',
  autorun INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT NOT NULL DEFAULT '', created_at REAL);
CREATE TABLE chapter(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
  run_requested INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '', policy_json TEXT NOT NULL DEFAULT '{}',
  needs_recheck INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', video_asset_id INTEGER, srt_asset_id INTEGER,
  comic_cbz_asset_id INTEGER, comic_pdf_asset_id INTEGER, created_at REAL, UNIQUE(project_id, idx));
CREATE TABLE character(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]', appearance TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
  first_chapter INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending', flags_json TEXT NOT NULL DEFAULT '[]',
  reviewer TEXT NOT NULL DEFAULT '', created_at REAL);
CREATE TABLE state_event(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, character_id INTEGER NOT NULL,
  chapter_id INTEGER NOT NULL, chapter_idx INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
  permanent INTEGER NOT NULL DEFAULT 0, until_chapter INTEGER, evidence TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', flags_json TEXT NOT NULL DEFAULT '[]', reviewer TEXT NOT NULL DEFAULT '', created_at REAL);
CREATE TABLE location(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, name TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT 'default', description TEXT NOT NULL DEFAULT '', first_chapter INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending', flags_json TEXT NOT NULL DEFAULT '[]', reviewer TEXT NOT NULL DEFAULT '', created_at REAL);
CREATE TABLE ref(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL,
  state_key TEXT NOT NULL DEFAULT 'base', label TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
  refs_json TEXT NOT NULL DEFAULT '[]', width INTEGER NOT NULL DEFAULT 1024, height INTEGER NOT NULL DEFAULT 1024,
  seed INTEGER NOT NULL DEFAULT 0, input_hash TEXT NOT NULL DEFAULT '', asset_id INTEGER, status TEXT NOT NULL DEFAULT 'missing',
  flags_json TEXT NOT NULL DEFAULT '[]', reviewer TEXT NOT NULL DEFAULT '', regen_count INTEGER NOT NULL DEFAULT 0,
  locked INTEGER NOT NULL DEFAULT 0, created_at REAL, UNIQUE(owner_type, owner_id, state_key));
CREATE TABLE beat(id INTEGER PRIMARY KEY, project_id INTEGER, chapter_id INTEGER, idx INTEGER, para_start INTEGER, para_end INTEGER);
CREATE TABLE segment(id INTEGER PRIMARY KEY, project_id INTEGER, chapter_id INTEGER, beat_id INTEGER, image_asset_id INTEGER);
CREATE TABLE page(id INTEGER PRIMARY KEY, chapter_id INTEGER, layout TEXT);
CREATE TABLE panel(id INTEGER PRIMARY KEY, page_id INTEGER, width INTEGER, height INTEGER);
CREATE TABLE balloon(id INTEGER PRIMARY KEY, panel_id INTEGER, text TEXT);
CREATE TABLE asset(id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT NOT NULL, path TEXT NOT NULL, mime TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL, input_hash TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}', created_at REAL);
CREATE TABLE job(id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT,
  owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL);
CREATE TABLE audit(id INTEGER PRIMARY KEY, project_id INTEGER, entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
  action TEXT NOT NULL, reviewer TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at REAL);
CREATE TABLE worker(id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', capabilities_json TEXT NOT NULL DEFAULT '[]',
  loaded_json TEXT NOT NULL DEFAULT '[]', models_json TEXT NOT NULL DEFAULT '[]', last_seen REAL, jobs_done INTEGER NOT NULL DEFAULT 0);
"""


def make_v1_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.executescript(V1_SCHEMA)
    c.execute("INSERT INTO project(id,name,mode) VALUES(1,'Cũ','both')")
    for i, st in ((1, "done"), (2, "producing"), (3, "extracting")):
        c.execute("INSERT INTO chapter(id,project_id,idx,title,text,status,summary) VALUES(?,1,?,?,?,?,?)",
                  (i, i, f"Chương {i}", "Lâm An đi.", st, "tóm tắt"))
    c.execute("INSERT INTO character(id,project_id,name,status) VALUES(1,1,'Lâm An','approved')")
    c.execute("INSERT INTO location(id,project_id,name,variant,description,status) VALUES(1,1,'Thanh Vân Tông','default','temple','approved')")
    c.execute("INSERT INTO location(id,project_id,name,variant,description,status) VALUES(2,1,'Thanh Vân Tông','night','temple at night','approved')")
    c.execute("INSERT INTO state_event(id,project_id,character_id,chapter_id,chapter_idx,field,value,permanent,status) "
              "VALUES(1,1,1,2,2,'injury','scar on cheek',1,'approved')")
    c.execute("INSERT INTO state_event(id,project_id,character_id,chapter_id,chapter_idx,field,value,permanent,status) "
              "VALUES(2,1,1,2,2,'outfit','robe',0,'approved')")
    c.execute("INSERT INTO ref(project_id,owner_type,owner_id,state_key,status,refs_json) VALUES(1,'character_face',1,'base','approved','[]')")
    c.execute("INSERT INTO ref(project_id,owner_type,owner_id,state_key,status) VALUES(1,'location',2,'base','approved')")
    c.execute("INSERT INTO beat(id,project_id,chapter_id,idx,para_start,para_end) VALUES(1,1,1,0,1,1)")
    c.execute("INSERT INTO job(project_id,kind,payload_json,status,owner_type,owner_id) VALUES(1,'llm.chat','{}','queued','chapter_extract',3)")
    c.commit()
    c.close()


def make_v2_db(path: Path) -> None:
    """DB v0.2: schema hiện tại trừ 3 cột mới của bảng job."""
    from storyforge.app.db import SCHEMA

    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    schema = SCHEMA.replace("  heartbeat_at REAL,\n  last_progress_at REAL,\n  stalls INTEGER NOT NULL DEFAULT 0,\n", "")
    assert "heartbeat_at" not in schema
    c.executescript(schema)
    c.execute("INSERT INTO meta(key,value) VALUES('schema_version','2')")
    c.execute("INSERT INTO project(id,name,mode) VALUES(1,'v2','comic')")
    c.execute("INSERT INTO job(project_id,kind,payload_json,owner_type,owner_id) VALUES(1,'llm.chat','{}','x',1)")
    c.commit()
    c.close()
