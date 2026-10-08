# Cấu hình worker

Một worker có thể có nhiều adapter. Mỗi máy chạy một worker trỏ về cùng một app; có thể chạy nhiều máy cùng lúc.

```toml
[server]
url = "http://127.0.0.1:8765"
token = "..."                 # storyforge-app token

[worker]
id = "mac-mini-m6"
memory_mode = "sequential"    # chỉ giữ một model nặng trong RAM
heartbeat_every = 20
cache_mb = 2048

[[adapters]]
kind = "llm.chat" | "image.generate" | "tts.synthesize" | "image.remove_bg"
type = "openai" | "mflux" | "command" | "diffusers" | "vieneu" | "edge_tts" | "rembg" | "mock"
model = "tên model"
aliases = ["tên ngắn"]        # khớp "Model ..." trong cài đặt dự án để worker ưu tiên
```

## Mac Apple Silicon

Xem `configs/worker.mac.toml`.

- **LLM**: chạy `mlx_lm.server`, LM Studio hoặc Ollama, adapter `openai`. Máy 16 GB nên dùng model 7–8B 4-bit; 32 GB dùng 14B. `json_mode = "object"` an toàn cho hầu hết server; server hỗ trợ JSON schema thì đặt `"schema"`. Model có chế độ suy nghĩ (Qwen3): thêm `extra_body = { chat_template_kwargs = { enable_thinking = false } }` để trả lời nhanh và gọn.
- **Ảnh**: mflux qua adapter `mflux` (dòng lệnh). Xem [mflux.md](mflux.md).
- **TTS**: `vieneu` (CPU), hoặc `edge_tts` (cần Internet, có mốc từng từ cho phụ đề).

Mẹo: trên máy RAM ít, đừng chạy server LLM lớn song song với tạo ảnh; tắt server LLM khi đang sản xuất ảnh hàng loạt.

## Máy NVIDIA

Xem `configs/worker.cuda.toml`: vLLM cho LLM (`json_mode = "schema"`), `diffusers` cho ảnh (`repo = "black-forest-labs/FLUX.2-klein-4B"`).

## Chỉ có CPU

LLM qua API (OpenRouter, server trong mạng nội bộ), TTS `vieneu` hoặc `edge_tts`, ảnh chạy trên máy khác.

## Model dự phòng

Đặt "Model dự phòng" trong cài đặt dự án (ví dụ `qwen3-32b`) và khai báo một adapter `llm.chat` có alias đó. Lần thử lại cuối của job LLM sẽ được chuyển sang model này.

## Khắc phục sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| Banner "không có worker nào nhận" | Worker chưa chạy, sai token, hoặc thiếu adapter cho loại job đó |
| HTTP 409 khi nhận job | App và worker khác phiên bản: cập nhật cả hai |
| LLM trả sai JSON liên tục | Đổi `json_mode`, tắt chế độ suy nghĩ, dùng model lớn hơn hoặc đặt model dự phòng |
| Ảnh tham chiếu "sha256 không khớp" | Mạng chập chờn; job tự chạy lại |
| Hết RAM khi tạo ảnh | `memory_mode = "sequential"`, giảm `image_area`, dùng lượng tử hóa 4-bit |
