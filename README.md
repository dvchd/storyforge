# StoryForge 0.2

Công cụ biến truyện chữ thành **video minh họa có lời đọc** và **truyện tranh** (trang in hoặc webtoon), xoay quanh một lớp quản lý **trạng thái nhân vật và bối cảnh theo chương**, **tài nguyên ảnh**, **hàng đợi AI** và **duyệt nhiều cấp**.

- **App** (FastAPI + Jinja + HTMX + Alpine + SQLite) chỉ quản lý dữ liệu, quy trình và cổng duyệt. App **không gọi trực tiếp model AI nào**.
- **Worker** là tiến trình riêng, nhận job qua HTTP, chạy model qua adapter: LLM (API kiểu OpenAI), ảnh (mflux, diffusers, lệnh bất kỳ), TTS (VieNeu, Edge TTS, lệnh bất kỳ). Đổi model chỉ cần đổi cấu hình worker.
- Chạy trên Windows, macOS, Linux. Không dùng ORM, chỉ `sqlite3` của thư viện chuẩn; schema có phiên bản và tự nâng cấp.

Có gì mới so với 0.1: xem [CHANGELOG.md](CHANGELOG.md).

---

## 1. Cài đặt

Python 3.11 trở lên. Khuyên dùng [uv](https://docs.astral.sh/uv/).

```bash
cd storyforge
uv venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[app]"                     # máy chạy app
storyforge-app vendor                          # tải htmx + Alpine về máy để dùng offline (tùy chọn)
```

Dựng video cần `ffmpeg` (`brew install ffmpeg`, `winget install ffmpeg`, `apt install ffmpeg`).

## 2. Chạy thử trong 2 phút (không cần model AI)

```bash
storyforge-app demo --mode both --level 2                  # truyện tiên hiệp 4 chương
storyforge-app demo --story modern_story --format webtoon  # truyện hiện đại, khổ webtoon
storyforge-app serve                                       # mở http://127.0.0.1:8765
# cửa sổ khác:
storyforge-worker --server http://127.0.0.1:8765 --token $(storyforge-app token) --mock
```

Trên web: mở dự án, xem khung **Việc cần làm**, bấm **Chạy tất cả chương**, rồi vào **Duyệt**. Worker giả lập tạo ảnh và audio giả để bạn thử toàn bộ luồng: xem trước video, xem trước truyện tranh, dựng MP4 + SRT, xuất CBZ/PDF.

## 3. Chạy với model thật

Xem [docs/worker.md](docs/worker.md). Tóm tắt cho Mac Apple Silicon:

```bash
uv pip install -e ".[mac]"
uv tool install --upgrade mflux
mlx_lm.server --model mlx-community/Qwen3-14B-4bit --port 8080
cp configs/worker.mac.toml worker.toml     # điền token, kiểm tra lệnh mflux bằng --help
storyforge-worker --config worker.toml
```

| Việc | Gợi ý trên Mac | Ghi chú |
|---|---|---|
| LLM | Qwen3 14B 4-bit qua mlx_lm.server | Máy 16 GB nên dùng 7–8B |
| Ảnh có tham chiếu | FLUX.2 klein 4B qua mflux | Apache 2.0, nhận nhiều ảnh tham chiếu |
| TTS | VieNeu-TTS (CPU) | Có giọng miền Bắc dựng sẵn |

---

## 4. Quy trình

```
Chương ─► Trích trạng thái (LLM) ─► [G1 danh tính, G2 trạng thái] ─► Chia nhịp (LLM) ─► [G3]
                                                                                       │
   Ảnh tham chiếu: mặt (theo tuổi, dấu vết), toàn thân (theo trang phục),               │
   bối cảnh (theo biến thể ngày/đêm/mưa và hiện trạng) ─► [G4]  ◄────────────────────────┘
                       │
   MỘT ảnh gốc cho mỗi nhịp ─► [G5] ─┬─► VIDEO: cắt 16:9 theo điểm lấy nét, chuyển động, giọng đọc [G6]
                                     │          xem trước trên trình duyệt ─► dựng MP4 + SRT [G7]
                                     └─► TRUYỆN: cắt theo khung, bố cục theo góc máy, bóng thoại dạng dữ liệu
                                                xem trước, kéo thả bóng thoại ─► PNG, CBZ, PDF [G7]
```

- Bước trạng thái xử lý **tuần tự** theo chương; các bước sau chạy song song.
- **Ảnh gốc dùng chung**: mỗi nhịp tạo đúng một ảnh, kích thước tính sao cho cắt được cả khung video và khung truyện. Dự án "cả hai" không tốn gấp đôi.

### Trạng thái theo chương

- LLM đề xuất **sự kiện**: trường, giá trị, vĩnh viễn hay tạm thời, kéo dài bao lâu, và **câu trích nguyên văn** làm bằng chứng.
- Trạng thái **không lưu cố định** mà tính từ sự kiện đã duyệt. Sửa một sự kiện ở chương 13 thì mọi chương sau tự cập nhật; ảnh tham chiếu mới được tạo trước, ảnh cảnh bị ảnh hưởng hiện nhãn **"ảnh cũ"** kèm **so sánh khác biệt prompt**.
- Nhân vật: ảnh **mặt** theo `age`, `face`, `mark` (sẹo, hình xăm: vĩnh viễn, cộng dồn); ảnh **toàn thân** theo thêm `outfit`, `hair`, `accessory`, `body`; `injury` (vết thương tạm) chỉ đưa vào prompt.
- Bối cảnh: hiện trạng lâu dài theo chương (`condition`, `decor`), cộng với biến thể từng nhịp (`night`, `rain`...). Mỗi tổ hợp có ảnh tham chiếu riêng.

### Kiểm tra tự động và gộp trùng

App tự gắn cờ: câu trích không có trong chương (so theo từng câu, không bị chương dài làm sai), tên gần giống nhân vật đã có, tên gọi khác đụng người khác, tuổi giảm, mâu thuẫn trạng thái, LLM chia nhịp sai khoảng đoạn, lời thoại tràn khung, tốc độ đọc bất thường.

Tên gần giống (Lâm An / An / L. An / Lam An) sinh **gợi ý gộp**; có nút **Nhờ LLM rà trùng** cho biệt danh và cách xưng hô. Gộp sẽ chuyển sự kiện, nhịp, lời thoại sang nhân vật giữ lại.

### Duyệt nhiều cấp

7 cổng (danh tính, trạng thái, chia nhịp, tham chiếu, ảnh cảnh, giọng đọc, xuất bản) × 4 chế độ (`auto`, `auto_if_clean`, `sample`, `manual`), gom theo mục hoặc theo chương, danh sách cờ luôn chờ người. 5 cấp có sẵn từ tự động hoàn toàn đến duyệt chặt; ghi đè theo dự án và theo chương (đặt cấp cho chương **không xóa** ghi đè cổng của dự án). Điểm kiểm tra định kỳ khi tự chạy. Xem [docs/review-policy.md](docs/review-policy.md).

---

## 5. Giao diện

| Trang | Nội dung |
|---|---|
| Dự án | **Việc cần làm** (chờ duyệt, job lỗi, thiếu worker, chương cần rà lại...), bảng chương tự làm mới với thanh tiến độ, thống kê thời gian và token, nhập truyện |
| Chương | Văn bản (liên kết tới đúng đoạn chứa bằng chứng), sự kiện, bảng trạng thái nhân vật và bối cảnh, nhịp, **Sản xuất**: ảnh gốc + xem trước khung cắt video/truyện, chọn điểm lấy nét, chuyển động, sửa prompt tay, khóa ảnh, tải ảnh của bạn, giọng đọc |
| Duyệt | Lọc theo chương và loại; **phím tắt** `j`/`k` chọn, `a` duyệt, `r` từ chối, `e` sửa, `g` tạo lại |
| Nhân vật, Bối cảnh | Dòng thời gian trạng thái, ảnh tham chiếu theo giai đoạn, gợi ý gộp trùng, gộp thủ công, hồ sơ Markdown |
| Xem trước video | Phát nối audio, chuyển động theo cài đặt, phụ đề, báo rõ đoạn còn thiếu ảnh/giọng; `Space`, `←`, `→` |
| Xem trước truyện | Trang in hoặc webtoon; kéo thả, đổi độ rộng, sửa bóng thoại; khung tràn chữ được tô viền đỏ |
| Hàng đợi | Tiến độ %, vị trí trong hàng, ước tính thời gian còn lại, token, hủy job đang chạy, chạy lại hàng loạt |
| Dung lượng | Dung lượng theo loại và dự án, dọn tài nguyên không còn dùng (có chạy thử), sao lưu DB |
| Worker | Worker online, năng lực, model, cảnh báo loại job không có worker |

Đặt `STORYFORGE_UI_PASSWORD` để bật đăng nhập Basic Auth.

## 6. Dòng lệnh

```bash
storyforge-app serve [--host 0.0.0.0 --port 8765]
storyforge-app demo [--story sample_story|modern_story|<thư mục>] [--mode video|comic|both] [--format page|webtoon] [--level 0..4]
storyforge-app token        # in worker token
storyforge-app backup       # sao lưu DB vào data/backups/
storyforge-app gc [--hours 24] [--dry-run]
storyforge-app vendor       # tải htmx, Alpine để chạy offline
storyforge-worker --config worker.toml | --mock [--once]
```

## 7. Tài liệu

- [docs/workflow.md](docs/workflow.md): quy trình chi tiết, mô hình dữ liệu, ảnh gốc dùng chung, ảnh cũ
- [docs/review-policy.md](docs/review-policy.md): cổng, chế độ, cấp, cờ cảnh báo
- [docs/protocol.md](docs/protocol.md): hợp đồng app ↔ worker, viết adapter mới
- [docs/worker.md](docs/worker.md): cấu hình worker cho Mac, NVIDIA, CPU
- [docs/mflux.md](docs/mflux.md): dùng mflux, xử lý khi tham số đổi theo phiên bản

## 8. Kiểm thử

```bash
uv pip install -e ".[app,dev]"
pytest -q          # 25 bài: đơn vị + đầu-cuối với worker giả lập
```

## 9. Lưu ý

- **Bản quyền**: chỉ chuyển thể truyện bạn có quyền.
- **Giấy phép model**: FLUX.2 klein 4B là Apache 2.0; klein 9B và một số model tiếng Việt chỉ cho phi thương mại. Edge TTS là dịch vụ không chính thức.
- Tham số mflux và API VieNeu có thể đổi theo phiên bản: kiểm tra `--help` / tài liệu thư viện, sửa `worker.toml` hoặc `adapters/real.py`.
