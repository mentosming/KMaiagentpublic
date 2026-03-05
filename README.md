# 🧠 Nexus-OS — 個人自主 AI 錢包與助理平台

以 **指揮官大腦（Director）** 為決策中樞，透過 **AgentRegistry** 統一調度各專門 Agent，支援多回合對話、以及 24/7 自主優化。本專案採輕量化架構，主要透過 **Telegram** 進行互動。

> [!TIP]
> **新手上路？** 請參閱 [使用手冊 (SOP)](使用手冊_SOP.md) 以獲取詳細的部署與設定指南。

---

---

## 目錄結構

```
kmpersonalaiagent/
├── telegram_bot.py         # Telegram 機器人核心
├── main.py                 # REST API 伺服器入口 (提供 OAuth 回呼)
├── database.py             # ORM 模型（資料持久化）
├── Dockerfile              # Docker 鏡像（Python 後端 / Bot）
├── docker-compose.yml      # 一鍵啟動 Bot + PostgreSQL + pgvector
├── .env.example            # 環境變數範本
└── logic/
    ├── director.py             # 指揮官大腦（決策中樞）
    ├── agent_registry.py       # Agent 共享登記表
    ├── calendar_agent.py       # 日程師：Google Calendar CRUD
    ├── weather_agent.py        # 天氣師：Open-Meteo API
    ├── reminder_agent.py       # 排程師：APScheduler
    ├── secretary_agent.py      # 秘書：Google Drive
    ├── crawler_agent.py        # 爬虫師：抓取與熱門話題快取
    ├── image_artist_agent.py   # 圖片師：生成與改造
    ├── treasurer_agent.py      # 財務官：Token 與成本分析
    ├── finance_agent.py        # 分析師：美股、加密貨幣分析
    ├── wallet_agent.py         # 錢包師：Web3 錢包與鏈上資產管理
    ├── agent_wallet.py         # 錢包實體：Base/Solana 私鑰與 RPC
    └── optimizer_agent.py      # 🔧 優化師：系統分析與 Git 備份還原
```

---

## Agent 功能

| Agent | 功能 | 指令範例 |
|-------|------|---------|
| 🗓️ **日程師** | Google Calendar 新增/查詢/修改/刪除 | 「下週三三點幫我加個會議」 |
| ⛅ **天氣師** | Open-Meteo 即時天氣與今日預報 | 「現在香港天氣如何？」 |
| ⏰ **排程師** | 一次性提醒、定時通知（行程/天氣/熱門話題） | 「每天早上8點告訴我天氣」 |
| 📂 **秘書** | Drive 文件管理、Telegram↔Drive 傳輸、圖片 OCR | 「搜尋關於會議的文件」 |
| 🕷️ **爬虫師** | 網頁搜尋、抓取總結、LIHKG/Threads/FB 熱門話題 | 「連登今日最熱話題」 |
| 🎨 **圖片師** | AI 生成圖片（Imagen 4）、改造圖片風格、AI 描述圖片 | 「生成一張香港夜景圖片」 |
| 💰 **財務官** | 監控系統 API Token 消耗與成本估算 | 「預估這個月的 API 費用」 |
| 📈 **分析師** | 查詢美股報價、技術分析、加密貨幣走勢、大盤概況 | 「幫我分析 NVDA 走勢」 |
| 🔧 **優化師** | 24/7 自動分析系統、產生最佳化提案與 Admin 審批還原 | `[Admin 自動心跳驅動]` |
| 👛 **錢包師** | 管理 Base/Solana 錢包、餘額查詢、以及 Solana 鏈上 Swap | 「查我的 SOL 餘額」、「幫我將 $1 USDT 換成 SOL」 |

### 語音輸入 🎤
按 Telegram 麥克風按鈕說話 → 自動轉錄（Gemini Vision）→ 正常處理

### 文件/圖片上傳
傳送任何文件或圖片 → 自動暫存至 **Google Drive Staging 資料夾** → 等待指示

---

## 快速開始

### 1. 安裝依賴

```bash
pip install -r requirements.txt
```

### 2. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env 填入金鑰
```

### 3. 初始化資料庫（PostgreSQL + pgvector）

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 4. 啟動伺服器與 Bot

```bash
# 啟動 FastAPI 後端 (接收 OAuth callback)
uvicorn main:app --reload

# 啟動 Telegram Bot (新終端)
python telegram_bot.py
```

### 或：Docker Compose 一鍵啟動

```bash
docker compose up -d
```

---

## 環境變數

| 變數 | 說明 | 必填 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | ✅ |
| `GEMINI_API_KEY` | Gemini API 金鑰 | ✅ |
| `DATABASE_URL` | PostgreSQL 連線字串 | ✅ |
| `BACKEND_URL` | 後端 API 域名 (用於 Google OAuth 回呼) | ✅ |
| `ADMIN_IDS` | 管理員的 Telegram IDs | ✅ |
| `GOOGLE_CLIENT_ID` | Google OAuth 用戶端 ID | ✅ (Google Calendar) |
| `GOOGLE_CLIENT_SECRET`| Google OAuth 用戶端密鑰 | ✅ (Google Calendar) |
| `SOLANA_RPC_URL` | Solana Mainnet RPC 連結 | ✅ |
| `WEBHOOK_URL` | Webhook URL（生產環境）| ⬜ 空值用 Polling |

---

## 模型與 API 配置 (Model Configuration)

Nexus-OS 全面整合 Google Gemini AI 模型，您可以透過環境變數靈活配置不同任務所使用的模型：

| 應用場景 | 預設模型 | 環境變數 | 難度 | 說明 |
|----------|----------|----------|------|------|
| **核心大腦 (日常)** | `gemini-3-flash-preview` | `GEMINI_MODEL_FLASH` | 🟢 低 | 負責日常對話、天氣、股價等簡單指令與快速回覆。 |
| **核心大腦 (複雜)** | `gemini-3.1-pro-preview` | `GEMINI_MODEL_PRO` | 🔴 高 | 負責長文總結、程式碼分析、深層邏輯推理。 |
| **網頁詮釋 (Crawler)** | `gemini-2.5-flash` | `GEMINI_CRAWLER_MODEL` | 🟡 中 | 摸要網頁內容、归納熱門話題，軟q模型選這就夠。 |
| **圖片辨識 (OCR)** | `gemini-2.5-flash` | `GEMINI_OCR_MODEL` | 🟡 中 | 秘書 Agent 辨識圖片文字， Gemini Vision 輕量模型足該。 |
| **語音轉文字 (STT)** | `gemini-3-pro-preview` | `GEMINI_VOICE_MODEL` | 🟡 中 | Telegram 語音訊息轉錄（支援多模態音訊輸入）。 |
| **圖片生成 (Imagen)** | `imagen-4.0-generate-001` | `GEMINI_IMAGE_MODEL` | 🟡 中 | 圖片師 Agent 文字生成圖片，使用 Imagen 4。 |
| **圖片改造 (Vision)** | `gemini-3-pro-image-preview` | `GEMINI_IMAGE_VISION_MODEL` | 🟡 中 | 圖片師 Agent 分析與改造圖片（支援圖片輸入輸出）。 |
| **向量記憶 (Embedding)** | `models/gemini-embedding-001` | *(固定不變)* | 🟢 低 | 將歷史對話存入 pgvector 供長期語義檢索。 |

> [!NOTE]
> 各場景均已配置最適合的預設模型（見上表），日常用 Flash、複雜用 Pro，您也可以透過環境變數隨時覆蓋。

### 支援的 Gemini 模型清單

您的 API 金鑰（搭配目前的權限設定）已知支援以下模型，您可以將 `GEMINI_MODEL` 或 `GEMINI_VOICE_MODEL` 設為以下任一值：

<details>
<summary>點擊展開支援的模型清單</summary>

**核心對話與推理模型 (Pro / Flash)**
- `gemini-3.1-pro-preview`
- `gemini-3.1-pro-preview-customtools`
- `gemini-3-pro-preview`
- `gemini-3-flash-preview`
- `gemini-2.5-pro`
- `gemini-2.5-flash`
- `gemini-2.5-flash-lite`
- `gemini-2.0-flash`
- `gemini-2.0-flash-001`
- `gemini-pro-latest`
- `gemini-flash-latest`
- `gemini-flash-lite-latest`

**語音與音訊專用模型 (TTS / Audio)**
- `gemini-2.5-flash-preview-tts`
- `gemini-2.5-pro-preview-tts`
- `gemini-2.5-flash-native-audio-latest`
- `gemini-2.5-flash-native-audio-preview-09-2025`
- `gemini-2.5-flash-native-audio-preview-12-2025`

**多模態與視覺模型 (Image / Video)**
- `gemini-2.5-flash-image`
- `gemini-3-pro-image-preview`
- `gemini-2.0-flash-exp-image-generation`
- `imagen-4.0-generate-001`
- `imagen-4.0-ultra-generate-001`
- `imagen-4.0-fast-generate-001`
- `veo-2.0-generate-001`
- `veo-3.0-generate-001`
- `veo-3.0-fast-generate-001`
- `veo-3.1-generate-preview`
- `veo-3.1-fast-generate-preview`

**專項能力模型 (Research / Computer Use / Robotics / AQA)**
- `deep-research-pro-preview-12-2025`
- `gemini-2.5-computer-use-preview-10-2025`
- `gemini-robotics-er-1.5-preview`
- `aqa`

**輕量與邊緣裝置模型 (Gemma 3 / Nano)**
- `gemma-3-1b-it`
- `gemma-3-4b-it`
- `gemma-3-12b-it`
- `gemma-3-27b-it`
- `gemma-3n-e4b-it`
- `gemma-3n-e2b-it`
- `gemini-2.0-flash-lite`
- `gemini-2.0-flash-lite-001`
- `gemini-2.5-flash-lite-preview-09-2025`
- `nano-banana-pro-preview`

</details>

---

## 完成里程碑

| 模組 | 功能 | 狀態 |
|-------|------|------|
| 🗓️ 日程師 (CalendarAgent) | Google Calendar CRUD (支援 OAuth) | ✅ v1.1 |
| ⛅ 天氣師 (WeatherAgent) | 即時天氣 + 今日預報 | ✅ v1.2 |
| ⏰ 排程師 (ReminderAgent) | 定時提醒 + 持久化 + 任務協作 | ✅ v1.2 |
| 📂 秘書 (SecretaryAgent) | Drive 管理 + OCR + 雙向傳輸 | ✅ v1.2 |
| 🕷️ 爬虫師 (CrawlerAgent) | 網頁搜尋 + 抓取歸納 + 熱門話題 | ✅ v1.3 |
| 🎨 圖片師 (ImageArtistAgent) | Imagen 4 生成 + Gemini Vision 改造 | ✅ v1.4 |
| 💰 財務官 (Treasurer) | 內部 Token 消耗與 API 成本監控 | ✅ v1.5 |
| 📈 分析師 (Finance/Analyst) | 美股、加密貨幣報價與走勢分析 | ✅ v1.6 |
| 🔧 優化師 (OptimizerAgent) | 自我反思與修復 (Git-based rollback) | ✅ v2.0 |
| 👛 錢包師 (WalletAgent) | Base & Solana 雙鏈錢包 + Jupiter 鏈上交易 | ✅ v2.1 |
