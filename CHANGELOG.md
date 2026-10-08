# Nhật ký thay đổi

## 0.2.0

### Sửa lỗi nhất quán (P0)
- **Sẹo vĩnh viễn đổi ảnh mặt**: thêm trường `mark` (dấu vết vĩnh viễn, cộng dồn). Thương tích `permanent` tự chuẩn hóa thành `mark`. Ảnh tham chiếu mặt phụ thuộc `age`, `face`, `mark`; vết thương tạm (`injury`) chỉ vào prompt.
- **Bối cảnh có trạng thái theo chương**: `state_event` dùng chung cho nhân vật và bối cảnh (`subject_type`). Biến thể ngày/đêm/mưa chuyển về từng nhịp (`beat.variant`), không còn tạo bối cảnh riêng. Ảnh tham chiếu bối cảnh theo `biến thể + hiện trạng`.
- **Chuẩn hóa tên thống nhất**: mọi so khớp tên dùng `checks.norm()` (NFC, chữ thường); lời thoại gán đúng nhân vật có dấu.
- **Một ảnh gốc cho mỗi nhịp**: video và truyện tranh cắt từ cùng một ảnh theo điểm lấy nét; kích thước gốc tính theo cả tỉ lệ video và tỉ lệ khung. Dự án "cả hai" không tạo ảnh hai lần.

### LLM và kiểm tra (P1)
- Cờ mới: `possible_duplicate`, `alias_collision`, `age_regression`; kiểm tra bằng chứng theo từng câu thay vì cả chương.
- Chia nhịp sai khoảng đoạn: mặc định **bắt LLM chia lại** kèm danh sách lỗi cụ thể; tùy chọn tự sửa và gắn cờ.
- Thử lại khi LLM trả sai: chỉ giữ prompt gốc + câu trả lời sai gần nhất (không phình token), tăng dần temperature, lần cuối dùng **model dự phòng**. Ghi số token vào/ra.
- **Gợi ý gộp trùng** nhân vật/bối cảnh (gần đúng, bỏ dấu, viết tắt, tên con) và job **LLM rà trùng**; gộp chuyển sự kiện, nhịp, lời thoại, xóa ảnh tham chiếu thừa.

### Vận hành và dữ liệu (P1)
- **Schema có phiên bản** (`meta.schema_version`), tự **sao lưu** trước khi nâng cấp và trước `demo`; nâng cấp từ 0.1 giữ chương, nhân vật, sự kiện, tài nguyên.
- **Dung lượng và dọn dẹp**: trang Dung lượng, `storyforge-app gc`, xóa tài nguyên mồ côi (có chạy thử), không đụng thư mục job đang chạy.
- Hàng đợi: **chống bỏ đói** (job chờ lâu được cộng ưu tiên), vị trí trong hàng, **tiến độ %** từ worker, **hủy job đang chạy** (adapter dòng lệnh và diffusers dừng ngay), ước tính thời gian còn lại.
- **Thời gian thuê theo loại job** (ảnh 30 phút, LLM 15 phút...); worker tự đặt heartbeat ≈ thuê/3.
- Worker: **kiểm tra sha256** ảnh tham chiếu sau khi tải, **giới hạn dung lượng cache**.

### Âm thanh, hình ảnh (P2)
- Video: chuyển động luân phiên theo góc máy (zoom vào/ra, lia trái/phải), **mờ chuyển cảnh**, **chuẩn hóa âm lượng**, khoảng lặng giữa đoạn, thời lượng đo bằng ffprobe; phụ đề theo **mốc từng từ** (Edge TTS, giả lập), tách câu dài, tối đa 2 dòng.
- Kiểm tra tốc độ đọc theo âm tiết thay vì ký tự.
- Truyện tranh: **bố cục động theo góc máy và mật độ thoại** (wide/nhiều thoại → khung rộng; close → 3 khung một hàng), **khổ webtoon**, bóng thoại đặt ở dải trên theo độ dài chữ, **tự thu nhỏ chữ, gắn cờ khi tràn**.
- Ảnh cảnh: **sửa prompt tay**, **khóa**, tải ảnh của bạn, xem **khác biệt prompt** khi ảnh cũ.
- Xem trước video không dùng ảnh bối cảnh thay ảnh cảnh nữa; hiện rõ đang chờ ảnh / giọng.

### Trải nghiệm (P2)
- Đặt cấp duyệt cho chương **giữ nguyên** ghi đè cổng của dự án.
- Duyệt hàng loạt ảnh tham chiếu lọc theo chương; đếm chờ duyệt bằng một truy vấn.
- Trang dự án có **Việc cần làm**, bảng chương tự làm mới, thanh tiến độ, các bước; cảnh báo khi **không có worker** cho loại job đang chờ.
- **Phím tắt** khi duyệt; liên kết từ bằng chứng tới đúng đoạn văn; điều hướng chương trước/sau.
- **Hồ sơ nhân vật và bối cảnh** dạng Markdown; trang chi tiết bối cảnh.
- `storyforge-app vendor` để chạy giao diện offline.
- Tài liệu `docs/`, thêm truyện mẫu hiện đại; LLM giả lập không còn gắn với truyện mẫu.

## 0.1.0
- Phiên bản đầu.
