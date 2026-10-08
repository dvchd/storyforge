# Dùng mflux trên Mac

[mflux](https://github.com/filipstrand/mflux) chạy các model tạo ảnh bằng MLX trên GPU của Apple Silicon. StoryForge gọi mflux qua **dòng lệnh** (adapter `mflux` = adapter `command`), nên không phụ thuộc API Python của mflux.

```bash
uv tool install --upgrade mflux
mflux-generate-flux2 --help
mflux-generate-flux2-edit --help
```

## Mẫu cấu hình (FLUX.2 klein 4B)

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

- `cmd` dùng khi không có ảnh tham chiếu (ảnh mặt đầu tiên, ảnh bối cảnh), `cmd_with_refs` khi có.
- `{refs}` được thay bằng `--image-paths a.png b.png ...`. Nếu phiên bản của bạn cần lặp cờ cho từng ảnh (`--image a.png --image b.png`), dùng `{refs_repeat}` và `ref_flag = "--image"`.

## Khi tham số đổi theo phiên bản

1. Chạy `--help`, đối chiếu tên lệnh và tên cờ (`--image-paths`, `--steps`, `-q`, `--output`...).
2. Sửa mảng `cmd`/`cmd_with_refs` trong `worker.toml`. Không cần sửa code.
3. Thử nhanh: tạo một dự án 1 chương, bật cấp duyệt 4, xem job ở trang Hàng đợi (trang chi tiết job có payload và lỗi đầy đủ).

Lỗi thường gặp:

| Lỗi | Nguyên nhân |
|---|---|
| `unrecognized arguments` | Tên cờ đã đổi: sửa mẫu lệnh |
| `Không thấy file kết quả` | Cờ output khác (`--output` / `-o`) hoặc mflux tự thêm hậu tố: kiểm tra thư mục job |
| Ảnh không giống nhân vật | Lệnh đang dùng `cmd` thay vì `cmd_with_refs`; kiểm tra `ref_flag` |
| Chậm | Giảm `image_area` trong cài đặt dự án, dùng `-q 4`, giảm `default_steps` |

## Model khác

- **Z-Image Turbo**: đẹp, nhanh, không nhận ảnh tham chiếu. Dùng cho ảnh bối cảnh qua một adapter riêng có alias, rồi đặt "Model ảnh" phù hợp; hoặc dùng LoRA nhân vật.
- **FLUX.2 klein 9B**: nhận tới 4 ảnh tham chiếu, chất lượng cao hơn, **giấy phép phi thương mại**.

Tiến độ: mflux in thanh tiến độ dạng `37%` hoặc `3/4`, worker đọc và hiển thị trên trang Hàng đợi. Bấm Hủy sẽ dừng tiến trình mflux ngay.
