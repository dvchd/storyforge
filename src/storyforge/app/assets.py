"""Luu tai nguyen theo hash noi dung, duong dan tuong doi, an toan tren moi he dieu hanh."""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
from pathlib import Path

from .config import get_settings
from .db import db, jd, now

EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
            ".mp4": "video/mp4", ".srt": "text/plain", ".cbz": "application/zip", ".pdf": "application/pdf"}


def _rel(sha: str, ext: str) -> str:
    return f"{sha[:2]}/{sha[2:4]}/{sha}{ext}"


def abs_path(asset: dict) -> Path:
    return get_settings().assets_dir / asset["path"]


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
                     input_hash=input_hash, model_id=model_id, meta_json=jd(meta or {}), created_at=now())


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
