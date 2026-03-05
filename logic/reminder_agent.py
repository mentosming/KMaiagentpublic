"""
logic/reminder_agent.py — 排程師 Agent

使用 APScheduler AsyncIOScheduler 管理定時任務，並透過 send_message_fn
在觸發時向用戶推播 Telegram 訊息。

支援 job_type：
    remind           — 單次或週期純文字提醒
    calendar_summary — 觸發時查詢日程師，推播今日行程
    weather_report   — 觸發時查詢天氣師，推播天氣摘要
    crawler_trending — 觸發時呼叫爬虫師，推播 LIHKG+Threads+Facebook 熱門話題

架構規則：
    符合 AgentRegistry 統一建構簽名 __init__(self, registry, ...)
    透過 registry.get("calendar") / registry.get("weather") 協作其他 Agent。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

load_dotenv()

# 型別別名：推播函式 (chat_id, text) -> None
SendFn = Callable[[str, str], Awaitable[None]]


class ReminderAgent:
    """
    排程師 Agent — 管理定時提醒並支援 Agent 間協作。

    支援兩種觸發模式：
      - 單次：指定 remind_at datetime
      - 週期：指定 cron_expr（如 「0 8 * * *」= 每天早上8點）
    """

    def __init__(
        self,
        registry: "AgentRegistry",
        send_message_fn: SendFn | None = None,
    ) -> None:
        """
        初始化排程師。

        參數：
            registry        - AgentRegistry 實例
            send_message_fn - 推播函式 async (chat_id, text) -> None
        """
        self._registry = registry
        self._send_fn = send_message_fn
        self._scheduler = AsyncIOScheduler(timezone="Asia/Hong_Kong")
        self._scheduler.start()
        # 啟動時從 DB 重載已存的排程（在外部呼叫 restore_from_db()）

    # ------------------------------------------------------------------
    # 公開方法：新增提醒
    # ------------------------------------------------------------------

    async def add_reminder(
        self,
        user_id: str,
        chat_id: str,
        job_type: str,
        message: str = "",
        remind_at: datetime | None = None,
        cron_expr: str | None = None,
    ) -> dict[str, Any]:
        """
        新增一個排程任務並存入 DB。

        參數：
            user_id    - Telegram User ID
            chat_id    - Telegram Chat ID（推播目標）
            job_type   - 任務類型：remind / calendar_summary / weather_report
            message    - 提醒文字（remind 類型用）
            remind_at  - 單次觸發時間
            cron_expr  - 週期 Cron 表達式（如「0 8 * * *」）

        回傳：{job_id, job_type, remind_at, cron_expr, message}
        """
        job_id = str(uuid.uuid4())[:16]

        # 加入 APScheduler
        self._schedule_job(
            job_id=job_id,
            chat_id=chat_id,
            job_type=job_type,
            message=message,
            remind_at=remind_at,
            cron_expr=cron_expr,
        )

        # 存入 DB
        await self._save_to_db(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            job_type=job_type,
            message=message,
            remind_at=remind_at,
            cron_expr=cron_expr,
        )

        return {
            "job_id": job_id,
            "job_type": job_type,
            "remind_at": remind_at.isoformat() if remind_at else None,
            "cron_expr": cron_expr,
            "message": message,
        }

    # ------------------------------------------------------------------
    # 公開方法：查詢提醒
    # ------------------------------------------------------------------

    async def list_reminders(self, user_id: str) -> list[dict]:
        """查詢用戶的所有有效提醒。"""
        from database import AsyncSessionLocal, ReminderStore
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReminderStore)
                .where(
                    ReminderStore.user_id == user_id,
                    ReminderStore.is_active == True,  # noqa: E712
                )
                .order_by(ReminderStore.created_at.desc())
            )
            rows = result.scalars().all()

        return [
            {
                "job_id": r.job_id,
                "job_type": r.job_type,
                "remind_at": r.remind_at.isoformat() if r.remind_at else None,
                "cron_expr": r.cron_expr,
                "message": r.message,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 公開方法：取消提醒
    # ------------------------------------------------------------------

    async def cancel_reminder(self, job_id: str) -> bool:
        """
        取消指定排程並在 DB 標記為失效。

        回傳：True 代表成功，False 代表找不到
        """
        # 移除 APScheduler job
        job = self._scheduler.get_job(job_id)
        if job:
            job.remove()

        # 更新 DB
        from database import AsyncSessionLocal, ReminderStore
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReminderStore).where(ReminderStore.job_id == job_id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.is_active = False
                await session.commit()
                return True
        return False

    # ------------------------------------------------------------------
    # 公開方法：從 DB 重載排程（重啟持久化）
    # ------------------------------------------------------------------

    async def restore_from_db(self) -> None:
        """
        應用程式啟動時呼叫，從 DB 重載所有未觸發的排程。
        確保 Bot 重啟後提醒仍然有效。
        """
        from database import AsyncSessionLocal, ReminderStore
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReminderStore).where(ReminderStore.is_active == True)  # noqa: E712
            )
            rows = result.scalars().all()

        now = datetime.now(tz=timezone(timedelta(hours=8)))
        restored = 0
        for row in rows:
            # 單次任務：若已過期則標記失效
            if row.remind_at and row.remind_at < now and not row.cron_expr:
                await self.cancel_reminder(row.job_id)
                continue
            self._schedule_job(
                job_id=row.job_id,
                chat_id=row.chat_id,
                job_type=row.job_type,
                message=row.message or "",
                remind_at=row.remind_at,
                cron_expr=row.cron_expr,
            )
            restored += 1

        print(f"📅 排程師：已從 DB 重載 {restored} 個排程任務")

    # ------------------------------------------------------------------
    # 私有方法：將任務加入 APScheduler
    # ------------------------------------------------------------------

    def _schedule_job(
        self,
        job_id: str,
        chat_id: str,
        job_type: str,
        message: str,
        remind_at: datetime | None,
        cron_expr: str | None,
    ) -> None:
        """根據觸發類型選擇 DateTrigger 或 CronTrigger 加入排程。"""
        if cron_expr:
            trigger = CronTrigger.from_crontab(cron_expr, timezone="Asia/Hong_Kong")
        elif remind_at:
            trigger = DateTrigger(run_date=remind_at, timezone="Asia/Hong_Kong")
        else:
            raise ValueError("必須提供 remind_at 或 cron_expr 其中之一")

        self._scheduler.add_job(
            func=self._trigger_job,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            kwargs={
                "job_id": job_id,
                "chat_id": chat_id,
                "job_type": job_type,
                "message": message,
            },
        )

    # ------------------------------------------------------------------
    # 私有方法：任務觸發（含 Agent 間協作）
    # ------------------------------------------------------------------

    async def _trigger_job(
        self,
        job_id: str,
        chat_id: str,
        job_type: str,
        message: str,
    ) -> None:
        """
        任務觸發時的回呼函式。
        根據 job_type 決定是否需要向其他 Agent 查詢資料。
        """
        if not self._send_fn:
            print(f"⚠️ 排程師觸發 {job_id} 但 send_message_fn 未設定")
            return

        try:
            if job_type == "remind":
                text = f"⏰ **提醒**\n\n{message}"

            elif job_type == "calendar_summary":
                # 向日程師查詢今日行程
                calendar = self._registry.get("calendar")
                now_hk = datetime.now(tz=timezone(timedelta(hours=8)))
                end_of_day = now_hk.replace(hour=23, minute=59, second=59)
                events = await calendar.list_events(
                    time_min=now_hk, time_max=end_of_day
                )
                if events:
                    from logic.calendar_agent import CalendarAgent
                    event_lines = [CalendarAgent.format_event(e) for e in events]
                    text = (
                        f"📅 **今日行程**（{now_hk.strftime('%m月%d日')}）\n\n"
                        + "\n\n".join(event_lines)
                    )
                else:
                    text = f"📅 今日（{now_hk.strftime('%m月%d日')}）沒有行程安排，可以輕鬆一天！☀️"

            elif job_type == "weather_report":
                # 向天氣師查詢天氣
                weather = self._registry.get("weather")
                text = await weather.get_weather()

            elif job_type == "crawler_trending":
                # 向爬虫師查詢三平台熱門話題
                crawler = self._registry.get("crawler")
                text = await crawler.get_cached_trending(platform="all")

            elif job_type == "finance_report":
                # 向金融分析師查詢大盤概況
                from logic.finance_agent import FinanceAgent
                finance: FinanceAgent = self._registry.get("finance")
                # message 欄可選地存放自訂 watchlist，格式：stocks=AAPL,TSLA;cryptos=bitcoin,ethereum
                stocks_wl = ""
                cryptos_wl = ""
                if message:
                    for seg in message.split(";"):
                        if seg.startswith("stocks="):
                            stocks_wl = seg[7:]
                        elif seg.startswith("cryptos="):
                            cryptos_wl = seg[8:]
                text = await finance.get_market_overview(
                    watchlist_stocks=stocks_wl,
                    watchlist_cryptos=cryptos_wl,
                )

            else:
                text = f"⏰ 排程觸發（未知類型：{job_type}）"

            await self._send_fn(chat_id, text)

            # 單次任務觸發後標記失效
            await self._mark_fired(job_id)

        except Exception as e:
            print(f"❌ 排程師觸發失敗 job_id={job_id}: {e}")

    # ------------------------------------------------------------------
    # 私有方法：DB 操作
    # ------------------------------------------------------------------

    @staticmethod
    async def _save_to_db(
        job_id: str,
        user_id: str,
        chat_id: str,
        job_type: str,
        message: str,
        remind_at: datetime | None,
        cron_expr: str | None,
    ) -> None:
        from database import AsyncSessionLocal, ReminderStore

        record = ReminderStore(
            user_id=user_id,
            chat_id=chat_id,
            job_id=job_id,
            job_type=job_type,
            message=message,
            remind_at=remind_at,
            cron_expr=cron_expr,
            is_active=True,
        )
        async with AsyncSessionLocal() as session:
            session.add(record)
            await session.commit()

    @staticmethod
    async def _mark_fired(job_id: str) -> None:
        """將單次任務標記為 is_active=False（已觸發）。"""
        from database import AsyncSessionLocal, ReminderStore
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReminderStore).where(ReminderStore.job_id == job_id)
            )
            row = result.scalar_one_or_none()
            if row and not row.cron_expr:  # 週期任務不標記失效
                row.is_active = False
                await session.commit()
