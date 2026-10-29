# Gothic Donate Bot (Discord + Python + Render + SePay)

Bot Discord tạo bài đăng donate theo kênh được chọn, tạo QR VietQR riêng cho từng lần ủng hộ, xác nhận qua webhook SePay, gửi lời cảm ơn và xếp hạng tổng tiền đã xác nhận.

> Chỉ cấu hình tài khoản ngân hàng mà bạn được phép sử dụng. Nếu tài khoản thuộc người thân/người giám hộ, cần được họ đồng ý. Không đưa token, mật khẩu database hoặc API key vào GitHub.

## Tính năng

- `/setupnd kenh_donate:<kênh> kenh_cam_on:<kênh>`: chỉ quản trị viên được dùng; tạo/cập nhật bài đăng có nút **Tạo QR chuyển khoản** và **Top Donate**.
- Nút tạo QR mở form nhập số tiền. Mỗi QR có mã giao dịch duy nhất dạng `DNTXXXXXXXX`.
- QR điền sẵn tài khoản, số tiền và nội dung chuyển khoản.
- SePay webhook được xác thực bằng API Key. Bot chỉ công nhận giao dịch **tiền vào**, đúng mã và đúng số tiền.
- Giao dịch được chống trùng theo ID do SePay gửi; một mã donate chỉ được tính vào bảng xếp hạng một lần.
- `/topnd` hiển thị từ 10 đến 25 người đứng đầu; nút Top Donate trong bài đăng hiển thị top 10.
- Cấu hình, giao dịch và tổng donate lưu trong PostgreSQL, không dùng SQLite trên filesystem tạm của Render.

## 1. Tạo Discord application

1. Vào <https://discord.com/developers/applications> và chọn **New Application**.
2. Mở mục **Bot**, tạo/copy bot token. Token chỉ lưu trong biến môi trường Render.
3. Trong **OAuth2 → URL Generator**, chọn scopes `bot` và `applications.commands`.
4. Chỉ cấp quyền cần thiết: `View Channels`, `Send Messages`, `Embed Links`, `Use Application Commands`, `Read Message History` cho kênh cần thiết. Mời bot vào server.

Discord yêu cầu tên slash command viết thường; vì vậy lệnh thực tế hiển thị là `/setupnd`, không phải `/setupND`.

## 2. Đưa code lên GitHub

Tải các file trong thư mục này lên repository GitHub. Có thể đặt repository Private. Không tải `.env` thật lên GitHub; chỉ `.env.example` chứa placeholder.

## 3. Tạo PostgreSQL trên Render

1. Mở <https://dashboard.render.com/> → **New → PostgreSQL**.
2. Tạo database. Khi triển khai thực tế, nên dùng database không tự hết hạn và có backup.
3. Copy **Internal Database URL** nếu web service ở cùng region; nếu không, dùng connection URL phù hợp mà Render cung cấp.

**Lưu ý gói miễn phí:** Render Free PostgreSQL hiện hết hạn sau 30 ngày; sau thời gian gia hạn, database và dữ liệu có thể bị xóa. Không dùng database Free hết hạn làm kho dữ liệu donate dài hạn.

## 4. Deploy Web Service

1. Vào Render → **New → Web Service** → kết nối GitHub repository.
2. Cấu hình:
   - Runtime/Language: Python
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Region: chọn cùng region với PostgreSQL nếu dùng Internal Database URL
3. Thêm các Environment Variables sau trong tab **Environment**:

| Key | Value |
|---|---|
| `DISCORD_TOKEN` | Token bot từ Discord Developer Portal |
| `DATABASE_URL` | URL PostgreSQL do Render cấp |
| `SEPAY_API_KEY` | Chuỗi bí mật ngẫu nhiên do bạn tạo; nhập cùng giá trị trong cấu hình webhook SePay |
| `BANK_CODE` | Mã/tên ngân hàng được QR provider chấp nhận; kiểm tra danh sách ngân hàng SePay/VietQR |
| `BANK_ACCOUNT` | Số tài khoản nhận donate |
| `BANK_ACCOUNT_NAME` | Tên chủ tài khoản |
| `MIN_DONATION_AMOUNT` | `1000` (tuỳ chỉnh nếu cần) |
| `MAX_DONATION_AMOUNT` | `100000000` (tuỳ chỉnh nếu cần) |

4. Chọn **Deploy Web Service** và xem Logs. Khi Discord đăng nhập thành công, các slash command sẽ được đồng bộ. Lệnh global đôi khi mất một lúc mới hiện trong Discord.
5. Mở `https://TEN-SERVICE.onrender.com/health`; phản hồi bình thường là JSON `{"ok": true, "service": "gothic-donate-bot"}`.

## 5. Cấu hình webhook SePay

Bạn cần một tài khoản SePay đã liên kết với tài khoản ngân hàng được phép sử dụng, vì chỉ QR không đủ để bot biết có tiền vào thật hay chưa.

1. Trong SePay, tạo webhook, chọn sự kiện **Có tiền vào** và chọn đúng tài khoản ngân hàng.
2. URL webhook: `https://TEN-SERVICE.onrender.com/sepay-webhook`
3. Chọn phương thức xác thực **API Key** và đặt cùng chuỗi với `SEPAY_API_KEY` trên Render. SePay gửi header `Authorization: Apikey ...`.
4. Nếu cấu hình nhận diện mã thanh toán, đặt tiền tố `DNT`, hậu tố 8 ký tự chữ/số. Lúc thử nghiệm, có thể không lọc tiền tố để kiểm tra payload trước.
5. Bật tự động gửi lại webhook khi endpoint lỗi. Dùng nút gửi thử trong SePay để kiểm tra; sau đó thử một khoản nhỏ trên tài khoản được phép sử dụng.

Webhook chỉ lưu ID giao dịch, loại tiền vào/ra, số tiền, mã donate nếu tìm thấy và mã tham chiếu; bot không lưu toàn bộ payload ngân hàng. SePay phải có thể truy cập endpoint HTTPS công khai.

## 6. Sử dụng trong Discord

1. Chạy `/setupnd`, chọn **kênh donate** và **kênh cảm ơn**.
2. Bot sẽ đăng bài có nút Gothic. Thành viên nhấn **Tạo QR chuyển khoản**, nhập số tiền rồi quét QR bằng app ngân hàng.
3. Người chuyển khoản cần giữ nguyên nội dung mã `DNTXXXXXXXX` và chuyển đúng số tiền.
4. Khi webhook xác nhận tiền vào trùng mã và số tiền, bot ghi nhận donate, cập nhật `/topnd` và gửi lời cảm ơn vào kênh đã chọn.

Các lệnh Discord phải viết thường theo quy tắc của Discord: `/setupnd` và `/topnd`. `/topnd` hỗ trợ giới hạn 10–25 người.

## Giới hạn và lưu ý quan trọng

- Render Free Web Service có thể spin down sau 15 phút không nhận traffic đến; khi có request tiếp theo, khởi động lại mất khoảng một phút. Không bảo đảm bot nhận webhook và phản hồi 24/7. Dùng gói dịch vụ không spin down nếu đây là hệ thống donate đang sử dụng thật.
- Free PostgreSQL trên Render chỉ dùng thử: database hết hạn sau 30 ngày. Hãy chọn database lâu dài và có backup nếu cần giữ lịch sử donate.
- QR image endpoint là dịch vụ bên ngoài; nếu không hiển thị QR, hãy kiểm tra `BANK_CODE` theo danh sách nhà cung cấp và kiểm tra endpoint VietQR/SePay.
- Chỉ xác nhận theo webhook đã xác thực, đúng transfer type `in`, đúng mã và đúng số tiền. Người dùng chuyển sai số tiền hoặc thiếu nội dung sẽ không tự động được ghi nhận.
- Hệ thống cảm ơn sử dụng hàng đợi lưu trong PostgreSQL; nếu Discord tạm lỗi, bot sẽ thử gửi lại ở vòng xử lý sau. Trong trường hợp hiếm Discord gửi thành công nhưng tiến trình dừng trước khi đánh dấu đã gửi, thông báo có thể bị lặp.
- Không dùng ảnh biên lai, lời nhắn hay nút “đã chuyển” để thay cho xác nhận giao dịch ngân hàng.

## Tài liệu chính thức

- Render Free: <https://render.com/docs/free>
- Render Environment Variables: <https://render.com/docs/configure-environment-variables>
- SePay Webhooks: <https://docs.sepay.vn/tich-hop-webhooks.html>
- SePay tạo webhook: <https://developer.sepay.vn/vi/sepay-webhooks/tao-webhook>
- SePay tạo QR: <https://developer.sepay.vn/vi/sepay-webhooks/tao-qr-va-form-thanh-toan>
- SePay xác thực webhook: <https://developer.sepay.vn/vi/sepay-webhooks/xac-thuc>
