# Cấu hình worker

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
aliases = ["tên ngắn"]        # khớp "Model ..." trong cài đặt dự án
```

## Mac Apple Silicon

`configs/worker.mac.toml`: LLM qua `mlx_lm.server`/LM Studio/Ollama (`json_mode = "object"`; Qwen3 nên tắt chế độ suy nghĩ bằng `extra_body`), ảnh qua mflux ([mflux.md](mflux.md)), TTS VieNeu (CPU) hoặc Edge TTS. Máy 16 GB: LLM 7–8B, đừng chạy LLM lớn song song với tạo ảnh.

## NVIDIA

`configs/worker.cuda.toml`: vLLM (`json_mode = "schema"`), diffusers với `black-forest-labs/FLUX.2-klein-4B`.

## Khắc phục sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| Banner "không có worker nào nhận" | Worker chưa chạy, sai token, hoặc thiếu adapter cho loại job |
| HTTP 409 | App và worker khác phiên bản |
| Job bị "thu hồi vì đứng yên" | Worker treo, hết RAM, hoặc công cụ không in tiến độ; xem log worker. Tăng ngưỡng nếu model rất chậm |
| LLM trả sai JSON liên tục | Đổi `json_mode`, tắt chế độ suy nghĩ, đặt model dự phòng |
| Cờ "Mô tả không phải tiếng Anh" nhiều | Thêm "Hướng dẫn thêm cho LLM": luôn viết mô tả bằng tiếng Anh; hoặc dùng model lớn hơn |
| "Không tìm thấy chương trình" | Lệnh trong `cmd` chưa cài hoặc không có trong PATH |
