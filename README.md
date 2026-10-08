# StoryForge

Công cụ biến truyện chữ thành **video minh họa có lời đọc** và **truyện tranh**, với lớp quản lý trạng thái nhân vật theo chương, tài nguyên ảnh, hàng đợi AI và cơ chế duyệt nhiều cấp.

- **App** (FastAPI + Jinja + HTMX + Alpine + SQLite) chỉ quản lý dữ liệu, quy trình và cổng duyệt. App **không gọi trực tiếp model AI nào**.
- **Worker** là tiến trình riêng, nhận job qua HTTP và chạy model qua adapter: LLM (OpenAI-compatible), tạo ảnh (mflux, diffusers, lệnh bất kỳ), TTS (VieNeu, Edge TTS, lệnh bất kỳ). Đổi model chỉ cần đổi cấu hình worker.
- Chạy được trên Windows, macOS, Linux. App không cần ORM, chỉ dùng `sqlite3` của thư viện chuẩn.

---

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên. Khuyên dùng [uv](https://docs.astral.sh/uv/).

```bash
cd storyforge
uv venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[app]"                     # máy chạy app
uv pip install -e ".[mac]"                     # thêm adapter cho Mac (VieNeu, Edge TTS)
uv tool install --upgrade mflux                # tạo ảnh bằng MLX trên Mac
```

Dựng video cần `ffmpeg` (`brew install ffmpeg`, `winget install ffmpeg`, `apt install ffmpeg`).

## 2. Chạy thử trong 2 phút (không cần model AI)

```bash
storyforge-app demo --mode both --level 2      # tạo dự án mẫu 3 chương
storyforge-app serve                           # mở http://127.0.0.1:8765
# cửa sổ khác:
storyforge-worker --server http://127.0.0.1:8765 --token $(storyforge-app token) --mock
```

Trên web: mở dự án, bấm **Chạy tất cả chương**, rồi vào **Duyệt** để duyệt nhân vật, ảnh tham chiếu. Worker giả lập tạo ảnh và audio giả để bạn thử toàn bộ luồng, xem trước video và truyện tranh, dựng MP4, xuất CBZ/PDF.

## 3. Chạy với model thật trên Mac (ví dụ Mac Mini M6)

1. Chạy server LLM tương thích OpenAI, ví dụ:
   ```bash
   mlx_lm.server --model mlx-community/Qwen3-14B-4bit --port 8080
   ```
   Hoặc Ollama (`http://127.0.0.1:11434/v1`), LM Studio (`http://127.0.0.1:1234/v1`).
2. Sao chép `configs/worker.mac.toml`, điền `token`, kiểm tra lệnh mflux bằng `--help`.
3. Chạy worker: `storyforge-worker --config worker.mac.toml`
4. Trong **Cài đặt dự án**, đặt "Model LLM", "Model ảnh", "Giọng đọc" trùng với `model` hoặc `aliases` trong worker để worker ưu tiên đúng model.

Gợi ý mặc định trên Mac:

| Việc | Lựa chọn | Ghi chú |
|---|---|---|
| LLM | Qwen3 14B 4-bit qua mlx_lm.server | Máy 16 GB nên dùng model 7 đến 8B |
| Ảnh có tham chiếu | FLUX.2 klein 4B qua `mflux-generate-flux2-edit` | Apache 2.0, nhận nhiều ảnh tham chiếu |
| Ảnh không tham chiếu | Z-Image Turbo qua mflux | Đẹp, nhanh |
| TTS | VieNeu-TTS (CPU) | Có giọng miền Bắc dựng sẵn |

Worker để `memory_mode = "sequential"`: chỉ giữ một model nặng trong RAM. Hàng đợi ưu tiên job dùng model đang tải sẵn, nên worker làm hết loạt job cùng model rồi mới đổi.

Có thể chạy nhiều worker trên nhiều máy (Mac, máy NVIDIA, máy thuê theo giờ). Mỗi worker khai báo năng lực riêng, cùng trỏ về một app.

---

## 4. Quy trình

```
Chương ─► Trích trạng thái (LLM) ─► [G1 danh tính, G2 trạng thái] ─► Chia nhịp (LLM) ─► [G3]
                                                                                       │
                       ┌───────────────────────────────────────────────────────────────┘
                       ▼
   Ảnh tham chiếu: mặt, toàn thân theo trạng thái, bối cảnh ─► [G4]
                       │
          ┌────────────┴─────────────┐
          ▼                          ▼
   VIDEO: ảnh cảnh 16:9 [G5]    TRUYỆN TRANH: ảnh khung theo tỉ lệ ô [G5]
          giọng đọc [G6]             bóng thoại dạng dữ liệu
          xem trước trên trình duyệt xem trước, kéo thả bóng thoại
          dựng MP4 + SRT [G7]        ghép trang PNG, CBZ, PDF [G7]
```

- Chương xử lý **tuần tự** cho bước trạng thái: chương k chỉ trích xuất khi chương k-1 đã chốt trạng thái. Các bước sau (chia nhịp, ảnh, giọng) chạy song song giữa các chương.
- Hai luồng **dùng chung** nhân vật, trạng thái, bối cảnh, ảnh tham chiếu và nhịp (beat). Chỉ tách ra ở bước dựng.

### Trạng thái nhân vật theo chương

- LLM đề xuất **sự kiện** (`state_event`): chương, trường (`outfit`, `hair`, `injury`, `age`, `body`, `face`, `accessory`, `other`), giá trị, vĩnh viễn hay tạm thời, kéo dài bao nhiêu chương, và **câu trích nguyên văn làm bằng chứng**.
- Trạng thái **không lưu cố định** mà tính từ các sự kiện đã duyệt (`state.resolve`). Sửa một sự kiện ở chương 13 thì mọi chương sau tự cập nhật, các chương sau được đánh dấu "cần kiểm tra lại".
- Ảnh tham chiếu mặt phụ thuộc `age`, `face`. Ảnh toàn thân phụ thuộc thêm `outfit`, `hair`, `accessory`, `body`. Thương tích tạm thời chỉ đưa vào prompt, không cần ảnh tham chiếu mới.
- Mỗi ảnh mang **hash đầu vào** (prompt, hash các ảnh tham chiếu, seed, model, kích thước). Đổi trạng thái hay ảnh tham chiếu thì ảnh cũ hiện nhãn "ảnh cũ", bấm một nút để tạo lại những ảnh đó.

### Cổng duyệt và cấp độ

| Cổng | Duyệt cái gì |
|---|---|
| G1 identity | Nhân vật, bối cảnh mới |
| G2 state | Sự kiện trạng thái |
| G3 breakdown | Chia nhịp |
| G4 reference | Ảnh tham chiếu |
| G5 scene | Ảnh cảnh / khung |
| G6 audio | Giọng đọc |
| G7 publish | Dựng và xuất bản |

Mỗi cổng có chế độ `auto`, `auto_if_clean`, `sample` (rút mẫu theo tỉ lệ), `manual`; gom duyệt theo `item` hoặc `chapter`; và danh sách cờ **luôn phải qua người** (`always_review`).

| Cấp | Ý nghĩa |
|---|---|
| 0 | Tự động hoàn toàn, chỉ dừng ở điểm kiểm tra định kỳ |
| 1 | Tự động, chỉ dừng khi có cảnh báo |
| 2 | **Khuyên dùng**: bạn duyệt danh tính, ảnh tham chiếu, thay đổi vĩnh viễn |
| 3 | Duyệt trạng thái và nhịp theo cả chương |
| 4 | Mọi bước đều chờ người |

Thứ tự áp dụng: mặc định theo cấp, rồi ghi đè theo dự án, rồi ghi đè theo chương. **Điểm kiểm tra**: khi tự chạy, cứ N chương dự án tự tạm dừng để bạn xem tổng quan.

Cờ cảnh báo app tự kiểm tra: câu trích không có trong chương, tên không khớp, mâu thuẫn trạng thái, thay đổi vĩnh viễn, khoảng đoạn bị chỉnh, quá nhiều nhân vật, audio bất thường...

Duyệt tay và duyệt tự động đi **cùng một luồng code**: engine chỉ tạo bước tiếp theo sau khi mục được duyệt. Mọi lần duyệt ghi vào **Nhật ký**, có bộ lọc "máy tự duyệt" để rà lại.

### Từ chối và tạo lại

Từ chối ảnh hay giọng thì app tự tạo lại với seed mới (tắt được, giới hạn số lần). Ảnh tham chiếu có thể sửa prompt, **tải ảnh của bạn lên** thay thế, và khóa lại.

---

## 5. Giao diện

| Trang | Nội dung |
|---|---|
| Dự án | Danh sách chương, tiến độ, nhập chương (dán văn bản hoặc nhiều file .txt, tự tách theo "Chương N"), tự chạy, tạm dừng |
| Chương | Văn bản, sự kiện trạng thái, bảng trạng thái tại chương, nhịp (sửa được), ảnh cảnh và khung, job |
| Duyệt | Mọi mục chờ duyệt, lọc theo chương, duyệt từng mục hoặc hàng loạt |
| Nhân vật | Sửa ngoại hình, tên gọi khác, gộp nhân vật trùng, dòng thời gian trạng thái, ảnh tham chiếu |
| Bối cảnh, Tham chiếu | Quản lý ảnh gốc và biến thể |
| Xem trước video | Trình duyệt tự phát nối audio, đổi ảnh, hiệu ứng zoom, phụ đề. Chưa cần dựng video |
| Xem trước truyện tranh | Trang theo bố cục, kéo thả bóng thoại, nhấp đúp sửa chữ |
| Hàng đợi | Job đang chạy, lỗi, chạy lại, hủy, xem payload và kết quả |
| Cài đặt | Model gợi ý, kích thước, giọng, chính sách duyệt từng cổng |
| Worker | Worker đang kết nối, token |

Đặt `STORYFORGE_UI_PASSWORD` để bật đăng nhập Basic Auth cho giao diện (API worker dùng token riêng).

---

## 6. Hợp đồng app và worker

Gói `storyforge.protocol` là hợp đồng duy nhất. Loại job:

| Kind | Đầu vào | Đầu ra |
|---|---|---|
| `llm.chat` | messages, json_schema, temperature | `text` |
| `image.generate` | prompt, refs (asset_id + vai trò), width, height, seed, steps | file ảnh |
| `image.remove_bg` | ảnh | ảnh PNG nền trong suốt |
| `tts.synthesize` | text, voice, rate | file audio, duration, mốc câu |

API (header `X-Worker-Token`, `X-Worker-Id`):

```
POST /api/worker/claim                 {worker_id, capabilities, loaded_models} -> job | 204
POST /api/worker/jobs/{id}/heartbeat   -> {cancel}
GET  /api/worker/assets/{id}           tải ảnh tham chiếu
POST /api/worker/jobs/{id}/artifact?name=x.png   (body là nội dung file)
POST /api/worker/jobs/{id}/complete    {output, model_id, elapsed}
POST /api/worker/jobs/{id}/fail        {error, retryable}
```

- Nhận job bằng một câu `UPDATE ... RETURNING` nên an toàn khi nhiều worker. Job hết hạn thuê mà không có heartbeat tự quay lại hàng đợi.
- App tự dựng prompt và **tự kiểm tra kết quả** LLM bằng Pydantic. Sai schema thì app đưa lỗi vào hội thoại và cho chạy lại.
- Job `local.render_video` và `local.compose_comic` (ffmpeg, Pillow) chạy ngay trong tiến trình app, không cần AI.

### Viết adapter mới

```python
# src/storyforge/worker/adapters/my_adapter.py
from .base import Adapter, AdapterResult, JobContext
from storyforge.protocol import ImageGeneratePayload

class MyImage(Adapter):
    kind = "image.generate"
    type_name = "my"
    def run(self, job, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        refs = [ctx.fetch_asset(r.asset_id, r.sha256) for r in p.refs]
        out = ctx.workdir / f"img_{job.id}.png"
        ...  # gọi model, lưu ra out
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id=self.model)
```

Đăng ký trong `adapters/__init__.py` (`REGISTRY`), rồi khai báo `type = "my"` trong worker.toml. Không cần sửa app.

Nhiều công cụ dùng được ngay bằng adapter `command` (mẫu lệnh có placeholder `{prompt}`, `{width}`, `{seed}`, `{output}`, `{refs}`, `{text_file}`...).

---

## 7. Cấu trúc thư mục

```
src/storyforge/
  protocol/          hợp đồng app <-> worker
  app/
    db.py            schema SQLite, helper
    engine.py        điều phối quy trình, xử lý kết quả job
    policy.py        cổng duyệt, cấp độ, decide()
    review.py        hành động duyệt
    state.py         tính trạng thái nhân vật theo chương
    checks.py        cờ cảnh báo
    llm.py           schema đầu ra và prompt LLM
    prompts.py       ghép prompt ảnh, hash đầu vào
    comic.py         bố cục trang, bóng thoại, ghép trang, CBZ/PDF
    video.py         dựng video bằng ffmpeg, SRT
    routes_web.py    giao diện
    routes_worker.py API cho worker
    templates/, static/
  worker/
    runner.py        vòng lặp worker
    client.py        HTTP client
    adapters/        mock, openai, command (mflux...), diffusers, vieneu, edge_tts, rembg
configs/             worker.mock.toml, worker.mac.toml, worker.cuda.toml
examples/sample_story/
tests/test_pipeline.py
```

## 8. Kiểm thử

```bash
uv pip install -e ".[app,dev]"
pytest -q
```

Bộ kiểm thử chạy app và worker giả lập trong cùng tiến trình: tự động hoàn toàn cả hai luồng, duyệt cấp 2 qua giao diện, từ chối và tạo lại, điểm kiểm tra, LLM trả sai JSON rồi tự sửa, adapter dòng lệnh.

## 9. Lưu ý

- **Bản quyền**: chỉ chuyển thể truyện bạn có quyền (truyện tự viết hoặc được tác giả đồng ý).
- **Giấy phép model**: FLUX.2 klein 4B là Apache 2.0; klein 9B và một số model tiếng Việt chỉ cho phi thương mại. Edge TTS là dịch vụ không chính thức, quyền dùng thương mại chưa rõ ràng.
- Tham số dòng lệnh của mflux thay đổi theo phiên bản: luôn kiểm tra bằng `--help` và sửa mẫu lệnh trong worker.toml.
- Tên `"Thiện Minh"` của VieNeu và cách gọi `Vieneu().infer(...)` theo tài liệu hiện tại của VieNeu. Nếu thư viện đổi API, sửa `adapters/real.py` hoặc dùng adapter `command`.
- Trên Mac 16 GB, nên để worker chạy tuần tự và chọn LLM 7 đến 8B.
