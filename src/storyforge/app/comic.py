"""Truyện tranh: bố cục động theo góc máy và mật độ thoại, bóng thoại dạng dữ liệu, ghép trang, CBZ/PDF.

- Khổ "page": wide hoặc nhiều thoại -> khung rộng cả hàng; medium -> 2 khung một hàng; close ít thoại -> 3 khung.
- Khổ "webtoon": dải dọc, mỗi nhịp một khung rộng toàn chiều ngang, chiều cao theo góc máy.
- Bóng thoại lưu thành dữ liệu (tọa độ 0..1 trong khung), không để model vẽ chữ.
"""
from __future__ import annotations

import math
import os
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .checks import norm

ROW_KIND_COLS = {"full": 1, "half": 2, "small": 3}
ROW_KIND_HEIGHT = {"full": 0.52, "half": 0.42, "small": 0.34}
WEBTOON_RATIO = {"wide": 0.56, "medium": 0.75, "close": 0.95}


def classify(beat: dict, dialogue: list[dict]) -> str:
    n = len(dialogue)
    if beat.get("shot") == "wide" or n >= 3:
        return "full"
    if beat.get("shot") == "close" and n <= 1:
        return "small"
    return "half"


def plan_rows(beats: list[tuple[dict, list[dict]]]) -> list[tuple[str, list[dict]]]:
    rows: list[tuple[str, list[dict]]] = []
    for beat, dlg in beats:
        kind = classify(beat, dlg)
        if rows and rows[-1][0] == kind and len(rows[-1][1]) < ROW_KIND_COLS[kind]:
            rows[-1][1].append(beat)
        else:
            rows.append((kind, [beat]))
    return rows


def plan_pages(beats: list[tuple[dict, list[dict]]], page_w: int, page_h: int, margin: int, max_panels: int) -> list[dict]:
    inner_w, inner_h = page_w - 2 * margin, page_h - 2 * margin
    pages, cur, used = [], [], 0.0
    for kind, members in plan_rows(beats):
        hpx = ROW_KIND_HEIGHT[kind] * inner_w
        n_cur = sum(len(m) for _, m, _ in cur)
        if cur and (used + hpx > inner_h * 1.02 or n_cur + len(members) > max_panels):
            pages.append(cur)
            cur, used = [], 0.0
        cur.append((kind, members, hpx))
        used += hpx
    if cur:
        pages.append(cur)
    out = []
    for i, rows_ in enumerate(pages):
        total = sum(h for _, _, h in rows_)
        last = i == len(pages) - 1
        scale = inner_h / total if not (last and total < inner_h * 0.7) else 1.0
        y, panels = 0.0, []
        for kind, members, hpx in rows_:
            h = hpx * scale / inner_h
            w = 1.0 / len(members)
            for k, b in enumerate(members):
                panels.append({"beat": b, "x": k * w, "y": y, "w": w, "h": h})
            y += h
        out.append({"width": page_w, "height": page_h, "panels": panels})
    return out


def plan_webtoon(beats: list[tuple[dict, list[dict]]], width: int, margin: int, gutter: int, per_page: int) -> list[dict]:
    out = []
    for s in range(0, len(beats), max(1, per_page)):
        chunk = beats[s:s + per_page]
        inner_w = width - 2 * margin
        heights = [inner_w * (WEBTOON_RATIO.get(b.get("shot"), 0.75) + (0.15 if len(d) >= 3 else 0)) for b, d in chunk]
        inner_h = sum(heights) + gutter * (len(chunk) - 1)
        y, panels = 0.0, []
        for (b, _), h in zip(chunk, heights):
            panels.append({"beat": b, "x": 0.0, "y": y / inner_h, "w": 1.0, "h": (h + gutter) / inner_h})
            y += h + gutter
        out.append({"width": width, "height": int(inner_h + 2 * margin), "panels": panels})
    return out


def panel_px(p: dict, page_w: int, page_h: int, margin: int, gutter: int) -> tuple[int, int, int, int]:
    iw, ih = page_w - 2 * margin, page_h - 2 * margin
    x = margin + int(p["x"] * iw) + gutter // 2
    y = margin + int(p["y"] * ih) + gutter // 2
    return x, y, max(8, int(p["w"] * iw) - gutter), max(8, int(p["h"] * ih) - gutter)


# --------------------------------------------------------------- cắt khung từ ảnh gốc
def crop_box(src_w: float, src_h: float, dst_aspect: float, fx: float, fy: float) -> tuple[float, float, float, float]:
    """Vùng (x, y, w, h) chuẩn hóa 0..1 trên ảnh gốc khi cắt kiểu cover ra tỉ lệ dst_aspect tại điểm lấy nét."""
    sa = src_w / max(1e-6, src_h)
    if dst_aspect >= sa:
        w, h = 1.0, sa / dst_aspect
    else:
        w, h = dst_aspect / sa, 1.0
    return (1 - w) * fx, (1 - h) * fy, w, h


def overlap(a: tuple, b: tuple) -> float:
    """Phần giao / diện tích vùng nhỏ hơn: 1 = vùng nhỏ nằm trọn trong vùng lớn."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return (ix * iy) / max(1e-9, min(a[2] * a[3], b[2] * b[3]))


# --------------------------------------------------------------- font
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-VF.ttf.ttc",
]


def find_font(preferred: str = "") -> str | None:
    for p in [preferred, *FONT_CANDIDATES]:
        if p and os.path.exists(p):
            return p
    return None


_FONT_CACHE: dict[tuple, object] = {}


def load_font(path: str | None, size: int):
    key = (path, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    if path:
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            f = None
    if f is None:
        try:
            f = ImageFont.load_default(size)
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


_MEASURE = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def wrap(text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            t = (cur + " " + w).strip()
            if _MEASURE.textlength(t, font=font) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
    return lines or [""]


def balloon_box(b: dict, pw: int, ph: int, font_path: str | None, size: int) -> tuple[int, int, int, list[str], int]:
    bw = max(60, int(b["w"] * pw))
    font = load_font(font_path, size)
    pad = int(size * .7)
    lines = wrap(b["text"], font, bw - 2 * pad)
    lh = int(size * 1.25)
    return bw, lh * len(lines) + 2 * pad, pad, lines, lh


def fit_size(b: dict, pw: int, ph: int, font_path: str | None, base: int) -> tuple[int, bool]:
    size = base
    while True:
        _, bh, _, _, _ = balloon_box(b, pw, ph, font_path, size)
        if b["y"] * ph + bh <= ph * 0.96:
            return size, False
        if size <= int(base * 0.7):
            return size, True
        size = max(int(base * 0.7), size - 2)


def default_balloons(beat: dict, dialogue: list[dict], char_ids: dict[str, int], pw: int, ph: int,
                     font_path: str | None, base: int) -> list[dict]:
    out: list[dict] = []
    lines = dialogue[:3]
    if not lines:
        text = (beat.get("narration") or "").strip()
        if text:
            first = text.split(". ")[0].strip()
            if len(first) > 140:
                first = first[:137].rsplit(" ", 1)[0] + "..."
            lines = [{"text": first, "kind": "caption", "character": ""}]
    y = 0.03
    for i, d in enumerate(lines):
        kind = d.get("kind") or "speech"
        w = min(0.62, max(0.3, 0.22 + len(d["text"]) * 0.004 * (1000 / max(pw, 200))))
        left = i % 2 == 0
        x = 0.03 if left else max(0.03, 0.97 - w)
        b = {"kind": kind, "text": d["text"], "x": x, "y": y, "w": w, "tail_x": None, "tail_y": None,
             "character_id": char_ids.get(norm(d.get("character") or ""))}
        _, bh, _, _, _ = balloon_box(b, pw, ph, font_path, base)
        if kind == "speech":
            b["tail_x"] = x + w * (0.35 if left else 0.65)
            b["tail_y"] = min(0.92, y + bh / ph + 0.12)
        out.append(b)
        y += bh / ph + 0.025
    return out


def overflow(balloons: list[dict], pw: int, ph: int, font_path: str | None, base: int) -> bool:
    return any(fit_size(b, pw, ph, font_path, base)[1] for b in balloons)


def _cover(img: Image.Image, w: int, h: int, fx: float = .5, fy: float = .5) -> Image.Image:
    img = img.convert("RGB")
    s = max(w / img.width, h / img.height)
    img = img.resize((max(1, math.ceil(img.width * s)), max(1, math.ceil(img.height * s))), Image.LANCZOS)
    x, y = int((img.width - w) * fx), int((img.height - h) * fy)
    return img.crop((x, y, x + w, y + h))


def draw_balloon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], b: dict, font_path: str | None,
                 base: int) -> bool:
    px, py, pw, ph = box
    size, over = fit_size(b, pw, ph, font_path, base)
    bw, bh, pad, lines, lh = balloon_box(b, pw, ph, font_path, size)
    font = load_font(font_path, size)
    bx, by = px + int(b["x"] * pw), py + int(b["y"] * ph)
    kind = b.get("kind") or "speech"
    ink = (20, 20, 20)
    if kind == "caption":
        draw.rectangle((bx, by, bx + bw, by + bh), fill=(255, 250, 225), outline=ink, width=3)
    else:
        if b.get("tail_x") is not None and kind == "speech":
            tx, ty = px + int(b["tail_x"] * pw), py + int(b["tail_y"] * ph)
            cx = bx + bw // 2
            draw.polygon([(cx - bw // 10, by + bh - 6), (cx + bw // 10, by + bh - 6), (tx, ty)], fill="white", outline=ink)
        draw.ellipse((bx - pad, by - pad // 2, bx + bw + pad, by + bh + pad // 2), fill="white", outline=ink,
                     width=3 if kind == "speech" else 2)
        if kind == "thought":
            for i in range(3):
                r = 7 - i * 2
                cx, cy = bx + bw // 3 - i * 14, by + bh + pad // 2 + 10 + i * 14
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white", outline=ink)
    y = by + pad
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        draw.text((bx + (bw - tw) / 2 if kind != "caption" else bx + pad, y), ln, fill=(10, 10, 10), font=font)
        y += lh
    return over


def compose_page(page_w: int, page_h: int, panels: list[dict], images: dict[int, Path | None],
                 balloons: dict[int, list[dict]], font_path: str | None, margin: int, gutter: int,
                 base_font: int) -> tuple[Image.Image, set[int]]:
    page = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(page)
    over: set[int] = set()
    for p in panels:
        x, y, w, h = panel_px(p, page_w, page_h, margin, gutter)
        src = images.get(p["id"])
        if src and Path(src).exists():
            page.paste(_cover(Image.open(src), w, h, p.get("focus_x", .5), p.get("focus_y", .5)), (x, y))
        else:
            draw.rectangle((x, y, x + w, y + h), fill=(235, 235, 240))
            draw.text((x + 20, y + 20), f"Khung {p['slot'] + 1}", fill=(120, 120, 130), font=load_font(font_path, base_font))
        draw.rectangle((x, y, x + w, y + h), outline=(0, 0, 0), width=5)
        for b in balloons.get(p["id"], []):
            if draw_balloon(draw, (x, y, w, h), b, font_path, base_font):
                over.add(p["id"])
    return page, over


def export_cbz(pages: list[Path], out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for i, p in enumerate(pages, 1):
            z.write(p, f"{i:03d}.png")
    return out


def export_pdf(pages: list[Path], out: Path) -> Path:
    imgs = [Image.open(p).convert("RGB") for p in pages]
    if imgs:
        imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=150)
    return out
