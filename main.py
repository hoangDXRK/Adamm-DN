from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import string
from urllib.parse import urlencode

import asyncpg
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gothic-donate-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEPAY_API_KEY = os.getenv("SEPAY_API_KEY", "").strip()
BANK_CODE = os.getenv("BANK_CODE", "").strip()
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "").strip()
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "").strip()
MIN_DONATION_AMOUNT = int(os.getenv("MIN_DONATION_AMOUNT", "1000"))
MAX_DONATION_AMOUNT = int(os.getenv("MAX_DONATION_AMOUNT", "100000000"))
PORT = int(os.getenv("PORT", "10000"))

# The code is also inserted into the QR transfer description.
PAYMENT_CODE_PATTERN = re.compile(r"\b(DNT[A-Z0-9]{8})\b", re.IGNORECASE)

pool: asyncpg.Pool | None = None
notify_lock = asyncio.Lock()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
bot.commands_synced = False  # type: ignore[attr-defined]
bot.reconcile_task = None  # type: ignore[attr-defined]


def format_vnd(value: int) -> str:
    return f"{value:,}".replace(",", ".") + " đ"


def has_bank_configuration() -> bool:
    return bool(BANK_CODE and BANK_ACCOUNT and BANK_ACCOUNT_NAME)


def require_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool has not been initialized")
    return pool


def build_qr_url(amount: int, payment_code: str) -> str:
    # Endpoint documented by SePay for prefilled VietQR transfer images.
    query = urlencode(
        {
            "acc": BANK_ACCOUNT,
            "bank": BANK_CODE,
            "amount": str(amount),
            "des": payment_code,
        }
    )
    return f"https://vietqr.app/img?{query}"


def donation_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="༺̷̛⛧̸̢༒̷̢⸸̸̛༻ ỦNG HỘ SERVER ༺̷̛⸸̸̢༒̷̢⛧̸̛༻",
        description=(
            f"**{discord.utils.escape_markdown(guild_name)}**\n\n"
            "Bạn có muốn đóng góp để hỗ trợ server duy trì và phát triển không?\n"
            "Mỗi khoản ủng hộ đều hoàn toàn tự nguyện. Không có phần thưởng bắt buộc.\n\n"
            "༺̷̛⛧̸̢༒̷̢⸸̸̛༻ **CÁCH ỦNG HỘ** ༺̷̛⛧̸̢༒̷̢⸸̸̛༻\n"
            "Nhấn **Tạo QR chuyển khoản**, nhập số tiền rồi quét mã bằng ứng dụng ngân hàng.\n"
            "Mỗi mã QR có nội dung chuyển khoản riêng để bot đối soát giao dịch.\n\n"
            "༺̷̛⛧̸̢༒̷̢⸸̸̛༻ **MINH BẠCH** ༺̷̛⛧̸̢༒̷̢⸸̸̛༻\n"
            "Bot chỉ ghi nhận khi webhook ngân hàng xác nhận tiền vào và khớp cả mã lẫn số tiền."
        ),
        color=discord.Color.from_rgb(33, 33, 37),
    )
    embed.add_field(
        name="༺ Hạng ủng hộ ༻",
        value="Dùng nút **Top Donate** hoặc lệnh `/topnd` để xem bảng xếp hạng theo tổng tiền đã xác nhận.",
        inline=False,
    )
    embed.set_footer(text="༺̷̛⛧̸̢༒̷̢⸸̸̛༻ Ủng hộ tự nguyện • Gothic Donation System ༺̷̛⛧̸̢༒̷̢⸸̸̛༻")
    return embed


async def create_tables() -> None:
    db = require_pool()
    statements = [
        """
        CREATE TABLE IF NOT EXISTS guild_configs (
            guild_id BIGINT PRIMARY KEY,
            donation_channel_id BIGINT NOT NULL,
            thank_channel_id BIGINT NOT NULL,
            donation_message_id BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS donations (
            payment_code TEXT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            amount BIGINT NOT NULL CHECK (amount > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            confirmed_at TIMESTAMPTZ,
            transaction_id BIGINT UNIQUE,
            notified_at TIMESTAMPTZ
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sepay_transactions (
            sepay_id BIGINT PRIMARY KEY,
            transfer_type TEXT NOT NULL,
            transfer_amount BIGINT NOT NULL,
            payment_code TEXT,
            reference_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            matched_payment_code TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS donor_totals (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            total_amount BIGINT NOT NULL DEFAULT 0,
            donation_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_donations_pending ON donations(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_transactions_unmatched ON sepay_transactions(matched_payment_code, received_at)",
        "CREATE INDEX IF NOT EXISTS idx_donors_leaderboard ON donor_totals(guild_id, total_amount DESC)",
    ]
    async with db.acquire() as conn:
        for statement in statements:
            await conn.execute(statement)


async def get_top_donors(guild_id: int, limit: int = 10) -> list[asyncpg.Record]:
    db = require_pool()
    return await db.fetch(
        """
        SELECT user_id, total_amount, donation_count
        FROM donor_totals
        WHERE guild_id = $1
        ORDER BY total_amount DESC, user_id ASC
        LIMIT $2
        """,
        guild_id,
        limit,
    )


async def build_leaderboard_embed(guild_id: int, guild_name: str, limit: int = 10) -> discord.Embed:
    rows = await get_top_donors(guild_id, limit)
    embed = discord.Embed(
        title=f"༺̷̛⛧̸̢༒ BẢNG XẾP HẠNG DONATE — {discord.utils.escape_markdown(guild_name)} ༒̷̢⛧̸̛༻",
        color=discord.Color.from_rgb(33, 33, 37),
    )
    if not rows:
        embed.description = "Chưa có khoản ủng hộ nào được xác nhận."
    else:
        lines = []
        for rank, row in enumerate(rows, start=1):
            lines.append(
                f"`#{rank:02}`  <@{row['user_id']}> — **{format_vnd(row['total_amount'])}**"
                f"\n　└ {row['donation_count']} giao dịch đã xác nhận"
            )
        embed.description = "\n".join(lines)
    embed.set_footer(text="Chỉ tính các giao dịch đã được xác nhận từ webhook • Cập nhật tự động")
    return embed


class DonationAmountModal(discord.ui.Modal, title="༺ Tạo QR ủng hộ server ༻"):
    amount_text = discord.ui.TextInput(
        label="Số tiền muốn ủng hộ (VND)",
        placeholder="Ví dụ: 20000",
        required=True,
        min_length=1,
        max_length=12,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_amount = str(self.amount_text.value).strip().replace(".", "").replace(",", "").replace(" ", "")
        try:
            amount = int(raw_amount)
        except ValueError:
            await interaction.response.send_message(
                "Số tiền không hợp lệ. Nhập số nguyên VND, ví dụ `20000`.", ephemeral=True
            )
            return

        if amount < MIN_DONATION_AMOUNT or amount > MAX_DONATION_AMOUNT:
            await interaction.response.send_message(
                f"Số tiền phải từ **{format_vnd(MIN_DONATION_AMOUNT)}** đến **{format_vnd(MAX_DONATION_AMOUNT)}**.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server Discord.", ephemeral=True)
            return
        if not has_bank_configuration():
            await interaction.response.send_message(
                "Bot chưa được cấu hình thông tin ngân hàng. Quản trị viên cần thiết lập `BANK_CODE`, `BANK_ACCOUNT` và `BANK_ACCOUNT_NAME` trên Render.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        db = require_pool()
        payment_code = ""
        alphabet = string.ascii_uppercase + string.digits
        # A long random code makes accidental collisions extremely unlikely.
        for _ in range(5):
            payment_code = "DNT" + "".join(secrets.choice(alphabet) for _ in range(8))
            try:
                await db.execute(
                    "INSERT INTO donations(payment_code, guild_id, user_id, amount) VALUES($1, $2, $3, $4)",
                    payment_code,
                    interaction.guild_id,
                    interaction.user.id,
                    amount,
                )
                break
            except asyncpg.UniqueViolationError:
                payment_code = ""
        if not payment_code:
            await interaction.followup.send("Không tạo được mã thanh toán. Hãy thử lại sau.", ephemeral=True)
            return

        qr_url = build_qr_url(amount, payment_code)
        embed = discord.Embed(
            title="༺̷̛⛧̸̢༒ QR CHUYỂN KHOẢN ༒̷̢⛧̸̛༻",
            description=(
                f"**Số tiền:** {format_vnd(amount)}\n"
                f"**Ngân hàng:** {BANK_CODE}\n"
                f"**Chủ tài khoản:** `{BANK_ACCOUNT_NAME}`\n"
                f"**Số tài khoản:** `{BANK_ACCOUNT}`\n\n"
                "**Nội dung chuyển khoản bắt buộc:**\n"
                f"```{payment_code}```\n"
                "Quét mã QR bằng ứng dụng ngân hàng. Giữ nguyên số tiền và nội dung để bot đối soát tự động.\n\n"
                "`Trạng thái: ĐANG CHỜ GIAO DỊCH`\n"
                "Bot chỉ xác nhận sau khi hệ thống nhận được thông báo tiền vào thật. Không gửi ảnh biên lai thay cho giao dịch."
            ),
            color=discord.Color.from_rgb(33, 33, 37),
        )
        embed.set_image(url=qr_url)
        embed.set_footer(text="༺̷̛⛧̸̢༒̷̢⸸̸̛༻ Mã thanh toán riêng cho lần ủng hộ này ༺̷̛⛧̸̢༒̷̢⸸̸̛༻")
        await interaction.followup.send(embed=embed, ephemeral=True)


class DonationView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="༺ Tạo QR chuyển khoản ༻",
        style=discord.ButtonStyle.primary,
        custom_id="gothic_donate:create_qr:v1",
    )
    async def create_qr_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DonationAmountModal())

    @discord.ui.button(
        label="༺ Top Donate ༻",
        style=discord.ButtonStyle.secondary,
        custom_id="gothic_donate:leaderboard:v1",
    )
    async def leaderboard_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Nút này chỉ dùng trong server.", ephemeral=True)
            return
        embed = await build_leaderboard_embed(interaction.guild.id, interaction.guild.name, 10)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setupnd", description="Thiết lập bài đăng ủng hộ và kênh cảm ơn")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    kenh_donate="Kênh bot sẽ đăng bài ủng hộ",
    kenh_cam_on="Kênh bot gửi lời cảm ơn sau khi xác nhận chuyển khoản",
)
async def setupnd(
    interaction: discord.Interaction,
    kenh_donate: discord.TextChannel,
    kenh_cam_on: discord.TextChannel,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Chỉ quản trị viên mới được dùng lệnh này.", ephemeral=True)
        return
    if not has_bank_configuration():
        await interaction.response.send_message(
            "Thiếu cấu hình ngân hàng trên Render. Cần đặt `BANK_CODE`, `BANK_ACCOUNT` và `BANK_ACCOUNT_NAME` trước khi chạy `/setupnd`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    db = require_pool()
    old = await db.fetchrow(
        "SELECT donation_channel_id, donation_message_id FROM guild_configs WHERE guild_id = $1",
        interaction.guild.id,
    )
    message: discord.Message | None = None
    if old and int(old["donation_channel_id"]) == kenh_donate.id:
        try:
            message = await kenh_donate.fetch_message(int(old["donation_message_id"]))
            await message.edit(embed=donation_embed(interaction.guild.name), view=DonationView())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        try:
            message = await kenh_donate.send(embed=donation_embed(interaction.guild.name), view=DonationView())
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Bot không gửi được tin nhắn. Kiểm tra quyền **View Channel**, **Send Messages** và **Embed Links** ở kênh donate.",
                ephemeral=True,
            )
            return

    await db.execute(
        """
        INSERT INTO guild_configs(guild_id, donation_channel_id, thank_channel_id, donation_message_id)
        VALUES($1, $2, $3, $4)
        ON CONFLICT(guild_id) DO UPDATE SET
            donation_channel_id = EXCLUDED.donation_channel_id,
            thank_channel_id = EXCLUDED.thank_channel_id,
            donation_message_id = EXCLUDED.donation_message_id,
            updated_at = NOW()
        """,
        interaction.guild.id,
        kenh_donate.id,
        kenh_cam_on.id,
        message.id,
    )
    await interaction.followup.send(
        f"Đã thiết lập bài donate tại {kenh_donate.mention} và kênh cảm ơn tại {kenh_cam_on.mention}.\n"
        "Các giao dịch mới sẽ được đối soát qua webhook SePay.",
        ephemeral=True,
    )


@bot.tree.command(name="topnd", description="Xem bảng xếp hạng tổng số tiền donate đã xác nhận")
@app_commands.guild_only()
@app_commands.describe(limit="Số người hiển thị (10–25)")
async def topnd(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 10, 25] = 10,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
        return
    embed = await build_leaderboard_embed(interaction.guild.id, interaction.guild.name, int(limit))
    await interaction.response.send_message(embed=embed)


async def match_unprocessed_transactions() -> None:
    """Match stored incoming SePay events to unique pending QR codes exactly once."""
    db = require_pool()
    transactions = await db.fetch(
        """
        SELECT sepay_id, transfer_amount, payment_code
        FROM sepay_transactions
        WHERE LOWER(transfer_type) = 'in'
          AND payment_code IS NOT NULL
          AND matched_payment_code IS NULL
          AND received_at > NOW() - INTERVAL '30 days'
        ORDER BY received_at DESC
        LIMIT 500
        """
    )
    for transaction in transactions:
        code = str(transaction["payment_code"]).upper()
        async with db.acquire() as conn:
            async with conn.transaction():
                locked_tx = await conn.fetchrow(
                    "SELECT sepay_id, transfer_amount, matched_payment_code FROM sepay_transactions WHERE sepay_id = $1 FOR UPDATE",
                    transaction["sepay_id"],
                )
                if not locked_tx or locked_tx["matched_payment_code"] is not None:
                    continue

                donation = await conn.fetchrow(
                    "SELECT payment_code, guild_id, user_id, amount, status FROM donations WHERE payment_code = $1 FOR UPDATE",
                    code,
                )
                if not donation or donation["status"] != "pending":
                    continue
                if int(donation["amount"]) != int(locked_tx["transfer_amount"]):
                    # Do not mark this transaction as paid: amount must match the QR exactly.
                    continue

                await conn.execute(
                    """
                    UPDATE donations
                    SET status = 'confirmed', confirmed_at = NOW(), transaction_id = $2
                    WHERE payment_code = $1 AND status = 'pending'
                    """,
                    code,
                    locked_tx["sepay_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO donor_totals(guild_id, user_id, total_amount, donation_count)
                    VALUES($1, $2, $3, 1)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        total_amount = donor_totals.total_amount + EXCLUDED.total_amount,
                        donation_count = donor_totals.donation_count + 1
                    """,
                    donation["guild_id"],
                    donation["user_id"],
                    donation["amount"],
                )
                await conn.execute(
                    "UPDATE sepay_transactions SET matched_payment_code = $1 WHERE sepay_id = $2",
                    code,
                    locked_tx["sepay_id"],
                )
                log.info("Confirmed donation code=%s amount=%s", code, locked_tx["transfer_amount"])


async def send_pending_thank_yous() -> None:
    """Deliver persisted confirmation notices; leave failed sends queued for retry."""
    async with notify_lock:
        db = require_pool()
        rows = await db.fetch(
            """
            SELECT d.payment_code, d.guild_id, d.user_id, d.amount, d.confirmed_at,
                   c.thank_channel_id
            FROM donations d
            JOIN guild_configs c ON c.guild_id = d.guild_id
            WHERE d.status = 'confirmed' AND d.notified_at IS NULL
            ORDER BY d.confirmed_at ASC
            LIMIT 25
            """
        )
        for row in rows:
            try:
                channel = bot.get_channel(int(row["thank_channel_id"]))
                if channel is None:
                    channel = await bot.fetch_channel(int(row["thank_channel_id"]))
                if not hasattr(channel, "send"):
                    log.warning("Thank-you channel %s cannot send messages", row["thank_channel_id"])
                    continue

                db_total = await db.fetchrow(
                    "SELECT total_amount FROM donor_totals WHERE guild_id = $1 AND user_id = $2",
                    row["guild_id"],
                    row["user_id"],
                )
                total_amount = int(db_total["total_amount"]) if db_total else int(row["amount"])
                higher_count = await db.fetchval(
                    """
                    SELECT COUNT(*) FROM donor_totals
                    WHERE guild_id = $1
                      AND (total_amount > $2 OR (total_amount = $2 AND user_id < $3))
                    """,
                    row["guild_id"],
                    total_amount,
                    row["user_id"],
                )
                rank = int(higher_count or 0) + 1
                message = (
                    "༺̷̛⛧̸̢༒̷̢⸸̸̛༻ **ĐÃ XÁC NHẬN ỦNG HỘ** ༺̷̛⸸̸̢༒̷̢⛧̸̛༻\n\n"
                    f"Cảm ơn <@{row['user_id']}> đã đóng góp cho server.\n"
                    f"**Khoản vừa nhận:** {format_vnd(int(row['amount']))}\n"
                    f"**Tổng ủng hộ đã xác nhận:** {format_vnd(total_amount)}\n"
                    f"**Xếp hạng hiện tại:** `#{rank}`\n\n"
                    "Sự đóng góp của bạn đã được ghi nhận."
                )
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                await db.execute(
                    "UPDATE donations SET notified_at = NOW() WHERE payment_code = $1 AND notified_at IS NULL",
                    row["payment_code"],
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, OSError):
                log.exception("Unable to send thank-you for code %s", row["payment_code"])
            except Exception:
                log.exception("Unexpected error while delivering thank-you for code %s", row["payment_code"])


async def reconciliation_loop() -> None:
    while not bot.is_closed():
        try:
            await match_unprocessed_transactions()
            await send_pending_thank_yous()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reconciliation cycle failed; will retry")
        await asyncio.sleep(10)


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "gothic-donate-bot"})


async def sepay_webhook(request: web.Request) -> web.Response:
    # Require API Key authentication. Never expose this endpoint without authentication.
    expected_key = SEPAY_API_KEY
    authorization = request.headers.get("Authorization", "")
    prefix = "Apikey "
    if not expected_key:
        return web.json_response({"success": False, "message": "Webhook authentication is not configured"}, status=503)
    if not authorization.startswith(prefix) or not secrets.compare_digest(authorization[len(prefix):], expected_key):
        return web.json_response({"success": False, "message": "Unauthorized"}, status=401)

    try:
        payload = await request.json()
        sepay_id = int(payload["id"])
        transfer_type = str(payload.get("transferType", "")).strip().lower()
        transfer_amount = int(float(payload.get("transferAmount", 0)))
    except (ValueError, TypeError, KeyError, AttributeError):
        return web.json_response({"success": False, "message": "Invalid payload"}, status=400)

    # Store only fields needed for reconciliation; don't persist the full bank payload.
    transfer_text = " ".join(
        str(payload.get(field, "") or "")
        for field in ("content", "description", "code")
    ).upper()
    found = PAYMENT_CODE_PATTERN.search(transfer_text)
    payment_code = found.group(1).upper() if found else None
    reference_code = str(payload.get("referenceCode", "") or "")[:200] or None

    db = require_pool()
    try:
        await db.execute(
            """
            INSERT INTO sepay_transactions(
                sepay_id, transfer_type, transfer_amount, payment_code, reference_code
            ) VALUES($1, $2, $3, $4, $5)
            ON CONFLICT(sepay_id) DO NOTHING
            """,
            sepay_id,
            transfer_type,
            transfer_amount,
            payment_code,
            reference_code,
        )
    except Exception:
        log.exception("Could not store SePay transaction id=%s", sepay_id)
        return web.json_response({"success": False, "message": "Temporary database error"}, status=500)

    # The durable event is stored before returning. A background loop processes it and retries
    # after restarts or transient Discord/database failures. Duplicate SePay deliveries are safe.
    return web.json_response({"success": True})


async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    if not bot.commands_synced:  # type: ignore[attr-defined]
        try:
            synced = await bot.tree.sync()
            bot.commands_synced = True  # type: ignore[attr-defined]
            log.info("Synced %s slash commands", len(synced))
        except Exception:
            log.exception("Could not sync slash commands; will retry after reconnect")
    task = bot.reconcile_task  # type: ignore[attr-defined]
    if task is None or task.done():
        bot.reconcile_task = asyncio.create_task(reconciliation_loop())  # type: ignore[attr-defined]


bot.add_view(DonationView())
bot.add_listener(on_ready, "on_ready")


async def main() -> None:
    global pool
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing required environment variable DISCORD_TOKEN")
    if not DATABASE_URL:
        raise RuntimeError("Missing required environment variable DATABASE_URL (PostgreSQL connection URL)")
    if MIN_DONATION_AMOUNT < 1 or MAX_DONATION_AMOUNT < MIN_DONATION_AMOUNT:
        raise RuntimeError("Invalid MIN_DONATION_AMOUNT / MAX_DONATION_AMOUNT")

    database_url = DATABASE_URL
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5, command_timeout=15)
    await create_tables()

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/sepay-webhook", sepay_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP endpoints listening on port %s", PORT)

    if not SEPAY_API_KEY:
        log.warning("SEPAY_API_KEY is not configured; SePay webhook endpoint will reject requests")
    if not has_bank_configuration():
        log.warning("Bank details are missing; /setupnd and QR creation will be unavailable")

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        task = bot.reconcile_task  # type: ignore[attr-defined]
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()
        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
