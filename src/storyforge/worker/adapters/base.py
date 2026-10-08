"""Giao dien chung cho moi adapter.

Moi adapter chi can mot ham run(job, ctx) -> AdapterResult. Them model moi
la viet them mot class, app va prompt khong phai sua.
"""
from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from storyforge.protocol import ClaimedJob


class RetryableError(RuntimeError):
    """Loi tam thoi (mat mang, het RAM): app se dua job lai hang doi."""


@dataclass
class AdapterResult:
    output: dict[str, Any]
    files: list[Path] = field(default_factory=list)
    model_id: str = ""


@dataclass
class JobContext:
    workdir: Path
    fetch_asset: Callable[[int, str], Path]   # (asset_id, sha256) -> duong dan file da tai
    is_cancelled: Callable[[], bool] = lambda: False


class Adapter:
    kind: str = ""
    type_name: str = ""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = str(cfg.get("model", "") or self.type_name)
        # cac ten model ma adapter nhan (de khop model_hint cua job)
        self.aliases = [self.model, *[str(a) for a in cfg.get("aliases", [])]]
        self._loaded = False

    # --------------------------------------------------------- lifecycle
    def model_ids(self) -> list[str]:
        return [a for a in self.aliases if a]

    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def matches(self, hint: str) -> bool:
        return not hint or hint in self.model_ids()

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:  # pragma: no cover
        raise NotImplementedError


# ------------------------------------------------------------- helpers
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
        import soundfile  # type: ignore

        info = soundfile.info(str(path))
        return float(info.duration)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None
