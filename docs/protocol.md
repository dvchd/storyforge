# Hợp đồng app ↔ worker

Gói `storyforge.protocol`, `SCHEMA_VERSION = 2` (app từ chối worker khác phiên bản, HTTP 409).

| Kind | Payload | Output |
|---|---|---|
| `llm.chat` | `messages`, `json_schema`, `temperature`, `max_tokens` | `text`, `usage` |
| `image.generate` | `prompt`, `refs[{asset_id, role, sha256}]`, `width`, `height`, `seed`, `steps` | `files[]` |
| `image.remove_bg` | `image` | `files[]` |
| `tts.synthesize` | `text`, `voice`, `rate` | `file`, `duration`, `sentences[]`, `words[]` |

```
POST /api/worker/claim                    → job (kèm lease_seconds) | 204
POST /api/worker/jobs/{id}/heartbeat      {progress?, message?} → {ok, cancel}
GET  /api/worker/assets/{id}              file + header X-Asset-Sha256
POST /api/worker/jobs/{id}/artifact?name=...
POST /api/worker/jobs/{id}/complete       {output, model_id, elapsed}
POST /api/worker/jobs/{id}/fail           {error, retryable, cancelled}
```

## Tiến độ, heartbeat, watchdog

- Worker gửi heartbeat khoảng `lease/3`, kèm tiến độ khi có thay đổi.
- App chỉ ghi nhận tiến độ khi **tăng**. `last_progress_at` chỉ đổi khi tiến độ tăng.
- Watchdog (mỗi 30 giây trong app) thu hồi job ảnh/giọng/tách nền không tiến triển quá ngưỡng (mặc định 600 giây; `Settings.stall` để đổi, 0 = tắt). LLM mặc định tắt vì không báo tiến độ trong lúc sinh chữ.
- Job bị thu hồi quay lại hàng đợi (`stalls` +1); worker cũ nhận `cancel: true` ở heartbeat kế tiếp và mọi `complete` của nó bị từ chối.

## Viết adapter

```python
class MyImage(Adapter):
    kind, type_name, heavy = "image.generate", "my", True
    def run(self, job, ctx):
        p = ImageGeneratePayload(**job.payload)
        refs = [ctx.fetch_asset(r.asset_id, r.sha256) for r in p.refs]   # đã kiểm tra sha256, có cache
        for i in range(n):
            ctx.check_cancel(); ctx.report((i + 1) / n, f"bước {i + 1}/{n}")
        ...
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id=self.model)
```

Đăng ký trong `REGISTRY` (`worker/adapters/__init__.py`), khai báo `type = "my"` trong `worker.toml`. Lỗi: `RetryableError`, `Cancelled`, ngoại lệ khác.

Adapter dòng lệnh (`type = "command"` / `"mflux"`): placeholder `{prompt} {width} {height} {seed} {steps} {output} {text_file} {voice}...`, `{refs}` / `{refs_repeat}`; dấu `{...}` khác giữ nguyên; tiến độ đọc từ log `37%` hoặc `12/30`.
