# Dùng mflux trên Mac

StoryForge gọi [mflux](https://github.com/filipstrand/mflux) qua **dòng lệnh** (adapter `mflux` = `command`), không phụ thuộc API Python của mflux.

```bash
uv tool install --upgrade mflux
mflux-generate-flux2 --help
mflux-generate-flux2-edit --help
```

```toml
[[adapters]]
kind = "image.generate"
type = "mflux"
model = "flux2-klein-4b"
default_steps = 4
max_refs = 4
cmd = ["mflux-generate-flux2", "--model", "flux2-klein-4b", "-q", "8",
       "--prompt", "{prompt}", "--width", "{width}", "--height", "{height}",
       "--seed", "{seed}", "--steps", "{steps}", "--output", "{output}"]
cmd_with_refs = ["mflux-generate-flux2-edit", "--model", "flux2-klein-4b", "-q", "8",
                 "{refs}", "--prompt", "{prompt}", "--width", "{width}", "--height", "{height}",
                 "--seed", "{seed}", "--steps", "{steps}", "--output", "{output}"]
ref_flag = "--image-paths"
```

Nếu phiên bản của bạn cần lặp cờ cho từng ảnh (`--image a.png --image b.png`): dùng `{refs_repeat}` và `ref_flag = "--image"`.

## Khi tham số đổi theo phiên bản

1. Chạy `--help`, đối chiếu tên lệnh và tên cờ.
2. Sửa `cmd` / `cmd_with_refs` trong `worker.toml`, không cần sửa code.
3. Thử với dự án 1 chương, cấp duyệt 4; trang chi tiết job có payload và lỗi đầy đủ.

| Lỗi | Nguyên nhân |
|---|---|
| `unrecognized arguments` | Tên cờ đã đổi |
| `Không thấy file kết quả` | Cờ output khác hoặc mflux tự thêm hậu tố |
| Ảnh không giống nhân vật | Đang dùng `cmd` thay vì `cmd_with_refs`; kiểm tra `ref_flag` |
| Chậm | Bấm "Dùng diện tích ảnh gốc gợi ý" trong Cài đặt, dùng `-q 4`, giảm `default_steps` |
| Job bị thu hồi vì đứng yên | Phiên bản mflux không in tiến độ: tăng ngưỡng `stall` cho `image.generate` |

FLUX.2 klein 9B nhận tới 4 ảnh tham chiếu nhưng là giấy phép phi thương mại. Z-Image Turbo nhanh, đẹp, không nhận ảnh tham chiếu.
