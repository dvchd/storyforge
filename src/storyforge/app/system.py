"""Kiểm tra công cụ hệ thống (ffmpeg, ffprobe) và thông báo cài đặt bằng tiếng Việt."""
from __future__ import annotations

import shutil
import socket
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


# ------------------------------------------------------------- diagnose mở rộng
def port_free(host: str, port: int) -> bool:
    """Cổng còn trống không (False = đang có tiến trình khác nghe, serve sẽ lỗi)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(1.0)
        try:
            sk.bind((host, port))
        except OSError:
            return False
    return True


def dir_writable(d: str | Path) -> bool:
    try:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def disk_free_mb(d: str | Path) -> float:
    try:
        return shutil.disk_usage(d).free / 1e6
    except OSError:
        return -1.0


def queued_without_workers() -> list[str]:
    """Các loại job AI đang chờ mà không có worker online nào nhận. Lỗi DB (chưa có dữ
    liệu) thì trả [] để bỏ qua mục này thay vì báo sai."""
    try:
        from . import jobs

        return jobs.missing_workers()
    except Exception:  # noqa: BLE001
        return []


def doctor_report() -> list[dict]:
    """Danh sách kiểm tra cho `storyforge-app doctor`: mỗi mục {ok, text}."""
    from .config import get_settings as _gs

    s = _gs()
    out: list[dict] = []

    def item(ok: bool, text: str) -> None:
        out.append({"ok": ok, "text": text})

    item(dir_writable(s.data_dir), f"Thư mục dữ liệu: {s.data_dir}"
         + ("" if dir_writable(s.data_dir) else " ✗ KHÔNG ghi được (kiểm tra quyền)"))
    free = disk_free_mb(s.data_dir)
    item(free < 0 or free >= 500,
         f"Ổ đĩa còn trống: {free:,.0f} MB" if free >= 0 else "Ổ đĩa: không đọc được"
         + (" (cảnh báo: sắp đầy, ảnh/video tốn dung lượng)" if 0 <= free < 500 else ""))
    item(port_free(s.host, s.port), f"Cổng {s.host}:{s.port}"
         + ("" if port_free(s.host, s.port) else " ✗ ĐANG BẬN (app khác đang chạy? đổi cổng bằng --port)"))
    miss = missing_tools()
    item(not miss, "ffmpeg + ffprobe: đủ" if not miss else f"✗ Thiếu {', '.join(miss)}. {INSTALL_HINT}")
    try:
        from . import comic

        font = comic.find_font(s.font_path)
        item(True, f"Font: {font or 'không thấy (dùng font mặc định, chữ có dấu có thể lỗi)'}")
    except Exception as e:  # noqa: BLE001
        item(False, f"Font: lỗi khi tìm ({e})")
    try:
        from .db import db

        db.init(s.db_path)
        kinds = queued_without_workers()
        item(True, "Hàng đợi: không có job nào kẹt vì thiếu worker" if not kinds
             else f"Hàng đợi: job {', '.join(kinds)} đang chờ mà chưa có worker nào nhận (xem trang /workers)")
    except Exception as e:  # noqa: BLE001
        item(True, f"Hàng đợi: bỏ qua (chưa có cơ sở dữ liệu: {e})")
    return out
