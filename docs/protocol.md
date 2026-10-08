# Hợp đồng app ↔ worker

Gói `storyforge.protocol` là hợp đồng duy nhất. `SCHEMA_VERSION = 2`; app từ chối worker khác phiên bản (HTTP 409).

## Loại job

| Kind | Payload | Output |
|---|---|---|
| `llm.chat` | `messages`, `json_schema`, `schema_name`, `temperature`, `max_tokens`, `meta` | `text`, `usage{prompt_tokens, completion_tokens}` |
| `image.generate` | `prompt`, `negative_prompt`, `refs[{asset_id, role, sha256}]`, `width`, `height`, `seed`, `steps` | `files[]`, `seed` |
| `image.remove_bg` | `image{asset_id, sha256}` | `files[]` |
| `tts.synthesize` | `text`, `voice`, `rate`, `language` | `file`, `duration`, `sentences[]`, `words[]` (`{start, end, text}`) |

`meta` chỉ để tham khảo (adapter giả lập dùng); adapter thật bỏ qua.

## API

Header: `X-Worker-Token`, `X-Worker-Id`.

```
GET  /api/worker/ping
POST /api/worker/claim                    {worker_id, capabilities, loaded_models, model_ids} → job | 204
POST /api/worker/jobs/{id}/heartbeat      {progress?, message?} → {ok, cancel}
GET  /api/worker/assets/{id}              file, header X-Asset-Sha256
POST /api/worker/jobs/{id}/artifact?name=x.png   (body là nội dung file)
POST /api/worker/jobs/{id}/complete       {output, model_id, elapsed}
POST /api/worker/jobs/{id}/fail           {error, retryable, cancelled}
```

- Job trả về có `lease_seconds`; worker gửi heartbeat khoảng `lease/3`. Hết thuê mà không heartbeat, job quay lại hàng đợi.
- `heartbeat` trả `cancel: true` khi người dùng bấm Hủy; adapter dừng ở điểm an toàn rồi gửi `fail` với `cancelled: true`.
- Thứ tự nhận job: ưu tiên + điểm chờ (chống bỏ đói), rồi job dùng model worker đang tải, rồi job cũ trước.

## Viết adapter

```python
from storyforge.protocol import ImageGeneratePayload
from storyforge.worker.adapters.base import Adapter, AdapterResult, JobContext

class MyImage(Adapter):
    kind = "image.generate"
    type_name = "my"
    heavy = True                     # giữ model trong RAM: worker giải phóng khi đổi model

    def load(self): ...              # tải model
    def unload(self): ...            # giải phóng

    def run(self, job, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        refs = [ctx.fetch_asset(r.asset_id, r.sha256) for r in p.refs]   # đã kiểm tra sha256, có cache
        for step in range(n):
            ctx.check_cancel()                                            # dừng nếu bị hủy
            ctx.report((step + 1) / n, f"bước {step + 1}/{n}")             # tiến độ lên giao diện
        out = ctx.workdir / f"img_{job.id}.png"
        ...
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id=self.model)
```

Đăng ký trong `worker/adapters/__init__.py` (`REGISTRY[("image.generate", "my")] = MyImage`), khai báo `type = "my"` trong `worker.toml`.

Lỗi: `RetryableError` (mất mạng, hết RAM: app cho chạy lại), `Cancelled`, ngoại lệ khác (lỗi hẳn).

## Adapter dòng lệnh

`type = "command"` (hoặc `"mflux"`): mẫu lệnh với placeholder `{prompt} {negative} {width} {height} {seed} {steps} {output} {model} {text} {text_file} {voice} {rate} {input}`. Token `{refs}` thay bằng danh sách ảnh tham chiếu (có `ref_flag` phía trước), `{refs_repeat}` lặp `ref_flag ảnh`. Dấu `{...}` khác được giữ nguyên. Tiến độ đọc từ log dạng `37%` hoặc `12/30`; bấm Hủy sẽ dừng tiến trình.
