# 🧠 Nexus-OS — 標準作業程序 (SOP) 與使用手冊

本手冊旨在指導開發者與使用者如何從零開始部署、設定並營運 **Nexus-OS — 個人自主 AI 錢包與助理平台**。

---

## 1. 前期準備 (Prerequisites)

在開始部署前，請確保您已準備好以下資源：

### 🛠️ 技術組件
*   **Python 3.11+**: 核心執行環境。
*   **PostgreSQL (含 pgvector)**: 用於儲存對話記憶、任務與用量記錄。
*   **Docker & Docker Compose** (建議): 用於快速啟動資料庫與 Bot。

### 🔑 必要的 API 金鑰
1.  **Gemini API Key**: 到 [Google AI Studio](https://aistudio.google.com/) 申請。
2.  **Telegram Bot Token**: 透過 [@BotFather](https://t.me/botfather) 建立機器人並獲取 Token。
3.  **Google Cloud Project**: 
    *   啟用 **Google Calendar API** 與 **Google Drive API**。
    *   建立 **OAuth 2.0 用戶端 ID** (Web 應用程式)，用於用戶個人授權。
    *   (可選) 建立 **Service Account** 用於祕書 Agent 的共用空間。
4.  **Web3 RPC URL**:
    *   **Base**: 預設使用 `https://mainnet.base.org`。
    *   **Solana**: 建議使用 Infura 或 QuickNode 的 Mainnet 連結，填入 `SOLANA_RPC_URL`。

---

## 2. 環境部署步驟 (Installation)

### 第一步：複製儲存庫與安裝依賴
```bash
git clone https://github.com/mentosming/KMaiagentpublic.git
cd KMaiagentforpublic

# 建立並啟動虛擬環境
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate

# 安裝依賴
pip install -r requirements.txt
```

### 第二步：設定環境變數 (.env)
複製 `env.example` 並重新命名為 `.env`，填入以下關鍵數值：
*   `GEMINI_API_KEY`: 您的 Gemini 金鑰。
*   `TELEGRAM_BOT_TOKEN`: 機器人 Token。
*   `ADMIN_IDS`: 您的 Telegram ID (用於管理功能)。
*   `BACKEND_URL`: 部署後的網址 (Google OAuth 回傳需要)。
*   `SECRET_KEY`: 32 位元的隨機字串。

### 第三步：啟動資料庫
推薦使用 Docker Compose：
```bash
docker compose up -d db
```
或者，如果您手動安裝了 PostgreSQL，請確保執行：
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 3. 系統運行 (Execution)

### 啟動核心服務
您需要同時啟動 **API 伺服器** (負責 OAuth) 與 **Telegram Bot**:

```bash
# 終端 1: 啟動 API
uvicorn main:app --host 0.0.0.0 --port 8000

# 終端 2: 啟動 Bot
python telegram_bot.py
```

---

## 4. 功能使用指南 (Usage)

### 👛 錢包與交易 (Wallet & Swap)
*   **查詢餘額**: 在 Telegram 輸入「查我的 SOL 餘額」或「我有多少代幣」。
*   **執行交易**: 輸入「幫我把 1 USDT 換成 SOL」。Agent 會自動調用 **Jupiter Aggregator** 獲取最優報價並簽名發送。
*   **安全注意**: 首次啟動會生成 `AGENT_SOLANA_PRIVATE_KEY` 於 `.env` 中，請務必妥善保管。

### 🗓️ 日程與文件管理 (Google Service)
*   **連結 Google**: 輸入「我想連結 Google 日曆」，Agent 會給您一個授權連結。完成後，Agent 即可代為管理日程與 Drive 文件。
*   **操作指令**: 「搜尋 Drive 裡關於合約的文件」或「下週一下午三點安排會議」。

### 🕷️ 資訊撈取與分析 (Crawler & Finance)
*   **熱門話題**: 「今天連登有什麼新鮮事？」
*   **金融分析**: 「分析一下 NVDA 現價與走勢」。

---

## 5. 保安與存取控制 (Security & Access Control)

為了確保您的 AI Agent 不被未經授權的第三方使用，本系統內建了 **強制存取控制 (White-list)**：

1.  **管理員權限**：在 `.env` 中設定的 `ADMIN_IDS` 擁有最高權限，可直接使用機器人。
2.  **新用戶審批流**：
    *   任何新用戶向機器人發送第一條訊息時，系統會自動建立「待審批 (Pending)」記錄。
    *   機器人會立即向所有管理員發送通知訊息，包含用戶的名稱與 ID。
    *   管理員可直接在 Telegram 對話框點選 **「✅ 批准」** 或 **「❌ 拒絕」**。
    *   一旦批准，用戶將收到通知並獲得完整的 AI 存取權限。
3.  **防止濫用**：已被拒絕或未經授權的用戶將無法接收 AI 的任何回覆。

---

## 6. 常見問題與維護 (Maintenance)

*   **無法調用工具**: 檢查 `GEMINI_API_KEY` 是否有效。
*   **Solana 交易失敗**: 確保錢包中有少許 SOL 作為 Gas Fee。
*   **資料庫連線錯誤**: 檢查 `.env` 中的 `DATABASE_URL` 格式是否為 `postgresql+asyncpg://...`。

---

**Nexus-OS — 您的自主 AI 助理，讓鏈上與生活更聰明。**
