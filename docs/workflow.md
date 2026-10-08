# Quy trình và mô hình dữ liệu

## Trạng thái chương

```
new → extracting → review_state → state_ready → beating → review_beats → producing → done
                                                                                    ↘ error (thử lại được)
```

Chương k chỉ trích trạng thái khi chương k−1 đã `state_ready`. Các bước sau chạy song song giữa các chương.

## Bảng chính

| Bảng | Ý nghĩa |
|---|---|
| `character`, `location` | Danh tính, tên gọi khác |
| `state_event` | Sự kiện trạng thái của nhân vật hoặc bối cảnh (`subject_type`, `subject_id`) |
| `ref` | Ảnh tham chiếu `character_face`, `character_outfit`, `location`, khóa theo `state_key` |
| `beat` | Nhịp + **ảnh gốc** (`image_*`, `prompt_override`, `image_locked`) |
| `segment` | Đoạn video: lời đọc, giọng, mốc thời gian, điểm lấy nét, chuyển động |
| `page`, `panel`, `balloon` | Trang, khung (tọa độ chuẩn hóa, điểm lấy nét), bóng thoại |
| `suggestion` | Gợi ý gộp trùng |
| `asset` | Tệp theo sha256; `input_hash` để dùng lại kết quả |
| `job` | Hàng đợi; `heartbeat_at`, `last_progress_at`, `stalls` cho watchdog |

## Trạng thái nhân vật

| Trường | Ảnh hưởng |
|---|---|
| `age`, `face`, `mark` | Ảnh tham chiếu **mặt** (và toàn thân) |
| `outfit`, `hair`, `accessory`, `body` | Ảnh tham chiếu **toàn thân** |
| `injury`, `other` | Chỉ đưa vào prompt |

`mark` cộng dồn: mỗi giá trị thêm một dấu vết; `-<dấu vết>` xóa đúng dấu vết đó; rỗng xóa hết. Trên giao diện, mỗi dấu vết là một chip có nút ×.

Bối cảnh: `condition`, `decor`, `other` theo chương; biến thể ánh sáng/thời tiết thuộc từng nhịp. Ảnh tham chiếu bối cảnh khóa theo `biến thể + hiện trạng`.

## Ảnh gốc dùng chung và khung cắt

Kích thước ảnh gốc: chỉ video → tỉ lệ video; chỉ truyện → tỉ lệ khung; cả hai → trung bình nhân hai tỉ lệ (giới hạn 0.55–1.95); diện tích theo `image_area` (trang Cài đặt gợi ý giá trị đủ để không phải phóng to).

Mỗi khung cắt kiểu cover tại **điểm lấy nét**. Mặc định theo góc máy: close (0.5, 0.35), medium (0.5, 0.42), wide (0.5, 0.5). Ảnh gốc trên giao diện có hai khung chồng lên: xanh là video, cam là truyện. Khi phần giao < `crop_overlap_min` (0.6) hiện cảnh báo "khung video và truyện lệch nhau"; bấm "Theo video" để đồng bộ.

## Ảnh cũ

Ảnh gốc mang `image_hash` = hash(prompt, sha256 ảnh tham chiếu, seed, model, kích thước). Sửa trạng thái/mô tả: app tạo trước ảnh tham chiếu mới (`refresh_refs`), ảnh cảnh bị ảnh hưởng hiện nhãn "ảnh cũ" kèm khác biệt prompt; nút "Tạo lại N ảnh cũ" bỏ qua ảnh đã khóa.

## Truyện tranh

Trang in: hàng khung theo góc máy (wide hoặc ≥3 lời thoại → rộng cả hàng; medium → 2; close ít thoại → 3), tối đa `comic_max_panels`. Webtoon: mỗi nhịp một khung toàn chiều ngang. Bóng thoại đặt ở dải trên, chữ tự thu nhỏ đến 70%, còn tràn thì gắn cờ.

## Video

Mỗi đoạn: cắt theo điểm lấy nét → chuyển động theo góc máy → mờ đầu/cuối → loudnorm + khoảng lặng. Phụ đề theo mốc từ, rồi mốc câu, rồi theo số âm tiết. Cần ffmpeg; thiếu thì job báo lỗi kèm hướng dẫn cài.
