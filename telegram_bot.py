"""
telegram_bot.py — Nexus-OS v1.1 Telegram 前端

python-telegram-bot v20+ 正確啟動方式：
- run_polling() / run_webhook() 是同步方法，自行管理 event loop
- 不能用 asyncio.run() 包裝，也不能 await 它們
- 非同步初始化（init_db、Director）透過 post_init 回呼完成
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest

from database import init_db, AsyncSessionLocal, User
from logic.director import Director
from sqlalchemy import select

load_dotenv()

# ------------------------------------------------------------------
# 日誌設定
# ------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 環境變數
# ------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PORT = int(os.getenv("PORT", "8443"))
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ------------------------------------------------------------------
# 全域 Director 實例
# ------------------------------------------------------------------

_director: Director | None = None


# ------------------------------------------------------------------
# post_init 回呼（非同步初始化的正確位置）
# ------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """
    Application 建立完成後的非同步初始化回呼。
    這是 python-telegram-bot v20+ 做非同步 setup 的正確方式。
    """
    global _director

    # ── 資料庫初始化（指數退避重試）──────────────────────────────
    max_retries = 10
    retry_delay = 3

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"正在初始化資料庫（第 {attempt}/{max_retries} 次）...")
            await init_db()
            logger.info("✅ 資料庫初始化成功！")
            break
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"❌ 達到最大重試次數：{e}")
                logger.warning("Bot 將以無記憶模式運行")
            else:
                wait_secs = min(retry_delay * (2 ** (attempt - 1)), 60)
                logger.warning(f"DB 連線失敗，{wait_secs}s 後重試... ({e})")
                await asyncio.sleep(wait_secs)

    # ── 建立 Director 實例（注入 Telegram 推播 callback）───────────
    logger.info("正在啟動指揮官大腦...")

    async def _send_reminder_fn(chat_id: str, text: str) -> None:
        """ReminderAgent 觸發提醒時的推播 callback。"""
        try:
            await application.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error(f"推播提醒失敗 chat_id={chat_id}: {exc}")

    async def _send_file_fn(chat_id: str, filename: str, data: bytes, mime: str, caption: str) -> None:
        """秘書 Agent 將 Drive 文件傳送給用戶的 callback。"""
        import io
        try:
            await application.bot.send_document(
                chat_id=int(chat_id),
                document=io.BytesIO(data),
                filename=filename,
                caption=caption or None,
            )
        except Exception as exc:
            logger.error(f"傳送文件失敗 chat_id={chat_id}: {exc}")

    async def _send_photo_fn(chat_id: str, image_bytes: bytes, caption: str) -> None:
        """圖片師 Agent 生成圖片後的推播 callback。"""
        import io
        try:
            await application.bot.send_photo(
                chat_id=int(chat_id),
                photo=io.BytesIO(image_bytes),
                caption=caption or None,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error(f"推播圖片失敗 chat_id={chat_id}: {exc}")

    async def _send_approval_fn(chat_id: str, proposal: dict) -> None:
        """優化師 Agent 發送優化提案給 Admin 批准的 callback。"""
        try:
            target = proposal.get("target_file", "未知檔案")
            reason = proposal.get("reason", "未提供原因")
            text = (
                f"🔧 **【系統優化提案】**\n\n"
                f"**目標檔案**：`{target}`\n"
                f"**優化原因**：{reason}\n\n"
                f"是否同意套用此優化？"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ 批准並執行", callback_data="opt_approve"),
                    InlineKeyboardButton("❌ 拒絕", callback_data="opt_reject"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await application.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.error(f"發送審批請求失敗 chat_id={chat_id}: {exc}")

    _director = Director(
        send_message_fn=_send_reminder_fn, 
        send_file_fn=_send_file_fn, 
        send_photo_fn=_send_photo_fn,
        send_approval_fn=_send_approval_fn
    )

    # ── 從 DB 重載未觸發的排程任務（重啟持久化）────────────────────
    from logic.reminder_agent import ReminderAgent as _RA
    _reminder: _RA = _director._agent_registry.get("reminder")
    await _reminder.restore_from_db()
    logger.info("🧠 指揮官大腦已就緒！")



# ------------------------------------------------------------------
# Telegram 指令處理器
# ------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start 指令：歡迎訊息"""
    user = update.effective_user
    await update.message.reply_text(
        f"你好，{user.first_name}！👋\n\n"
        "我是 **Nexus-OS**，你的個人 AI 助理。\n\n"
        "你可以：\n"
        "• 直接打字\n"
        "• 🎤 發送語音訊息（我會自動轉寫）\n"
        "• 📎 傳送文件或圖片（會暫存至 Google Drive）\n"
        "• 🎨 要求生成或改造圖片\n"
        "• 💰 查詢系統 API 成本與用量\n\n"
        "例如：\n"
        "• 「下週三三點幫我加個會議」\n"
        "• 「現在天氣如何？」\n"
        "• 「生成一張香港夜景圖片」\n"
        "• 「計算本月的 API 使用成本」\n"
        "• 語音輸入：直接按麥克風按鈕說話🚀",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help 指令：顯示可用功能"""
    await update.message.reply_text(
        "📖 **Nexus-OS 使用說明**\n\n"
        "**📅 行事曆功能**\n"
        "• 「下週三三點幫我加個與 Alice 的會議」\n"
        "• 「這週我有什麼行程？」\n\n"
        "**⛅ 天氣功能**\n"
        "• 「現在天氣如何？」\n\n"
        "**⏰ 提醒功能**\n"
        "• 「下午三點提醒我開會」\n"
        "• 「每天早上 8 點報告天氣」\n\n"
        "**📂 秘書功能**\n"
        "• 「列出我的 Drive 文件」\n"
        "• 「搜尋關於會議的文件」\n"
        "• 传送文件/圖片 → 暫存至 Google Drive\n\n"
        "**🎨 圖片師功能**\n"
        "• 「生成一張香港夜景的圖片」\n"
        "• 「生成 16:9 的日系風格貓咪圖片」\n"
        "• 「描述我剛才上傳的圖片」（需提供 Drive ID）\n"
        "• 「把剛才的圖片改成卡通風格」（需提供 Drive ID）\n\n"
        "**🎤 語音輸入**\n"
        "• 直接按 Telegram 麥克風按鈕說話，我會自動轉寫\n\n"
        "**指令列表**\n"
        "/start — 重新開始\n"
        "/help — 顯示此說明\n"
        "/clear — 清除當前對話記憶",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear 指令：重設 session_id 開啟新對話"""
    context.user_data["session_id"] = str(update.message.message_id)
    await update.message.reply_text("🗑️ 已開啟新的對話！之前的記憶不會被帶入。")

async def cmd_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rollback 指令：僅限管理員，還原最新優化"""
    if not _director:
        return
        
    user_id = str(update.effective_user.id)
    admin_ids_str = os.getenv("ADMIN_IDS", "")
    admin_ids = [x.strip() for x in admin_ids_str.split(",") if x.strip()]
    
    if user_id not in admin_ids:
        await update.message.reply_text("⛔ 權限不足：你不是系統管理員。")
        return
        
    msg = await update.message.reply_text("⏳ 正在還原最新的優化備份...")
    try:
        from logic.optimizer_agent import OptimizerAgent
        optimizer: OptimizerAgent = _director._agent_registry.get("optimizer")
        result = optimizer.rollback_optimization()
        await msg.edit_text(result, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ 執行還原時發生錯誤：{e}")


# ── 權限檢查輔助函式 ────────────────────────────────────────────────
async def check_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    檢查使用者權限。
    1. 管理員 (ADMIN_IDS) 自動通行。
    2. 已批准 (approved) 用戶通行。
    3. 新用戶自動建立為 pending 並通知管理員。
    """
    user = update.effective_user
    if not user: return False
    user_id = str(user.id)

    logger.info(f"[Access] Checking access for user={user_id} (@{user.username})")

    # 1. 管理員直接核准
    if user_id in ADMIN_IDS:
        logger.info(f"[Access] User {user_id} is in ADMIN_IDS. Permitted.")
        return True

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            db_user = result.scalar_one_or_none()

            # 2. 已核准用戶
            if db_user and db_user.status == "approved":
                logger.info(f"[Access] User {user_id} is approved in DB. Permitted.")
                return True
            
            # 3. 被封鎖用戶
            if db_user and db_user.status == "banned":
                logger.warning(f"[Access] User {user_id} is banned in DB. Denied.")
                await update.message.reply_text("⛔ 存取被拒：您的帳號已被封鎖。如有疑問請聯繫管理員。")
                return False

            # 4. 等待審批中
            if db_user and db_user.status == "pending":
                logger.info(f"[Access] User {user_id} is pending in DB. Denied.")
                await update.message.reply_text("⏳ 您的申請正在等待審批中，請稍候。管理員核准後您將收到通知。")
                return False

            # 5. 新用戶：建立記錄並通知管理員
            logger.info(f"[Access] New user {user_id}. Creating pending record.")
            new_user = User(
                telegram_id=user_id,
                username=user.username,
                first_name=user.first_name,
                status="pending"
            )
            session.add(new_user)
            await session.commit()

            # 通知管理員
            admin_text = (
                f"👤 **【新用戶存取申請】**\n\n"
                f"**ID**: `{user_id}`\n"
                f"**名稱**: {user.first_name}\n"
                f"**帳號**: @{user.username or '無'}\n\n"
                f"是否批准此用戶使用 Nexus-OS？"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ 批准", callback_data=f"auth_approve_{user_id}"),
                    InlineKeyboardButton("❌ 拒絕", callback_data=f"auth_reject_{user_id}")
                ]
            ])
            
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_id),
                        text=admin_text,
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"通知管理員失敗: {e}")

            await update.message.reply_text("👋 您好！已收到您的存取申請。為了保安理由，新用戶需要管理員手動核准。請耐心等候，核准後我會通知您！")
            return False
    except Exception as e:
        logger.error(f"[Access] Error checking access for {user_id}: {e}", exc_info=True)
        await update.message.reply_text("⚠️ 系統保安模組發生錯誤，暫時拒絕存取。請聯絡管理員檢查資料庫連線。")
        return False

# ------------------------------------------------------------------
# 訊息處理器（核心路由）
# ------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    所有文字訊息的唯一處理入口。
    嚴格遵守架構原則：所有訊息透過 Director.process_request() 處理。
    """
    if _director is None:
        await update.message.reply_text("⚠️ 系統初始化中，請稍候片刻再試。")
        return

    # 權限檢查
    if not await check_user_access(update, context):
        return

    user = update.effective_user
    message_text = update.message.text
    user_id = str(user.id)
    session_id = context.user_data.get("session_id", str(update.effective_chat.id))

    logger.info(f"收到訊息 user={user_id} | {message_text[:50]}")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    try:
        reply = await _director.process_request(
            user_id=user_id,
            message=message_text,
            session_id=session_id,
        )
        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"處理訊息時發生錯誤：{e}", exc_info=True)
        await update.message.reply_text(
            "😅 抱歉，我暫時無法回應，請稍後再試。\n"
            f"（錯誤：{str(e)[:100]}）"
        )



# ------------------------------------------------------------------
# 語音輸入 handler（Telegram Voice → Gemini 轉錄 → Director）
# ------------------------------------------------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    接收 Telegram 語音訊息（OGG/Opus 格式）。
    流程：
      1. 從 Telegram 下載音頻
      2. 用 Gemini 進行語音轉文字（STT）
      3. 顯示轉錄結果給用戶
      4. 將轉錄文字送入 Director.process_request()
    """
    if _director is None:
        await update.message.reply_text("⚠️ 系統初始化中，請稍候片刻再試。")
        return

    # 權限檢查
    if not await check_user_access(update, context):
        return

    user = update.effective_user
    user_id = str(user.id)
    session_id = context.user_data.get("session_id", "default")

    # 顯示處理中提示（語音轉錄需要幾秒）
    processing_msg = await update.message.reply_text("🎤 正在轉錄語音，請稍候...")

    try:
        import asyncio
        import google.generativeai as genai

        # ── 步驟 1：從 Telegram 下載音頻 ──────────────────────────────
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        audio_data = await tg_file.download_as_bytearray()

        # ── 步驟 2：用 Gemini 轉錄（inline audio，無需上傳到 Files API）──
        _stt_model_name = os.getenv("GEMINI_VOICE_MODEL", "gemini-3-pro-preview")
        stt_model = genai.GenerativeModel(_stt_model_name)
        loop = asyncio.get_event_loop()

        transcription_response = await stt_model.generate_content_async([
                {
                    "mime_type": "audio/ogg",
                    "data": bytes(audio_data),
                },
                "請將這段語音的內容完整轉錄為文字。只需回傳轉錄的文字，不需要任何其他說明。",
            ])
        transcribed_text = transcription_response.text.strip()

        if not transcribed_text:
            await processing_msg.edit_text("❌ 無法辨識語音內容，請重試或改用文字輸入。")
            return

        # ── 步驟 3：顯示轉錄結果 ─────────────────────────────────────
        await processing_msg.edit_text(f"🎤 你說：\n「{transcribed_text}」")

        # ── 步驟 4：送入 Director 處理（如同用戶打字）─────────────────
        await update.message.chat.send_action("typing")
        reply = await _director.process_request(
            user_id=user_id,
            message=transcribed_text,
            session_id=session_id,
        )
        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"語音處理失敗：{e}", exc_info=True)
        await processing_msg.edit_text(
            f"❌ 語音轉錄失敗：{str(e)[:100]}\n\n請改用文字輸入。"
        )


# ------------------------------------------------------------------
# 秘書 Agent：文件/圖片接收 handler（Telegram → Drive 暫存）
# ------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    接收用戶上傳的文件 → 透過秘書 Agent 暫存至 Google Drive Staging 資料夾。
    回覆確認訊息（含 Drive File ID）並等待用戶進一步指示。
    """
    if not _director:
        return

    # 權限檢查
    if not await check_user_access(update, context):
        return

    msg = update.message
    doc = msg.document
    await msg.reply_text("⏳ 正在上傳至 Google Drive，請稍候...")

    try:
        # 從 Telegram 下載文件
        tg_file = await context.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()

        # 透過秘書 Agent 上傳至 Drive
        from logic.secretary_agent import SecretaryAgent as SA
        secretary: SA = _director._agent_registry.get("secretary")
        result = await secretary.upload_bytes(
            filename=doc.file_name or "document",
            data=bytes(data),
            mime_type=doc.mime_type or "application/octet-stream",
        )

        drive_id = result.get("id", "未知")
        link = result.get("webViewLink", "")
        await msg.reply_text(
            f"✅ **文件已暫存至 Google Drive**\n\n"
            f"• 名稱：{result.get('name')}\n"
            f"• Drive ID：`{drive_id}`\n"
            + (f"• [在 Drive 中開啟]({link})\n" if link else "") +
            f"\n請告訴我下一步如何處理這個文件！"
        )
    except Exception as e:
        logger.error(f"文件上傳 Drive 失敗：{e}", exc_info=True)
        await msg.reply_text(f"❌ 上傳失敗：{e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    接收用戶上傳的圖片。

    兩條路徑：
      A) 有 caption（說明文字）→ 直接交給圖片師 Agent 處理（改造/描述），無需上傳 Drive
      B) 無 caption → 上傳至 Google Drive Staging，等待指示
    """
    if not _director:
        return

    msg = update.message
    photo = msg.photo[-1]          # 最高解析度
    caption = (msg.caption or "").strip()
    user = update.effective_user
    user_id = str(user.id)
    session_id = context.user_data.get("session_id", str(update.effective_chat.id))

    # 權限檢查
    if not await check_user_access(update, context):
        return

    # ── 路徑 A：有 caption → 直接讓圖片師處理（改造 / 描述）────────
    if caption:
        processing_msg = await msg.reply_text("🎨 正在處理圖片，請稍候...")
        try:
            # 從 Telegram 下載圖片 bytes
            tg_file = await context.bot.get_file(photo.file_id)
            image_bytes = bytes(await tg_file.download_as_bytearray())

            # 取得圖片師 Agent
            from logic.image_artist_agent import ImageArtistAgent as IA
            artist: IA = _director._agent_registry.get("image_artist")

            # 判斷指令類型（描述 or 改造）
            describe_keywords = ["描述", "分析", "看看", "是什麼", "告訴我", "解釋", "identify", "what is", "describe"]
            is_describe = any(kw in caption for kw in describe_keywords)

            if is_describe:
                # 描述圖片
                result_text = await artist.describe_image(
                    image_bytes=image_bytes,
                    question=caption,
                    mime_type="image/jpeg",
                )
                await processing_msg.edit_text(f"🔍 **圖片分析結果**\n\n{result_text}")
            else:
                # 改造圖片（transform_image 失敗時會 raise，由外層 except 捕捉顯示真實錯誤）
                transformed = await artist.transform_image(
                    image_bytes=image_bytes,
                    instruction=caption,
                    mime_type="image/jpeg",
                )
                if transformed:
                    import io
                    await processing_msg.delete()
                    await msg.reply_photo(
                        photo=io.BytesIO(transformed),
                        caption=f"✨ 已改造：{caption[:80]}",
                    )
                else:
                    await processing_msg.edit_text("❌ 圖片改造失敗（未返回圖片），請重試。")
        except Exception as e:
            logger.error(f"圖片師處理失敗：{e}", exc_info=True)
            err_str = str(e)
            display_err = err_str[:300] if len(err_str) > 300 else err_str
            # 不用 parse_mode，避免錯誤訊息中含有 Markdown 特殊字符導致解析失敗
            await processing_msg.edit_text(
                f"❌ 處理失敗\n\n{display_err}\n\n可在伺服器日誌查看完整錯誤。"
            )
        return

    # ── 路徑 B：無 caption → 上傳至 Google Drive 暫存 ──────────────
    await msg.reply_text("⏳ 正在上傳圖片至 Google Drive，請稍候...")
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        data = await tg_file.download_as_bytearray()

        from logic.secretary_agent import SecretaryAgent as SA
        secretary: SA = _director._agent_registry.get("secretary")
        filename = f"photo_{photo.file_unique_id}.jpg"
        result = await secretary.upload_bytes(
            filename=filename,
            data=bytes(data),
            mime_type="image/jpeg",
        )

        drive_id = result.get("id", "未知")
        link = result.get("webViewLink", "")
        await msg.reply_text(
            f"✅ **圖片已暫存至 Google Drive**\n\n"
            f"• Drive ID：{drive_id}\n"
            + (f"• 在 Drive 中開啟: {link}\n" if link else "") +
            f"\n可以：\n"
            f"• 「描述這張圖片」（附上 Drive ID）\n"
            f"• 「把圖片改成卡通風格」（附上 Drive ID）\n"
            f"• 「識別圖片中的文字（OCR）」\n\n"
            f"💡 **貼士**：下次直接傳圖片時附帶說明文字，我可以立即處理，無需上傳 Drive！"
        )
    except Exception as e:
        logger.error(f"圖片上傳 Drive 失敗：{e}", exc_info=True)
        await msg.reply_text(
            f"❌ 上傳至 Drive 失敗：{str(e)[:100]}\n\n"
            f"💡 **貼士**：傳送圖片時附帶說明文字（如「改成卡通風格」），可直接處理，無需 Drive！"
        )



# ------------------------------------------------------------------
# 錯誤處理器與 Callback 處理器
# ------------------------------------------------------------------

async def handle_auth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理新用戶審批的 Callback Query"""
    query = update.callback_query
    await query.answer()
    
    admin_id = str(query.from_user.id)
    admin_ids_str = os.getenv("ADMIN_IDS", "")
    admin_ids = [x.strip() for x in admin_ids_str.split(",") if x.strip()]
    
    if admin_id not in admin_ids:
        await query.edit_message_text("⛔ 權限不足：你不是系統管理員，無法審批。")
        return
        
    data = query.data # "auth_approve_UID" or "auth_reject_UID"
    parts = data.split("_")
    action = parts[1] # "approve" or "reject"
    target_uid = parts[2]
    
    from database import AsyncSessionLocal, User
    from sqlalchemy import select
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == target_uid))
        user = result.scalar_one_or_none()
        
        if not user:
            await query.edit_message_text(f"{query.message.text}\n\n⚠️ 找不到該用戶 (ID: `{target_uid}`) 資料。", parse_mode="Markdown")
            return
            
        if user.status != "pending":
            status_map = {"approved": "已批准", "banned": "已拒絕"}
            await query.edit_message_text(f"{query.message.text}\n\nℹ️ 該用戶已被處理，目前狀態：**{status_map.get(user.status, user.status)}**", parse_mode="Markdown")
            return
            
        if action == "approve":
            user.status = "approved"
            msg_appendix = "✅ **已批准此用戶**"
            # 通知用戶
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text="🎉 **您的存取申請已獲批准！**\n\n現在您可以開始與 Nexus-OS 對話了。輸入 /help 查看可用功能。",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"通知用戶批准失敗: {e}")
        else:
            user.status = "banned"
            msg_appendix = "❌ **已拒絕此用戶**"
            # 通知用戶
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text="⛔ **您的存取申請已被拒絕。**\n\n如有疑問，請聯繫系統管理員。"
                )
            except Exception as e:
                logger.error(f"通知用戶拒絕失敗: {e}")
            
        await session.commit()
        
    await query.edit_message_text(f"{query.message.text}\n\n{msg_appendix}", parse_mode="Markdown")

async def handle_optimizer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理 Inline Keyboard 的點擊事件 (優化提案的批准或拒絕)"""
    query = update.callback_query
    await query.answer()
    
    if not _director:
        return
        
    user_id = str(query.from_user.id)
    admin_ids_str = os.getenv("ADMIN_IDS", "")
    admin_ids = [x.strip() for x in admin_ids_str.split(",") if x.strip()]
    
    if user_id not in admin_ids:
        await query.edit_message_text("⛔ 權限不足：你不是系統管理員，無法審批提案。")
        return

    from logic.optimizer_agent import OptimizerAgent
    optimizer: OptimizerAgent = _director._agent_registry.get("optimizer")
    
    if query.data == "opt_approve":
        await query.edit_message_text(f"{query.message.text}\n\n⏳ 正在執行優化中，請稍候...")
        result = await optimizer.apply_optimization()
        await query.edit_message_text(f"{query.message.text}\n\n**執行結果**：\n{result}", parse_mode="Markdown")
    elif query.data == "opt_reject":
        optimizer.pending_proposal = None
        await query.edit_message_text(f"{query.message.text}\n\n❌ **已拒絕套用此優化**", parse_mode="Markdown")

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全域錯誤處理，記錄日誌但不中斷 Bot 運行。"""
    logger.error(f"Bot 發生錯誤：{context.error}", exc_info=context.error)



# ------------------------------------------------------------------
# 主函式（同步入口，不用 asyncio.run()）
# ------------------------------------------------------------------

def main() -> None:
    """
    python-telegram-bot v20+ 正確啟動方式。

    run_polling() / run_webhook() 是同步方法，自行管理 event loop，
    絕對不能在 asyncio.run() 中呼叫或被 await。
    非同步初始化透過 post_init 回呼完成。
    """
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("請設定 TELEGRAM_BOT_TOKEN 環境變數")

    # 自定義更長的逾時（Zeabur 冷啟動可能有延遲）
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    # 建立 Application，注入 post_init 非同步初始化回呼
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)       # ← 非同步 DB + Director 初始化
        .build()
    )


    # 登記指令處理器
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("rollback", cmd_rollback))

    # 登記文字訊息處理器
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # 登記語音訊息處理器
    app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # 登記文件/圖片處理器（秘書 Agent 暫存至 Drive）
    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    # 登記 Callback Query 處理器
    app.add_handler(CallbackQueryHandler(handle_optimizer_callback, pattern="^opt_"))
    app.add_handler(CallbackQueryHandler(handle_auth_callback, pattern="^auth_"))

    # 全域錯誤處理
    app.add_error_handler(handle_error)

    # ── 啟動模式（同步呼叫，不用 await）──────────────────────────
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        logger.info(f"🌐 Webhook 模式啟動：{webhook_url}")
        app.run_webhook(                    # ← 同步呼叫，無 await
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
        )
    else:
        logger.info("🔄 Polling 模式啟動（本地開發 或 零停機部署）...")
        import time
        from telegram.error import Conflict
        
        while True:
            try:
                app.run_polling(                    # ← 同步呼叫，無 await
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )
                break  # Normal exit
            except Conflict:
                logger.warning("⚠️ 偵測到 Conflict 錯誤 (可能有其他實例正在運行)。5 秒後重試...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"❌ polling 發生非預期錯誤: {e}")
                time.sleep(5)


if __name__ == "__main__":
    main()              # ← 直接呼叫，無 asyncio.run()
