"""
logic/treasurer_agent.py — 內部財務官 Agent

負責分析系統自身的 Token 消耗與 API 成本。
資料來源為現有 database.py 中的 UsageLog 資料表。
它不負責個人記帳，而是系統監控工具。
"""

import asyncio
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, extract, func, desc

from database import AsyncSessionLocal, UsageLog

# 內建費率表 (USD per 1M tokens)
# 數值截至 2024–2025 Google AI Studio 公布定價
MODEL_RATES = {
    "gemini-3.1-pro-preview": {"input": 3.50, "output": 10.50},
    "gemini-3-pro-image-preview": {"input": 3.50, "output": 10.50},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.15, "output": 0.60},     # 假設與 2.5 flash 相同
    "gemini-2.0-flash-exp": {"input": 0.15, "output": 0.60},
    "gemini-1.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-embedding-001": {"input": 0.10, "output": 0.0},  # embedding 沒有 output token
}

# Imagen 生成圖片每次呼叫算作多少 USD
IMAGEN_COST_PER_IMAGE = Decimal("0.03")

class TreasurerAgent:
    """
    內部財務官 Agent，用於查詢系統 Token 使用量與預估成本。
    所有操作均讀取 UsageLog 資料表。
    """

    def __init__(self, agent_registry):
        self._registry = agent_registry

    async def get_daily_report(self, user_id: str, target_date: str = "today") -> str:
        """
        獲取特定日期的用量報告。
        """
        # 解析日期
        if target_date.lower() == "today":
            dt = datetime.now(timezone.utc).date()
        else:
            try:
                dt = datetime.strptime(target_date, "%Y-%m-%d").date()
            except ValueError:
                return f"❌ 日期格式錯誤。請使用 YYYY-MM-DD，目前輸入為：{target_date}"

        async with AsyncSessionLocal() as session:
            # 篩選當日資料 (注意 created_at 是 DateTime，需轉成 date 或範圍查詢)
            stmt = select(
                func.sum(UsageLog.prompt_tokens).label("in_tokens"),
                func.sum(UsageLog.completion_tokens).label("out_tokens"),
                func.sum(UsageLog.total_tokens).label("total"),
                func.count(UsageLog.id).label("requests")
            ).where(
                UsageLog.user_id == user_id,
                func.date(UsageLog.created_at) == dt
            )
            
            result = await session.execute(stmt)
            row = result.fetchone()

            if not row or not row.requests:
                return f"📅 {dt.strftime('%Y-%m-%d')} 沒有任何 API 呼叫記錄。"

            return (
                f"📊 **今日 ({dt.strftime('%Y-%m-%d')}) API 用量報告**\n"
                f"• 總呼叫次數：{row.requests} 次\n"
                f"• 輸入 Tokens：{row.in_tokens:,}\n"
                f"• 輸出 Tokens：{row.out_tokens:,}\n"
                f"• 總消耗量：{row.total:,} Tokens"
            )
        return ""

    async def get_monthly_report(self, user_id: str, year: int, month: int) -> str:
        """
        獲取某個月份的整體用量彙總。
        """
        async with AsyncSessionLocal() as session:
            stmt = select(
                func.sum(UsageLog.prompt_tokens).label("in_tokens"),
                func.sum(UsageLog.completion_tokens).label("out_tokens"),
                func.sum(UsageLog.total_tokens).label("total"),
                func.count(UsageLog.id).label("requests")
            ).where(
                UsageLog.user_id == user_id,
                extract('year', UsageLog.created_at) == year,
                extract('month', UsageLog.created_at) == month
            )
            
            result = await session.execute(stmt)
            row = result.fetchone()

            if not row or not row.requests:
                return f"📅 {year}年{month}月 沒有任何 API 呼叫記錄。"

            return (
                f"📊 **本月 ({year}/{month:02d}) API 用量總結**\n"
                f"• 總呼叫次數：{row.requests} 次\n"
                f"• 輸入 Tokens：{row.in_tokens:,}\n"
                f"• 輸出 Tokens：{row.out_tokens:,}\n"
                f"• 總消耗量：{row.total:,} Tokens\n\n"
                f"💡 想要知道預估成本，可要求「計算本月成本」。"
            )
        return ""

    async def get_cost_estimate(self, user_id: str, period: str = "current_month") -> str:
        """
        按模型計算成本。
        period: "today" | "current_month" | "all_time"
        """
        now = datetime.now(timezone.utc)
        
        async with AsyncSessionLocal() as session:
            stmt = select(
                UsageLog.model,
                func.sum(UsageLog.prompt_tokens).label("in_tokens"),
                func.sum(UsageLog.completion_tokens).label("out_tokens"),
                func.count(UsageLog.id).label("requests")
            ).where(
                UsageLog.user_id == user_id
            ).group_by(UsageLog.model)

            if period == "today":
                stmt = stmt.where(func.date(UsageLog.created_at) == now.date())
                period_str = "今日"
            elif period == "current_month":
                stmt = stmt.where(
                    extract('year', UsageLog.created_at) == now.year,
                    extract('month', UsageLog.created_at) == now.month
                )
                period_str = f"本月 ({now.year}/{now.month:02d})"
            else:
                period_str = "歷史總計"

            result = await session.execute(stmt)
            rows = result.fetchall()

            if not rows:
                return f"🧾 {period_str} 沒有產生任何 API 成本。"

            total_cost = Decimal("0.0")
            report_lines = [f"💰 **{period_str} 預估 API 成本**\n"]

            for row in rows:
                model_name = row.model
                in_t = row.in_tokens or 0
                out_t = row.out_tokens or 0
                reqs = row.requests or 0

                cost = Decimal("0.0")
                if "imagen-4" in model_name:
                    cost = Decimal(reqs) * IMAGEN_COST_PER_IMAGE
                    report_lines.append(f"• `{model_name}`: ${cost:.4f} ({reqs} 張圖)")
                else:
                    rates = MODEL_RATES.get(model_name)
                    if not rates:
                         # 若無明確費率，假設最低的 flash 費率
                         rates = {"input": 0.15, "output": 0.60}
                    
                    in_cost = Decimal(in_t) / Decimal("1000000") * Decimal(str(rates["input"]))
                    out_cost = Decimal(out_t) / Decimal("1000000") * Decimal(str(rates["output"]))
                    cost = in_cost + out_cost
                    report_lines.append(f"• `{model_name}`: ${cost:.4f}")
                
                total_cost += cost

            report_lines.append(f"\n💵 **總計：約 ${total_cost:.4f} USD**")
            report_lines.append("*(此為估算值，實際請以 Google Cloud 帳單為準)*")

            return "\n".join(report_lines)
        return ""

    async def get_top_tools(self, user_id: str, period: str = "current_month") -> str:
        """
        統計最常被呼叫的工具。
        """
        now = datetime.now(timezone.utc)
        
        async with AsyncSessionLocal() as session:
            stmt = select(
                UsageLog.tool_called,
                func.count(UsageLog.id).label("calls")
            ).where(
                UsageLog.user_id == user_id,
                UsageLog.tool_called.isnot(None)
            ).group_by(UsageLog.tool_called).order_by(desc("calls"))

            if period == "today":
                stmt = stmt.where(func.date(UsageLog.created_at) == now.date())
                period_str = "今日"
            elif period == "current_month":
                stmt = stmt.where(
                    extract('year', UsageLog.created_at) == now.year,
                    extract('month', UsageLog.created_at) == now.month
                )
                period_str = "本月"
            else:
                period_str = "歷史"

            result = await session.execute(stmt)
            rows = result.fetchall()

            if not rows:
                return f"🛠️ {period_str} 沒有工具呼叫記錄。"

            report_lines = [f"🏆 **{period_str} 最常用 Agent 排行**\n"]
            for idx, row in enumerate(rows, 1):
                report_lines.append(f"{idx}. `{row.tool_called}`: {row.calls} 次")

            return "\n".join(report_lines)
        return ""

    async def get_model_breakdown(self, user_id: str, period: str = "current_month") -> str:
        """
        各模型 Token 分佈。
        """
        now = datetime.now(timezone.utc)
        
        async with AsyncSessionLocal() as session:
            stmt = select(
                UsageLog.model,
                func.sum(UsageLog.total_tokens).label("total")
            ).where(
                UsageLog.user_id == user_id
            ).group_by(UsageLog.model).order_by(desc("total"))

            if period == "today":
                stmt = stmt.where(func.date(UsageLog.created_at) == now.date())
                period_str = "今日"
            elif period == "current_month":
                stmt = stmt.where(
                    extract('year', UsageLog.created_at) == now.year,
                    extract('month', UsageLog.created_at) == now.month
                )
                period_str = "本月"
            else:
                period_str = "歷史"

            result = await session.execute(stmt)
            rows = result.fetchall()

            if not rows:
                return f"🤖 {period_str} 沒有模型使用記錄。"

            total_all = sum([r.total or 0 for r in rows])
            
            report_lines = [f"🤖 **{period_str} 各模型用量分佈** (總額: {total_all:,})\n"]
            for row in rows:
                cnt = row.total or 0
                pct = (cnt / total_all * 100) if total_all else 0
                report_lines.append(f"• `{row.model}`: {cnt:,} ({pct:.1f}%)")

            return "\n".join(report_lines)
        return ""
