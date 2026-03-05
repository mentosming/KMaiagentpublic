# 📊 Wednesday (Nexus-OS) 系統部署狀態報告

本報告旨在針對您提交的四大核心要點進行現狀核查，確認系統在 **Zeabur** 環境下的運行穩定性與數據安全性。

---

## 1. 運行環境與穩定性 (Zeabur Docker)
*   **容器化架構**：系統基於 `Dockerfile` 構建，採用輕量且穩定的 `python:3.11-slim` 鏡像。
*   **自癒機制**：`start.sh` 配置了無限循環重啟邏輯。若 `telegram_bot.py` 發生崩潰或非預期停止，系統將在 5 秒內自動拉起，確保 24/7 在線。
*   **資源管理**：系統導入了 **WorkerBus（解耦事件匯流排）**。所有的後台任務（如日誌紀錄、向量檢索後的寫入）均非同步執行，不會阻塞主對話流程，有效降低 RAM 峰值並提升反應速度。

## 2. 資料庫連線狀態 (PostgreSQL)
*   **連線驅動**：使用 `asyncpg` 高性能非同步驅動程式。
*   **雲端適配**：`database.py` 已內置 `_resolve_db_url()` 函式，能自動識別並解析 Zeabur 內部注入的各式變數（`POSTGRES_URI`, `DATABASE_URL` 等），確保能成功穩定連接至 **Zeabur 內部內部 PostgreSQL**。
*   **初始化檢查**：系統啟動時會自動檢查 `pgvector` 擴充。若資料庫就緒，會自動建立 `MemoryStore` (對話記憶)、`UsageLog` (用量) 及 `ReminderStore` (提醒) 等資料表。

## 3. 持久化存儲 (Persistence)
*   **對話長期記憶**：數據存儲於 `memory_store_v3`。利用 `pgvector` 進行語義嵌入，即使重啟容器，歷史對話記憶依然存在，系統會根據上下文自動檢索相關內容。
*   **Docker 持久化**：`docker-compose.yml` 中配置了 `nexus_pgdata` 卷，將 `/var/lib/postgresql/data` 直接映射至持久盤。
*   **雲端託管模式**：在 Zeabur 部署時，推薦使用其託管的 PostgreSQL 服務，這將保證數據在多版本部署（Zero-downtime deployment）之間是無縫共享且永久保存的。

## 4. API Token 傳輸與紀錄 (Usage Tracking)
*   **精準紀錄**：`Director` 在每次與 Gemini 交互後，會擷取回傳的 Token 數據並包裝成 `TelemetryEvent`。
*   **非同步寫入**：這些數據透過 `WorkerBus` 發送給 `AuditorAgent`（專屬監控 Agent），並最終寫入 `UsageLog` 表中。
*   **財務分析**：現狀已完整支持透過 `TreasurerAgent` 隨時查詢「今日成本」、「月度帳單」以及「模型消耗排行」。

---

**⚠️ 目前建議：**
*   請確保您的 Zeabur 環境變數中 `ADMIN_IDS` 已正確設定為您的 Telegram ID，以便接收最新的系統心跳日誌。
*   若需進行大規模壓力測試，目前的 `500` 筆 Queue 緩衝已足夠因應日常高頻對話。

**報告人：Nexus-OS 指揮官**
