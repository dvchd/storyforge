"""Giao dien web quan ly: Jinja + HTMX + Alpine."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import assets, checks, engine, jobs, llm, policy, review, state as st
from .config import get_settings
from .db import db, jd, jl, now

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter()

# ------------------------------------------------------------ jinja helpers
env = templates.env
env.filters["fromjson"] = lambda s: jl(s, [])
env.filters["dt"] = lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
env.filters["secs"] = lambda t: f"{t:.1f}s" if t else ""
env.globals.update(
    STATUS_LABELS=engine.STATUS_LABELS, FLAG_LABELS=policy.FLAG_LABELS, GATE_LABELS=policy.GATE_LABELS,
    MODE_LABELS=policy.MODE_LABELS, LEVELS=policy.LEVELS, GATES=policy.GATES, FIELDS=st.FIELDS,
    FIELD_LABELS=st.FIELD_LABELS, FLAGS=list(policy.FLAG_LABELS),
)


def media(aid: Any) -> str:
    return f"/media/{aid}" if aid else ""


env.globals["media"] = media


def hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def proj_ctx(pid: int) -> dict:
    p = engine.project(pid)
    return {"p": p, "pending": review.pending_counts(pid), "jobcounts": jobs.counts(pid)}


# ------------------------------------------------------------ enrich rows
def enrich(entity: str, row: dict) -> dict:
    r = dict(row)
    if entity == "character":
        r["aliases"] = jl(r["aliases_json"], [])
        face = db.one("SELECT * FROM ref WHERE owner_type='character_face' AND owner_id=? AND asset_id IS NOT NULL "
                      "ORDER BY status='approved' DESC, id DESC LIMIT 1", r["id"])
        r["face_asset"] = face["asset_id"] if face else None
    elif entity == "location":
        lr = db.one("SELECT * FROM ref WHERE owner_type='location' AND owner_id=?", r["id"])
        r["ref_asset"] = lr["asset_id"] if lr else None
    elif entity == "event":
        c = db.get("character", r["character_id"])
        r["char_name"] = c["name"] if c else "?"
    elif entity == "beat":
        loc = db.get("location", r["location_id"]) if r["location_id"] else None
        r["loc"] = loc
        r["cast"] = jl(r["cast_json"], [])
        r["dialogue"] = jl(r["dialogue_json"], [])
    elif entity == "ref":
        if r["owner_type"] == "location":
            o = db.get("location", r["owner_id"])
            r["owner_name"] = o["name"] if o else "?"
        else:
            o = db.get("character", r["owner_id"])
            r["owner_name"] = o["name"] if o else "?"
    elif entity in ("segment_image", "segment_audio", "panel_image"):
        table = "segment" if entity.startswith("segment") else "panel"
        ch = db.get("chapter", r["chapter_id"])
        p = engine.project(r["project_id"])
        r["stale"] = engine.is_stale(p, ch, table, row) if ch else False
        r["chapter_idx"] = ch["idx"] if ch else 0
        b = db.get("beat", r["beat_id"])
        r["beat"] = enrich("beat", b) if b else None
        if table == "panel":
            r["balloons"] = db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", r["id"])
    r["_entity"] = entity
    r["_flags"] = review.flags_of(entity, row) if entity in review.ENTITIES else []
    return r


ROW_MACRO = {"character": "character_row", "location": "location_row", "event": "event_row", "beat": "beat_row",
             "ref": "ref_card", "segment_image": "segment_card", "segment_audio": "segment_card",
             "panel_image": "panel_card"}


def row_html(request: Request, entity: str, row: dict) -> HTMLResponse:
    tpl = env.from_string('{% import "_macros.html" as m %}{{ m.' + ROW_MACRO[entity] + '(r) }}')
    return HTMLResponse(tpl.render(r=enrich(entity, row), request=request))


# ============================================================== projects
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
                   style_prompt: str = Form("")):
    pid = db.insert("project", name=name.strip() or "Dự án", mode=mode, style_prompt=style_prompt.strip(),
                    policy_json=jd({**policy.DEFAULT_POLICY, "level": level}), created_at=now())
    return RedirectResponse(f"/p/{pid}", status_code=303)


@router.post("/p/{pid}/delete")
def delete_project(pid: int):
    db.delete("project", pid)
    return RedirectResponse("/", status_code=303)


@router.get("/p/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    chapters = db.q("SELECT * FROM chapter WHERE project_id=? ORDER BY idx", pid)
    for c in chapters:
        c["progress"] = engine.chapter_progress(ctx["p"], c)
        c["pending"] = (db.val("SELECT COUNT(*) FROM state_event WHERE chapter_id=? AND status='pending'", c["id"]) or 0) \
            + (db.val("SELECT COUNT(*) FROM beat WHERE chapter_id=? AND status='pending'", c["id"]) or 0) \
            + engine.pending_identity(c)
    return render(request, "project.html", chapters=chapters, **ctx)


HEADING = re.compile(r"^\s*((?:chương|chapter|chuong|hồi|quyển)\s*[\dIVXLCivxlc]+[^\n]*)$", re.I | re.M)


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
async def import_chapters(pid: int, request: Request, text: str = Form(""), files: list[UploadFile] | None = None):
    items = split_chapters(text)
    for f in sorted(files or [], key=lambda f: f.filename or ""):
        if not f.filename:
            continue
        raw = (await f.read()).decode("utf-8-sig", errors="replace")
        items += split_chapters(raw, Path(f.filename).stem)
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


# ============================================================== chapters
@router.get("/p/{pid}/c/{cid}", response_class=HTMLResponse)
def chapter_page(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    if not ch:
        raise HTTPException(404)
    events = [enrich("event", e) for e in db.q("SELECT * FROM state_event WHERE chapter_id=? ORDER BY character_id, id", cid)]
    beats = [enrich("beat", b) for b in db.q("SELECT * FROM beat WHERE chapter_id=? ORDER BY idx", cid)]
    segments = [enrich("segment_image", s) for s in db.q("SELECT * FROM segment WHERE chapter_id=? ORDER BY idx", cid)]
    panels = [enrich("panel_image", s) for s in db.q("SELECT * FROM panel WHERE chapter_id=? ORDER BY page_id, slot", cid)]
    new_chars = [enrich("character", c) for c in db.q("SELECT * FROM character WHERE project_id=? AND first_chapter=?", pid, ch["idx"])]
    new_locs = [enrich("location", l) for l in db.q("SELECT * FROM location WHERE project_id=? AND first_chapter=?", pid, ch["idx"])]
    paras = llm.paragraphs(ch["text"])
    states = []
    for c in engine.all_chars(pid):
        if c["first_chapter"] <= ch["idx"] and c["status"] == "approved":
            states.append({"c": c, "state": st.state_at(c["id"], ch["idx"])})
    stale = sum(1 for s in segments if s["stale"]) + sum(1 for s in panels if s["stale"])
    cpol = jl(ch["policy_json"], {}) or {}
    chjobs = db.q("SELECT * FROM job WHERE owner_id=? AND owner_type IN ('chapter_extract','chapter_beats','render_video','compose_comic') ORDER BY id DESC LIMIT 6", cid)
    return render(request, "chapter.html", ch=ch, events=events, beats=beats, segments=segments, panels=panels,
                  new_chars=new_chars, new_locs=new_locs, paras=paras, states=states, stale=stale,
                  progress=engine.chapter_progress(ctx["p"], ch), cpol=cpol, chjobs=chjobs, **ctx)


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
        db.update("chapter", cid, status="state_ready" if ch["summary"] and not has_beats else "new",
                  error="", run_requested=1)
        if has_beats:
            db.update("chapter", cid, status="review_beats")
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
    engine.audit(p["id"], "chapter", cid, "publish", "user")
    engine.publish(p, ch)
    return back(request)


@router.post("/c/{cid}/policy")
def chapter_policy(cid: int, request: Request, level: str = Form("")):
    db.update("chapter", cid, policy_json=jd({"level": level} if level else {}))
    return back(request)


@router.post("/c/{cid}/edit")
def edit_chapter(cid: int, request: Request, title: str = Form(""), text: str = Form("")):
    db.update("chapter", cid, title=title, text=text)
    return back(request)


@router.post("/c/{cid}/delete")
def delete_chapter(cid: int, request: Request):
    ch = db.get("chapter", cid)
    db.delete("chapter", cid)
    return RedirectResponse(f"/p/{ch['project_id']}", status_code=303)


# ============================================================== review
@router.get("/p/{pid}/review", response_class=HTMLResponse)
def review_page(request: Request, pid: int, chapter: int | None = None):
    ctx = proj_ctx(pid)
    sections = []
    for ent, (table, col, _) in review.ENTITIES.items():
        sql = f"SELECT * FROM {table} WHERE project_id=? AND {col}='pending'"
        args: list[Any] = [pid]
        if chapter and ent not in ("character", "location", "ref"):
            sql += " AND chapter_id=?"
            args.append(chapter)
        rows = [enrich(ent, r) for r in db.q(sql + " ORDER BY id LIMIT 200", *args)]
        sections.append({"entity": ent, "rows": rows})
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    return render(request, "review.html", sections=sections, chapters=chapters, chapter=chapter, **ctx)


@router.post("/review/{entity}/{id_}/{action}")
def review_action(entity: str, id_: int, action: str, request: Request, note: str = Form("")):
    status = {"approve": "approved", "reject": "rejected", "reopen": "pending"}.get(action)
    if entity not in review.ENTITIES or not status:
        raise HTTPException(400)
    row = review.set_status(entity, id_, status, "user", note)
    if hx(request) and row:
        return row_html(request, entity, review.get(entity, id_))
    return back(request)


@router.post("/p/{pid}/review/bulk")
def review_bulk(pid: int, request: Request, entity: str = Form(...), action: str = Form("approve"),
                chapter_id: int | None = Form(None)):
    status = {"approve": "approved", "reject": "rejected"}[action]
    review.bulk(entity, chapter_id, pid, status, "user:bulk")
    return back(request)


# ============================================================== characters
@router.get("/p/{pid}/characters", response_class=HTMLResponse)
def characters_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    chars = [enrich("character", c) for c in engine.all_chars(pid, include_rejected=True)]
    return render(request, "characters.html", chars=chars, **ctx)


@router.get("/p/{pid}/characters/{cid}", response_class=HTMLResponse)
def character_page(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    c = enrich("character", db.get("character", cid))
    chapters = db.q("SELECT id, idx, title FROM chapter WHERE project_id=? ORDER BY idx", pid)
    tl = st.timeline(cid, chapters)
    events = [enrich("event", e) for e in db.q("SELECT * FROM state_event WHERE character_id=? ORDER BY chapter_idx, id", cid)]
    refs = [enrich("ref", r) for r in db.q(
        "SELECT * FROM ref WHERE owner_id=? AND owner_type IN ('character_face','character_outfit') ORDER BY owner_type, id", cid)]
    return render(request, "character.html", c=c, timeline=tl, events=events, refs=refs, chapters=chapters, **ctx)


@router.post("/p/{pid}/characters/new")
def new_character(pid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                  appearance: str = Form(""), role: str = Form(""), first_chapter: int = Form(1)):
    db.insert("character", project_id=pid, name=name.strip(), aliases_json=jd(_split(aliases)),
              appearance=appearance, role=role, first_chapter=first_chapter, status="approved", reviewer="user",
              flags_json="[]", created_at=now())
    return back(request)


def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]


@router.post("/character/{cid}/edit")
def edit_character(cid: int, request: Request, name: str = Form(...), aliases: str = Form(""),
                   appearance: str = Form(""), role: str = Form("")):
    db.update("character", cid, name=name.strip(), aliases_json=jd(_split(aliases)), appearance=appearance.strip(),
              role=role.strip())
    if hx(request):
        return row_html(request, "character", db.get("character", cid))
    return back(request)


@router.post("/character/{cid}/merge")
def merge_character(cid: int, request: Request, into: int = Form(...)):
    """Gop nhan vat trung (LLM tao nham) vao nhan vat co san."""
    src, dst = db.get("character", cid), db.get("character", into)
    if not src or not dst or src["id"] == dst["id"]:
        raise HTTPException(400)
    with db.tx():
        al = list(dict.fromkeys([*jl(dst["aliases_json"], []), src["name"], *jl(src["aliases_json"], [])]))
        db.update("character", dst["id"], aliases_json=jd(al))
        db.ex("UPDATE state_event SET character_id=? WHERE character_id=?", dst["id"], src["id"])
        for b in db.q("SELECT id, cast_json FROM beat WHERE project_id=?", src["project_id"]):
            cast = jl(b["cast_json"], [])
            changed = False
            for c in cast:
                if c.get("character_id") == src["id"]:
                    c["character_id"], c["name"] = dst["id"], dst["name"]
                    changed = True
            if changed:
                db.update("beat", b["id"], cast_json=jd(cast))
        db.delete("character", src["id"])
    engine.audit(src["project_id"], "character", dst["id"], "merge", "user", f"gộp {src['name']}")
    engine.advance_project(src["project_id"])
    return RedirectResponse(f"/p/{src['project_id']}/characters/{dst['id']}", status_code=303)


# ============================================================== state events
@router.post("/event/{eid}/edit")
def edit_event(eid: int, request: Request, field: str = Form(...), value: str = Form(""),
               permanent: str = Form(""), until_chapter: str = Form(""), evidence: str = Form("")):
    e = db.get("state_event", eid)
    db.update("state_event", eid, field=st.normalize_field(field), value=value.strip(), permanent=1 if permanent else 0,
              until_chapter=int(until_chapter) if until_chapter.strip() else None, evidence=evidence)
    engine.mark_recheck_after(e["project_id"], e["chapter_idx"])
    if hx(request):
        return row_html(request, "event", db.get("state_event", eid))
    return back(request)


@router.post("/c/{cid}/event/new")
def new_event(cid: int, request: Request, character_id: int = Form(...), field: str = Form(...),
              value: str = Form(""), permanent: str = Form(""), until_chapter: str = Form(""), evidence: str = Form("")):
    ch = db.get("chapter", cid)
    eid = db.insert("state_event", project_id=ch["project_id"], character_id=character_id, chapter_id=cid,
                    chapter_idx=ch["idx"], field=st.normalize_field(field), value=value.strip(),
                    permanent=1 if permanent else 0,
                    until_chapter=int(until_chapter) if until_chapter.strip() else None, evidence=evidence,
                    status="approved", reviewer="user", flags_json="[]", created_at=now())
    engine.audit(ch["project_id"], "event", eid, "create", "user")
    engine.mark_recheck_after(ch["project_id"], ch["idx"])
    return back(request)


@router.post("/event/{eid}/delete")
def delete_event(eid: int, request: Request):
    e = db.get("state_event", eid)
    db.delete("state_event", eid)
    engine.mark_recheck_after(e["project_id"], e["chapter_idx"])
    if hx(request):
        return HTMLResponse("")
    return back(request)


# ============================================================== locations
@router.get("/p/{pid}/locations", response_class=HTMLResponse)
def locations_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    rows = [enrich("location", l) for l in engine.locs(pid, include_rejected=True)]
    return render(request, "locations.html", locs=rows, **ctx)


@router.post("/p/{pid}/locations/new")
def new_location(pid: int, request: Request, name: str = Form(...), variant: str = Form("default"),
                 description: str = Form(""), first_chapter: int = Form(1)):
    db.insert("location", project_id=pid, name=name.strip(), variant=variant.strip() or "default",
              description=description, first_chapter=first_chapter, status="approved", reviewer="user",
              flags_json="[]", created_at=now())
    return back(request)


@router.post("/location/{lid}/edit")
def edit_location(lid: int, request: Request, name: str = Form(...), variant: str = Form("default"),
                  description: str = Form("")):
    db.update("location", lid, name=name.strip(), variant=variant.strip() or "default", description=description.strip())
    if hx(request):
        return row_html(request, "location", db.get("location", lid))
    return back(request)


# ============================================================== beats
def _parse_cast(pid: int, text: str) -> list[dict]:
    idx = engine.name_index(pid)
    out = []
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if not parts or not parts[0]:
            continue
        c = idx.get(checks.norm(parts[0]))
        if not c:
            continue
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
        loc = engine.find_location(p["id"], location, variant) or engine.create_location(p, ch, location, variant, "")
        loc_id = loc["id"]
    db.update("beat", bid, location_id=loc_id, cast_json=jd(_parse_cast(p["id"], cast)), shot=shot,
              action=action.strip(), mood=mood.strip(), dialogue_json=jd(_parse_dialogue(dialogue)))
    if hx(request):
        return row_html(request, "beat", db.get("beat", bid))
    return back(request)


# ============================================================== refs
@router.get("/p/{pid}/refs", response_class=HTMLResponse)
def refs_page(request: Request, pid: int, status: str = ""):
    ctx = proj_ctx(pid)
    sql = "SELECT * FROM ref WHERE project_id=?"
    args: list[Any] = [pid]
    if status:
        sql += " AND status=?"
        args.append(status)
    refs = [enrich("ref", r) for r in db.q(sql + " ORDER BY owner_type, owner_id, id", *args)]
    return render(request, "refs.html", refs=refs, status=status, **ctx)


@router.post("/ref/{rid}/regen")
def regen_ref(rid: int, request: Request, prompt: str = Form("")):
    if prompt.strip():
        db.update("ref", rid, prompt=prompt.strip(), locked=0)
    engine.regen_ref(rid)
    if hx(request):
        return row_html(request, "ref", db.get("ref", rid))
    return back(request)


@router.post("/ref/{rid}/upload")
async def upload_ref(rid: int, request: Request, file: UploadFile):
    """Tai anh tham chieu cua ban (anh ve tay, anh chon loc) thay anh model tao."""
    r = db.get("ref", rid)
    data = await file.read()
    ext = Path(file.filename or "x.png").suffix.lower() or ".png"
    aid = assets.save_bytes(data, ext, "ref", r["project_id"], meta={"uploaded": True})
    db.update("ref", rid, asset_id=aid, status="approved", reviewer="user:upload", locked=1)
    engine.audit(r["project_id"], "ref", rid, "upload", "user")
    engine.advance_producing(r["project_id"])
    return back(request)


# ============================================================== segments / panels
@router.post("/segment/{sid}/regen-image")
def regen_segment_image(sid: int, request: Request):
    engine.regen_scene("segment", sid)
    if hx(request):
        return row_html(request, "segment_image", db.get("segment", sid))
    return back(request)


@router.post("/segment/{sid}/regen-audio")
def regen_segment_audio(sid: int, request: Request):
    engine.regen_audio(sid)
    if hx(request):
        return row_html(request, "segment_image", db.get("segment", sid))
    return back(request)


@router.post("/segment/{sid}/narration")
def edit_narration(sid: int, request: Request, narration: str = Form(...)):
    db.update("segment", sid, narration=narration.strip())
    engine.regen_audio(sid)
    if hx(request):
        return row_html(request, "segment_image", db.get("segment", sid))
    return back(request)


@router.post("/panel/{pid_}/regen")
def regen_panel(pid_: int, request: Request):
    engine.regen_scene("panel", pid_)
    if hx(request):
        return row_html(request, "panel_image", db.get("panel", pid_))
    return back(request)


@router.post("/panel/{pid_}/balloon")
def add_balloon(pid_: int, request: Request, text: str = Form("..."), kind: str = Form("speech")):
    n = db.val("SELECT COUNT(*) FROM balloon WHERE panel_id=?", pid_) or 0
    bid = db.insert("balloon", panel_id=pid_, idx=n, kind=kind, text=text, x=.3, y=.1, w=.4, tail_x=.5, tail_y=.6)
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


# ============================================================== preview
@router.get("/p/{pid}/c/{cid}/preview/video", response_class=HTMLResponse)
def preview_video(request: Request, pid: int, cid: int):
    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    segs = []
    for s in db.q("SELECT * FROM segment WHERE chapter_id=? ORDER BY idx", cid):
        img = s["image_asset_id"]
        if not img:
            b = db.get("beat", s["beat_id"])
            if b and b["location_id"]:
                lr = db.one("SELECT asset_id FROM ref WHERE owner_type='location' AND owner_id=?", b["location_id"])
                img = lr["asset_id"] if lr else None
        segs.append({"id": s["id"], "image": media(img), "audio": media(s["audio_asset_id"]),
                     "text": s["narration"], "duration": s["duration"] or max(3.0, len(s["narration"]) / 14.0),
                     "status": s["image_status"]})
    return render(request, "preview_video.html", ch=ch, segs_json=json.dumps(segs, ensure_ascii=False), n=len(segs), **ctx)


@router.get("/p/{pid}/c/{cid}/preview/comic", response_class=HTMLResponse)
def preview_comic(request: Request, pid: int, cid: int):
    from . import comic

    ctx = proj_ctx(pid)
    ch = db.get("chapter", cid)
    s = ctx["p"]["settings"]
    pages = []
    for pg in db.q("SELECT * FROM page WHERE chapter_id=? ORDER BY idx", cid):
        rects = comic.LAYOUTS[pg["layout"]]
        panels = []
        for pn in db.q("SELECT * FROM panel WHERE page_id=? ORDER BY slot", pg["id"]):
            r = rects[min(pn["slot"], len(rects) - 1)]
            panels.append({"id": pn["id"], "rect": r, "image": media(pn["image_asset_id"]),
                           "status": pn["image_status"],
                           "balloons": db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", pn["id"])})
        pages.append({"id": pg["id"], "idx": pg["idx"], "panels": panels, "asset": media(pg["asset_id"])})
    return render(request, "preview_comic.html", ch=ch, pages=pages,
                  pages_json=json.dumps(pages, ensure_ascii=False),
                  ratio=f"{s['comic_page_width']}/{s['comic_page_height']}", **ctx)


# ============================================================== jobs
@router.get("/p/{pid}/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, pid: int, status: str = ""):
    ctx = proj_ctx(pid)
    return render(request, "jobs.html", status=status, rows=_jobs(pid, status), **ctx)


def _jobs(pid: int, status: str) -> list[dict]:
    sql = "SELECT * FROM job WHERE project_id=?"
    args: list[Any] = [pid]
    if status:
        sql += " AND status=?"
        args.append(status)
    return db.q(sql + " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, id DESC LIMIT 300", *args)


@router.get("/p/{pid}/jobs/table", response_class=HTMLResponse)
def jobs_table(request: Request, pid: int, status: str = ""):
    return render(request, "partials/jobs_table.html", rows=_jobs(pid, status), p={"id": pid},
                  jobcounts=jobs.counts(pid))


@router.post("/job/{jid}/retry")
def job_retry(jid: int, request: Request):
    j = db.get("job", jid)
    jobs.retry(jid)
    if j["owner_type"] in ("segment_image", "panel_image"):
        db.update(j["owner_type"].split("_")[0], j["owner_id"], image_status="generating")
    elif j["owner_type"] == "segment_audio":
        db.update("segment", j["owner_id"], audio_status="generating")
    elif j["owner_type"] == "ref_image":
        db.update("ref", j["owner_id"], status="generating")
    elif j["owner_type"] in ("chapter_extract", "chapter_beats"):
        db.update("chapter", j["owner_id"], status="extracting" if j["owner_type"] == "chapter_extract" else "beating", error="")
    return back(request)


@router.post("/job/{jid}/cancel")
def job_cancel(jid: int, request: Request):
    jobs.cancel(jid)
    return back(request)


@router.get("/job/{jid}", response_class=HTMLResponse)
def job_detail(request: Request, jid: int):
    j = db.get("job", jid)
    if not j:
        raise HTTPException(404)
    ctx = proj_ctx(j["project_id"]) if j["project_id"] else {}
    return render(request, "job.html", j=j, payload=json.dumps(jl(j["payload_json"], {}), ensure_ascii=False, indent=2),
                  output=json.dumps(jl(j["output_json"], {}), ensure_ascii=False, indent=2), **ctx)


# ============================================================== settings
SETTING_FIELDS = [
    ("llm_model", "Model LLM (gợi ý cho worker)", "text"), ("llm_temperature", "Temperature LLM", "float"),
    ("llm_max_tokens", "Max tokens LLM (để trống = mặc định)", "int?"),
    ("llm_extra", "Hướng dẫn thêm cho LLM", "area"),
    ("image_model", "Model ảnh (gợi ý cho worker)", "text"), ("image_steps", "Số bước tạo ảnh (trống = mặc định)", "int?"),
    ("negative_prompt", "Negative prompt", "text"), ("ref_size", "Cạnh ảnh tham chiếu", "int"),
    ("max_cast_refs", "Số nhân vật tối đa có ảnh tham chiếu mỗi cảnh", "int"),
    ("max_refs", "Số ảnh tham chiếu tối đa mỗi lần tạo", "int"),
    ("tts_model", "Model TTS (gợi ý cho worker)", "text"), ("tts_voice", "Giọng đọc", "text"),
    ("tts_rate", "Tốc độ đọc", "float"), ("language", "Ngôn ngữ", "text"),
    ("video_width", "Rộng video", "int"), ("video_height", "Cao video", "int"), ("video_fps", "FPS", "int"),
    ("video_encoder", "Bộ mã hóa (auto, libx264, h264_videotoolbox, h264_nvenc)", "text"),
    ("video_zoom", "Mức zoom Ken Burns", "float"),
    ("comic_page_width", "Rộng trang truyện", "int"), ("comic_page_height", "Cao trang truyện", "int"),
    ("comic_panels_per_page", "Số khung mỗi trang (1 đến 6)", "int"),
    ("comic_panel_area", "Diện tích ảnh mỗi khung (pixel)", "int"),
    ("context_chapters", "Số chương tóm tắt đưa vào ngữ cảnh", "int"),
    ("auto_regen_on_reject", "Tự tạo lại khi bị từ chối", "bool"), ("max_regen", "Số lần tạo lại tối đa", "int"),
]


@router.get("/p/{pid}/settings", response_class=HTMLResponse)
def settings_page(request: Request, pid: int):
    ctx = proj_ctx(pid)
    pp = policy.project_policy(ctx["p"])
    eff = policy.effective(ctx["p"])
    return render(request, "settings.html", fields=SETTING_FIELDS, pp=pp, eff=eff, **ctx)


@router.post("/p/{pid}/settings")
async def save_settings(pid: int, request: Request):
    form = await request.form()
    p = db.get("project", pid)
    s = jl(p["settings_json"], {}) or {}
    for key, _, typ in SETTING_FIELDS:
        v = form.get(key)
        if typ == "bool":
            s[key] = v is not None
            continue
        if v is None:
            continue
        v = str(v).strip()
        if typ == "int":
            s[key] = int(v) if v else engine.DEFAULT_SETTINGS[key]
        elif typ == "int?":
            s[key] = int(v) if v else None
        elif typ == "float":
            s[key] = float(v) if v else engine.DEFAULT_SETTINGS[key]
        else:
            s[key] = v
    db.update("project", pid, name=str(form.get("name") or p["name"]), mode=str(form.get("mode") or p["mode"]),
              style_prompt=str(form.get("style_prompt") or ""), settings_json=jd(s))
    return RedirectResponse(f"/p/{pid}/settings", status_code=303)


@router.post("/p/{pid}/policy")
async def save_policy(pid: int, request: Request):
    form = await request.form()
    pol = {"level": str(form.get("level") or "2"), "checkpoint_every": int(form.get("checkpoint_every") or 0), "gates": {}}
    for g in policy.GATES:
        if not form.get(f"{g}_override"):
            continue
        pol["gates"][g] = {
            "mode": str(form.get(f"{g}_mode") or "manual"),
            "batch": str(form.get(f"{g}_batch") or "item"),
            "sample_rate": float(form.get(f"{g}_sample") or 0.1),
            "always_review": [str(x) for x in form.getlist(f"{g}_always")],
        }
    db.update("project", pid, policy_json=jd(pol))
    engine.audit(pid, "project", pid, "policy", "user", f"cấp {pol['level']}")
    return RedirectResponse(f"/p/{pid}/settings", status_code=303)


@router.get("/p/{pid}/audit", response_class=HTMLResponse)
def audit_page(request: Request, pid: int, reviewer: str = ""):
    ctx = proj_ctx(pid)
    sql = "SELECT * FROM audit WHERE project_id=?"
    args: list[Any] = [pid]
    if reviewer == "auto":
        sql += " AND reviewer LIKE 'auto:%'"
    elif reviewer == "user":
        sql += " AND reviewer LIKE 'user%'"
    rows = db.q(sql + " ORDER BY id DESC LIMIT 500", *args)
    return render(request, "audit.html", rows=rows, reviewer=reviewer, **ctx)


# ============================================================== workers & media
@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request):
    ws = db.q("SELECT * FROM worker ORDER BY last_seen DESC")
    return render(request, "workers.html", workers=ws, token=get_settings().worker_token, now=now(),
                  base_url=str(request.base_url).rstrip("/"), jobcounts=jobs.counts())


@router.get("/media/{aid}")
def media_file(aid: int, download: int = 0):
    a = db.get("asset", aid)
    if not a:
        raise HTTPException(404)
    path = assets.abs_path(a)
    if download:
        return FileResponse(path, media_type=a["mime"], filename=path.name)
    return FileResponse(path, media_type=a["mime"])
