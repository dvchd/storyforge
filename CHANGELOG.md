# Nhật ký thay đổi

## 0.3.0

### Gộp trùng bớt ồn
- `name_similarity`: tên **một chữ** (An, Minh) không còn tự khớp vào tên dài (Lâm An, Tiểu An); chỉ khớp khi bỏ dấu trùng tuyệt đối. Luật "tập con" chỉ áp dụng khi tên ngắn có từ 2 chữ.
- Ngưỡng mặc định `dedupe_threshold` 0.84 → **0.90**; cảnh báo nếu đặt dưới 0.85.
- Cài đặt **Cách gợi ý gộp trùng**: `fuzzy` (theo tên) · `llm_only` (chỉ khi bấm "Nhờ LLM rà", hợp truyện nhiều tên trùng) · `off`.

### Dấu vết vĩnh viễn xóa lẻ
- `mark` cộng dồn; giá trị `-<dấu vết>` xóa đúng một dấu vết, giữ các dấu vết khác (giá trị rỗng vẫn xóa hết).
- Trang Nhân vật và bảng trạng thái của chương: mỗi dấu vết là một **chip có nút ×**, chọn chương áp dụng; ô thêm dấu vết mới. Ảnh tham chiếu mặt được tạo lại từ chương đó.
- LLM được dặn dùng `-...` để xóa một dấu vết; cờ `conflict` khi xóa dấu vết không tồn tại, `redundant` khi thêm trùng.

### Khung cắt từ ảnh gốc
- Điểm lấy nét mặc định theo góc máy: close (0.5, 0.35), medium (0.5, 0.42), wide giữa. Thêm lựa chọn "Mặt (trên giữa)".
- Prompt ảnh thêm gợi ý bố cục theo góc máy (cận mặt: mặt ở 1/3 trên, chừa khoảng trên đầu).
- Ảnh gốc hiển thị **hai khung cắt** (xanh: video, cam: truyện) chồng lên ảnh; xem trước khung truyện đúng tỉ lệ thật.
- Cảnh báo **khung video và truyện lệch nhau** khi phần giao < `crop_overlap_min` (mặc định 0.6); nút "Theo video" đồng bộ điểm lấy nét. Đếm số nhịp lệch trong "Việc cần làm".

### ffmpeg báo sớm, rõ
- `serve`/`demo` in cảnh báo tiếng Việt kèm lệnh cài cho macOS/Windows/Linux khi thiếu ffmpeg/ffprobe; lệnh mới `storyforge-app doctor`.
- Banner trên giao diện, mục "Công cụ hệ thống" ở trang Hệ thống, nút Xuất bản bị khóa khi dự án chỉ có video mà thiếu ffmpeg.
- Job dựng video lỗi với thông báo "Thiếu ffmpeg… cài bằng…" thay vì `FileNotFoundError`; không thử lại vô ích.
- Kiểm thử: bài cần ffmpeg tự `skip`; các bài khác kiểm tra nhánh "thiếu ffmpeg" thay vì báo đỏ.

### Chia nhịp không đốt token
- `beats_span_retries` (mặc định **1**, tối đa 2): chỉ bắt LLM chia lại một lần; sai tiếp thì tự sửa khoảng đoạn và gắn cờ `span_adjusted` cho người sửa tay. Lỗi JSON vẫn dùng cơ chế thử lại riêng.

### Không tin tuyệt đối vào worker
- Tiến độ chỉ ghi nhận khi **tăng**; lưu `heartbeat_at`, `last_progress_at`.
- **Watchdog** trong app: job ảnh/giọng không tiến triển quá 10 phút (cấu hình được) bị thu hồi và giao lại, kể cả khi worker vẫn gửi heartbeat; worker cũ nhận lệnh dừng. LLM (không báo tiến độ) chỉ dựa vào thời hạn thuê.
- Hàng đợi hiện worker, **tuổi heartbeat**, nhãn "đứng yên X", số lần bị thu hồi.

### Kiểm tra mô tả tiếng Anh
- Cờ `non_english_prompt` khi mô tả cho model ảnh (ngoại hình, mô tả bối cảnh, giá trị trạng thái, hành động, không khí, tư thế, biểu cảm) có vẻ là tiếng Việt; tên riêng đã biết được bỏ qua. Cờ cảnh báo, giữ lại ở cổng duyệt "tự động nếu sạch". Tắt được trong cài đặt.

### Cài đặt
- Kiểm tra chéo: diện tích ảnh gốc so với khung video và khung truyện lớn nhất (phóng to / phí pixel / quá 4 MP), kích thước video chẵn (tự sửa), số khung 1–9, ảnh tham chiếu < 512 px, ngưỡng gộp trùng, tốc độ đọc.
- Nút **Dùng diện tích ảnh gốc gợi ý**; mỗi nhóm có **Đặt lại mặc định**; hiển thị số giá trị đã đổi và giá trị mặc định; cảnh báo cài đặt xuất hiện trong "Việc cần làm".

### Dữ liệu
- Schema v3 (bảng job thêm `heartbeat_at`, `last_progress_at`, `stalls`); nâng cấp tự động từ v1 và v2, có sao lưu.

## 0.2.0
- Sẹo vĩnh viễn đổi ảnh mặt, trạng thái bối cảnh theo chương, ảnh gốc dùng chung cho video và truyện, chuẩn hóa tên thống nhất, gợi ý gộp trùng, schema có phiên bản, dọn tài nguyên, chống bỏ đói hàng đợi, hủy job đang chạy, bố cục truyện theo góc máy, webtoon, chuẩn hóa âm lượng, phụ đề theo từ, giao diện Việc cần làm, phím tắt.

## 0.1.0
- Phiên bản đầu.
