# Chính sách duyệt

## Cổng

| Cổng | Duyệt cái gì | Gợi ý |
|---|---|---|
| G1 identity | Nhân vật, bối cảnh mới | Sai là sai cả bộ truyện: nên duyệt tay |
| G2 state | Sự kiện trạng thái | Duyệt tay thay đổi vĩnh viễn |
| G3 breakdown | Chia nhịp | Tự động nếu sạch |
| G4 reference | Ảnh tham chiếu | Nên duyệt tay: quyết định độ nhất quán |
| G5 scene | Ảnh gốc của nhịp | Rút mẫu |
| G6 audio | Giọng đọc | Rút mẫu |
| G7 publish | Dựng và xuất bản | Tùy mục đích |

## Chế độ

- `auto`: duyệt luôn (trừ cờ trong `always_review`).
- `auto_if_clean`: duyệt nếu không có cờ **cảnh báo**. Cờ thông tin (`new_identity`, `regenerated`, `permanent`, `redundant`) không chặn.
- `sample`: như trên, thêm rút ngẫu nhiên `sample_rate` cho người xem.
- `manual`: luôn chờ người.

`batch = chapter`: nếu bất kỳ mục nào của chương cần duyệt thì cả chương chờ duyệt cùng nhau.

## Thứ tự áp dụng

1. Cấp của chương (nếu đặt), không thì cấp của dự án.
2. Ghi đè cổng của dự án.
3. Ghi đè cổng của chương.

Đặt cấp riêng cho một chương (ví dụ chương cao trào cấp 4) không làm mất các ghi đè cổng bạn đã cấu hình cho dự án.

## Cờ cảnh báo

| Cờ | Khi nào |
|---|---|
| `possible_duplicate` | Tên gần giống nhân vật/bối cảnh đã có (bỏ dấu, viết tắt, tên con). Kèm gợi ý gộp |
| `alias_collision` | Tên gọi khác trùng tên người khác (đã bị loại khỏi danh sách tên gọi) |
| `unknown_character` / `unknown_location` | Sự kiện/nhịp nhắc tên không có trong danh sách |
| `evidence_missing` | Câu trích không khớp câu nào trong chương |
| `permanent` | Thay đổi vĩnh viễn (thông tin; cấp 2 luôn đưa người duyệt) |
| `conflict` | Kết thúc một trạng thái chưa từng có |
| `age_regression` | Tuổi nhỏ hơn trước |
| `span_adjusted` | Khoảng đoạn của nhịp bị app tự sửa |
| `too_many_cast`, `no_location` | Nhịp có hơn 3 nhân vật / không có bối cảnh |
| `audio_mismatch` | Tốc độ đọc ngoài 0.12–0.7 giây mỗi âm tiết |
| `text_overflow` | Lời thoại tràn khung truyện |

## An toàn khi tự động

- `checkpoint_every`: khi bật Tự chạy, cứ N chương dự án tạm dừng để bạn xem tổng quan.
- Từ chối ảnh/giọng → tự tạo lại với seed mới, tối đa `max_regen` lần.
- **Nhật ký** ghi mọi lần duyệt; lọc "Máy tự duyệt" để rà lại.
