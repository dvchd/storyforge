# Chính sách duyệt

## Cổng và chế độ

| Cổng | Duyệt cái gì |
|---|---|
| G1 identity | Nhân vật, bối cảnh mới |
| G2 state | Sự kiện trạng thái |
| G3 breakdown | Chia nhịp |
| G4 reference | Ảnh tham chiếu |
| G5 scene | Ảnh gốc của nhịp |
| G6 audio | Giọng đọc |
| G7 publish | Dựng và xuất bản |

Chế độ: `auto`, `auto_if_clean` (cờ thông tin `new_identity`, `regenerated`, `permanent`, `redundant` không chặn), `sample`, `manual`. `batch = chapter` gom cả chương. `always_review`: cờ luôn chờ người.

Thứ tự áp dụng: cấp của chương (nếu có) hoặc của dự án → ghi đè cổng của dự án → ghi đè cổng của chương. Đặt cấp cho chương không xóa ghi đè cổng của dự án.

## Cờ cảnh báo

| Cờ | Khi nào |
|---|---|
| `possible_duplicate` | Tên gần giống mục đã có (≥ `dedupe_threshold`, mặc định 0.90). Tên một chữ không tự khớp tên dài |
| `alias_collision` | Tên gọi khác trùng tên người khác |
| `unknown_character` / `unknown_location` | Nhắc tên không có trong danh sách |
| `evidence_missing` | Câu trích không khớp câu nào trong chương |
| `permanent` | Thay đổi vĩnh viễn (thông tin; cấp 2 luôn đưa người duyệt) |
| `conflict` | Kết thúc trạng thái chưa từng có; xóa dấu vết không tồn tại |
| `age_regression` | Tuổi nhỏ hơn trước |
| `non_english_prompt` | Mô tả cho model ảnh có vẻ là tiếng Việt (bỏ qua tên riêng) |
| `span_adjusted` | Khoảng đoạn của nhịp bị tự sửa sau khi LLM chia lại vẫn sai |
| `too_many_cast`, `no_location` | Hơn 3 nhân vật / không có bối cảnh |
| `audio_mismatch` | Tốc độ đọc ngoài 0.12–0.7 giây mỗi âm tiết |
| `text_overflow` | Lời thoại tràn khung truyện |

## Gộp trùng

`dedupe_mode`: `fuzzy` (gợi ý theo tên) · `llm_only` (chỉ khi bấm "Nhờ LLM rà trùng") · `off`. Truyện có nhiều người trùng tên con nên dùng `llm_only`.

## An toàn khi tự động

`checkpoint_every` dừng định kỳ khi tự chạy; từ chối ảnh/giọng → tự tạo lại tối đa `max_regen` lần; nhật ký lọc "máy tự duyệt".
