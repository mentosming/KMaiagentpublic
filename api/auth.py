"""
api/auth.py — 認證相關 API 端點

功能：
1. POST /api/auth/telegram — 驗證 Telegram Login Widget 簽名，建立或查詢用戶
2. GET  /api/auth/google   — 生成 Google Calendar OAuth URL
3. GET  /api/auth/google/callback — 接收 Google 回呼，儲存 refresh_token
"""

import hashlib
import json
import os
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, UserGoogleCredential

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# ── 環境變數 ──────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
# SECRET_KEY 前 32 bytes base64url 編碼作為 Fernet key
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_ME_32_chars_secret_key_xx")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

import base64
_fernet_key = base64.urlsafe_b64encode(SECRET_KEY.encode()[:32].ljust(32, b"0"))
_fernet = Fernet(_fernet_key)

GOOGLE_REDIRECT_URI = f"{BACKEND_URL}/api/auth/google/callback"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar"


# ── 依賴注入 ──────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── 路由 ──────────────────────────────────────────────────────────

@router.get("/google")
async def google_oauth_redirect(telegram_id: str):
    """
    生成 Google Calendar OAuth 授權 URL 並重定向。
    需要在 query 帶上 telegram_id 作為 state。
    """
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": telegram_id,
    }
    auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"
    return RedirectResponse(auth_url)


@router.get("/google/callback")
async def google_oauth_callback(
    code: str,
    state: str,  # state = telegram_id
    db: AsyncSession = Depends(get_db)
):
    """
    Google 授權完成後的回呼端點。
    用 code 換取 refresh_token，Fernet 加密後存入 DB。
    """
    # 用 code 換取 tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            }
        )
        token_data = resp.json()

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return {"status": "error", "message": "Failed to obtain refresh token. Please try again."}

    # 加密後存入 DB
    encrypted = _fernet.encrypt(refresh_token.encode()).decode()
    telegram_id = state

    result = await db.execute(
        select(UserGoogleCredential).where(UserGoogleCredential.telegram_id == telegram_id)
    )
    cred = result.scalar_one_or_none()

    if cred:
        cred.refresh_token = encrypted
    else:
        cred = UserGoogleCredential(telegram_id=telegram_id, refresh_token=encrypted)
        db.add(cred)

    await db.commit()

    return {
        "status": "success",
        "message": "✅ Google Calendar 授權成功！您現在可以回到 Telegram 與 Agent 對話了。您可以關閉此視窗。"
    }


def get_google_credentials_for_user(telegram_id: str, refresh_token_encrypted: str):
    """
    解密 refresh_token 並返回 google.oauth2.credentials.Credentials 物件。
    供 CalendarAgent 使用。
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    refresh_token = _fernet.decrypt(refresh_token_encrypted.encode()).decode()

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds
