"""Giao diện web quản lý: Jinja + HTMX + Alpine."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import (assets, checks, comic, dedupe, engine, jobs, llm, policy, prompts, review, state as st, system,
               validate, video)
from .config import get_settings
from .db import db, jd, jl, now

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter()

FOCUS_PRESETS = {"face": (.5, .35), "center": (.5, .5), "top": (.5, .15), "bottom": (.5, .85), "left": (.15, .5),
                 "right": (.85, .5), "top-left": (.15, .15), "top-right": (.85, .15)}
FOCUS_LABELS = {"face": "Mặt (trên giữa)", "center": "Giữa", "top": "Trên", "bottom": "Dưới", "left": "Trái",
                "right": "Phải", "top-left": "Trên trái", "top-right": "Trên phải"}


def focus_name(x: float, y: float) -> str:
    return min(FOCUS_PRESETS, key=lambda k: abs(FOCUS_PRESETS[k][0] - x) + abs(FOCUS_PRESETS[k][1] - y))


def human_bytes(n: Any) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def human_secs(t: Any) -> str:
    t = float(t or 0)
    if t < 60:
        return f"{t:.0f}s" if t >= 1 else (f"{t:.1f}s" if t else "")
    if t < 3600:
        return f"{t / 60:.0f} phút"
    return f"{t / 3600:.1f} giờ"


def media(aid: Any) -> str:
    return f"/media/{aid}" if aid else ""


env = templates.env
env.filters.update(fromjson=lambda s: jl(s, []), secs=human_secs, bytes=human_bytes,
                   dt=lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "")
env.globals.update(
    STATUS_LABELS=engine.STATUS_LABELS, FLAG_LABELS=policy.FLAG_LABELS, GATE_LABELS=policy.GATE_LABELS,
    MODE_LABELS=policy.MODE_LABELS, LEVELS=policy.LEVELS, GATES=policy.GATES, FIELDS=st.FIELDS,
    FIELD_LABELS=st.FIELD_LABELS, LOC_FIELDS=st.LOC_FIELDS, LOC_FIELD_LABELS=st.LOC_FIELD_LABELS,
    FLAGS=list(policy.FLAG_LABELS), TITLES=review.TITLES, FOCUS_LABELS=FOCUS_LABELS, focus_name=focus_name,
    MOTION_LABELS=video.MOTION_LABELS, VARIANTS=list(prompts.VARIANT_TEXT), rank=engine.rank,
    MODE_NAMES={"video": "Video có lời đọc", "comic": "Truyện tranh", "both": "Video và truyện tranh"},
    media=media, now_ts=now, STALL_WARN=jobs.STALL_WARN, missing_tools=system.missing_tools,
    INSTALL_HINT=system.INSTALL_HINT, marks_of=st.marks_of, is_removal=st.is_removal,
    removal_target=st.removal_target,
    vendor=lambda name: (HERE / "static" / "vendor" / name).exists(),
)


def hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def proj_ctx(pid: int) -> dict:
    p = engine.project(pid)
    return {"p": p, "pending": review.pending_counts(pid), "jobcounts": jobs.counts(pid),
            "missing_workers": jobs.missing_workers(pid)}


# ------------------------------------------------------------ làm giàu dữ liệu cho template
def subject_name(subject_type: str, sid: int) -> str:
    r = db.get("character" if subject_type == "character" else "location", sid)
    return r["name"] if r else "?"


def enrich(entity: str, row: dict) -> dict:
    r = dict(row)
    if entity == "character":
        r["aliases"] = jl(r["aliases_json"], [])
        face = db.one("SELECT asset_id FROM ref WHERE owner_type='character_face' AND owner_id=? AND asset_id IS NOT NULL "
                      "ORDER BY status='approved' DESC, id DESC LIMIT 1", r["id"])
        r["face_asset"] = face["asset_id"] if face else None
    elif entity == "location":
        r["aliases"] = jl(r["aliases_json"], [])
        lr = db.one("SELECT asset_id FROM ref WHERE owner_type='location' AND owner_id=? AND asset_id IS NOT NULL "
                    "ORDER BY status='approved' DESC, id LIMIT 1", r["id"])
        r["ref_asset"] = lr["asset_id"] if lr else None
    elif entity == "event":
        r["subject_name"] = subject_name(r["subject_type"], r["subject_id"])
        ch = db.get("chapter", r["chapter_id"])
        r["para"] = checks.locate(r["evidence"], llm.paragraphs(ch["text"])) if ch and r["evidence"] else None
        r["labels"] = st.FIELD_LABELS if r["subject_type"] == "character" else st.LOC_FIELD_LABELS
    elif entity == "beat":
        r["loc"] = db.get("location", r["location_id"]) if r["location_id"] else None
        r["cast"] = jl(r["cast_json"], [])
        r["dialogue"] = jl(r["dialogue_json"], [])
    elif entity == "ref":
        o = db.get("location" if r["owner_type"] == "location" else "character", r["owner_id"])
        r["owner_name"] = o["name"] if o else "?"
    elif entity == "beat_image":
        b = enrich("beat", row)
        ch = db.get("chapter", r["chapter_id"])
        p = engine.project(r["project_id"])
        s = p["settings"]
        b["chapter_idx"] = ch["idx"] if ch else 0
        b["stale"] = engine.stale_info(p, ch, row) if ch else {"stale": False}
        b["segment"] = db.one("SELECT * FROM segment WHERE beat_id=?", r["id"])
        b["panel"] = db.one("SELECT * FROM panel WHERE beat_id=?", r["id"])
        b["video_aspect"] = f"{s['video_width']}/{s['video_height']}"
        if b["panel"]:
            b["panel"]["flags"] = jl(b["panel"]["flags_json"], [])
            b["panel_aspect"] = round(engine.panel_aspect(p, b["panel"]), 4)
            b["balloons"] = db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", b["panel"]["id"])
        b["crop"] = engine.crop_info(p, row, b["segment"], b["panel"])
        b["_entity"] = "beat_image"
        b["_flags"] = jl(row.get("image_flags"), [])
        return b
    elif entity == "segment_audio":
        b = db.get("beat", r["beat_id"])
        r["image_asset_id"] = b["image_asset_id"] if b else None
        ch = db.get("chapter", r["chapter_id"])
        r["chapter_idx"] = ch["idx"] if ch else 0
    r["_entity"] = entity
    r["_flags"] = review.flags_of(entity, row) if entity in review.ENTITIES else []
    return r


ROW_MACRO = {"character": "character_row", "location": "location_row", "event": "event_row", "beat": "beat_row",
             "ref": "ref_card", "beat_image": "scene_card", "segment_audio": "audio_card"}


def row_html(request: Request, entity: str, row: dict) -> HTMLResponse:
    tpl = env.from_string('{% import "_macros.html" as m %}{{ m.' + ROW_MACRO[entity] + '(r) }}')
    return HTMLResponse(tpl.render(r=enrich(entity, row), request=request))


def scene_html(request: Request, beat_id: int):
    return row_html(request, "beat_image", db.get("beat", beat_id))


# ============================================================== dự án
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    projects = db.q("SELECT * FROM project ORDER BY id DESC")
    for p in projects:
        p["chapters"] = db.val("SELECT COUNT(*) FROM chapter WHERE project_id=?", p["id"])
        p["done"] = db.val("SELECT COUNT(*) FROM chapter WHERE project_id=? AND status='done'", p["id"])
        p["pending"] = review.pending_counts(p["id"])["total"]
    return render(request, "index.html", projects=projects)


@router.post("/projects")
def create_project(name: str = Form(...), mode: str = Form("video"), level: str = Form("2"),
                   style_prompt: str = Form(""), comic_format: str = Form("page")):
    pid = db.insert("project", name=name.strip() or "Dự án", mode=mode, style_prompt=style_prompt.strip(),
                    settings_json=jd({"comic_format": comic_format}),
                    policy_json=jd({**policy.DEFAULT_POLICY, "level": level}), created_at=now())
    return RedirectResponse(f"/p/{pid}", status_code=303)


@router.post("/p/{pid}/delete")
def delete_project(pid: int):
    db.delete("project", pid)
    return RedirectResponse("/", status_code=303)


def chapter_rows(pid: int) -> list[dict]:
    p = engine.project(pid)
    chapters = db.q("SELECT * FROM chapter WHERE project_id=? ORDER BY idx", pid)
    for c in chapters:
        c["progress"] = engine.chapter_progress(p, c)
        c["pending"] = (db.val("SELECT COUNT(*) FROM state_event WHERE chapter_id=? AND status='pending'", c["id"]) or 0) \
            + (db.val("SELECT COUNT(*) FROM beat WHERE chapter_id=? AND (status='pending' OR image_status='pending')",
                      c["id"]) or 0) \
            + (db.val("SELECT COUNT(*) FROM segment WHERE chapter_id=? AND audio_status='pending'", c["id"]) or 0)
        c["active"] = db.val("SELECT COUNT(*) FROM job WHERE project_id=? AND status IN ('queued','running') AND "
                             "((owner_type IN ('chapter_extract','chapter_beats','render_video','compose_comic') AND owner_id=?) "
                             "OR (owner_type='beat_image' AND owner_id IN (SELECT id FROM beat WHERE chapter_id=?)) "
                             "OR (owner_type='segment_audio' AND owner_id IN (SELECT id FROM segment WHERE chapter_id=?)))",
                             pid, c["id"], c["id"], c["id"]) or 0
    return chapters


def next_actions(ctx: dict, chapters: list[dict]) -> list[dict]:
    p, pend = ctx["p"], ctx["pending"]
    out = []
    if not chapters:
        out.append({"kind": "info", "text": "Chưa có chương nào. Nhập truyện ở cuối trang.", "href": "#import"})
    miss = system.missing_tools()
    if miss and engine.wants_video(p):
        out.append({"kind": "bad", "text": f"Thiếu {', '.join(miss)}: sẽ không dựng được video. {system.INSTALL_HINT}",
                    "href": "/storage#system"})
    if ctx["missing_workers"]:
        out.append({"kind": "bad", "text": "Có việc đang chờ nhưng không có worker nào nhận: "
                    + ", ".join(ctx["missing_workers"]), "href": "/workers"})
    if pend["total"]:
        out.append({"kind": "warn", "text": f"{pend['total']} mục chờ bạn duyệt", "href": f"/p/{p['id']}/review"})
    if pend["suggestions"]:
        out.append({"kind": "warn", "text": f"{pend['suggestions']} gợi ý gộp nhân vật / bối cảnh trùng",
                    "href": f"/p/{p['id']}/characters#suggestions"})
    failed = ctx["jobcounts"].get("failed", 0)
    if failed:
        out.append({"kind": "bad", "text": f"{failed} job lỗi", "href": f"/p/{p['id']}/jobs?status=failed"})
    stalled = db.val("SELECT COUNT(*) FROM job WHERE project_id=? AND stalls>0 AND status IN ('queued','running')", p["id"]) or 0
    if stalled:
        out.append({"kind": "warn", "text": f"{stalled} job từng bị thu hồi vì đứng yên: kiểm tra worker",
                    "href": f"/p/{p['id']}/jobs"})
    err = [c for c in chapters if c["status"] == "error"]
    if err:
        out.append({"kind": "bad", "text": f"Chương {', '.join(str(c['idx']) for c in err[:5])} bị lỗi",
                    "href": f"/p/{p['id']}/c/{err[0]['id']}"})
    pub_err = [c for c in chapters if c["status"] == "done" and c["error"]]
    if pub_err:
        out.append({"kind": "bad", "text": f"Chương {', '.join(str(c['idx']) for c in pub_err[:5])} xuất bản lỗi",
                    "href": f"/p/{p['id']}/c/{pub_err[0]['id']}"})
    recheck = [c for c in chapters if c["needs_recheck"]]
    if recheck:
        out.append({"kind": "info", "text": f"{len(recheck)} chương có trạng thái chương trước đã đổi, nên rà lại",
                    "href": f"/p/{p['id']}/c/{recheck[0]['id']}?tab=state"})
    warns = [w for w in engine.settings_warnings(p) if w["level"] in ("warn", "error")]
    if warns:
        out.append({"kind": "warn", "text": f"Cài đặt có {len(warns)} cảnh báo: {warns[0]['text'][:120]}",
                    "href": f"/p/{p['id']}/settings"})
    crops = engine.crop_mismatches(p)
    if crops:
        out.append({"kind": "info", "text": f"{crops} nhịp có khung video và khung truyện lệch nhau nhiều", "href": None})
    overflow = sum(c["progress"]["overflow"] for c in chapters)
    if overflow:
        out.append({"kind": "info", "text": f"{overflow} khung truyện có lời thoại tràn", "href": None})
    new = [c for c in chapters if c["status"] == "new" and not c["run_requested"]]
    if new and not p["autorun"]:
        out.append({"kind": "info", "text": f"{len(new)} chương chưa chạy. Bấm 'Chạy tất cả chương' hoặc bật Tự chạy.",
                    "href": None})
    ready = [c for c in chapters if c["status"] == "done" and not c["video_asset_id"] and not c["comic_cbz_asset_id"]
             and not c["error"]]
    if ready:
        out.append({"kind": "ok", "text": f"{len(ready)} chương đã xong, sẵn sàng xuất bản",
                    "href": f"/p/{p['id']}/c/{ready[0]['id']}"})
    return out


@router.get("/p/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    chapters = chapter_rows(pid)
    return render(request, "project.html", chapters=chapters, actions=next_actions(ctx, chapters),
                  stats=jobs.stats(pid), **ctx)


@router.get("/p/{pid}/chapters/table", response_class=HTMLResponse)
def chapters_table(request: Request, pid: int):
    return render(request, "partials/chapters_table.html", chapters=chapter_rows(pid), p=engine.project(pid))


HEADING = re.compile(r"^\s*((?:chương|chapter|chuong|hồi|quyển|phần)\s*[\dIVXLCivxlc]+[^\n]*)$", re.I | re.M)


def split_chapters(text: str, fallback_title: str = "") -> list[tuple[str, str]]:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    marks = list(HEADING.finditer(text))
    if not marks:
        return [(fallback_title, text)]
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append((m.group(1).strip(), body))
    pre = text[:marks[0].start()].strip()
    if pre and out:
        out[0] = (out[0][0], pre + "\n\n" + out[0][1])
    return out


def add_chapters(pid: int, items: list[tuple[str, str]]) -> int:
    nxt = (db.val("SELECT MAX(idx) FROM chapter WHERE project_id=?", pid) or 0) + 1
    for title, body in items:
        db.insert("chapter", project_id=pid, idx=nxt, title=title or f"Chương {nxt}", text=body, created_at=now())
        nxt += 1
    return len(items)


@router.post("/p/{pid}/import")
async def import_chapters(pid: int, text: str = Form(""), files: list[UploadFile] | None = None):
    items = split_chapters(text)
    for f in sorted(files or [], key=lambda f: f.filename or ""):
        if f.filename:
            items += split_chapters((await f.read()).decode("utf-8-sig", errors="replace"), Path(f.filename).stem)
    add_chapters(pid, items)
    engine.advance_project(pid)
    return RedirectResponse(f"/p/{pid}", status_code=303)


@router.post("/p/{pid}/autorun")
def toggle_autorun(pid: int, request: Request):
    p = db.get("project", pid)
    db.update("project", pid, autorun=0 if p["autorun"] else 1)
    engine.advance_project(pid)
    return back(request, f"/p/{pid}")


@router.post("/p/{pid}/pause")
def pause(pid: int, request: Request):
    db.update("project", pid, paused=1, pause_reason="Tạm dừng thủ công")
    return back(request, f"/p/{pid}")


@router.post("/p/{pid}/resume")
def resume(pid: int, request: Request):
    db.update("project", pid, paused=0, pause_reason="")
    engine.advance_project(pid)
    return back(request, f"/p/{pid}")


@router.post("/p/{pid}/run-all")
def run_all(pid: int, request: Request):
    db.ex("UPDATE chapter SET run_requested=1 WHERE project_id=?", pid)
    engine.advance_project(pid)
    return back(request, f"/p/{pid}")


@router.get("/p/{pid}/bible.md", response_class=PlainTextResponse)
def story_bible(pid: int):
    p = engine.project(pid)
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    out = [f"# {p['name']}: hồ sơ nhân vật và bối cảnh", "", f"Phong cách: {p['style_prompt']}", "", "## Nhân vật", ""]
    for c in engine.all_chars(pid):
        out += [f"### {c['name']}", f"- Tên gọi khác: {', '.join(c['aliases']) or '-'}", f"- Vai trò: {c['role'] or '-'}",
                f"- Ngoại hình: {c['appearance'] or '-'}", f"- Xuất hiện từ chương {c['first_chapter']}"]
        for t in st.timeline("character", c["id"], chapters):
            if t["changed"] and t["state"]:
                out.append(f"  - Chương {t['chapter']['idx']}: {st.describe(t['state'])}")
        out.append("")
    out += ["## Bối cảnh", ""]
    for l in engine.locs(pid):
        out += [f"### {l['name']}", f"- Mô tả: {l['description'] or '-'}", f"- Xuất hiện từ chương {l['first_chapter']}"]
        for t in st.timeline("location", l["id"], chapters):
            if t["changed"] and t["state"]:
                out.append(f"  - Chương {t['chapter']['idx']}: {st.describe(t['state'], st.LOC_FIELDS)}")
        out.append("")
    out += ["## Tóm tắt", ""] + [f"- Chương {c['idx']}: {c['summary']}" for c in
                                   db.q("SELECT idx, summary FROM chapter WHERE project_id=? ORDER BY idx", pid) if c["summary"]]
    return PlainTextResponse("\n".join(out), media_type="text/markdown; charset=utf-8")


# ============================================================== chương
@router.get("/p/{pid}/c/{cid}", response_class=HTMLResponse)
def chapter_page(request: Request, pid: int, cid: int, tab: str = ""):
    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    if not ch:
        raise HTTPException(404)
    p = ctx["p"]
    events = [enrich("event", e) for e in db.q("SELECT * FROM state_event WHERE chapter_id=? ORDER BY subject_type, subject_id, id", cid)]
    beats = [enrich("beat", b) for b in db.q("SELECT * FROM beat WHERE chapter_id=? ORDER BY idx", cid)]
    scenes = [enrich("beat_image", b) for b in db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved' ORDER BY idx", cid)]
    new_chars = [enrich("character", c) for c in db.q("SELECT * FROM character WHERE project_id=? AND first_chapter=?", pid, ch["idx"])]
    new_locs = [enrich("location", l) for l in db.q("SELECT * FROM location WHERE project_id=? AND first_chapter=?", pid, ch["idx"])]
    states = [{"c": c, "state": st.state_at(c["id"], ch["idx"])} for c in engine.all_chars(pid)
              if c["first_chapter"] <= ch["idx"] and c["status"] == "approved"]
    loc_states = [{"l": l, "state": st.loc_state_at(l["id"], ch["idx"])} for l in engine.locs(pid)
                  if l["first_chapter"] <= ch["idx"] and l["status"] == "approved"]
    chjobs = db.q("SELECT * FROM job WHERE (owner_type IN ('chapter_extract','chapter_beats','render_video','compose_comic') "
                  "AND owner_id=?) OR (owner_type='beat_image' AND owner_id IN (SELECT id FROM beat WHERE chapter_id=?)) "
                  "OR (owner_type='segment_audio' AND owner_id IN (SELECT id FROM segment WHERE chapter_id=?)) "
                  "ORDER BY id DESC LIMIT 40", cid, cid, cid)
    if not tab:
        tab = "prod" if ch["status"] in ("producing", "done") else "beats" if ch["status"] in ("review_beats", "beating") else "state"
    nav = db.one("SELECT (SELECT id FROM chapter WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT 1) prev, "
                 "(SELECT id FROM chapter WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1) nxt", pid, ch["idx"], pid, ch["idx"])
    return render(request, "chapter.html", ch=ch, events=events, beats=beats, scenes=scenes, new_chars=new_chars,
                  new_locs=new_locs, paras=llm.paragraphs(ch["text"]), states=states, loc_states=loc_states,
                  stale=sum(1 for s in scenes if s["stale"].get("stale")),
                  crops=sum(1 for s in scenes if s["crop"].get("mismatch")),
                  progress=engine.chapter_progress(p, ch), cpol=policy.chapter_policy(ch), chjobs=chjobs, tab=tab,
                  nav=nav, all_chars=engine.all_chars(pid), all_locs=engine.locs(pid), **ctx)


@router.post("/c/{cid}/run")
def run_chapter(cid: int, request: Request):
    engine.request_run(cid)
    return back(request)


@router.post("/c/{cid}/reset")
def reset_chapter(cid: int, request: Request, to: str = Form("extract")):
    engine.reset_chapter(cid, to)
    return back(request)


@router.post("/c/{cid}/retry")
def retry_chapter(cid: int, request: Request):
    ch = db.get("chapter", cid)
    if ch["status"] == "error":
        has_beats = db.val("SELECT COUNT(*) FROM beat WHERE chapter_id=?", cid)
        status = "review_beats" if has_beats else ("state_ready" if ch["summary"] else "new")
        db.update("chapter", cid, status=status, error="", run_requested=1)
    engine.advance_chapter(cid)
    return back(request)


@router.post("/c/{cid}/regen-stale")
def regen_stale(cid: int, request: Request):
    engine.regen_stale(cid)
    return back(request)


@router.post("/c/{cid}/publish")
def publish(cid: int, request: Request):
    ch = db.get("chapter", cid)
    p = engine.project(ch["project_id"])
    db.update("chapter", cid, error="")
    engine.audit(p["id"], "chapter", cid, "publish", "user")
    engine.publish(p, ch)
    return back(request)


@router.post("/c/{cid}/policy")
def chapter_policy(cid: int, request: Request, level: str = Form("")):
    cp = policy.chapter_policy(db.get("chapter", cid))
    cp["level"] = level
    db.update("chapter", cid, policy_json=jd({k: v for k, v in cp.items() if v}))
    return back(request)


@router.post("/c/{cid}/edit")
def edit_chapter(cid: int, request: Request, title: str = Form(""), text: str = Form("")):
    db.update("chapter", cid, title=title, text=text)
    return back(request)


@router.post("/c/{cid}/delete")
def delete_chapter(cid: int):
    ch = db.get("chapter", cid)
    db.delete("chapter", cid)
    return RedirectResponse(f"/p/{ch['project_id']}", status_code=303)


# ============================================================== duyệt
@router.get("/p/{pid}/review", response_class=HTMLResponse)
def review_page(request: Request, pid: int, chapter: int | None = None, entity: str = ""):
    ctx = proj_ctx(pid)
    sections = []
    for ent in review.ENTITIES:
        if entity and ent != entity:
            continue
        sql, args = review.pending_query(ent, pid, chapter)
        sections.append({"entity": ent, "rows": [enrich(ent, r) for r in db.q(sql + " ORDER BY id LIMIT 200", *args)]})
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    return render(request, "review.html", sections=sections, chapters=chapters, chapter=chapter, entity=entity, **ctx)


@router.post("/review/{entity}/{id_}/{action}")
def review_action(entity: str, id_: int, action: str, request: Request, note: str = Form("")):
    status = {"approve": "approved", "reject": "rejected", "reopen": "pending"}.get(action)
    if entity not in review.ENTITIES or not status:
        raise HTTPException(400)
    row = review.set_status(entity, id_, status, "user", note)
    if hx(request) and row and request.query_params.get("as") == "scene" and entity == "segment_audio":
        return scene_html(request, row["beat_id"])
    if hx(request) and row:
        return row_html(request, entity, review.get(entity, id_))
    return back(request)


@router.post("/p/{pid}/review/bulk")
def review_bulk(pid: int, request: Request, entity: str = Form(...), action: str = Form("approve"),
                chapter_id: int | None = Form(None)):
    review.bulk(entity, chapter_id, pid, {"approve": "approved", "reject": "rejected"}[action], "user:bulk")
    return back(request)


# ============================================================== nhân vật, bối cảnh, gộp trùng
def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]


@router.get("/p/{pid}/characters", response_class=HTMLResponse)
def characters_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    chars = [enrich("character", c) for c in engine.all_chars(pid, include_rejected=True)]
    return render(request, "characters.html", chars=chars, suggestions=dedupe.open_suggestions(pid), **ctx)


@router.get("/p/{pid}/characters/{cid}", response_class=HTMLResponse)
def character_page(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    row = db.get("character", cid)
    if not row:
        raise HTTPException(404)
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    events = [enrich("event", e) for e in db.q("SELECT * FROM state_event WHERE subject_type='character' AND subject_id=? "
                                              "ORDER BY chapter_idx, id", cid)]
    refs = [enrich("ref", r) for r in db.q("SELECT * FROM ref WHERE owner_id=? AND owner_type IN "
                                          "('character_face','character_outfit') ORDER BY owner_type, first_chapter, id", cid)]
    appearances = db.q("SELECT DISTINCT c.idx, c.id, c.title FROM beat b JOIN chapter c ON c.id=b.chapter_id "
                       "WHERE b.project_id=? AND b.cast_json LIKE ? ORDER BY c.idx", pid, f'%"character_id": {cid}%')
    last = chapters[-1] if chapters else None
    current = st.state_at(cid, last["idx"]) if last else {}
    # dấu vết hiện tại kèm chương bắt đầu có
    mark_since = {}
    for e in sorted(db.q("SELECT * FROM state_event WHERE subject_type='character' AND subject_id=? AND field='mark' "
                         "AND status='approved'", cid), key=lambda e: (e["chapter_idx"], e["id"])):
        if e["value"] and not st.is_removal(e["value"]):
            mark_since.setdefault(e["value"].strip().lower(), e["chapter_idx"])
    marks = [{"text": m, "since": mark_since.get(m.strip().lower(), 1)} for m in st.marks_of(current)]
    return render(request, "character.html", c=enrich("character", row), timeline=st.timeline("character", cid, chapters),
                  events=events, refs=refs, appearances=appearances, chapters=chapters, marks=marks,
                  others=[c for c in engine.all_chars(pid) if c["id"] != cid], **ctx)


@router.post("/character/{cid}/mark/remove")
def remove_mark(cid: int, request: Request, mark: str = Form(...), chapter_id: int = Form(...)):
    """Xóa đúng một dấu vết (sẹo, hình xăm) từ chương chỉ định, giữ các dấu vết khác."""
    engine.remove_mark(cid, mark, chapter_id)
    if hx(request):
        return HTMLResponse('<span class="chip gone">đã xóa</span>')
    return back(request)


@router.post("/character/{cid}/mark/add")
def add_mark(cid: int, request: Request, mark: str = Form(...), chapter_id: int = Form(...)):
    if mark.strip():
        engine.add_manual_event(db.get("chapter", chapter_id), "character", cid, "mark", mark.strip(), True,
                                evidence="(thêm thủ công)", note="add_mark")
    return back(request)


@router.post("/p/{pid}/characters/new")
def new_character(pid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                  appearance: str = Form(""), role: str = Form(""), first_chapter: int = Form(1)):
    db.insert("character", project_id=pid, name=name.strip(), aliases_json=jd(_split(aliases)), appearance=appearance,
              role=role, first_chapter=first_chapter, status="approved", reviewer="user", flags_json="[]", created_at=now())
    return back(request)


@router.post("/character/{cid}/edit")
def edit_character(cid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                   appearance: str = Form(""), role: str = Form(""), notes: str = Form("")):
    db.update("character", cid, name=name.strip(), aliases_json=jd(_split(aliases)), appearance=appearance.strip(),
              role=role.strip(), notes=notes.strip())
    c = db.get("character", cid)
    engine.refresh_refs(c["project_id"], c["first_chapter"])
    if hx(request):
        return row_html(request, "character", c)
    return back(request)


@router.get("/p/{pid}/locations", response_class=HTMLResponse)
def locations_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    rows = [enrich("location", l) for l in engine.locs(pid, include_rejected=True)]
    return render(request, "locations.html", locs=rows, suggestions=dedupe.open_suggestions(pid), **ctx)


@router.get("/p/{pid}/locations/{lid}", response_class=HTMLResponse)
def location_page(request: Request, pid: int, lid: int):
    ctx = proj_ctx(pid)
    row = db.get("location", lid)
    if not row:
        raise HTTPException(404)
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    events = [enrich("event", e) for e in db.q("SELECT * FROM state_event WHERE subject_type='location' AND subject_id=? "
                                              "ORDER BY chapter_idx, id", lid)]
    refs = [enrich("ref", r) for r in db.q("SELECT * FROM ref WHERE owner_type='location' AND owner_id=? "
                                          "ORDER BY first_chapter, id", lid)]
    return render(request, "location.html", l=enrich("location", row), timeline=st.timeline("location", lid, chapters),
                  events=events, refs=refs, others=[l for l in engine.locs(pid) if l["id"] != lid], **ctx)


@router.post("/p/{pid}/locations/new")
def new_location(pid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                 description: str = Form(""), first_chapter: int = Form(1)):
    db.insert("location", project_id=pid, name=name.strip(), aliases_json=jd(_split(aliases)), description=description,
              first_chapter=first_chapter, status="approved", reviewer="user", flags_json="[]", created_at=now())
    return back(request)


@router.post("/location/{lid}/edit")
def edit_location(lid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                  description: str = Form("")):
    db.update("location", lid, name=name.strip(), aliases_json=jd(_split(aliases)), description=description.strip())
    l = db.get("location", lid)
    engine.refresh_refs(l["project_id"], l["first_chapter"])
    if hx(request):
        return row_html(request, "location", l)
    return back(request)


@router.post("/merge/{kind}")
def merge_entities(kind: str, request: Request, keep: int = Form(...), merge: int = Form(...)):
    if kind not in ("merge_character", "merge_location"):
        raise HTTPException(400)
    dedupe.merge(kind, keep, merge)
    return back(request)


@router.post("/suggestion/{sid}/dismiss")
def dismiss_suggestion(sid: int, request: Request):
    dedupe.dismiss(sid)
    return HTMLResponse("") if hx(request) else back(request)


@router.post("/p/{pid}/dedupe/scan")
def dedupe_scan(pid: int, request: Request):
    dedupe.scan(pid)
    return back(request)


@router.post("/p/{pid}/dedupe/llm")
def dedupe_llm(pid: int, request: Request):
    dedupe.enqueue_llm(pid)
    return back(request)


# ============================================================== sự kiện trạng thái
@router.post("/event/{eid}/edit")
def edit_event(eid: int, request: Request, field: str = Form(...), value: str = Form(""),
               permanent: str = Form(""), until_chapter: str = Form(""), evidence: str = Form("")):
    e = db.get("state_event", eid)
    perm = bool(permanent)
    db.update("state_event", eid, field=st.normalize_field(field, perm, e["subject_type"]), value=value.strip(),
              permanent=int(perm), until_chapter=int(until_chapter) if until_chapter.strip() else None, evidence=evidence)
    engine.mark_recheck_after(e["project_id"], e["chapter_idx"])
    engine.refresh_refs(e["project_id"], e["chapter_idx"])
    if hx(request):
        return row_html(request, "event", db.get("state_event", eid))
    return back(request)


@router.post("/c/{cid}/event/new")
def new_event(cid: int, request: Request, subject: str = Form(...), field: str = Form(...), value: str = Form(""),
              permanent: str = Form(""), until_chapter: str = Form(""), evidence: str = Form("")):
    stype, _, sid = subject.partition(":")
    engine.add_manual_event(db.get("chapter", cid), stype, int(sid), field, value, bool(permanent),
                            int(until_chapter) if until_chapter.strip() else None, evidence)
    return back(request)


@router.post("/event/{eid}/delete")
def delete_event(eid: int, request: Request):
    e = db.get("state_event", eid)
    db.delete("state_event", eid)
    engine.mark_recheck_after(e["project_id"], e["chapter_idx"])
    engine.refresh_refs(e["project_id"], e["chapter_idx"])
    return HTMLResponse("") if hx(request) else back(request)


# ============================================================== nhịp và ảnh cảnh
def _parse_cast(pid: int, text: str) -> list[dict]:
    idx = engine.name_index(pid)
    out = []
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        c = idx.get(checks.norm(parts[0])) if parts and parts[0] else None
        if c:
            out.append({"character_id": c["id"], "name": c["name"], "pose": parts[1] if len(parts) > 1 else "",
                        "expression": parts[2] if len(parts) > 2 else ""})
    return out


def _parse_dialogue(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        kind = "speech"
        if line.startswith("(") and line.endswith(")"):
            kind, line = "thought", line[1:-1]
        who, sep, said = line.partition(":")
        if not sep:
            who, said, kind = "", line, "caption"
        out.append({"character": who.strip(), "text": said.strip(), "kind": kind})
    return out


@router.post("/beat/{bid}/edit")
def edit_beat(bid: int, request: Request, location: str = Form(""), variant: str = Form("default"),
              cast: str = Form(""), shot: str = Form("medium"), action: str = Form(""), mood: str = Form(""),
              dialogue: str = Form("")):
    b = db.get("beat", bid)
    ch = db.get("chapter", b["chapter_id"])
    p = engine.project(b["project_id"])
    loc_id = None
    if location.strip():
        loc, _ = engine.resolve_name(engine.loc_index(p["id"]), location, 1.0)
        loc = loc or engine.create_location(p, ch, location, [], "")
        loc_id = loc["id"]
    db.update("beat", bid, location_id=loc_id, variant=(variant or "default").strip().lower(),
              cast_json=jd(_parse_cast(p["id"], cast)), shot=shot, action=action.strip(), mood=mood.strip(),
              dialogue_json=jd(_parse_dialogue(dialogue)))
    return row_html(request, "beat", db.get("beat", bid)) if hx(request) else back(request)


@router.post("/beat/{bid}/regen")
def regen_beat(bid: int, request: Request):
    engine.regen_beat_image(bid)
    return scene_html(request, bid) if hx(request) else back(request)


@router.post("/beat/{bid}/prompt")
def beat_prompt(bid: int, request: Request, prompt_override: str = Form(""), regen: str = Form("")):
    db.update("beat", bid, prompt_override=prompt_override.strip())
    if regen:
        engine.regen_beat_image(bid, new_seed=False)
    return scene_html(request, bid) if hx(request) else back(request)


@router.post("/beat/{bid}/lock")
def beat_lock(bid: int, request: Request):
    b = db.get("beat", bid)
    db.update("beat", bid, image_locked=0 if b["image_locked"] else 1)
    return scene_html(request, bid) if hx(request) else back(request)


@router.post("/beat/{bid}/upload")
async def beat_upload(bid: int, request: Request, file: UploadFile):
    b = db.get("beat", bid)
    aid = assets.save_bytes(await file.read(), Path(file.filename or "x.png").suffix.lower() or ".png", "scene",
                            b["project_id"], meta={"uploaded": True})
    db.update("beat", bid, image_asset_id=aid, image_status="approved", image_reviewer="user:upload", image_locked=1)
    engine.audit(b["project_id"], "beat_image", bid, "upload", "user")
    engine.advance_chapter(b["chapter_id"])
    return back(request)


@router.post("/segment/{sid}/frame")
def segment_frame(sid: int, request: Request, focus: str = Form("center"), motion: str = Form("auto")):
    fx, fy = FOCUS_PRESETS.get(focus, (.5, .5))
    db.update("segment", sid, focus_x=fx, focus_y=fy, motion=motion if motion in video.MOTION_LABELS else "auto")
    s = db.get("segment", sid)
    return scene_html(request, s["beat_id"]) if hx(request) else back(request)


@router.post("/panel/{pid_}/frame")
def panel_frame(pid_: int, request: Request, focus: str = Form("center")):
    fx, fy = FOCUS_PRESETS.get(focus, (.5, .5))
    db.update("panel", pid_, focus_x=fx, focus_y=fy)
    pn = db.get("panel", pid_)
    return scene_html(request, pn["beat_id"]) if hx(request) else back(request)


@router.post("/beat/{bid}/sync-focus")
def sync_focus(bid: int, request: Request, to: str = Form("panel")):
    """Đồng bộ điểm lấy nét: khung truyện theo video (to=panel) hoặc ngược lại."""
    seg, pn = db.one("SELECT * FROM segment WHERE beat_id=?", bid), db.one("SELECT * FROM panel WHERE beat_id=?", bid)
    if seg and pn:
        if to == "panel":
            db.update("panel", pn["id"], focus_x=seg["focus_x"], focus_y=seg["focus_y"])
        else:
            db.update("segment", seg["id"], focus_x=pn["focus_x"], focus_y=pn["focus_y"])
    return scene_html(request, bid) if hx(request) else back(request)


@router.post("/segment/{sid}/regen-audio")
def regen_segment_audio(sid: int, request: Request):
    engine.regen_audio(sid)
    return scene_html(request, db.get("segment", sid)["beat_id"]) if hx(request) else back(request)


@router.post("/segment/{sid}/narration")
def edit_narration(sid: int, request: Request, narration: str = Form(...)):
    db.update("segment", sid, narration=narration.strip())
    engine.regen_audio(sid)
    return scene_html(request, db.get("segment", sid)["beat_id"]) if hx(request) else back(request)


# ============================================================== ảnh tham chiếu
@router.get("/p/{pid}/refs", response_class=HTMLResponse)
def refs_page(request: Request, pid: int, status: str = "", kind: str = ""):
    ctx = proj_ctx(pid)
    sql, args = "SELECT * FROM ref WHERE project_id=?", [pid]
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND owner_type=?"
        args.append(kind)
    refs = [enrich("ref", r) for r in db.q(sql + " ORDER BY owner_type, owner_id, first_chapter, id", *args)]
    return render(request, "refs.html", refs=refs, status=status, kind=kind, **ctx)


@router.post("/ref/{rid}/regen")
def regen_ref(rid: int, request: Request, prompt: str = Form("")):
    if prompt.strip():
        r = db.get("ref", rid)
        db.update("ref", rid, prompt=prompt.strip(), locked=1, status="missing", regen_count=r["regen_count"] + 1)
        engine.enqueue_ref(rid)
        engine.advance_producing(r["project_id"])
    else:
        engine.regen_ref(rid)
    return row_html(request, "ref", db.get("ref", rid)) if hx(request) else back(request)


@router.post("/ref/{rid}/lock")
def lock_ref(rid: int, request: Request):
    r = db.get("ref", rid)
    db.update("ref", rid, locked=0 if r["locked"] else 1)
    return row_html(request, "ref", db.get("ref", rid)) if hx(request) else back(request)


@router.post("/ref/{rid}/upload")
async def upload_ref(rid: int, request: Request, file: UploadFile):
    r = db.get("ref", rid)
    aid = assets.save_bytes(await file.read(), Path(file.filename or "x.png").suffix.lower() or ".png", "ref",
                            r["project_id"], meta={"uploaded": True})
    db.update("ref", rid, asset_id=aid, status="approved", reviewer="user:upload", locked=1)
    engine.audit(r["project_id"], "ref", rid, "upload", "user")
    engine.advance_producing(r["project_id"])
    return back(request)


# ============================================================== bóng thoại
@router.post("/panel/{pid_}/balloon")
def add_balloon(pid_: int, text: str = Form("..."), kind: str = Form("speech")):
    n = db.val("SELECT COUNT(*) FROM balloon WHERE panel_id=?", pid_) or 0
    bid = db.insert("balloon", panel_id=pid_, idx=n, kind=kind, text=text, x=.3, y=.06, w=.4, tail_x=.5, tail_y=.45)
    return JSONResponse(db.get("balloon", bid))


@router.post("/balloon/{bid}")
async def update_balloon(bid: int, request: Request):
    data = await request.json()
    allowed = {k: data[k] for k in ("x", "y", "w", "text", "kind", "tail_x", "tail_y") if k in data}
    for k in ("x", "y", "w", "tail_x", "tail_y"):
        if k in allowed and allowed[k] is not None:
            allowed[k] = max(0.0, min(1.0, float(allowed[k])))
    db.update("balloon", bid, **allowed)
    return JSONResponse(db.get("balloon", bid))


@router.post("/balloon/{bid}/delete")
def delete_balloon(bid: int):
    db.delete("balloon", bid)
    return JSONResponse({"ok": True})


# ============================================================== xem trước
@router.get("/p/{pid}/c/{cid}/preview/video", response_class=HTMLResponse)
def preview_video(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    segs = []
    for s in db.q("""SELECT s.*, b.image_asset_id, b.image_status, b.shot FROM segment s JOIN beat b ON b.id=s.beat_id
                     WHERE s.chapter_id=? ORDER BY s.idx""", cid):
        motion = s["motion"] if s["motion"] != "auto" else video.auto_motion(s["idx"], s["shot"])
        segs.append({"id": s["id"], "beat_id": s["beat_id"], "image": media(s["image_asset_id"]),
                     "image_status": s["image_status"], "audio": media(s["audio_asset_id"]),
                     "audio_status": s["audio_status"], "text": s["narration"],
                     "duration": s["duration"] or video.estimate_duration(s["narration"]),
                     "fx": s["focus_x"], "fy": s["focus_y"], "motion": motion})
    s = ctx["p"]["settings"]
    return render(request, "preview_video.html", ch=ch, segs_json=json.dumps(segs, ensure_ascii=False), n=len(segs),
                  aspect=f"{s['video_width']}/{s['video_height']}", **ctx)


@router.get("/p/{pid}/c/{cid}/preview/comic", response_class=HTMLResponse)
def preview_comic(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    g = engine.comic_geometry(ctx["p"])
    pages = []
    for pg in db.q("SELECT * FROM page WHERE chapter_id=? ORDER BY idx", cid):
        panels = []
        for pn in db.q("""SELECT pn.*, b.image_asset_id, b.image_status FROM panel pn JOIN beat b ON b.id=pn.beat_id
                          WHERE pn.page_id=? ORDER BY pn.slot""", pg["id"]):
            x, y, w, h = comic.panel_px(pn, pg["width"], pg["height"], g["margin"], g["gutter"])
            panels.append({"id": pn["id"], "beat_id": pn["beat_id"],
                           "rect": [x / pg["width"], y / pg["height"], w / pg["width"], h / pg["height"]],
                           "image": media(pn["image_asset_id"]), "status": pn["image_status"],
                           "fx": pn["focus_x"], "fy": pn["focus_y"], "flags": jl(pn["flags_json"], []),
                           "font": round(100 * engine.base_font(pg["width"]) / max(1, w), 2),
                           "balloons": db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", pn["id"])})
        pages.append({"id": pg["id"], "idx": pg["idx"], "w": pg["width"], "h": pg["height"], "panels": panels})
    return render(request, "preview_comic.html", ch=ch, pages_json=json.dumps(pages, ensure_ascii=False),
                  n=len(pages), g=g, **ctx)


# ============================================================== job
def _jobs(pid: int | None, status: str) -> list[dict]:
    sql, args = "SELECT * FROM job WHERE 1=1", []
    if pid is not None:
        sql += " AND project_id=?"
        args.append(pid)
    if status:
        sql += " AND status=?"
        args.append(status)
    rows = db.q(sql + " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 "
                      "ELSE 3 END, id DESC LIMIT 300", *args)
    pos = jobs.queue_positions(rows)
    t = now()
    for r in rows:
        r["pos"] = pos.get(r["id"])
        r["hb_age"] = t - r["heartbeat_at"] if r["heartbeat_at"] else None
        r["idle"] = t - (r["last_progress_at"] or r["started_at"] or t) if r["status"] == "running" else 0
    return rows


@router.get("/p/{pid}/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, pid: int, status: str = ""):
    ctx = proj_ctx(pid)
    return render(request, "jobs.html", status=status, rows=_jobs(pid, status), stats=jobs.stats(pid), **ctx)


@router.get("/p/{pid}/jobs/table", response_class=HTMLResponse)
def jobs_table(request: Request, pid: int, status: str = ""):
    return render(request, "partials/jobs_table.html", rows=_jobs(pid, status), p={"id": pid},
                  jobcounts=jobs.counts(pid), stats=jobs.stats(pid))


@router.post("/job/{jid}/retry")
def job_retry(jid: int, request: Request):
    j = db.get("job", jid)
    jobs.retry(jid)
    ot, oid = j["owner_type"], j["owner_id"]
    if ot == "beat_image":
        db.update("beat", oid, image_status="generating")
    elif ot == "segment_audio":
        db.update("segment", oid, audio_status="generating")
    elif ot == "ref_image":
        db.update("ref", oid, status="generating")
    elif ot in ("chapter_extract", "chapter_beats"):
        db.update("chapter", oid, status="extracting" if ot == "chapter_extract" else "beating", error="")
    elif ot in ("render_video", "compose_comic"):
        db.update("chapter", oid, error="")
    return back(request)


@router.post("/job/{jid}/cancel")
def job_cancel(jid: int, request: Request):
    jobs.cancel(jid)
    return back(request)


@router.post("/p/{pid}/jobs/retry-failed")
def retry_failed(pid: int, request: Request):
    for j in db.q("SELECT id FROM job WHERE project_id=? AND status='failed'", pid):
        job_retry(j["id"], request)
    return back(request)


@router.get("/job/{jid}", response_class=HTMLResponse)
def job_detail(request: Request, jid: int):
    j = db.get("job", jid)
    if not j:
        raise HTTPException(404)
    ctx = proj_ctx(j["project_id"]) if j["project_id"] else {}
    return render(request, "job.html", j=j, payload=json.dumps(jl(j["payload_json"], {}), ensure_ascii=False, indent=2),
                  output=json.dumps(jl(j["output_json"], {}), ensure_ascii=False, indent=2), **ctx)


# ============================================================== cài đặt
SETTING_GROUPS = [
    ("LLM", [
        ("llm_model", "Model LLM (gợi ý cho worker)", "text"),
        ("llm_fallback_model", "Model dự phòng khi thử lại lần cuối", "text"),
        ("llm_temperature", "Temperature", "float"), ("llm_max_tokens", "Max tokens (trống = mặc định)", "int?"),
        ("beats_span_retries", "Số lần bắt LLM chia nhịp lại khi sai khoảng đoạn (0–2; hết lượt thì tự sửa + gắn cờ)", "int"),
        ("context_chapters", "Số chương tóm tắt đưa vào ngữ cảnh", "int"),
        ("check_english", "Gắn cờ khi mô tả cho model ảnh không phải tiếng Anh", "bool"),
        ("english_allow", "Từ/cụm tiếng Việt cho phép trong mô tả ảnh (phân tách dấu phẩy)", "text"),
        ("llm_extra", "Hướng dẫn thêm cho LLM", "area"),
    ]),
    ("Gộp trùng", [
        ("dedupe_fuzzy", "Tự gợi ý khi tên gần giống", "bool"),
        ("dedupe_llm", "Hiện nút “Nhờ LLM rà trùng”", "bool"),
        ("dedupe_threshold", "Ngưỡng tên gần giống (0.85–1; mặc định 0.90)", "float"),
    ]),
    ("Ảnh", [
        ("image_model", "Model ảnh (gợi ý cho worker)", "text"), ("image_steps", "Số bước (trống = mặc định)", "int?"),
        ("image_area", "Diện tích ảnh gốc mỗi nhịp (pixel)", "int"), ("ref_size", "Cạnh ảnh tham chiếu", "int"),
        ("max_cast_refs", "Số nhân vật tối đa có ảnh tham chiếu mỗi cảnh", "int"),
        ("max_refs", "Số ảnh tham chiếu tối đa mỗi lần tạo", "int"),
        ("crop_overlap_min", "Cảnh báo khi khung video và khung truyện trùng nhau dưới (0–1)", "float"),
        ("negative_prompt", "Negative prompt", "text"),
    ]),
    ("Giọng đọc", [
        ("tts_model", "Model TTS (gợi ý cho worker)", "text"), ("tts_voice", "Giọng", "text"),
        ("tts_rate", "Tốc độ", "float"), ("language", "Ngôn ngữ", "text"),
    ]),
    ("Video", [
        ("video_width", "Rộng (số chẵn)", "int"), ("video_height", "Cao (số chẵn)", "int"), ("video_fps", "FPS", "int"),
        ("video_encoder", "Bộ mã hóa (auto, libx264, h264_videotoolbox, h264_nvenc)", "text"),
        ("video_zoom", "Mức zoom / lia", "float"), ("video_transition", "Mờ chuyển cảnh (giây, 0 = tắt)", "float"),
        ("video_gap", "Khoảng lặng giữa đoạn (giây)", "float"), ("video_loudnorm", "Chuẩn hóa âm lượng", "bool"),
    ]),
    ("Truyện tranh", [
        ("comic_format", "Khổ", "select:page=Trang truyện in,webtoon=Webtoon cuộn dọc"),
        ("comic_page_width", "Rộng trang", "int"), ("comic_page_height", "Cao trang", "int"),
        ("comic_max_panels", "Số khung tối đa mỗi trang (1–9)", "int"), ("comic_margin", "Lề", "int"),
        ("comic_gutter", "Khoảng cách khung", "int"), ("webtoon_width", "Rộng webtoon", "int"),
        ("webtoon_panels_per_page", "Số khung mỗi đoạn webtoon", "int"),
    ]),
    ("Duyệt", [
        ("auto_regen_on_reject", "Tự tạo lại khi bị từ chối", "bool"), ("max_regen", "Số lần tạo lại tối đa", "int"),
    ]),
]
GROUP_KEYS = {g: [k for k, _, _ in fields] for g, fields in SETTING_GROUPS}


@router.get("/p/{pid}/settings", response_class=HTMLResponse)
def settings_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    p = ctx["p"]
    warns = engine.settings_warnings(p)
    need = validate.required_image_area(p, p["settings"])
    overridden = set((jl(p["settings_json"], {}) or {}).keys())
    return render(request, "settings.html", groups=SETTING_GROUPS, pp=policy.project_policy(p), eff=policy.effective(p),
                  warns=warns, warn_keys={w["key"] for w in warns}, need=need, overridden=overridden,
                  defaults=engine.DEFAULT_SETTINGS, **ctx)


@router.post("/p/{pid}/settings")
async def save_settings(pid: int, request: Request):
    form = await request.form()
    p = db.get("project", pid)
    s = jl(p["settings_json"], {}) or {}
    for _, fields in SETTING_GROUPS:
        for key, _, typ in fields:
            v = form.get(key)
            if typ == "bool":
                s[key] = v is not None
                continue
            if v is None:
                continue
            v = str(v).strip()
            try:
                if typ == "int":
                    s[key] = int(v) if v else engine.DEFAULT_SETTINGS[key]
                elif typ == "int?":
                    s[key] = int(v) if v else None
                elif typ == "float":
                    s[key] = float(v) if v else engine.DEFAULT_SETTINGS[key]
                else:
                    s[key] = v
            except ValueError:
                pass
    s = validate.fix(s)
    db.update("project", pid, name=str(form.get("name") or p["name"]), mode=str(form.get("mode") or p["mode"]),
              style_prompt=str(form.get("style_prompt") or ""), settings_json=jd(s))
    return RedirectResponse(f"/p/{pid}/settings?saved=1", status_code=303)


@router.post("/p/{pid}/settings/reset")
def reset_settings(pid: int, group: str = ""):
    p = db.get("project", pid)
    s = jl(p["settings_json"], {}) or {}
    for k in GROUP_KEYS.get(group, []):
        s.pop(k, None)
    db.update("project", pid, settings_json=jd(s))
    return RedirectResponse(f"/p/{pid}/settings?saved=1#g-{group}", status_code=303)


@router.post("/p/{pid}/settings/recommended")
def apply_recommended(pid: int):
    p = engine.project(pid)
    s = jl(p["settings_json"], {}) or {}
    s["image_area"] = validate.required_image_area(p, p["settings"])["recommended"]
    db.update("project", pid, settings_json=jd(s))
    return RedirectResponse(f"/p/{pid}/settings?saved=1", status_code=303)


@router.post("/p/{pid}/policy")
async def save_policy(pid: int, request: Request):
    form = await request.form()
    pol = {"level": str(form.get("level") or "2"), "checkpoint_every": int(form.get("checkpoint_every") or 0), "gates": {}}
    for g in policy.GATES:
        if form.get(f"{g}_override"):
            pol["gates"][g] = {"mode": str(form.get(f"{g}_mode") or "manual"), "batch": str(form.get(f"{g}_batch") or "item"),
                               "sample_rate": float(form.get(f"{g}_sample") or 0.1),
                               "always_review": [str(x) for x in form.getlist(f"{g}_always")]}
    db.update("project", pid, policy_json=jd(pol))
    engine.audit(pid, "project", pid, "policy", "user", f"cấp {pol['level']}")
    return RedirectResponse(f"/p/{pid}/settings?saved=1", status_code=303)


@router.get("/p/{pid}/audit", response_class=HTMLResponse)
def audit_page(request: Request, pid: int, reviewer: str = ""):
    ctx = proj_ctx(pid)
    sql, args = "SELECT * FROM audit WHERE project_id=?", [pid]
    if reviewer == "auto":
        sql += " AND reviewer LIKE 'auto:%'"
    elif reviewer == "user":
        sql += " AND reviewer LIKE 'user%'"
    return render(request, "audit.html", rows=db.q(sql + " ORDER BY id DESC LIMIT 500", *args), reviewer=reviewer, **ctx)


# ============================================================== hệ thống
@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request):
    return render(request, "workers.html", workers=db.q("SELECT * FROM worker ORDER BY last_seen DESC"),
                  token=get_settings().worker_token, now=now(), base_url=str(request.base_url).rstrip("/"),
                  jobcounts=jobs.counts(), missing=jobs.missing_workers(), stats=jobs.stats())


@router.get("/storage", response_class=HTMLResponse)
def storage_page(request: Request, result: str = ""):
    s = get_settings()
    return render(request, "storage.html", u=assets.usage(), result=result, cfg=s, font=comic.find_font(s.font_path),
                  backups=sorted(s.backup_dir.glob("storyforge-*.db"), reverse=True)[:20])


@router.post("/storage/gc")
def storage_gc(hours: float = Form(24.0), dry_run: str = Form("")):
    r = assets.gc(hours, bool(dry_run))
    msg = f"{'Thử: sẽ ' if r['dry_run'] else 'Đã '}xóa {r['rows']} bản ghi, giải phóng {human_bytes(r['bytes'])}"
    return RedirectResponse(f"/storage?result={msg}", status_code=303)


@router.post("/storage/backup")
def storage_backup():
    from . import migrations

    out = migrations.backup(get_settings().db_path, get_settings().backup_dir, "manual")
    return RedirectResponse(f"/storage?result=Đã sao lưu {out.name if out else ''}", status_code=303)


@router.get("/media/{aid}")
def media_file(aid: int, download: int = 0):
    a = db.get("asset", aid)
    if not a:
        raise HTTPException(404)
    path = assets.abs_path(a)
    if not path.exists():
        raise HTTPException(404, "File đã bị xóa")
    return FileResponse(path, media_type=a["mime"], filename=path.name if download else None)
