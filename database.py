"""
database.py — Nexus-OS v2.0 資料庫層
負責初始化 PostgreSQL 連線、定義 ORM 模型（MemoryStore, UsageLog, ReminderStore, AuditLog）
並提供 init_db() 啟動函式。
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# 載入環境變數
load_dotenv()

# ------------------------------------------------------------------
# 資料庫引擎設定
# ------------------------------------------------------------------

def _resolve_db_url() -> str:
    """
    自動解析資料庫連線字串。
    覆蓋 Zeabur / Render / Railway 等各平台的環境變數命名慣例。
    並自動將 postgresql:// 轉換為 postgresql+asyncpg://（asyncpg 驅動要求）
    """
    # 按優先順序嘗試所有可能的變數名
    raw = (
        os.getenv("DATABASE_URL")           # 手動設定的標準名
        or os.getenv("POSTGRES_URI")        # Zeabur 自動注入（用戶手動設定）
        or os.getenv("POSTGRESQL_URI")      # Zeabur 另一種格式
        or os.getenv("POSTGRES_URL")        # Render / Railway
        or os.getenv("POSTGRESQL_URL")      # 另一種常見格式
        or os.getenv("DB_URL")              # 其他平台
        or "postgresql+asyncpg://user:password@localhost:5432/nexus_os"
    )
    # asyncpg 需要 postgresql+asyncpg:// 前綴
    if raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)

    # 啟動時印出解析到的 host（密碼遮蔽，方便 debug）
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw)
        masked = f"{parsed.scheme}://***@{parsed.hostname}:{parsed.port}{parsed.path}"
        print(f"[DB] 已解析連線目標：{masked}")
    except Exception:
        pass
    return raw

DATABASE_URL = _resolve_db_url()

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))

engine = create_async_engine(
    DATABASE_URL,
    echo=False,           # 生產環境設為 False，除錯時可改 True
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,   # 確保連線存活
)

# 非同步 Session 工廠
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ------------------------------------------------------------------
# ORM 基礎類別
# ------------------------------------------------------------------

class Base(DeclarativeBase):
    """所有 ORM 模型的基礎類別。"""
    pass


# ------------------------------------------------------------------
# 模型：MemoryStore（對話記憶 + 向量嵌入）
# ------------------------------------------------------------------

class MemoryStore(Base):
    """
    儲存對話歷史與向量嵌入，用於大腦在回覆前進行語義相似度檢索。

    每條記錄代表一輪對話中的單一訊息（user 或 assistant）。
    embedding 欄位儲存由 Gemini text-embedding-004 生成的 768 維向量。
    """

    __tablename__ = "memory_store_v3"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="主鍵")
    user_id = Column(String(128), nullable=False, index=True, comment="用戶唯一識別碼")
    session_id = Column(String(128), nullable=False, index=True, comment="對話 Session ID")
    role = Column(String(16), nullable=False, comment="訊息角色：user 或 assistant")
    content = Column(Text, nullable=False, comment="訊息內容（原始文字）")
    embedding = Column(
        Vector(EMBEDDING_DIM),
        nullable=True,
        comment=f"向量嵌入（{EMBEDDING_DIM} 維）",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="記錄建立時間",
    )


# ------------------------------------------------------------------
# 模型：UsageLog（Token 消耗追蹤）
# ------------------------------------------------------------------

class UsageLog(Base):
    """
    追蹤每次 Gemini API 呼叫的 Token 消耗，用於成本監控與用量分析。

    tool_called 記錄本次呼叫是否觸發了工具（如 google_calendar_tool），
    方便日後統計各 Agent 的使用頻率。
    """

    __tablename__ = "usage_log_v3"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="主鍵")
    user_id = Column(String(128), nullable=False, index=True, comment="用戶唯一識別碼")
    session_id = Column(String(128), nullable=True, comment="對話 Session ID")
    model = Column(String(64), nullable=False, comment="使用的模型名稱（如 gemini-2.0-flash）")
    prompt_tokens = Column(Integer, nullable=False, default=0, comment="輸入 Token 數量")
    completion_tokens = Column(Integer, nullable=False, default=0, comment="輸出 Token 數量")
    total_tokens = Column(Integer, nullable=False, default=0, comment="總 Token 數量")
    tool_called = Column(String(64), nullable=True, comment="本次呼叫觸發的工具名稱（若無則為 None）")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="記錄建立時間",
    )


# ------------------------------------------------------------------
# 模型：User（多用戶身份與審批管理）
# ------------------------------------------------------------------

class User(Base):
    """
    儲存透過 Telegram Login Widget 登入的用戶資料。
    status: pending（等待審批）/ approved（已批准）/ banned（已封禁）
    """

    __tablename__ = "users_v1"

    telegram_id   = Column(String(32), primary_key=True, comment="Telegram 用戶 ID")
    username      = Column(String(128), nullable=True, comment="Telegram 用戶名")
    first_name    = Column(String(128), nullable=True, comment="Telegram 名字")
    status        = Column(String(16), nullable=False, default="pending", comment="pending/approved/banned")
    token_quota   = Column(Integer, nullable=False, default=100000, comment="每月 Token 配額")
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ------------------------------------------------------------------
# 模型：UserGoogleCredential（Google Calendar OAuth Token）
# ------------------------------------------------------------------

class UserGoogleCredential(Base):
    """
    儲存每個用戶的 Google OAuth2 refresh_token（Fernet 加密）。
    用於讓 CalendarAgent 以用戶身份存取其 Google Calendar。
    """

    __tablename__ = "user_google_credentials_v1"

    telegram_id    = Column(String(32), primary_key=True, comment="Telegram 用戶 ID（外鍵關聯 users_v1）")
    refresh_token  = Column(Text, nullable=False, comment="Fernet 加密的 Google refresh_token")
    calendar_id    = Column(String(256), nullable=False, default="primary", comment="Google Calendar ID")
    connected_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ------------------------------------------------------------------
# 模型：ReminderStore（排程師任務持久化）
# ------------------------------------------------------------------

class ReminderStore(Base):
    """
    儲存排程師 Agent（ReminderAgent）的定時任務，確保重啟後任務不丟失。

    job_type 說明：
        remind           — 單次/週期純文字提醒
        calendar_summary — 定時從日程師取得行程後推播
        weather_report   — 定時從天氣師取得天氣後推播
    """

    __tablename__ = "reminder_store_v1"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True, comment="Telegram User ID")
    chat_id = Column(String(128), nullable=False, comment="推播目標 Chat ID")
    job_id = Column(String(64), nullable=False, unique=True, index=True, comment="APScheduler Job ID")
    job_type = Column(String(32), nullable=False, comment="任務類型：remind / calendar_summary / weather_report")
    cron_expr = Column(String(64), nullable=True, comment="週期任務 Cron 表達式（如 0 8 * * *）")
    remind_at = Column(DateTime(timezone=True), nullable=True, comment="單次任務觸發時間")
    message = Column(Text, nullable=True, comment="提醒文字（remind 類型用）")
    is_active = Column(Boolean, nullable=False, default=True, comment="是否啟用")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="建立時間",
    )


# ------------------------------------------------------------------
# 模型：CrawlCache（爬虫師預取快取）
# ------------------------------------------------------------------

class CrawlCache(Base):
    """
    儲存爬虫師定期預取的熱門話題結果。

    爬虫師每 2 小時在背景刷新一次，用戶查詢時直接讀此表，
    避免即時抓取的延遲（3–10 秒 → 毫秒級）。

    platform 可為：lihkg / threads / facebook / all
    """

    __tablename__ = "crawl_cache_v1"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform = Column(
        String(32), nullable=False, index=True,
        comment="平台識別：lihkg / threads / facebook / all",
    )
    content = Column(Text, nullable=False, comment="預取結果（格式化文字或 JSON）")
    fetched_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="資料抓取時間（用於判斷快取是否過期）",
    )


# ------------------------------------------------------------------
# 模型：AuditLog（監控層評估週期快照）
# ------------------------------------------------------------------

class AuditLog(Base):
    """
    儲存 AuditorAgent 每次評估週期的結果快照。

    AuditorAgent 每 6 小時執行一次 evaluate_cycle()，將分析結果
    寫入此表，提供管理員效率監控與趨勢分析。

    efficiency_score：0–100 綜合評分，低於 60 時觸發優化提案。
    """

    __tablename__ = "audit_log_v1"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cycle_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="評估週期執行時間",
    )
    total_tokens_7d = Column(Integer, nullable=False, default=0, comment="過去 7 天累計 Token 消耗")
    avg_latency_ms = Column(Integer, nullable=False, default=0, comment="平均回應延遲（毫秒）")
    tool_call_breakdown = Column(Text, nullable=True, comment="各 Agent 工具呼叫次數（JSON）")
    efficiency_score = Column(Integer, nullable=False, default=100, comment="效率評分 0–100")
    anomalies = Column(Text, nullable=True, comment="偵測到的異常描述")
    proposal_sent = Column(Boolean, nullable=False, default=False, comment="本次週期是否發送優化提案")
    raw_report = Column(Text, nullable=True, comment="Auditor 完整 JSON 報告")


# ------------------------------------------------------------------
# 資料庫初始化
# ------------------------------------------------------------------

async def init_db() -> None:
    """
    初始化資料庫：
    1. 啟用 pgvector 擴充（CREATE EXTENSION IF NOT EXISTS vector）
    2. 建立所有 ORM 模型對應的資料表（若不存在）

    此函式應在應用程式啟動時呼叫（FastAPI lifespan 事件）。
    """
    async with engine.begin() as conn:
        # 嘗試啟用 pgvector 擴充（若 Zeabur PostgreSQL 不支援則跳過）
        try:
            await conn.execute(
                __import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS vector")
            )
        except Exception as e:
            print(f"⚠️ pgvector 擴充無法啟用（將以純文字記憶模式運行）：{e}")
        # 根據 ORM 模型建立資料表（不會自動新增欄位）
        await conn.run_sync(Base.metadata.create_all)

    print("✅ 資料庫初始化完成（MemoryStore、UsageLog、ReminderStore、CrawlCache、User、UserGoogleCredential、AuditLog 資料表已就緒）")




# ------------------------------------------------------------------
# 工具函式：取得非同步 Session
# ------------------------------------------------------------------

async def get_session() -> AsyncSession:
    """
    提供一個非同步資料庫 Session，適用於 FastAPI 依賴注入。

    使用方式：
        async with get_session() as session:
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
