# ======================================
# Nexus-OS v1.1 — Dockerfile
# 用於 Zeabur / 任何 Docker 環境部署
# ======================================

# 使用官方 Python 3.11 slim 鏡像（穩定、輕量）
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 設定 Python 輸出為即時（避免日誌延遲）
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# 安裝系統依賴（PostgreSQL client 函式庫）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先複製依賴清單（利用 Docker 層快取）
COPY requirements.txt .

# 安裝 Python 依賴
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade -r requirements.txt

# 複製源碼
COPY . .

# 暴露端口（Zeabur 會自動偵測 PORT 環境變數）
EXPOSE 8443

# 在同一個容器內啟動 FastAPI 後台與 Telegram Bot
CMD ["bash", "start.sh"]
