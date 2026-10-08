"""Kiểm tra chéo cài đặt dự án: tránh ảnh gốc bị phóng to khi cắt hoặc phí pixel, và các giá trị vô lý."""
from __future__ import annotations

import math

from . import comic


def _even(v: int) -> int:
    return v if v % 2 == 0 else v + 1


def fix(s: dict) -> dict:
    """Sửa tự động các giá trị chắc chắn sai (lưu ngay khi bấm Lưu)."""
    for k in ("video_width", "video_height"):
        if k in s and isinstance(s[k], int):
            s[k] = max(64, _even(s[k]))      # bộ mã hóa H.264 cần kích thước chẵn
    if "video_fps" in s and isinstance(s["video_fps"], int):
        s["video_fps"] = min(60, max(1, s["video_fps"]))
    if "comic_max_panels" in s and isinstance(s["comic_max_panels"], int):
        s["comic_max_panels"] = min(9, max(1, s["comic_max_panels"]))
    if "beats_span_retries" in s and isinstance(s["beats_span_retries"], int):
        s["beats_span_retries"] = min(2, max(0, s["beats_span_retries"]))
    return s


def largest_panel(s: dict) -> tuple[float, float]:
    """Kích thước (rộng, cao) pixel của khung truyện lớn nhất có thể có."""
    if s["comic_format"] == "webtoon":
        inner = s["webtoon_width"] - s["comic_margin"]
        return inner, inner * (max(comic.WEBTOON_RATIO.values()) + 0.15)
    inner_w = s["comic_page_width"] - 2 * s["comic_margin"]
    inner_h = s["comic_page_height"] - 2 * s["comic_margin"]
    return inner_w - s["comic_gutter"], min(inner_h, comic.ROW_KIND_HEIGHT["full"] * inner_w * 1.6)


def _crop_fraction(master_aspect: float, target_aspect: float) -> float:
    return min(master_aspect / target_aspect, target_aspect / master_aspect)


def required_image_area(project: dict, s: dict) -> dict:
    """Diện tích ảnh gốc tối thiểu để vùng cắt ra mỗi khung không phải phóng to."""
    va = s["video_width"] / s["video_height"]
    want_v, want_c = project["mode"] in ("video", "both"), project["mode"] in ("comic", "both")
    pw, ph = largest_panel(s)
    pa = pw / ph
    if want_v and want_c:
        ma = math.sqrt(va * pa)
    elif want_c:
        ma = pa
    else:
        ma = va
    ma = max(0.55, min(1.95, ma))
    need = {}
    if want_v:
        need["video"] = s["video_width"] * s["video_height"] / _crop_fraction(ma, va)
    if want_c:
        need["comic"] = pw * ph / _crop_fraction(ma, pa)
    need["required"] = max(need.values()) if need else 0
    need["recommended"] = int(math.ceil(need["required"] / 65536) * 65536)
    return need


def warnings(project: dict, s: dict) -> list[dict]:
    out: list[dict] = []

    def w(level: str, key: str, text: str) -> None:
        out.append({"level": level, "key": key, "text": text})

    need = required_image_area(project, s)
    area = int(s["image_area"])
    if area < need["required"] * 0.9:
        which = " và ".join(k for k in ("video", "comic") if need.get(k, 0) > area * 1.1) or "khung"
        w("warn", "image_area", f"Ảnh gốc {area:,} px nhỏ hơn mức cần ({int(need['required']):,} px): khung {which} sẽ bị "
                                f"phóng to, ảnh mờ. Gợi ý: {need['recommended']:,} px.")
    elif area > need["required"] * 3:
        w("info", "image_area", f"Ảnh gốc {area:,} px lớn gấp {area / need['required']:.1f} lần mức cần: tốn thời gian "
                                f"tạo ảnh mà không rõ hơn. Gợi ý: {need['recommended']:,} px.")
    if area > 4_200_000:
        w("info", "image_area", "Nhiều model (FLUX, Z-Image) tạo đẹp nhất quanh 1–2 MP; trên 4 MP thường chậm và dễ lỗi bố cục.")
    if s["video_width"] % 2 or s["video_height"] % 2:
        w("error", "video_width", "Kích thước video phải là số chẵn.")
    if int(s["ref_size"]) < 512:
        w("warn", "ref_size", "Ảnh tham chiếu dưới 512 px thường làm mất chi tiết khuôn mặt.")
    if int(s["max_cast_refs"]) * 2 > int(s["max_refs"]) + 1:
        w("info", "max_refs", "Số ảnh tham chiếu tối đa nhỏ hơn số nhân vật × 2: một số nhân vật sẽ chỉ có ảnh toàn thân.")
    if float(s["dedupe_threshold"]) < 0.85 and s.get("dedupe_mode", "fuzzy") == "fuzzy":
        w("warn", "dedupe_threshold", "Ngưỡng gộp trùng dưới 0.85 sẽ sinh nhiều gợi ý sai với truyện nhiều tên giống nhau.")
    if not 0.5 <= float(s["tts_rate"]) <= 2.0:
        w("warn", "tts_rate", "Tốc độ đọc nên trong khoảng 0.5 đến 2.0.")
    if float(s["video_transition"]) * 2 > 3:
        w("info", "video_transition", "Thời gian mờ chuyển cảnh dài: đoạn ngắn sẽ gần như chỉ thấy chuyển cảnh.")
    if s["comic_format"] == "page" and s["comic_page_height"] < s["comic_page_width"]:
        w("info", "comic_page_height", "Trang truyện đang nằm ngang (rộng hơn cao).")
    return out
