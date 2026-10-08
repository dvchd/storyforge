# Quy trình và mô hình dữ liệu

## Trạng thái chương

```
new → extracting → review_state → state_ready → beating → review_beats → producing → done
                                                                                    ↘ error (thử lại được)
```

- `new`: chờ chạy (bật **Tự chạy** cho dự án hoặc bấm **Chạy**). Chương k chỉ trích trạng thái khi chương k−1 đã `state_ready`.
- `review_state`: chờ duyệt nhân vật/bối cảnh mới và sự kiện trạng thái.
- `review_beats`: chờ duyệt nhịp. Duyệt xong app dựng khung sản xuất (đoạn video, trang/khung truyện) và chuyển `producing`.
- `producing`: tạo ảnh tham chiếu → ảnh gốc từng nhịp → giọng đọc. Xong hết thì `done`.
- Xuất bản (dựng MP4, ghép trang) là job cục bộ chạy trong app.

## Bảng chính

| Bảng | Ý nghĩa |
|---|---|
| `character`, `location` | Danh tính; `aliases_json` chứa tên gọi khác |
| `state_event` | Sự kiện trạng thái của nhân vật **hoặc** bối cảnh (`subject_type`, `subject_id`) |
| `ref` | Ảnh tham chiếu: `character_face`, `character_outfit`, `location`; khóa theo `state_key` |
| `beat` | Nhịp truyện + **ảnh gốc** (`image_*`, `prompt_override`, `image_locked`) |
| `segment` | Đoạn video: lời đọc, giọng, thời lượng, mốc thời gian, điểm lấy nét, chuyển động |
| `page`, `panel`, `balloon` | Trang, khung (tọa độ chuẩn hóa), bóng thoại (dữ liệu) |
| `suggestion` | Gợi ý gộp trùng |
| `asset` | Tệp theo sha256, `input_hash` để dùng lại kết quả |
| `job`, `worker`, `audit`, `meta` | Hàng đợi, worker, nhật ký, phiên bản schema |

## Khóa ảnh tham chiếu

- Mặt: `face_key(state)` từ `age`, `face`, `mark`.
- Toàn thân: `outfit_key(state)` từ `outfit`, `hair`, `accessory`, `body` + các trường của mặt.
- Bối cảnh: `location_key(state, variant)` = biến thể của nhịp (`default`, `night`, `rain`...) + hiện trạng (`condition`, `decor`).

Khi mô tả nhân vật (appearance) hoặc ảnh mặt đổi, ảnh tham chiếu chưa khóa được tạo lại tự động. Ảnh **đã khóa** giữ nguyên prompt bạn sửa tay.

## Ảnh gốc dùng chung

Kích thước ảnh gốc của nhịp:
- chỉ video: tỉ lệ video (mặc định 1344×768);
- chỉ truyện: tỉ lệ khung của nhịp;
- cả hai: trung bình nhân của hai tỉ lệ, giới hạn 0.55–1.95; diện tích theo `image_area`.

Đoạn video và khung truyện cắt (cover) từ ảnh gốc theo **điểm lấy nét** chọn trên giao diện (giữa, trên, dưới, trái, phải...). Prompt ảnh có câu nhắc giữ chủ thể gần giữa khung để cắt an toàn.

## Ảnh cũ

Ảnh gốc mang `image_hash` = hash(prompt, sha256 các ảnh tham chiếu, seed, model, kích thước). Khi trạng thái/mô tả đổi:
1. App tạo trước ảnh tham chiếu mới cho các chương đã sản xuất (`refresh_refs`).
2. Ảnh cảnh có hash khác hiện nhãn **ảnh cũ**; bấm để xem khác biệt prompt (`<del>`/`<ins>`) và ảnh tham chiếu đã đổi.
3. Nút **Tạo lại N ảnh cũ** ở chương; ảnh đã khóa được bỏ qua.

Tạo lại không xóa ảnh cũ ngay: nếu bạn quay lại đúng đầu vào cũ, app dùng lại ngay (theo `input_hash`). Dọn ở trang **Dung lượng**.

## Truyện tranh

- Khổ **trang**: hàng khung theo góc máy (wide hoặc ≥3 lời thoại → rộng cả hàng; medium → 2 khung; close ít thoại → 3 khung), tối đa `comic_max_panels` khung/trang, các hàng giãn đều cho kín trang.
- Khổ **webtoon**: mỗi nhịp một khung rộng toàn chiều ngang, cao theo góc máy, `webtoon_panels_per_page` khung mỗi ảnh xuất.
- Bóng thoại đặt ở dải trên, xen kẽ trái phải, rộng theo độ dài chữ. Khi ghép trang, chữ tự thu nhỏ đến 70%; còn tràn thì gắn cờ `text_overflow`.

## Video

Mỗi đoạn: cắt theo điểm lấy nét → chuyển động (tự động: wide lia ngang, medium zoom vào/ra luân phiên, close zoom vào) → mờ đầu/cuối → audio `loudnorm` + khoảng lặng. Nối bằng concat không mã hóa lại. Phụ đề: mốc từng từ nếu TTS có, không thì mốc câu, không nữa thì chia theo số âm tiết; câu dài tách ở dấu phẩy, tối đa 2 dòng 42 ký tự.
