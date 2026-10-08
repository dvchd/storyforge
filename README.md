# StoryForge 0.3

Công cụ biến truyện chữ thành **video minh họa có lời đọc** và **truyện tranh** (trang in hoặc webtoon), xoay quanh lớp quản lý **trạng thái nhân vật và bối cảnh theo chương**, **tài nguyên ảnh**, **hàng đợi AI** và **duyệt nhiều cấp**.

- **App** (FastAPI + Jinja + HTMX + Alpine + SQLite) chỉ quản lý dữ liệu, quy trình và cổng duyệt; **không gọi trực tiếp model AI nào**.
- **Worker** là tiến trình riêng, nhận job qua HTTP, chạy model qua adapter: LLM (API kiểu OpenAI), ảnh (mflux, diffusers, lệnh bất kỳ), TTS (VieNeu, Edge TTS, lệnh bất kỳ).
- Chạy trên Windows, macOS, Linux. Schema có phiên bản, tự nâng cấp và sao lưu.

Có gì mới: [CHANGELOG.md](CHANGELOG.md).

## 1. Cài đặt

```bash
cd storyforge
uv venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[app]"
storyforge-app doctor                          # kiểm tra ffmpeg, ffprobe, font
storyforge-app vendor                          # (tùy chọn) tải htmx + Alpine để dùng offline
```

Dựng video cần **ffmpeg** (kèm ffprobe): `brew install ffmpeg` · `winget install Gyan.FFmpeg` · `sudo apt install ffmpeg`. Thiếu ffmpeg thì giao diện, duyệt và truyện tranh vẫn chạy; app báo rõ ở terminal, trên giao diện và trong lỗi job.

## 2. Chạy thử (không cần model AI)

```bash
storyforge-app demo --mode both --level 2                  # truyện tiên hiệp 4 chương
storyforge-app demo --story modern_story --format webtoon  # truyện hiện đại, khổ webtoon
storyforge-app serve                                       # http://127.0.0.1:8765
storyforge-worker --server http://127.0.0.1:8765 --token $(storyforge-app token) --mock
```

## 3. Model thật

Xem [docs/worker.md](docs/worker.md) và [docs/mflux.md](docs/mflux.md). Mac Apple Silicon: `configs/worker.mac.toml` (mlx_lm.server + FLUX.2 klein 4B qua mflux + VieNeu).

## 4. Điểm chính

- **Trạng thái theo chương** tính từ sự kiện đã duyệt; sửa ở chương k thì mọi chương sau tự cập nhật, ảnh bị ảnh hưởng hiện "ảnh cũ" kèm so sánh prompt.
- **Dấu vết vĩnh viễn** (sẹo, hình xăm) đổi ảnh tham chiếu mặt; mỗi dấu vết là một chip, xóa riêng được.
- **Bối cảnh** có hiện trạng theo chương và biến thể theo nhịp (ngày/đêm/mưa).
- **Một ảnh gốc cho mỗi nhịp**, cắt ra khung video và khung truyện theo điểm lấy nét (mặc định theo góc máy); cảnh báo khi hai khung lệch nhau.
- **Kiểm tra tự động**: câu trích không có trong chương, tên gần giống (tên một chữ không tự gộp), tuổi giảm, mô tả cho model ảnh không phải tiếng Anh, chia nhịp sai khoảng đoạn, lời thoại tràn khung, tốc độ đọc bất thường.
- **Duyệt nhiều cấp**: 7 cổng × 4 chế độ, 5 cấp có sẵn, ghi đè theo dự án và chương, điểm kiểm tra định kỳ, nhật ký máy tự duyệt. Xem [docs/review-policy.md](docs/review-policy.md).
- **Hàng đợi đáng tin**: chống bỏ đói, tiến độ đơn điệu, watchdog thu hồi job đứng yên, hủy job đang chạy, ETA, token.
- **Cài đặt có kiểm tra chéo**, gợi ý diện tích ảnh gốc, đặt lại mặc định theo nhóm.

## 5. Dòng lệnh

```bash
storyforge-app serve [--host 0.0.0.0 --port 8765]
storyforge-app demo [--story ...] [--mode video|comic|both] [--format page|webtoon] [--level 0..4]
storyforge-app doctor | token | backup | vendor
storyforge-app gc [--hours 24] [--dry-run]
storyforge-worker --config worker.toml | --mock [--once]
```

## 6. Tài liệu

[docs/workflow.md](docs/workflow.md) · [docs/review-policy.md](docs/review-policy.md) · [docs/protocol.md](docs/protocol.md) · [docs/worker.md](docs/worker.md) · [docs/mflux.md](docs/mflux.md)

## 7. Kiểm thử

```bash
uv pip install -e ".[app,dev]"
pytest -q          # 37 bài; bài cần ffmpeg tự bỏ qua nếu máy chưa cài
```

## 8. Lưu ý

- Chỉ chuyển thể truyện bạn có quyền. FLUX.2 klein 4B là Apache 2.0; klein 9B và một số model tiếng Việt chỉ cho phi thương mại; Edge TTS là dịch vụ không chính thức.
- Tham số mflux và API VieNeu có thể đổi theo phiên bản: kiểm tra `--help`, sửa `worker.toml` hoặc `adapters/real.py`.
