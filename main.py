"""
main.py — Nexus-OS v1.1 主入口點

架構原則：
- 所有用戶訊息必須且只能透過 Director.process_request() 處理
- 嚴禁在此檔案直接呼叫任何 Agent 或工具函式
- lifespan 事件負責資料庫初始化與資源清理
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import init_db
from logic.director import Director
from api.auth import router as auth_router

# ------------------------------------------------------------------
# 全域 Director 實例（單例模式）
# ------------------------------------------------------------------
_director: Director | None = None


# ------------------------------------------------------------------
# FastAPI 應用程式生命週期管理
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    應用程式生命週期：
    - 啟動時：初始化資料庫、建立 Director 實例
    - 關閉時：可在此加入清理邏輯（關閉連線池等）
    """
    global _director

    print("🚀 Nexus-OS v1.1 正在啟動...")

    # 初始化資料庫（建立 MemoryStore、UsageLog 資料表）
    await init_db()

    # 建立指揮官大腦實例
    _director = Director()
    print("🧠 指揮官大腦已就緒")

    yield  # 應用程式運行中

    # 關閉清理（如有需要可在此加入）
    print("👋 Nexus-OS v1.1 正在關閉...")
    if _director:
        print("🛑 正在關閉指揮官大腦...")
        await _director.shutdown()


# ------------------------------------------------------------------
# FastAPI 應用程式實例
# ------------------------------------------------------------------

app = FastAPI(
    title="Nexus-OS v1.1",
    description="個人 AI 助理系統 — 核心指揮架構",
    version="1.1.0",
    lifespan=lifespan,
)

# 掛載 API 路由
app.include_router(auth_router)


# ------------------------------------------------------------------
# 請求 / 回應模型
# ------------------------------------------------------------------

class MessageRequest(BaseModel):
    """用戶訊息請求體"""
    user_id: str = Field(..., description="用戶唯一識別碼", example="user_001")
    session_id: str = Field(default="default", description="對話 Session ID", example="session_001")
    message: str = Field(..., description="用戶訊息（口語化文字）", example="下週三三點幫我加個會議")


class MessageResponse(BaseModel):
    """大腦回覆體"""
    reply: str = Field(..., description="助理的回覆文字")
    user_id: str = Field(..., description="用戶唯一識別碼")
    session_id: str = Field(..., description="對話 Session ID")


# ------------------------------------------------------------------
# API 路由
# ------------------------------------------------------------------

@app.get("/", summary="健康檢查")
async def health_check():
    """
    確認服務是否正常運行。
    """
    return {"status": "🟢 Nexus-OS v1.1 運行中", "version": "1.1.0"}


@app.post(
    "/message",
    response_model=MessageResponse,
    summary="發送訊息給大腦",
    description=(
        "將用戶訊息傳遞給指揮官大腦（Director）進行處理。\n\n"
        "大腦會自動：\n"
        "1. 檢索相關對話記憶\n"
        "2. 進行語言理解與推理\n"
        "3. 必要時調度對應 Agent（如日程師）\n"
        "4. 回傳自然語言回覆"
    ),
)
async def send_message(request: MessageRequest) -> MessageResponse:
    """
    所有訊息的統一入口點。
    嚴禁繞過此端點直接呼叫任何 Agent 或工具。
    """
    if _director is None:
        raise HTTPException(
            status_code=503,
            detail="系統尚未完成初始化，請稍後再試。",
        )

    if not request.message.strip():
        raise HTTPException(
            status_code=400,
            detail="訊息內容不能為空。",
        )

    try:
        reply = await _director.process_request(
            user_id=request.user_id,
            message=request.message,
            session_id=request.session_id,
        )
    except Exception as e:
        # 生產環境應記錄詳細錯誤日誌，此處簡化處理
        raise HTTPException(
            status_code=500,
            detail=f"處理訊息時發生錯誤：{str(e)}",
        )

    return MessageResponse(
        reply=reply,
        user_id=request.user_id,
        session_id=request.session_id,
    )
