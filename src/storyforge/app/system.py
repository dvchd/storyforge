"""Kiểm tra công cụ hệ thống (ffmpeg, ffprobe) và thông báo cài đặt bằng tiếng Việt."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .config import get_settings

INSTALL_HINT = ("Cài ffmpeg (có kèm ffprobe): macOS `brew install ffmpeg` · Windows `winget install Gyan.FFmpeg` · "
                "Ubuntu/Debian `sudo apt install ffmpeg`. Hướng dẫn: https://ffmpeg.org/download.html . "
                "Nếu đã cài ở chỗ khác, đặt biến môi trường STORYFORGE_FFMPEG=/đường/dẫn/tới/ffmpeg rồi khởi động lại app.")
INSTALL_URL = "https://ffmpeg.org/download.html"


class ToolMissing(RuntimeError):
    """Thiếu công cụ hệ thống cần cho một bước."""


def _available(cmd: str) -> bool:
    return bool(cmd) and (Path(cmd).exists() or shutil.which(cmd) is not None)


def missing_tools() -> list[str]:
    s = get_settings()
    out = []
    if not _available(s.ffmpeg):
        out.append("ffmpeg")
    if not _available(s.ffprobe):
        out.append("ffprobe")
    return out


def require_ffmpeg() -> None:
    miss = missing_tools()
    if miss:
        raise ToolMissing(f"Thiếu {', '.join(miss)} nên không dựng được video. {INSTALL_HINT}")


def startup_report() -> str:
    miss = missing_tools()
    if not miss:
        return ""
    plat = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
    return (f"\n⚠  Không tìm thấy {', '.join(miss)} ({plat}). Giao diện, duyệt và truyện tranh vẫn chạy, "
            f"nhưng sẽ KHÔNG dựng được video và không đo chính xác thời lượng audio.\n   {INSTALL_HINT}\n")
