"""Giao diện chung cho mọi adapter: run(job, ctx) -> AdapterResult.

Trong run(): gọi ctx.report(0..1, "thông điệp") để báo tiến độ (app chỉ ghi nhận khi tăng;
job ảnh/giọng không tiến triển quá ngưỡng sẽ bị thu hồi), kiểm tra ctx.check_cancel() để dừng khi bị hủy.
"""
from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from storyforge.protocol import ClaimedJob


class RetryableError(RuntimeError):
    """Lỗi tạm thời (mất mạng, hết RAM): app đưa job lại hàng đợi."""


class Cancelled(RuntimeError):
    """Người dùng đã hủy job."""


@dataclass
class AdapterResult:
    output: dict[str, Any]
    files: list[Path] = field(default_factory=list)
    model_id: str = ""


@dataclass
class JobContext:
    workdir: Path
    fetch_asset: Callable[[int, str], Path]
    is_cancelled: Callable[[], bool] = lambda: False
    report: Callable[[float, str], None] = lambda frac, msg="": None

    def check_cancel(self) -> None:
        if self.is_cancelled():
            raise Cancelled("Job đã bị hủy")


class Adapter:
    kind: str = ""
    type_name: str = ""
    heavy: bool = False

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = str(cfg.get("model", "") or self.type_name)
        self.aliases = [self.model, *[str(a) for a in cfg.get("aliases", [])]]
        self._loaded = False

    def model_ids(self) -> list[str]:
        return [a for a in self.aliases if a]

    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:  # pragma: no cover
        raise NotImplementedError


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, OSError):
        return None


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float | None:
    d = wav_duration(path) if path.suffix.lower() == ".wav" else None
    if d:
        return d
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None
