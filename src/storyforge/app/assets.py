"""Lưu tài nguyên theo hash nội dung, đường dẫn tương đối; thống kê dung lượng và dọn file mồ côi."""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
from pathlib import Path

from .config import get_settings
from .db import db, jd, now

EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
            ".mp4": "video/mp4", ".srt": "text/plain", ".cbz": "application/zip", ".pdf": "application/pdf",
            ".md": "text/markdown"}


def _rel(sha: str, ext: str) -> str:
    return f"{sha[:2]}/{sha[2:4]}/{sha}{ext}"


def abs_path(asset: dict) -> Path:
    return get_settings().assets_dir / asset["path"]


def path_of(asset_id: int | None) -> Path | None:
    if not asset_id:
        return None
    a = db.get("asset", asset_id)
    return abs_path(a) if a else None


def save_file(src: Path, kind: str, project_id: int | None, input_hash: str = "", model_id: str = "",
              meta: dict | None = None, move: bool = False) -> int:
    src = Path(src)
    h = hashlib.sha256()
    with src.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    ext = src.suffix.lower() or ".bin"
    rel = _rel(sha, ext)
    dst = get_settings().assets_dir / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        (shutil.move if move else shutil.copyfile)(str(src), str(dst))
    elif move:
        src.unlink(missing_ok=True)
    mime = EXT_MIME.get(ext) or mimetypes.guess_type(dst.name)[0] or "application/octet-stream"
    return db.insert("asset", project_id=project_id, kind=kind, path=rel, mime=mime, sha256=sha,
                     size=dst.stat().st_size, input_hash=input_hash, model_id=model_id, meta_json=jd(meta or {}),
                     created_at=now())


def save_bytes(data: bytes, ext: str, kind: str, project_id: int | None, **kw) -> int:
    tmp = get_settings().tmp_dir / (hashlib.sha256(data).hexdigest() + ext)
    tmp.write_bytes(data)
    return save_file(tmp, kind, project_id, move=True, **kw)


def find_by_input_hash(input_hash: str, kind: str | None = None) -> dict | None:
    if not input_hash:
        return None
    if kind:
        return db.one("SELECT * FROM asset WHERE input_hash=? AND kind=? ORDER BY id DESC LIMIT 1", input_hash, kind)
    return db.one("SELECT * FROM asset WHERE input_hash=? ORDER BY id DESC LIMIT 1", input_hash)


def sha_of(asset_id: int | None) -> str:
    if not asset_id:
        return ""
    return db.val("SELECT sha256 FROM asset WHERE id=?", asset_id) or ""


# ------------------------------------------------------------ dung lượng & dọn dẹp
REF_COLUMNS = [
    ("ref", "asset_id"), ("beat", "image_asset_id"), ("segment", "audio_asset_id"), ("page", "asset_id"),
    ("chapter", "video_asset_id"), ("chapter", "srt_asset_id"), ("chapter", "comic_cbz_asset_id"),
    ("chapter", "comic_pdf_asset_id"),
]


def referenced_ids() -> set[int]:
    ids: set[int] = set()
    for t, col in REF_COLUMNS:
        ids.update(r[0] for r in db.conn().execute(f"SELECT {col} FROM {t} WHERE {col} IS NOT NULL"))
    return ids


def _fill_sizes() -> None:
    for a in db.q("SELECT id, path FROM asset WHERE size=0"):
        p = get_settings().assets_dir / a["path"]
        if p.exists():
            db.update("asset", a["id"], size=p.stat().st_size)


def usage() -> dict:
    _fill_sizes()
    refs = referenced_ids()
    by_kind = db.q("SELECT kind, COUNT(*) n, SUM(size) bytes FROM asset GROUP BY kind ORDER BY bytes DESC")
    by_project = db.q("""SELECT a.project_id, p.name, COUNT(*) n, SUM(a.size) bytes FROM asset a
                         LEFT JOIN project p ON p.id=a.project_id GROUP BY a.project_id ORDER BY bytes DESC""")
    orphans = [a for a in db.q("SELECT id, size, path, created_at FROM asset") if a["id"] not in refs]
    # Nhiều dòng asset có thể trỏ cùng một file (cùng nội dung): chỉ file không còn dòng nào được dùng mới xóa.
    live_paths = {a["path"] for a in db.q("SELECT id, path FROM asset") if a["id"] in refs}
    disk_total = sum(f.stat().st_size for f in get_settings().assets_dir.rglob("*") if f.is_file())
    return {
        "by_kind": by_kind, "by_project": by_project, "disk_total": disk_total,
        "orphan_count": len(orphans), "orphan_bytes": sum(a["size"] for a in orphans if a["path"] not in live_paths),
    }


def gc(min_age_hours: float = 24.0, dry_run: bool = False) -> dict:
    """Xóa asset không còn được tham chiếu (ảnh của lần tạo cũ, dự án đã xóa...) và file lạc trên đĩa.

    Ảnh cũ chỉ còn tác dụng làm bộ nhớ đệm khi bạn quay lại đúng đầu vào cũ; min_age_hours giữ lại các bản gần đây.
    """
    refs = referenced_ids()
    cutoff = now() - min_age_hours * 3600
    rows = db.q("SELECT id, path, size, created_at FROM asset")
    dead = [a for a in rows if a["id"] not in refs and (a["created_at"] or 0) < cutoff]
    dead_ids = {a["id"] for a in dead}
    live_paths = {a["path"] for a in rows if a["id"] not in dead_ids}
    freed, files = 0, 0
    if not dry_run:
        for a in dead:
            db.delete("asset", a["id"])
        for a in dead:
            if a["path"] in live_paths:
                continue
            p = get_settings().assets_dir / a["path"]
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
                files += 1
                live_paths.add(a["path"])
        known = {a["path"] for a in db.q("SELECT path FROM asset")}
        root = get_settings().assets_dir
        for f in root.rglob("*"):
            if f.is_file() and f.relative_to(root).as_posix() not in known and f.stat().st_mtime < cutoff:
                freed += f.stat().st_size
                f.unlink()
                files += 1
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        for t in get_settings().tmp_dir.iterdir():   # chỉ dọn thư mục tạm cũ, không đụng job đang chạy
            if t.stat().st_mtime < cutoff:
                shutil.rmtree(t, ignore_errors=True) if t.is_dir() else t.unlink(missing_ok=True)
    else:
        freed = sum(a["size"] for a in dead if a["path"] not in live_paths)
    return {"rows": len(dead), "files": files, "bytes": freed, "dry_run": dry_run}
