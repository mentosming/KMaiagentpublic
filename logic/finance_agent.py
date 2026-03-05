"""
logic/finance_agent.py — 金融分析師 Agent

職責：
1. 查詢美股即時報價（via yfinance / Yahoo Finance）
2. 計算股票技術指標（MA5/MA20/MA50、52週高低）
3. 查詢加密貨幣即時行情（via CoinGecko 免費 API）
4. 提供整體大盤 + 主流幣快覽
5. 支援排程師定時推播（finance_report job_type）

資料來源：
  - 美股：yfinance（Yahoo Finance，免費，不需 API Key）
  - 加密幣：CoinGecko REST API v3（免費，限速 30 req/min）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

# CoinGecko 免費 API 基礎 URL
_COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# 大盤快覽預設標的
_DEFAULT_STOCKS = ["^GSPC", "^IXIC", "^DJI"]   # S&P500, NASDAQ, Dow Jones
_DEFAULT_STOCKS_LABELS = ["標普500", "納斯達克", "道瓊斯"]
_DEFAULT_CRYPTOS = ["bitcoin", "ethereum", "solana"]


class FinanceAgent:
    """
    金融分析師 Agent — 查詢美股及加密貨幣行情。

    由 Director 透過 finance_tool 調用，
    亦可由 ReminderAgent 在 finance_report 排程中調用。
    """

    def __init__(self, registry: "AgentRegistry") -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # 公開方法：美股報價
    # ------------------------------------------------------------------

    async def get_stock_quote(self, symbol: str) -> str:
        """
        查詢單一美股即時報價（價格、漲跌、成交量、市值、PE）。
        symbol 例：AAPL / NVDA / TSLA / MSFT
        """
        loop = asyncio.get_event_loop()
        try:
            import yfinance as yf
            ticker = await loop.run_in_executor(None, lambda: yf.Ticker(symbol))
            info = await loop.run_in_executor(None, lambda: ticker.info)

            name = info.get("longName") or info.get("shortName") or symbol
            price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            prev_close = info.get("previousClose", price)
            change = price - prev_close
            pct = (change / prev_close * 100) if prev_close else 0
            arrow = "🟢" if change >= 0 else "🔴"
            sign = "+" if change >= 0 else ""

            market_cap = info.get("marketCap", 0)
            mc_str = _fmt_large_num(market_cap)
            pe = info.get("trailingPE")
            pe_str = f"PE: {pe:.1f}" if pe else "PE: N/A"

            vol = info.get("volume", 0)
            vol_str = _fmt_large_num(vol)

            currency = info.get("currency", "USD")
            now_str = datetime.now(tz=timezone(timedelta(hours=-5))).strftime("%m/%d %H:%M ET")

            return (
                f"📊 **{name}** ({symbol.upper()})\n"
                f"{arrow} **{currency} {price:.2f}**  {sign}{change:.2f} ({sign}{pct:.2f}%)\n"
                f"市值：{mc_str}　{pe_str}\n"
                f"成交量：{vol_str}\n"
                f"⏱ {now_str}"
            )
        except Exception as e:
            return f"❌ 無法取得 {symbol} 報價：{e}"

    # ------------------------------------------------------------------
    # 公開方法：股票技術摘要
    # ------------------------------------------------------------------

    async def get_stock_summary(self, symbol: str, period: str = "3mo") -> str:
        """
        查詢股票技術分析摘要：MA5/MA20/MA50、52週高低、近期走勢。
        period: 1mo / 3mo / 6mo / 1y
        """
        loop = asyncio.get_event_loop()
        try:
            import yfinance as yf
            import pandas as pd
            ticker = await loop.run_in_executor(None, lambda: yf.Ticker(symbol))
            hist = await loop.run_in_executor(
                None, lambda: ticker.history(period=period)
            )
            if hist.empty:
                return f"❌ 找不到 {symbol} 的歷史數據"

            closes = hist["Close"]
            current = float(closes.iloc[-1])

            # 移動平均
            def ma(n: int) -> float | None:
                return float(closes.tail(n).mean()) if len(closes) >= n else None

            ma5 = ma(5)
            ma20 = ma(20)
            ma50 = ma(50)

            # 52週高低（用 1y 數據更準，但 period 可能 < 1y）
            info = await loop.run_in_executor(None, lambda: ticker.info)
            high52 = info.get("fiftyTwoWeekHigh")
            low52 = info.get("fiftyTwoWeekLow")

            # 近30日漲跌
            month_ago = float(closes.iloc[-min(20, len(closes))]) if len(closes) >= 2 else current
            month_chg_pct = (current - month_ago) / month_ago * 100

            name = info.get("longName") or info.get("shortName") or symbol
            currency = info.get("currency", "USD")

            lines = [f"📈 **{name}** ({symbol.upper()}) 技術摘要"]
            lines.append(f"現價：{currency} {current:.2f}")
            if ma5:
                lines.append(f"MA5：{ma5:.2f}  {'▲' if current > ma5 else '▼'}")
            if ma20:
                lines.append(f"MA20：{ma20:.2f}  {'▲' if current > ma20 else '▼'}")
            if ma50:
                lines.append(f"MA50：{ma50:.2f}  {'▲' if current > ma50 else '▼'}")
            if high52 and low52:
                from_high = (current - high52) / high52 * 100
                lines.append(f"52週高：{high52:.2f}（距高點 {from_high:.1f}%）")
                lines.append(f"52週低：{low52:.2f}")
            sign = "+" if month_chg_pct >= 0 else ""
            lines.append(f"近20交易日：{sign}{month_chg_pct:.2f}%")

            return "\n".join(lines)
        except Exception as e:
            return f"❌ 無法取得 {symbol} 技術數據：{e}"

    # ------------------------------------------------------------------
    # 公開方法：加密貨幣報價
    # ------------------------------------------------------------------

    async def get_crypto_quote(self, coin_id: str) -> str:
        """
        查詢加密貨幣即時報價。
        coin_id 例：bitcoin / ethereum / solana / dogecoin
        """
        try:
            url = f"{_COINGECKO_BASE}/simple/price"
            params = {
                "ids": coin_id,
                "vs_currencies": "usd,hkd",
                "include_24hr_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
            data = resp.json().get(coin_id)
            if not data:
                return f"❌ 找不到幣種：{coin_id}（請使用 CoinGecko ID，如 bitcoin）"

            usd = data.get("usd", 0)
            hkd = data.get("hkd", 0)
            chg = data.get("usd_24h_change", 0)
            mc = data.get("usd_market_cap", 0)
            vol = data.get("usd_24h_vol", 0)

            arrow = "🟢" if chg >= 0 else "🔴"
            sign = "+" if chg >= 0 else ""
            name = coin_id.capitalize()

            return (
                f"🪙 **{name}** ({coin_id.upper()})\n"
                f"{arrow} **USD {usd:,.2f}**  ({sign}{chg:.2f}%)\n"
                f"HKD {hkd:,.2f}\n"
                f"市值：{_fmt_large_num(mc)}\n"
                f"24h 成交量：{_fmt_large_num(vol)}"
            )
        except Exception as e:
            return f"❌ 無法取得 {coin_id} 行情：{e}"

    # ------------------------------------------------------------------
    # 公開方法：加密貨幣走勢摘要
    # ------------------------------------------------------------------

    async def get_crypto_summary(self, coin_id: str, days: int = 7) -> str:
        """
        查詢加密貨幣多天走勢摘要（最高/最低/現價/漲跌）。
        days: 7 / 14 / 30
        """
        try:
            url = f"{_COINGECKO_BASE}/coins/{coin_id}/market_chart"
            params = {"vs_currency": "usd", "days": days, "interval": "daily"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
            chart = resp.json()
            prices = [p[1] for p in chart.get("prices", [])]
            if not prices:
                return f"❌ 找不到 {coin_id} 的走勢數據"

            start = prices[0]
            end = prices[-1]
            high = max(prices)
            low = min(prices)
            total_chg = (end - start) / start * 100
            sign = "+" if total_chg >= 0 else ""

            return (
                f"📉 **{coin_id.capitalize()} {days} 天走勢**\n"
                f"開始：USD {start:,.2f}  →  現在：USD {end:,.2f}\n"
                f"漲跌：{sign}{total_chg:.2f}%\n"
                f"期間最高：USD {high:,.2f}\n"
                f"期間最低：USD {low:,.2f}"
            )
        except Exception as e:
            return f"❌ 無法取得 {coin_id} 走勢：{e}"

    # ------------------------------------------------------------------
    # 公開方法：整體大盤概況（排程推播也用此方法）
    # ------------------------------------------------------------------

    async def get_market_overview(self, watchlist_stocks: str = "", watchlist_cryptos: str = "") -> str:
        """
        同時查詢大盤指數 + 主流幣，生成一份快覽報告。
        watchlist_stocks: 逗號分隔的股票代碼（自訂）
        watchlist_cryptos: 逗號分隔的幣種 CoinGecko ID（自訂）
        """
        stocks = [s.strip() for s in watchlist_stocks.split(",") if s.strip()] or _DEFAULT_STOCKS
        labels = _DEFAULT_STOCKS_LABELS if stocks == _DEFAULT_STOCKS else stocks
        cryptos = [c.strip() for c in watchlist_cryptos.split(",") if c.strip()] or _DEFAULT_CRYPTOS

        now_str = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%m/%d %H:%M HKT")
        parts = [f"🌐 **市場概況** — {now_str}\n"]

        # 美股部分
        parts.append("**📊 美股指數**")
        loop = asyncio.get_event_loop()
        try:
            import yfinance as yf
            for sym, label in zip(stocks, labels):
                try:
                    tick = await loop.run_in_executor(None, lambda s=sym: yf.Ticker(s))
                    info = await loop.run_in_executor(None, lambda t=tick: t.info)
                    price = info.get("regularMarketPrice") or info.get("currentPrice", 0)
                    prev = info.get("previousClose", price)
                    chg = (price - prev) / prev * 100 if prev else 0
                    arrow = "🟢" if chg >= 0 else "🔴"
                    sign = "+" if chg >= 0 else ""
                    parts.append(f"{arrow} {label} ({sym}): {price:,.2f}  {sign}{chg:.2f}%")
                except Exception:
                    parts.append(f"⚠️ {label} ({sym}): 無法取得")
        except ImportError:
            parts.append("⚠️ yfinance 未安裝，無法查詢美股")

        # 加密幣部分
        parts.append("\n**🪙 加密貨幣**")
        try:
            ids = ",".join(cryptos)
            params = {
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{_COINGECKO_BASE}/simple/price", params=params)
            data = resp.json()
            for coin in cryptos:
                d = data.get(coin)
                if d:
                    usd = d.get("usd", 0)
                    chg = d.get("usd_24h_change", 0)
                    arrow = "🟢" if chg >= 0 else "🔴"
                    sign = "+" if chg >= 0 else ""
                    parts.append(f"{arrow} {coin.capitalize()}: USD {usd:,.2f}  {sign}{chg:.2f}%")
                else:
                    parts.append(f"⚠️ {coin}: 無法取得")
        except Exception as e:
            parts.append(f"⚠️ 無法取得加密幣數據：{e}")

        return "\n".join(parts)


# ------------------------------------------------------------------
# 私有工具函式
# ------------------------------------------------------------------

def _fmt_large_num(n: float) -> str:
    """將大數字格式化為 T/B/M 縮寫。"""
    if not n:
        return "N/A"
    if n >= 1e12:
        return f"${n/1e12:.2f}T"
    if n >= 1e9:
        return f"${n/1e9:.2f}B"
    if n >= 1e6:
        return f"${n/1e6:.2f}M"
    return f"${n:,.0f}"
