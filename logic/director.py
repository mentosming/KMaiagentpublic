"""
logic/director.py — 指揮官大腦（系統唯一決策中樞）

職責：
1. 接收所有用戶訊息，嚴禁外部繞過大腦直接呼叫工具
2. 基於 pgvector 語義相似度，檢索相關對話記憶作為上下文
3. 使用 Gemini API 進行語言理解與推理
4. 透過標準化工具調度接口（Function Calling）分派任務給各 Agent
5. 管理對話記憶持久化與 Token 消耗日誌

工具箱設計原則（易擴展）：
    - 新增 Agent 只需在 _register_tools() 中加入一行
    - Gemini tools 聲明自動從工具登記表生成
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import dateparser
import google.generativeai as genai
from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, MemoryStore, UsageLog
from logic.agent_registry import AgentRegistry
from logic.calendar_agent import CalendarAgent
from logic.crawler_agent import CrawlerAgent
from logic.image_artist_agent import ImageArtistAgent
from logic.reminder_agent import ReminderAgent
from logic.secretary_agent import SecretaryAgent
from logic.weather_agent import WeatherAgent
from logic.treasurer_agent import TreasurerAgent
from logic.finance_agent import FinanceAgent
from logic.optimizer_agent import OptimizerAgent
from logic.crypto_agent import CryptoAgent
from logic.auditor_agent import AuditorAgent
from logic.worker_bus import WorkerBus, TelemetryEvent

load_dotenv()

# ------------------------------------------------------------------
# 環境變數
# ------------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Flash 模型：快速 / 日常回覆（可用 GEMINI_MODEL_FLASH 覆蓋）
GEMINI_MODEL_FLASH = os.getenv("GEMINI_MODEL_FLASH", "gemini-3-flash-preview")
# Pro 模型：複雜問題 / 長文分析（可用 GEMINI_MODEL_PRO 覆蓋）
GEMINI_MODEL_PRO = os.getenv("GEMINI_MODEL_PRO", "gemini-3.1-pro-preview")
# 向下相容：若舊環境變數 GEMINI_MODEL 存在，同時覆蓋 flash 與 pro
if os.getenv("GEMINI_MODEL"):
    _legacy = os.environ["GEMINI_MODEL"]
    GEMINI_MODEL_FLASH = _legacy
    GEMINI_MODEL_PRO = _legacy
MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "5"))

genai.configure(api_key=GEMINI_API_KEY)


# ------------------------------------------------------------------
# 工具聲明（Function Calling Schema）
# ------------------------------------------------------------------
# 新增 Agent 時，在此區塊加入對應的 Tool Schema，
# 並在 Director._register_tools() 中登記處理函式。

_TOOL_SCHEMAS = [
    # ── 日程師工具 ─────────────────────────────────────────────────
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="google_calendar_tool",
                description="管理 Google Calendar 行事曆。可新增、查詢、更新或刪除活動。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：create（新增）、list（查詢）、update（更新）、delete（刪除）",
                        ),
                        "summary": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="活動標題（新增/更新時必填）",
                        ),
                        "start_time": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="活動開始時間，ISO 8601 格式（如 2026-03-04T15:00:00+08:00）",
                        ),
                        "end_time": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="活動結束時間，ISO 8601 格式（如 2026-03-04T16:00:00+08:00）",
                        ),
                        "description": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="活動備注或描述（可選）",
                        ),
                        "event_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="活動 ID（更新/刪除時必填）",
                        ),
                        "query": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="搜尋關鍵字（查詢時可選）",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
    # ── 未來 Agent 工具預留位置 ────────────────────────────────────
    # genai.protos.Tool(function_declarations=[
    #     genai.protos.FunctionDeclaration(
    #         name="secretary_tool",
    #         description="管理 Google Drive 文件、進行 OCR 圖片識別。",
    #         ...
    #     )
    # ]),
    # genai.protos.Tool(function_declarations=[
    #     genai.protos.FunctionDeclaration(
    #         name="architect_tool",
    #         description="生成 3D 建模腳本或場景描述。",
    #         ...
    #     )
    # ]),
    # genai.protos.Tool(function_declarations=[
    #     genai.protos.FunctionDeclaration(
    #         name="treasurer_tool",
    #         description="追蹤支出、生成財務報告。",
    #         ...
    #     )
    # ]),
]

# ── 天氣師 + 排程師 Tool Schemas ─────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="weather_tool",
                description="查詢指定地點的當前天氣與今日預報（溫度、降雨、風速等）。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "location": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="地點名稱（如 '香港'、'九龍'），預設使用系統設定地點",
                        ),
                    },
                    required=[],
                ),
            )
        ]
    ),
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="scheduler_tool",
                description="管理定時提醒。可設定一次性提醒、週期通知（每日行程摘要、天氣報告等）。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作：add（新增）、list（查詢）、cancel（取消）",
                        ),
                        "job_type": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="任務類型：remind / calendar_summary / weather_report / crawler_trending",
                        ),
                        "remind_at": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="單次觸發時間，ISO 8601（如 2026-03-01T08:00:00+08:00）",
                        ),
                        "cron_expr": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="週期 Cron 表達式（如每天早上8點：0 8 * * *）",
                        ),
                        "message": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="提醒文字（remind 類型用）",
                        ),
                        "job_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="取消排程時必填的 Job ID",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 秘書 Tool Schema ────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="secretary_tool",
                description="管理 Google Drive：列出、搜尋、讀取、建立文件，將 Drive 文件傳送給用戶，或進行圖片 OCR。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作：list/search/read/create/send_to_me/ocr",
                        ),
                        "file_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="Drive 文件 ID（read、send_to_me、ocr 必填）",
                        ),
                        "query": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="搜尋關鍵字",
                        ),
                        "name": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="建立文件名稱",
                        ),
                        "content": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="建立文件內容",
                        ),
                        "folder_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="目標資料夾 ID（可選）",
                        ),
                        "caption": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="傳送文件時附帶的說明文字",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 爬虫師 Tool Schema ─────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="crawler_tool",
                description="網頁爬取與搜尋：搜尋網頁（search）、搜尋即時新聞（search_news）、抓取 URL 內容（fetch）、AI 歸納網頁（summarize）、獲取熱門話題（trending）、提取連結（extract_links）。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作：search（一般搜尋）/ search_news（Google News 即時新聞，適合查最新資訊）/ fetch（抓取網頁）/ summarize（AI 歸納）/ trending（熱門話題）/ extract_links（提取連結）",
                        ),
                        "url": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="目標 URL（fetch/summarize/extract_links 必填）",
                        ),
                        "query": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="搜尋關鍵字（search 必填）",
                        ),
                        "platform": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="熱門話題平台：lihkg/threads/facebook/all（trending 用）",
                        ),
                        "prompt": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="Gemini 詮釋提示（summarize 用）",
                        ),
                        "max_results": genai.protos.Schema(
                            type=genai.protos.Type.INTEGER,
                            description="返回筆數上限（預設 8）",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 圖片師 Tool Schema ─────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="image_artist_tool",
                description="圖片師：生成圖片（generate）、改造圖片（transform）、描述圖片（describe）。生成圖片時根據用戶的描述創作全新圖片；改造圖片時需提供 Drive 上的圖片 ID 與改造指示；描述圖片時分析圖片內容。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作：generate（文字生成圖片）/ transform（改造圖片）/ describe（描述圖片）",
                        ),
                        "prompt": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="圖片描述或生成指示（generate/transform 必填）",
                        ),
                        "aspect_ratio": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="長寬比：1:1（預設）/ 16:9 / 4:3 / 3:4 / 9:16",
                        ),
                        "drive_file_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="Google Drive 圖片 ID（transform/describe 時可選，若用戶已上傳圖片至 Drive 則填入）",
                        ),
                        "instruction": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="改造指示（transform 必填，如：改成卡通風格、把背景換成海灘）",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 內部財務官 Tool Schema ────────────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="treasurer_tool",
                description="內部財務官：查詢系統自身的 API Token 消耗量、成本估算、最常用 Agent 排行、模型使用分佈。僅用於系統內部監控，非個人記帳。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：'daily' (當日用量), 'monthly' (本月總結), 'cost' (成本估算), 'tools' (最常用Agent), 'models' (模型分佈)",
                        ),
                        "period": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="統計區間：'today' (今日), 'current_month' (本月), 'all_time' (歷史總計)。預設為 current_month。",
                        ),
                        "date": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="用於 daily 操作的特定日期，格式 YYYY-MM-DD，預設為 today",
                        ),
                        "year": genai.protos.Schema(
                            type=genai.protos.Type.INTEGER,
                            description="用於 monthly 操作的特定年份，預設為當前年份",
                        ),
                        "month": genai.protos.Schema(
                            type=genai.protos.Type.INTEGER,
                            description="用於 monthly 操作的特定月份 (1-12)，預設為當前月份",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 金融分析師 Tool Schema ──────────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="finance_tool",
                description="金融分析師：查詢美股即時報價、技術分析、加密貨幣報價、幣種走勢、整體大盤概況。資料來自 Yahoo Finance 及 CoinGecko。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：stock_quote / stock_summary / crypto_quote / crypto_summary / market_overview",
                        ),
                        "symbol": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="美股代碼，如 AAPL、NVDA、TSLA、^GSPC（stock_quote 與 stock_summary 需事先填入）",
                        ),
                        "period": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="股票歷史區間：1mo / 3mo / 6mo / 1y，預設 3mo",
                        ),
                        "coin_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="CoinGecko 幣種 ID，如 bitcoin、ethereum、solana（crypto_quote 與 crypto_summary 需事先填入）",
                        ),
                        "days": genai.protos.Schema(
                            type=genai.protos.Type.INTEGER,
                            description="幣種走勢天數：7 / 14 / 30，預設 7",
                        ),
                        "watchlist_stocks": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="大盤概況用：自訂股票代碼，逗號分隔，如 AAPL,TSLA,MSFT",
                        ),
                        "watchlist_cryptos": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="大盤概況用：自訂幣種 ID，逗號分隔，如 bitcoin,ethereum,solana",
                        ),
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 系統優化師 Tool Schema ──────────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="optimizer_tool",
                description="系統優化師：分析日誌與程式碼尋找優化空間，或強制還原最新修改。當遇到持續錯誤、或需重構程式碼以提高效能時使用。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：analyze (分析系統並提出優化提案) / rollback (撤銷並還原最新優化)"
                        )
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 加密貨幣交易員 Tool Schema ──────────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="crypto_tool",
                description="加密貨幣交易員：透過 ccxt 連接交易所。可用於查詢 USDT/幣種餘額、查詢即時價格、以及市價/限價下單。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：get_balance (查詢餘額) / get_price (查詢價格) / place_order (執行下單)"
                        ),
                        "symbol": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="交易對名稱（例如：BTC/USDT、ETH/USDT）。get_price 和 place_order 時必填。"
                        ),
                        "side": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="交易方向：buy (買) / sell (賣)。place_order 時必填。"
                        ),
                        "order_type": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="訂單類型：market (市價單) / limit (限價單)。place_order 時必填。"
                        ),
                        "amount": genai.protos.Schema(
                            type=genai.protos.Type.NUMBER,
                            description="下單數量（例如買入多少顆 BTC）。place_order 時必填。"
                        ),
                        "price": genai.protos.Schema(
                            type=genai.protos.Type.NUMBER,
                            description="限價單的觸發價格。當 order_type 為 limit 時必填。"
                        )
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]

# ── 系統錢包 Tool Schema ──────────────────────────────────────────
_TOOL_SCHEMAS += [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="wallet_tool",
                description="系統錢包：查詢 Agent 自身的 Web3 地址與餘額（ETH / ERC20 代幣），以及檢查系統資金維持健康狀態。這是查詢系統自己鏈上地址專用的工具。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "action": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="操作類型：get_address (取得錢包地址) / get_balance (查詢鏈上 ETH/Token 餘額) / check_health (檢查資金是否充足) / swap (Solana 鏈上代幣兌換)"
                        ),
                        "token_address": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="指定 ERC20/SPL 代幣的合約地址。若只是查詢原生幣 (ETH/SOL) 或預設代幣時請留空。提示：Solana 上的 USDT 地址為 Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB。"
                        ),
                        "chain": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="指定查詢的公鏈，如 'base' (預設), 'solana'。get_balance 時可用。"
                        ),
                        "from_token": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="swap 時必填：欲賣出的代幣名稱或地址 (如 SOL, USDT)"
                        ),
                        "to_token": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="swap 時必填：欲買入的目標代幣名稱或地址 (如 SOL, USDT)"
                        ),
                        "amount": genai.protos.Schema(
                            type=genai.protos.Type.NUMBER,
                            description="swap 或轉帳時的數量"
                        )
                    },
                    required=["action"],
                ),
            )
        ]
    ),
]


# 系統提示詞（繁體中文）
_SYSTEM_PROMPT = """你是 Nexus-OS，一個智能個人 AI 助理。

【核心規則】
1. 永遠使用繁體中文回覆，語氣親切自然，善用 emoji 增加可讀性
2. 回覆時直接提供有用資訊，避免 JSON 或程式碼格式（除非用戶明確要求）
3. 若用戶提到時間，先在腦中轉換為精確時間戳，再呼叫工具
4. 若記憶上下文中已有答案，優先用記憶回答，避免重複查詢工具

【工具使用原則】
- 只在真正需要時才呼叫工具，不要過度使用
- 呼叫工具後，將結果整理為自然語言再回覆用戶
- 若工具回傳錯誤，以友善方式告知用戶並提供替代方案

【圖片師工具（image_artist_tool）使用指引】
- 用戶說「生成」、「畫」、「幫我做」、「創作」一張圖片 → 使用 action=generate，prompt 為圖片描述
- 用戶想「改造」、「改成XX風格」、「把圖片變成」→ 使用 action=transform，需要 drive_file_id
- 用戶想「描述」、「分析」、「看看」圖片內容 → 使用 action=describe，需要 drive_file_id
- 若需改造/描述圖片但用戶未提供 Drive ID，請先提示用戶上傳圖片至 Drive，再告知其 Drive ID

【內部財務官工具（treasurer_tool）使用指引】
- 僅用於查詢系統「API Token 消耗」、「預估成本 (USD)」、「常用功能/模型排行榜」。
- 不是個人記帳工具。若用戶提到「午餐花費」、「記帳」，請說明系統目前不支援個人記帳功能。

【金融分析師工具（finance_tool）使用指引】
- 查詢美股即時報價：action=stock_quote，symbol=AAPL/NVDA/TSLA 等
- 股票技術摘要（MA均線、52週高低）：action=stock_summary
- 加密貨幣即時報價：action=crypto_quote，coin_id=bitcoin/ethereum/solana 等
- 幣種走勢摘要：action=crypto_summary，days=7/14/30
- 整體大盤概況（美股指數 + 主流幣）：action=market_overview
- 若用戶問「今日市場」/「大盤概況」/「美股/幣市表現」→ 優先用 market_overview

【時區】
- 預設時區：Asia/Hong_Kong（UTC+8）
- 解析「下週三」、「明天三點」等相對時間時，以當前時間為基準
"""


class Director:
    """
    指揮官大腦 — 系統唯一的決策中樞。

    所有來自用戶的訊息必須且只能透過 process_request() 進入系統。
    大腦負責記憶、推理、工具調度的完整流程。
    """

    def __init__(self, send_message_fn: "Callable[[str, str], Awaitable[None]] | None" = None,
                 send_file_fn: "Callable[[str, str, bytes, str, str], Awaitable[None]] | None" = None,
                 send_photo_fn: "Callable[[str, bytes, str], Awaitable[None]] | None" = None,
                 send_approval_fn: "Callable[[str, dict], Awaitable[None]] | None" = None) -> None:
        """初始化大腦：載入 Gemini 模型、建立 AgentRegistry、初始化所有 Agent。"""
        # Gemini 生成模型（Flash 預設快速 / Pro 複雜問題）
        self._model_flash = genai.GenerativeModel(
            model_name=GEMINI_MODEL_FLASH,
            system_instruction=_SYSTEM_PROMPT,
            tools=_TOOL_SCHEMAS,
        )
        self._model_pro = genai.GenerativeModel(
            model_name=GEMINI_MODEL_PRO,
            system_instruction=_SYSTEM_PROMPT,
            tools=_TOOL_SCHEMAS,
        )
        # 預設仍保留 self._model 供舊版相容引用
        self._model = self._model_flash
        self._embedding_model = "models/gemini-embedding-001"

        # ── 建立共享 AgentRegistry（所有 Agent 均注入此實例）──────
        registry = AgentRegistry()
        registry.register("calendar", CalendarAgent(registry))
        registry.register("weather", WeatherAgent(registry))
        registry.register("reminder", ReminderAgent(registry, send_message_fn))
        registry.register("secretary", SecretaryAgent(registry, send_file_fn))
        registry.register("crawler", CrawlerAgent(registry))
        registry.register("image_artist", ImageArtistAgent(registry, send_photo_fn))
        registry.register("treasurer", TreasurerAgent(registry))
        registry.register("finance", FinanceAgent(registry))
        registry.register("optimizer", OptimizerAgent(registry, send_approval_fn))
        registry.register("crypto", CryptoAgent(registry))
        registry.register("auditor", AuditorAgent(registry))
        
        from logic.wallet_agent import WalletAgent
        registry.register("wallet", WalletAgent(registry))
        
        self._agent_registry = registry

        # 啟動背景 crawler 預取 (每 2 小時)
        self._start_crawler_cache_job()
        
        # 啟動背景自我反思心跳機制 (每 4 小時)
        self._start_self_reflection_job()

        # 啟動 Auditor 消費遙測事件機制
        asyncio.create_task(self._agent_registry.get("auditor").start_consuming())

        self._tools: dict[str, Any] = {}
        self._register_tools()

        _MAX_HISTORY = int(os.getenv("SESSION_HISTORY_TURNS", "20"))
        self._session_histories: dict[str, list] = {}
        self._max_history_turns: int = _MAX_HISTORY
        self._current_user_id: str = ""
        self._current_chat_id: str = ""

    # ------------------------------------------------------------------
    # 工具登記（擴展新 Agent 的入口點）
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        """
        登記所有工具的處理函式。
        【擴展說明】新增 Agent：在 _TOOL_SCHEMAS 加 Schema → 在此登記 handler → 實作 handler
        """
        self._tools["google_calendar_tool"] = self._handle_calendar
        self._tools["weather_tool"] = self._handle_weather
        self._tools["scheduler_tool"] = self._handle_scheduler
        self._tools["secretary_tool"] = self._handle_secretary
        self._tools["crawler_tool"] = self._handle_crawler
        self._tools["image_artist_tool"] = self._handle_image_artist
        self._tools["treasurer_tool"] = self._handle_treasurer
        self._tools["finance_tool"] = self._handle_finance
        self._tools["optimizer_tool"] = self._handle_optimizer
        self._tools["crypto_tool"] = self._handle_crypto
        self._tools["wallet_tool"] = self._handle_wallet

    def _start_crawler_cache_job(self) -> None:
        """啟動爬虫師的背景快取刷新任務。"""
        try:
            reminder_agent = self._agent_registry.get("reminder")
            crawler_agent = self._agent_registry.get("crawler")

            from apscheduler.triggers.cron import CronTrigger
            reminder_agent._scheduler.add_job(
                func=crawler_agent.refresh_cache,
                trigger=CronTrigger.from_crontab("0 */2 * * *", timezone="Asia/Hong_Kong"),
                id="system_crawler_refresh",
                replace_existing=True,
            )
            print("🕒 已啟動爬虫師背景預取排程（每 2 小時）")
        except Exception as e:
            print(f"⚠️ 無法啟動爬虫師預取排程: {e}")

    def _start_self_reflection_job(self) -> None:
        """啟動系統的心跳反思機制，定時讓優化師分析系統健康狀態"""
        try:
            reminder_agent = self._agent_registry.get("reminder")
            optimizer_agent = self._agent_registry.get("optimizer")
            
            from apscheduler.triggers.cron import CronTrigger
            # 這裡設定每 4 小時進行一次自我反思 (可視需求調整)
            reminder_agent._scheduler.add_job(
                func=optimizer_agent.analyze_system,
                trigger=CronTrigger.from_crontab("0 */4 * * *", timezone="Asia/Hong_Kong"),
                id="system_self_reflection",
                replace_existing=True,
            )
            print("🕒 已啟動系統自我優化心跳排程（每 4 小時）")
        except Exception as e:
            print(f"⚠️ 無法啟動自我反思排程: {e}")

    # ------------------------------------------------------------------
    # 內部工具：複雜度判斷
    # ------------------------------------------------------------------

    @staticmethod
    def _is_complex(message: str) -> bool:
        """
        根据訊息內容判斷是否需要使用 Pro 模型。

        觸發條件（任一满足即升級）：
        - 訊息超過 200 字（長問題 / 文件分析）
        - 含有複雜任務關鍵詞（分析、總結、寫代碼、翻譯、對比、討論等）
        """
        if len(message) > 200:
            return True

        PRO_KEYWORDS = [
            # 分析 / 創作
            "分析", "總結", "摘要", "評估", "討論", "寫一篇", "寫作",
            # 程式碼
            "寫代碼", "寫 function", "寫程式", "debug", "修改代碼",
            # 翻譯
            "翻譯", "translate",
            # 比較 / 推理
            "比較", "差異", "優劣", "推萃", "推理",
            # 英文對泭
            "analyze", "summarize", "explain", "compare", "generate report",
        ]

        msg_lower = message.lower()
        return any(kw in msg_lower for kw in PRO_KEYWORDS)

    # ------------------------------------------------------------------
    # 公開入口：process_request()
    # ------------------------------------------------------------------

    async def process_request(
        self,
        user_id: str,
        message: str,
        session_id: str = "default",
    ) -> str:
        """
        處理用戶訊息的唯一公開入口。

        流程：
            1. 檢索相關對話記憶（pgvector 語義搜尋）
            2. 組合帶記憶上下文的提示詞
            3. 呼叫 Gemini，若觸發 Function Call 則分派給對應 Agent
            4. 持久化本輪對話至 MemoryStore
            5. 紀錄 Token 消耗至 UsageLog
            6. 回傳最終回覆文字

        參數：
            user_id    - 用戶唯一識別碼
            message    - 用戶訊息（口語化文字）
            session_id - 對話 Session ID（用於分組記憶）

        回傳：
            大腦的最終回覆文字（繁體中文）
        """
        session_key = f"{user_id}:{session_id}"
        self._current_user_id = user_id
        self._current_chat_id = user_id  # Telegram 私聊中 chat_id == user_id

        async with AsyncSessionLocal() as session:
            # ── 步驟 1：向量化用戶訊息，檢索相關記憶 ─────────────
            memory_context = await self._retrieve_memory(
                session, user_id, message
            )

            # ── 步驟 2：組合提示詞（含記憶上下文 + 當前時間）────
            now_hk = datetime.now(tz=timezone(timedelta(hours=8)))
            prompt = self._build_prompt(message, memory_context, now_hk)

            # ── 步驟 3：取得本 Session 的對話歷史 ────────────────
            history = self._session_histories.get(session_key, [])

            # ── 步驟 4：選擇模型並呼叫 Gemini（多輪 + Function Calling 循環）
            model = self._model_pro if self._is_complex(message) else self._model_flash
            start_time = time.time()
            reply_text, tool_called, usage, updated_history = \
                await self._generate_with_tools(prompt, history, model=model)
            latency_ms = int((time.time() - start_time) * 1000)

            # ── 步驟 5：更新 in-memory 對話歷史（限制長度）───────
            # 每輪含 user + model 兩條 Content，故 *2
            max_contents = self._max_history_turns * 2
            self._session_histories[session_key] = updated_history[-max_contents:]

            # ── 步驟 6：持久化對話記憶至 pgvector ────────────────
            await self._save_memory(session, user_id, session_id, "user", message)
            await self._save_memory(session, user_id, session_id, "assistant", reply_text)

            # ── 步驟 7：紀錄 Token 消耗 ───────────────────────────
            WorkerBus.emit_nowait(TelemetryEvent(
                user_id=user_id,
                session_id=session_id,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                latency_ms=latency_ms,
                tool_called=tool_called,
                model=usage.get("model", GEMINI_MODEL_FLASH),
                success=True
            ))

            await session.commit()

        return reply_text

    # ------------------------------------------------------------------
    # 私有方法：記憶檢索
    # ------------------------------------------------------------------

    async def _retrieve_memory(
        self, session: AsyncSession, user_id: str, message: str
    ) -> list[dict]:
        """
        將訊息向量化，在 MemoryStore 中以餘弦相似度搜尋最相關的歷史對話。

        回傳：最多 MEMORY_TOP_K 筆相關記憶（含 role 與 content）
        """
        try:
            # 生成查詢向量
            embed_result = genai.embed_content(
                model=self._embedding_model,
                content=message,
                task_type="retrieval_query",
            )
            query_vector = embed_result["embedding"]

            # pgvector 餘弦相似度查詢（<=> 為餘弦距離算符）
            sql = text(
                """
                SELECT role, content, 1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM memory_store_v3
                WHERE user_id = :user_id AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT :top_k
                """
            )
            result = await session.execute(
                sql,
                {
                    "vec": str(query_vector),
                    "user_id": user_id,
                    "top_k": MEMORY_TOP_K,
                },
            )
            rows = result.fetchall()
            return [{"role": r.role, "content": r.content, "similarity": float(r.similarity) if r.similarity is not None else 0.0} for r in rows]
        except Exception as e:
            # 記憶檢索失敗不應中斷主流程，記錄後繼續
            print(f"⚠️ 記憶檢索失敗（將繼續無記憶模式）：{e}")
            return []

    # ------------------------------------------------------------------
    # 私有方法：組合提示詞
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        message: str,
        memory_context: list[dict],
        now: datetime | None = None,
    ) -> str:
        """
        將用戶訊息與記憶上下文組合成最終提示詞。
        永遠在首行注入當前日期時間，讓 Gemini 能自行解析
        「下星期五」、「明天下午三點」等相對時間，無需向用戶確認日期。
        """
        lines: list[str] = []

        # ── 注入當前日期時間（Gemini 解析相對日期必需）────────────
        if now is not None:
            weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
            wd = weekday_names[now.weekday()]
            lines.append(
                f"【當前時間】{now.strftime('%Y年%m月%d日')}（星期{wd}）"
                f" {now.strftime('%H:%M')} HKT（UTC+8）"
            )
            lines.append(
                "請根據上述時間自行計算『下星期X』、『明天』、『今天』等相對日期，"
                "無需向用戶確認具體日期，直接使用正確的 ISO 8601 格式呼叫工具。"
            )

        # ── 記憶上下文 ────────────────────────────────────────────
        if memory_context:
            lines.append("【相關對話記憶】（請參考以下背景資訊回答）")
            for mem in memory_context:
                role_label = "用戶" if mem["role"] == "user" else "助理"
                lines.append(f"{role_label}：{mem['content']}")

        lines.append("【用戶當前訊息】")
        lines.append(message)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 私有方法：Gemini 推理 + Function Calling 循環
    # ------------------------------------------------------------------

    async def _generate_with_tools(
        self,
        prompt: str,
        history: list,
        model: genai.GenerativeModel | None = None,
    ) -> tuple[str, str | None, dict, list]:
        """
        使用 Gemini start_chat() 進行多輪推理，並支援連續 Function Calling。
        model: 指定使用的 Gemini 模型（預設使用 Flash）。
        """
        loop = asyncio.get_event_loop()
        tool_called: str | None = None
        usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        _MAX_TOOL_TURNS = 5  # 防止無限循環的安全上限

        # 選用傳入的 model，若無則 fallback 到 flash
        active_model = model if model is not None else self._model_flash
        used_model_name = getattr(active_model, '_model_name', GEMINI_MODEL_FLASH)
        print(f"🧠 使用模型：{used_model_name}")

        # ── 建立多輪 Chat Session（注入現有歷史）────────────────────
        chat = active_model.start_chat(history=history)

        # ── 第一次發送訊息（非阻塞）──────────────────────────────────
        response = await chat.send_message_async(prompt)
        self._accumulate_usage(response, usage)

        # ── Function Calling 循環 ─────────────────────────────────────
        for _turn in range(_MAX_TOOL_TURNS):
            if not response.candidates:
                break

            # 找出本次回覆中所有的 function_call parts
            fc_parts = [
                p for p in response.candidates[0].content.parts
                if hasattr(p, "function_call") and p.function_call.name
            ]
            if not fc_parts:
                break  # 無工具呼叫 → 最終文字回覆

            # 執行所有工具並收集回應
            tool_response_parts: list[genai.protos.Part] = []
            for fc_part in fc_parts:
                fc = fc_part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}
                tool_called = tool_name
                print(f"🔧 大腦調度工具：{tool_name}，參數：{tool_args}")

                if tool_name in self._tools:
                    tool_result = await self._tools[tool_name](tool_args)
                else:
                    tool_result = {"error": f"未知工具：{tool_name}"}

                tool_response_parts.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=tool_name,
                            response={"result": json.dumps(tool_result, ensure_ascii=False)},
                        )
                    )
                )

            # 將工具結果送回 Gemini（非阻塞）
            _parts = tool_response_parts  # capture for lambda
            response = await chat.send_message_async(_parts)
            self._accumulate_usage(response, usage)

        # ── 取得最終回覆文字 ─────────────────────────────────────────
        try:
            final_text = response.text.strip()
        except Exception:
            final_text = "抱歉，我暫時無法回應，請稍後再試。🙏"

        # ── 過濾工具呼叫，只保留文字歷史，避免產生 400 Invalid Argument 錯誤 ──
        clean_history = []
        for content in chat.history:
            has_func = False
            for p in content.parts:
                fc = getattr(p, "function_call", None)
                fr = getattr(p, "function_response", None)
                if (fc and getattr(fc, "name", None)) or (fr and getattr(fr, "name", None)):
                    has_func = True
                    break
            
            if not has_func:
                # 確保歷史必須由 user 開始，且 user/model 嚴格交替
                if not clean_history and content.role == "user":
                    clean_history.append(content)
                elif clean_history and clean_history[-1].role != content.role:
                    clean_history.append(content)

        # Chat 歷史結尾必須是 model，否則下次 send_message 會造成兩個 user 連續
        if clean_history and clean_history[-1].role == "user":
            clean_history.pop()

        return final_text, tool_called, usage, clean_history

    @staticmethod
    def _accumulate_usage(response: Any, usage: dict) -> None:
        """累加 Gemini API 回傳的 Token 用量到 usage dict。"""
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            usage["prompt_tokens"] += getattr(meta, "prompt_token_count", 0) or 0
            usage["completion_tokens"] += getattr(meta, "candidates_token_count", 0) or 0
            usage["total_tokens"] += getattr(meta, "total_token_count", 0) or 0

    # ------------------------------------------------------------------
    # 工具處理函式：日程師
    # ------------------------------------------------------------------

    async def _handle_calendar(self, args: dict) -> dict:
        """
        處理 google_calendar_tool 的調用，透過 AgentRegistry 取得 CalendarAgent。
        支援操作：create / list / update / delete
        """
        calendar_agent: CalendarAgent = self._agent_registry.get("calendar")

        action = args.get("action", "").lower()
        now_hk = datetime.now(tz=timezone(timedelta(hours=8)))

        try:
            # ── 新增活動 ──────────────────────────────────────────
            if action == "create":
                start_str = args.get("start_time", "")
                end_str = args.get("end_time", "")

                start_dt = self._parse_datetime(start_str, now_hk)
                if not start_dt:
                    return {"error": f"無法解析開始時間：{start_str}"}

                # 若未提供結束時間，預設活動時長 1 小時
                end_dt = self._parse_datetime(end_str, now_hk) if end_str else start_dt + timedelta(hours=1)

                event = await calendar_agent.create_event(
                    summary=args.get("summary", "新活動"),
                    start_dt=start_dt,
                    end_dt=end_dt,
                    description=args.get("description", ""),
                )
                return {
                    "success": True,
                    "action": "create",
                    "event_id": event.get("id"),
                    "summary": event.get("summary"),
                    "start": event.get("start", {}).get("dateTime"),
                    "html_link": event.get("htmlLink"),
                }

            # ── 查詢活動 ──────────────────────────────────────────
            elif action == "list":
                time_min = self._parse_datetime(args.get("start_time", ""), now_hk) or now_hk
                time_max = self._parse_datetime(args.get("end_time", ""), now_hk)

                events = await calendar_agent.list_events(
                    time_min=time_min,
                    time_max=time_max,
                    max_results=int(args.get("max_results", 10)),
                    query=args.get("query"),
                )
                formatted = [CalendarAgent.format_event(e) for e in events]
                return {
                    "success": True,
                    "action": "list",
                    "count": len(events),
                    "events": formatted,
                }

            # ── 更新活動 ──────────────────────────────────────────
            elif action == "update":
                event_id = args.get("event_id", "")
                if not event_id:
                    return {"error": "更新活動需要提供 event_id"}

                updates: dict[str, Any] = {}
                if args.get("summary"):
                    updates["summary"] = args["summary"]
                if args.get("description"):
                    updates["description"] = args["description"]
                if args.get("start_time"):
                    start_dt = self._parse_datetime(args["start_time"], now_hk)
                    if start_dt:
                        updates["start"] = {
                            "dateTime": start_dt.isoformat(),
                            "timeZone": "Asia/Hong_Kong",
                        }
                if args.get("end_time"):
                    end_dt = self._parse_datetime(args["end_time"], now_hk)
                    if end_dt:
                        updates["end"] = {
                            "dateTime": end_dt.isoformat(),
                            "timeZone": "Asia/Hong_Kong",
                        }

                event = await calendar_agent.update_event(event_id, updates)
                return {
                    "success": True,
                    "action": "update",
                    "event_id": event.get("id"),
                    "summary": event.get("summary"),
                }

            # ── 刪除活動 ──────────────────────────────────────────
            elif action == "delete":
                event_id = args.get("event_id", "")
                if not event_id:
                    return {"error": "刪除活動需要提供 event_id"}

                success = await calendar_agent.delete_event(event_id)
                return {"success": success, "action": "delete", "event_id": event_id}

            else:
                return {"error": f"不支援的操作：{action}。請使用 create / list / update / delete"}

        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 工具處理函式：天氣師
    # ------------------------------------------------------------------

    async def _handle_weather(self, args: dict) -> dict:
        """處理 weather_tool 的調用，透過 AgentRegistry 取得 WeatherAgent。"""
        weather_agent: WeatherAgent = self._agent_registry.get("weather")
        location = args.get("location")
        try:
            summary = await weather_agent.get_weather(
                location_name=location if location else None
            )
            return {"success": True, "weather": summary}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 工具處理函式：排程師
    # ------------------------------------------------------------------

    async def _handle_scheduler(self, args: dict) -> dict:
        """
        處理 scheduler_tool 的調用，透過 AgentRegistry 取得 ReminderAgent。
        支援 action：add / list / cancel
        """
        reminder_agent: ReminderAgent = self._agent_registry.get("reminder")
        action = args.get("action", "").lower()

        user_id = self._current_user_id or args.get("_user_id", "unknown")
        chat_id = self._current_chat_id or args.get("_chat_id", user_id)

        try:
            if action == "add":
                job_type = args.get("job_type", "remind")
                remind_at_str = args.get("remind_at")
                cron_expr = args.get("cron_expr")
                message = args.get("message", "")

                now_hk = datetime.now(tz=timezone(timedelta(hours=8)))
                remind_at = self._parse_datetime(remind_at_str, now_hk) if remind_at_str else None

                if not remind_at and not cron_expr:
                    return {"error": "新增排程需要提供 remind_at 或 cron_expr"}

                result = await reminder_agent.add_reminder(
                    user_id=user_id,
                    chat_id=chat_id,
                    job_type=job_type,
                    message=message,
                    remind_at=remind_at,
                    cron_expr=cron_expr,
                )
                return {"success": True, "action": "add", **result}

            elif action == "list":
                reminders = await reminder_agent.list_reminders(user_id)
                return {"success": True, "action": "list", "reminders": reminders}

            elif action == "cancel":
                job_id = args.get("job_id", "")
                if not job_id:
                    return {"error": "取消排程需要提供 job_id"}
                ok = await reminder_agent.cancel_reminder(job_id)
                return {"success": ok, "action": "cancel", "job_id": job_id}

            else:
                return {"error": f"不支援的操作：{action}。請使用 add / list / cancel"}

        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 未來 Agent 處理函式（預留位置）
    # ------------------------------------------------------------------

    # async def _handle_secretary(self, args: dict) -> dict:
    #     "秘書 Agent：處理 Google Drive 與 Vision OCR（v1.3 實作）"
    #     pass

    # async def _handle_treasurer(self, args: dict) -> dict:
    #     "財務官 Agent：支出追蹤與財務分析（v1.4 實作）"
    #     pass

    # ------------------------------------------------------------------
    # 工具處理函式：秘書
    # ------------------------------------------------------------------

    async def _handle_secretary(self, args: dict) -> dict:
        """
        處理 secretary_tool 的調用，透過 AgentRegistry 取得 SecretaryAgent。
        支援 action：list / search / read / create / send_to_me / ocr
        """
        from logic.secretary_agent import SecretaryAgent as SA
        secretary: SA = self._agent_registry.get("secretary")
        action = args.get("action", "").lower()
        chat_id = self._current_chat_id

        try:
            if action == "list":
                files = await secretary.list_files(
                    folder_id=args.get("folder_id"),
                    query=args.get("query"),
                    max_results=int(args.get("max_results", 10)),
                )
                if not files:
                    return {"success": True, "action": "list", "count": 0, "files": []}
                formatted = [SA.format_file(f) for f in files]
                return {"success": True, "action": "list", "count": len(files), "files": formatted}

            elif action == "search":
                query = args.get("query", "")
                if not query:
                    return {"error": "搜尋需要提供 query"}
                files = await secretary.search_files(query)
                formatted = [SA.format_file(f) for f in files]
                return {"success": True, "action": "search", "count": len(files), "files": formatted}

            elif action == "read":
                file_id = args.get("file_id", "")
                if not file_id:
                    return {"error": "讀取文件需要提供 file_id"}
                content = await secretary.read_file(file_id)
                return {"success": True, "action": "read", "content": content}

            elif action == "create":
                name = args.get("name", "")
                if not name:
                    return {"error": "廻建文件需要提供 name"}
                result = await secretary.create_file(
                    name=name,
                    content=args.get("content", ""),
                    folder_id=args.get("folder_id"),
                )
                return {"success": True, "action": "create", **result}

            elif action == "send_to_me":
                file_id = args.get("file_id", "")
                if not file_id:
                    return {"error": "傳送文件需要提供 file_id"}
                return await secretary.send_to_user(
                    file_id=file_id,
                    chat_id=chat_id,
                    caption=args.get("caption", ""),
                )

            elif action == "ocr":
                file_id = args.get("file_id", "")
                if not file_id:
                    return {"error": "OCR 需要提供 file_id"}
                text = await secretary.ocr_image(file_id)
                return {"success": True, "action": "ocr", "text": text}

            else:
                return {"error": f"不支援的操作：{action}，請用 list/search/read/create/send_to_me/ocr"}

        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 私有方法：爬虫師 handler
    # ------------------------------------------------------------------

    async def _handle_crawler(self, args: dict) -> dict:
        """處理 crawler_tool 工具呼叫，委派給 CrawlerAgent。"""
        from logic.crawler_agent import CrawlerAgent as CA
        crawler: CA = self._agent_registry.get("crawler")
        action = args.get("action", "").lower()

        try:
            if action == "search":
                query = args.get("query", "")
                if not query:
                    return {"error": "搜尋需要提供 query"}
                max_results = int(args.get("max_results", 8))
                results = await crawler.search(query, max_results=max_results)
                return {
                    "success": True,
                    "action": "search",
                    "count": len(results),
                    "results": CA.format_search_results(results),
                }

            elif action == "fetch":
                url = args.get("url", "")
                if not url:
                    return {"error": "fetch 需要提供 url"}
                text = await crawler.fetch(url)
                return {"success": True, "action": "fetch", "content": text}

            elif action == "summarize":
                url = args.get("url", "")
                if not url:
                    return {"error": "summarize 需要提供 url"}
                summary = await crawler.summarize(url, prompt=args.get("prompt", ""))
                return {"success": True, "action": "summarize", "summary": summary}

            elif action == "trending":
                platform = args.get("platform", "all").lower()
                # 預取快取架構：直接從 CrawlCache 讀取
                summary = await crawler.get_cached_trending(platform=platform)
                return {"success": True, "platform": platform, "trending": summary}

            elif action == "search_news":
                query = args.get("query", "")
                if not query:
                    return {"error": "搜尋新聞需要提供 query"}
                count = int(args.get("max_results", 8))
                results = await crawler.search_news(query, count=count)
                text = "\n\n".join([
                    f"**{i+1}. {r['title']}**\n{r.get('summary','')}\n🔗 {r.get('url','')}"
                    for i, r in enumerate(results)
                ])
                return {"success": True, "action": "search_news", "count": len(results), "results": text}

            elif action == "extract_links":
                url = args.get("url", "")
                if not url:
                    return {"error": "extract_links 需要提供 url"}
                links = await crawler.extract_links(url)
                return {"success": True, "action": "extract_links", "count": len(links), "links": links}

            else:
                return {"error": f"不支援的操作：{action}，請用 search/fetch/summarize/trending/extract_links"}

        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 工具處理函式：圖片師
    # ------------------------------------------------------------------

    async def _handle_image_artist(self, args: dict) -> dict:
        """
        處理 image_artist_tool 的調用，透過 AgentRegistry 取得 ImageArtistAgent。
        支援 action：generate / transform / describe
        """
        from logic.image_artist_agent import ImageArtistAgent as IA
        artist: IA = self._agent_registry.get("image_artist")
        action = args.get("action", "").lower()
        chat_id = self._current_chat_id

        try:
            # ── 生成圖片 ──────────────────────────────────────────────
            if action == "generate":
                prompt = args.get("prompt", "")
                if not prompt:
                    return {"error": "生成圖片需要提供 prompt 描述"}
                aspect_ratio = args.get("aspect_ratio", "1:1")

                image_bytes = await artist.generate_image(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )
                if not image_bytes:
                    return {"error": "圖片生成失敗，請稍後重試"}

                # 推播圖片給用戶
                result = await artist.send_to_user(
                    chat_id=chat_id,
                    image_bytes=image_bytes,
                    caption=f"🎨 已根據描述生成圖片：{prompt[:80]}",
                )
                return {"success": True, "action": "generate", **result}

            # ── 改造圖片 ──────────────────────────────────────────────
            elif action == "transform":
                instruction = args.get("instruction", "") or args.get("prompt", "")
                if not instruction:
                    return {"error": "改造圖片需要提供 instruction 指示"}

                drive_file_id = args.get("drive_file_id", "")
                if not drive_file_id:
                    return {"error": "改造圖片需要提供 drive_file_id（請先上傳圖片至 Drive）"}

                # 從 Drive 取得圖片 bytes
                from logic.secretary_agent import SecretaryAgent as SA
                secretary: SA = self._agent_registry.get("secretary")
                image_bytes_raw, _, _ = await secretary.download_bytes(drive_file_id)
                if not image_bytes_raw:
                    return {"error": f"無法從 Drive 取得圖片 ID：{drive_file_id}"}

                transformed = await artist.transform_image(
                    image_bytes=image_bytes_raw,
                    instruction=instruction,
                )
                if not transformed:
                    return {"error": "圖片改造失敗，請稍後重試"}

                result = await artist.send_to_user(
                    chat_id=chat_id,
                    image_bytes=transformed,
                    caption=f"✨ 圖片已改造：{instruction[:80]}",
                )
                return {"success": True, "action": "transform", **result}

            # ── 描述圖片 ──────────────────────────────────────────────
            elif action == "describe":
                drive_file_id = args.get("drive_file_id", "")
                if not drive_file_id:
                    return {"error": "描述圖片需要提供 drive_file_id"}

                from logic.secretary_agent import SecretaryAgent as SA
                secretary: SA = self._agent_registry.get("secretary")
                image_bytes_raw = await secretary.download_file(drive_file_id)
                if not image_bytes_raw:
                    return {"error": f"無法從 Drive 取得圖片 ID：{drive_file_id}"}

                question = args.get("prompt", "請詳細描述這張圖片的內容。")
                description = await artist.describe_image(
                    image_bytes=image_bytes_raw,
                    question=question,
                )
                return {"success": True, "action": "describe", "description": description}

            else:
                return {"error": f"不支援的操作：{action}，請用 generate / transform / describe"}

        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 私有工具：自然語言日期解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_datetime(time_str: str, relative_base: datetime) -> datetime | None:
        """
        將字串解析為 datetime 物件，支援 ISO 8601 及自然語言（如「下週三三點」）。

        參數：
            time_str      - 待解析的時間字串
            relative_base - 相對時間的基準點（通常為當前時間）

        回傳：
            解析成功回傳含時區的 datetime，失敗回傳 None
        """
        if not time_str:
            return None

        settings = {
            "RELATIVE_BASE": relative_base.replace(tzinfo=None),
            "PREFER_LOCALE_DATE_ORDER": False,
            "PREFER_DAY_OF_MONTH": "first",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": "Asia/Hong_Kong",
            "TO_TIMEZONE": "Asia/Hong_Kong",
        }

        parsed = dateparser.parse(time_str, languages=["zh"], settings=settings)
        if parsed is None:
            # 嘗試英文解析（ISO 格式或英文日期）
            parsed = dateparser.parse(time_str, settings=settings)

        return parsed

    # ------------------------------------------------------------------
    # 工具處理：內部財務官 (treasurer)
    # ------------------------------------------------------------------

    async def _handle_treasurer(self, args: dict) -> str:
        """
        分派任務給內部財務官 (TreasurerAgent)。
        """
        from logic.treasurer_agent import TreasurerAgent
        treasurer: TreasurerAgent = self._agent_registry.get("treasurer")
        action = args.get("action")

        now = datetime.now(timezone.utc)

        try:
            if action == "daily":
                date_str = args.get("date", "today")
                return await treasurer.get_daily_report(self._current_user_id, date_str)
            elif action == "monthly":
                year = int(args.get("year", now.year))
                month = int(args.get("month", now.month))
                return await treasurer.get_monthly_report(self._current_user_id, year, month)
            elif action == "cost":
                period = args.get("period", "current_month")
                return await treasurer.get_cost_estimate(self._current_user_id, period)
            elif action == "tools":
                period = args.get("period", "current_month")
                return await treasurer.get_top_tools(self._current_user_id, period)
            elif action == "models":
                period = args.get("period", "current_month")
                return await treasurer.get_model_breakdown(self._current_user_id, period)
            else:
                return f"⚠️ 內部財務官不支援操作：{action}"
        except Exception as e:
            return f"❌ 財務系統查詢失敗：{e}"

    # ------------------------------------------------------------------
    # 工具處理：金融分析師 (finance)
    # ------------------------------------------------------------------

    async def _handle_finance(self, args: dict) -> str:
        """
        分派任務給金融分析師 (FinanceAgent)。
        """
        from logic.finance_agent import FinanceAgent
        finance: FinanceAgent = self._agent_registry.get("finance")
        action = args.get("action")

        try:
            if action == "stock_quote":
                symbol = args.get("symbol")
                if not symbol:
                    return "⚠️ 需要提供美股代碼 (symbol)"
                return await finance.get_stock_quote(symbol)

            elif action == "stock_summary":
                symbol = args.get("symbol")
                if not symbol:
                    return "⚠️ 需要提供美股代碼 (symbol)"
                period = args.get("period", "3mo")
                return await finance.get_stock_summary(symbol, period)

            elif action == "crypto_quote":
                coin_id = args.get("coin_id")
                if not coin_id:
                    return "⚠️ 需要提供 CoinGecko 幣種 ID (coin_id)"
                return await finance.get_crypto_quote(coin_id)

            elif action == "crypto_summary":
                coin_id = args.get("coin_id")
                if not coin_id:
                    return "⚠️ 需要提供 CoinGecko 幣種 ID (coin_id)"
                days = args.get("days", 7)
                return await finance.get_crypto_summary(coin_id, int(days))

            elif action == "market_overview":
                stocks = args.get("watchlist_stocks", "")
                cryptos = args.get("watchlist_cryptos", "")
                return await finance.get_market_overview(stocks, cryptos)

            else:
                return f"⚠️ 金融分析師不支援操作：{action}"
        except Exception as e:
            return f"❌ 查詢金融數據失敗：{e}"

    # ------------------------------------------------------------------
    # 工具處理：系統優化師 (optimizer)
    # ------------------------------------------------------------------

    async def _handle_optimizer(self, args: dict) -> str:
        """
        分派任務給系統優化師 (OptimizerAgent)。
        """
        from logic.optimizer_agent import OptimizerAgent
        optimizer: OptimizerAgent = self._agent_registry.get("optimizer")
        action = args.get("action")

        try:
            if action == "analyze":
                result = await optimizer.analyze_system()
                return f"優化分析完成：\n{result}"
                
            elif action == "rollback":
                result = optimizer.rollback_optimization()
                return f"還原操作結果：\n{result}"
                
            else:
                return f"⚠️ 系統優化師不支援操作：{action}"
        except Exception as e:
            return f"❌ 執行系統優化師任務失敗：{e}"

    # ------------------------------------------------------------------
    # 工具處理：加密貨幣交易員 (crypto)
    # ------------------------------------------------------------------

    async def _handle_crypto(self, args: dict) -> str:
        """
        分派任務給加密貨幣交易員 (CryptoAgent)。
        """
        crypto = self._agent_registry.get("crypto")
        action = args.get("action")

        try:
            if action == "get_balance":
                return await crypto.get_balance()
                
            elif action == "get_price":
                symbol = args.get("symbol")
                if not symbol:
                    return "⚠️ 需要提供交易對 (symbol)，例如 BTC/USDT"
                return await crypto.get_price(symbol)
                
            elif action == "place_order":
                symbol = args.get("symbol")
                side = args.get("side")
                order_type = args.get("order_type")
                amount = args.get("amount")
                price = args.get("price")
                
                if not all([symbol, side, order_type, amount]):
                    return "⚠️ 下單需要提供 symbol, side, order_type, amount"
                    
                parsed_price = float(price) if price is not None else None
                return await crypto.place_order(symbol, side, order_type, float(amount), parsed_price)
                
            else:
                return f"⚠️ 加密交易員不支援操作：{action}"
        except Exception as e:
            return f"❌ 執行加密交易員任務失敗：{e}"

    # ------------------------------------------------------------------
    # 工具處理：系統 Web3 錢包 (wallet)
    # ------------------------------------------------------------------

    async def _handle_wallet(self, args: dict) -> str:
        """
        分派任務給系統錢包管理員 (WalletAgent)。
        """
        wallet = self._agent_registry.get("wallet")
        action = args.get("action")

        try:
            if action == "get_address":
                return await wallet.get_address()
                
            elif action == "get_balance":
                token_address = args.get("token_address")
                chain = args.get("chain", "base")
                return await wallet.get_balance(token_address, chain=chain)
                
            elif action == "check_health":
                return await wallet.check_health()
                
            elif action == "swap":
                from_token = args.get("from_token")
                to_token = args.get("to_token")
                amount = args.get("amount")
                if not from_token or not to_token or not amount:
                    return "❌ 錯誤：執行 swap 需要提供 from_token, to_token 與 amount。"
                return await wallet.swap(from_token, to_token, amount)
                
            else:
                return f"⚠️ Web3 錢包不支援操作：{action}"
        except Exception as e:
            return f"❌ 查詢 Web3 錢包失敗：{e}"

    # ------------------------------------------------------------------
    # 私有方法：持久化記憶
    # ------------------------------------------------------------------

    async def _save_memory(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """
        將訊息向量化後存入 MemoryStore。
        若向量化失敗（如 API 限流），仍保存純文字記錄（embedding=None）。
        """
        embedding = None
        try:
            embed_result = genai.embed_content(
                model="models/gemini-embedding-001",
                content=content,
                task_type="retrieval_document",
            )
            embedding = embed_result["embedding"]
        except Exception as e:
            print(f"⚠️ 向量化失敗，儲存純文字記憶：{e}")

        record = MemoryStore(
            user_id=user_id,
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
        )
        session.add(record)

    async def shutdown(self) -> None:
        """應用程式關閉時的資源清理"""
        try:
            # 關閉加密交易員的交易所連線
            crypto = self._agent_registry.get("crypto")
            if crypto and hasattr(crypto, "close"):
                print("🛑 正在關閉加密貨幣交易所連線...")
                await crypto.close()
        except Exception as e:
            print(f"⚠️ 清理加密貨幣連線時發生錯誤: {e}")


