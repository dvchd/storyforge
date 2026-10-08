"""Cấu hình app đọc từ biến môi trường."""
from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path
    worker_token: str
    host: str = "127.0.0.1"
    port: int = 8765
    ffmpeg: str = "ffmpeg"
    ui_password: str = ""
    font_path: str = ""
    start_local_runner: bool = True
    # Job chờ lâu được cộng thêm 1 điểm ưu tiên mỗi aging_seconds, tối đa aging_cap.
    aging_seconds: int = 300
    aging_cap: int = 5
    lease: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "storyforge.db"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def ffprobe(self) -> str:
        p = Path(self.ffmpeg)
        cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return str(cand)
        return shutil.which("ffprobe") or "ffprobe"


def _token(data_dir: Path) -> str:
    env = os.environ.get("STORYFORGE_WORKER_TOKEN", "").strip()
    if env:
        return env
    f = data_dir / "worker_token.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(24)
    f.write_text(tok, encoding="utf-8")
    return tok


def load_settings(data_dir: str | os.PathLike | None = None, **overrides) -> Settings:
    d = Path(data_dir or os.environ.get("STORYFORGE_DATA", "./data")).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    s = Settings(
        data_dir=d,
        worker_token=_token(d),
        host=os.environ.get("STORYFORGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("STORYFORGE_PORT", "8765")),
        ffmpeg=os.environ.get("STORYFORGE_FFMPEG", "") or shutil.which("ffmpeg") or "ffmpeg",
        ui_password=os.environ.get("STORYFORGE_UI_PASSWORD", ""),
        font_path=os.environ.get("STORYFORGE_FONT", ""),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    for p in (s.assets_dir, s.tmp_dir, s.backup_dir):
        p.mkdir(parents=True, exist_ok=True)
    return s


SETTINGS: Settings | None = None


def get_settings() -> Settings:
    if SETTINGS is None:
        raise RuntimeError("Settings chưa được khởi tạo")
    return SETTINGS


def set_settings(s: Settings) -> None:
    global SETTINGS
    SETTINGS = s
