"""Truyen tranh: bo cuc trang, bong thoai dang du lieu, ghep trang bang Pillow, xuat CBZ/PDF.

Bong thoai luu thanh du lieu (vi tri chuan hoa 0..1 trong khung), khong de model
ve chu. Sua loi thoai hay dich sang ngon ngu khac khong phai ve lai anh.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Moi bo cuc la danh sach o (x, y, w, h) chuan hoa tren trang.
LAYOUTS: dict[str, list[tuple[float, float, float, float]]] = {
    "1": [(0, 0, 1, 1)],
    "2": [(0, 0, 1, .5), (0, .5, 1, .5)],
    "3": [(0, 0, 1, .4), (0, .4, .5, .6), (.5, .4, .5, .6)],
    "4": [(0, 0, 1, .32), (0, .32, .5, .36), (.5, .32, .5, .36), (0, .68, 1, .32)],
    "5": [(0, 0, .55, .34), (.55, 0, .45, .34), (0, .34, 1, .32), (0, .66, .45, .34), (.45, .66, .55, .34)],
    "6": [(0, 0, .5, .33), (.5, 0, .5, .33), (0, .33, .5, .34), (.5, .33, .5, .34), (0, .67, .5, .33), (.5, .67, .5, .33)],
}
WIDE_FIRST = {"wide"}


def plan_pages(beats: list[dict], per_page: int) -> list[tuple[str, list[dict]]]:
    per_page = max(1, min(6, per_page))
    pages, cur = [], []
    for b in beats:
        cur.append(b)
        if len(cur) >= per_page:
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    return [(str(len(p)), p) for p in pages]


def panel_size(rect: tuple, page_w: int, page_h: int, area: int = 1024 * 1024) -> tuple[int, int]:
    rw, rh = rect[2] * page_w, rect[3] * page_h
    ratio = rw / max(rh, 1)
    h = (area / ratio) ** 0.5
    w = h * ratio
    r64 = lambda v: max(256, int(round(v / 64)) * 64)
    return r64(w), r64(h)


def default_balloons(beat: dict, dialogue: list[dict], char_ids: dict[str, int]) -> list[dict]:
    out = []
    lines = dialogue[:3]
    if not lines:
        text = (beat.get("narration") or "").strip()
        if text:
            first = text.split(". ")[0].strip()
            if len(first) > 140:
                first = first[:137].rsplit(" ", 1)[0] + "..."
            out.append({"kind": "caption", "text": first, "x": .03, "y": .03, "w": .6,
                        "tail_x": None, "tail_y": None, "character_id": None})
        return out
    for i, d in enumerate(lines):
        left = i % 2 == 0
        out.append({
            "kind": d.get("kind") or "speech",
            "text": d["text"],
            "x": .04 if left else .52, "y": .04 + i * .2, "w": .44,
            "tail_x": .3 if left else .7, "tail_y": min(.95, .04 + i * .2 + .32),
            "character_id": char_ids.get((d.get("character") or "").lower()),
        })
    return out


# --------------------------------------------------------------- fonts
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


def load_font(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=font) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
    return lines or [""]


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.convert("RGB")
    s = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * s + .5)), max(1, int(img.height * s + .5))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def draw_balloon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], b: dict, font_path: str | None,
                 base_size: int) -> None:
    px, py, pw, ph = box
    bx, by = px + int(b["x"] * pw), py + int(b["y"] * ph)
    bw = max(80, int(b["w"] * pw))
    font = load_font(font_path, base_size)
    pad = int(base_size * .7)
    lines = _wrap(draw, b["text"], font, bw - 2 * pad)
    lh = int(base_size * 1.25)
    bh = lh * len(lines) + 2 * pad
    kind = b.get("kind") or "speech"
    if kind == "caption":
        draw.rectangle((bx, by, bx + bw, by + bh), fill=(255, 250, 225), outline=(20, 20, 20), width=3)
    else:
        if b.get("tail_x") is not None and kind == "speech":
            tx, ty = px + int(b["tail_x"] * pw), py + int(b["tail_y"] * ph)
            cx = bx + bw // 2
            draw.polygon([(cx - bw // 10, by + bh - 6), (cx + bw // 10, by + bh - 6), (tx, ty)],
                         fill="white", outline=(20, 20, 20))
        draw.ellipse((bx - pad, by - pad // 2, bx + bw + pad, by + bh + pad // 2), fill="white",
                     outline=(20, 20, 20), width=3 if kind == "speech" else 2)
        if kind == "thought":
            for i in range(3):
                r = 6 - i * 2
                cx, cy = bx + bw // 3 - i * 14, by + bh + pad // 2 + 8 + i * 14
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white", outline=(20, 20, 20))
    y = by + pad
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        x = bx + (bw - tw) / 2 if kind != "caption" else bx + pad
        draw.text((x, y), ln, fill=(10, 10, 10), font=font)
        y += lh


def compose_page(layout: str, page_w: int, page_h: int, panels: list[dict], images: dict[int, Path | None],
                 balloons: dict[int, list[dict]], font_path: str | None, gutter: int = 24,
                 margin: int = 48) -> Image.Image:
    page = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(page)
    rects = LAYOUTS.get(layout, LAYOUTS["1"])
    iw, ih = page_w - 2 * margin, page_h - 2 * margin
    base = max(18, page_w // 52)
    for p in panels:
        r = rects[min(p["slot"], len(rects) - 1)]
        x = margin + int(r[0] * iw) + gutter // 2
        y = margin + int(r[1] * ih) + gutter // 2
        w = int(r[2] * iw) - gutter
        h = int(r[3] * ih) - gutter
        src = images.get(p["id"])
        if src and Path(src).exists():
            page.paste(_cover(Image.open(src), w, h), (x, y))
        else:
            draw.rectangle((x, y, x + w, y + h), fill=(235, 235, 240))
            draw.text((x + 20, y + 20), f"Khung {p['slot'] + 1}", fill=(120, 120, 130), font=load_font(font_path, base))
        draw.rectangle((x, y, x + w, y + h), outline=(0, 0, 0), width=5)
        for b in balloons.get(p["id"], []):
            draw_balloon(draw, (x, y, w, h), b, font_path, base)
    return page


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


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


IS_MAC = sys.platform == "darwin"
