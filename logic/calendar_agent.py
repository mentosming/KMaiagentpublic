"""
logic/calendar_agent.py — 先鋒 Agent：日程師
專門負責 Google Calendar 的 CRUD 操作（增刪改查）。

驗證方式：使用 GOOGLE_DRIVE_CREDENTIALS 環境變數指定的憑證 JSON 檔。
支援 Service Account 及 OAuth2 兩種憑證格式。
所有公開方法均為 async，內部透過 asyncio.get_event_loop().run_in_executor
將同步 Google API 呼叫轉為非阻塞。
"""

import asyncio
import os
from datetime import datetime, timezone
from functools import partial
from typing import Any

from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# Google Calendar API 所需的權限範圍
_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# 預設行事曆 ID（'primary' 代表用戶的主要行事曆）
_DEFAULT_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Google 憑證：支援 JSON 字串（雲端平台）或檔案路徑（本地）
_CREDENTIALS_RAW = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "credentials.json")


def _load_cred_data() -> dict:
    """
    解析憑證來源：
    - 若環境變數值以 '{' 開頭 → 直接當 JSON 字串解析（適合 Zeabur / Railway 等雲端平台）
    - 否則視為檔案路徑，讀取該檔案
    """
    import json

    raw = _CREDENTIALS_RAW.strip()

    # ── 方式一：JSON 字串（雲端環境直接貼上 JSON）──────────────────
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_DRIVE_CREDENTIALS 的 JSON 格式有誤：{e}") from e

    # ── 方式二：檔案路徑 ────────────────────────────────────────────
    if not os.path.exists(raw):
        raise FileNotFoundError(
            f"找不到 Google 憑證檔：{raw}\n"
            f"請在 .env 中將 GOOGLE_DRIVE_CREDENTIALS 設為 JSON 字串或正確的檔案路徑。"
        )
    with open(raw, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_service():
    """
    建立並回傳 Google Calendar API 服務物件。
    支援 Service Account 及含 refresh_token 的 OAuth2 兩種憑證格式。
    """
    cred_data = _load_cred_data()

    # ── 嘗試 Service Account 憑證（適合伺服器環境）─────────────────
    if cred_data.get("type") == "service_account":
        try:
            creds = service_account.Credentials.from_service_account_info(
                cred_data, scopes=_SCOPES
            )
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            raise RuntimeError(f"Service Account 憑證建立失敗：{e}") from e

    # ── 嘗試 OAuth2 憑證（含 refresh_token 的 authorized user）──────
    if "refresh_token" in cred_data:
        try:
            creds = Credentials.from_authorized_user_info(cred_data, _SCOPES)
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            raise RuntimeError(f"OAuth2 憑證建立失敗：{e}") from e

    raise RuntimeError(
        "不支援的憑證格式。請提供 Service Account JSON 或含 refresh_token 的 OAuth2 JSON。"
    )


class CalendarAgent:
    """
    日程師 Agent — 封裝 Google Calendar API 的所有操作。

    使用方式：
        agent = CalendarAgent()
        events = await agent.list_events(time_min=datetime.now(tz=timezone.utc))
    """

    def __init__(self, registry: "AgentRegistry | None" = None) -> None:
        """
        初始化日程師，建立 Google Calendar API 連線。

        參數：
            registry - AgentRegistry 實例（統一建構規則，供未來協作用）
        """
        self._registry = registry
        self._service = _build_service()
        self._default_calendar_id = _DEFAULT_CALENDAR_ID

    # ------------------------------------------------------------------
    # 公開方法：新增活動
    # ------------------------------------------------------------------

    async def create_event(
        self,
        summary: str,
        start_dt: datetime,
        end_dt: datetime,
        description: str = "",
        location: str = "",
        calendar_id: str = None,
    ) -> dict[str, Any]:
        """
        在 Google Calendar 新增一個活動。

        參數：
            summary     - 活動標題（如：「與 Alice 開會」）
            start_dt    - 活動開始時間（datetime，需含時區資訊）
            end_dt      - 活動結束時間（datetime，需含時區資訊）
            description - 活動描述（可選）
            location    - 活動地點（可選）
            calendar_id - 目標行事曆 ID（預設使用 GOOGLE_CALENDAR_ID 環境變數）

        回傳：
            Google Calendar API 回傳的活動物件（dict）
        """
        cal_id = calendar_id or self._default_calendar_id

        event_body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": str(start_dt.tzinfo) if start_dt.tzinfo else "Asia/Hong_Kong",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": str(end_dt.tzinfo) if end_dt.tzinfo else "Asia/Hong_Kong",
            },
        }

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                partial(
                    self._service.events().insert(
                        calendarId=cal_id, body=event_body
                    ).execute
                ),
            )
            return result
        except HttpError as e:
            raise RuntimeError(f"新增活動失敗：{e.reason}") from e

    # ------------------------------------------------------------------
    # 公開方法：查詢活動列表
    # ------------------------------------------------------------------

    async def list_events(
        self,
        time_min: datetime = None,
        time_max: datetime = None,
        max_results: int = 10,
        query: str = None,
        calendar_id: str = None,
    ) -> list[dict[str, Any]]:
        """
        查詢指定時間範圍內的活動列表。

        參數：
            time_min    - 查詢起始時間（預設為當下）
            time_max    - 查詢截止時間（可選）
            max_results - 最多返回幾筆（預設 10）
            query       - 關鍵字搜尋（可選）
            calendar_id - 目標行事曆 ID

        回傳：
            活動物件列表（list of dict）
        """
        cal_id = calendar_id or self._default_calendar_id

        if time_min is None:
            time_min = datetime.now(tz=timezone.utc)

        params: dict[str, Any] = {
            "calendarId": cal_id,
            "timeMin": time_min.isoformat(),
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_max:
            params["timeMax"] = time_max.isoformat()
        if query:
            params["q"] = query

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                partial(self._service.events().list(**params).execute),
            )
            return result.get("items", [])
        except HttpError as e:
            raise RuntimeError(f"查詢活動失敗：{e.reason}") from e

    # ------------------------------------------------------------------
    # 公開方法：更新活動
    # ------------------------------------------------------------------

    async def update_event(
        self,
        event_id: str,
        updates: dict[str, Any],
        calendar_id: str = None,
    ) -> dict[str, Any]:
        """
        更新指定活動的欄位。

        參數：
            event_id    - 要更新的活動 ID（從 list_events 結果中取得）
            updates     - 需要更新的欄位（dict），格式與 Google Calendar API 相同
            calendar_id - 目標行事曆 ID

        回傳：
            更新後的活動物件（dict）
        """
        cal_id = calendar_id or self._default_calendar_id

        loop = asyncio.get_event_loop()
        try:
            # 先取得現有活動資料
            existing = await loop.run_in_executor(
                None,
                partial(
                    self._service.events().get(
                        calendarId=cal_id, eventId=event_id
                    ).execute
                ),
            )
            # 合併更新欄位
            existing.update(updates)
            # 更新活動
            result = await loop.run_in_executor(
                None,
                partial(
                    self._service.events().update(
                        calendarId=cal_id, eventId=event_id, body=existing
                    ).execute
                ),
            )
            return result
        except HttpError as e:
            raise RuntimeError(f"更新活動失敗：{e.reason}") from e

    # ------------------------------------------------------------------
    # 公開方法：刪除活動
    # ------------------------------------------------------------------

    async def delete_event(
        self,
        event_id: str,
        calendar_id: str = None,
    ) -> bool:
        """
        刪除指定活動。

        參數：
            event_id    - 要刪除的活動 ID
            calendar_id - 目標行事曆 ID

        回傳：
            True 代表刪除成功，失敗則拋出 RuntimeError
        """
        cal_id = calendar_id or self._default_calendar_id

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(
                    self._service.events().delete(
                        calendarId=cal_id, eventId=event_id
                    ).execute
                ),
            )
            return True
        except HttpError as e:
            raise RuntimeError(f"刪除活動失敗：{e.reason}") from e

    # ------------------------------------------------------------------
    # 工具函式：格式化活動為可讀文字
    # ------------------------------------------------------------------

    @staticmethod
    def format_event(event: dict[str, Any]) -> str:
        """
        將 Google Calendar API 回傳的活動物件格式化為繁體中文可讀字串。

        用於在大腦回覆用戶前整理查詢結果。
        """
        summary = event.get("summary", "（無標題）")
        start = event.get("start", {})
        start_time = start.get("dateTime") or start.get("date", "（未知時間）")
        location = event.get("location", "")
        description = event.get("description", "")
        event_id = event.get("id", "")

        parts = [f"📅 **{summary}**", f"🕐 開始：{start_time}"]
        if location:
            parts.append(f"📍 地點：{location}")
        if description:
            parts.append(f"📝 備注：{description}")
        parts.append(f"🆔 ID：`{event_id}`")

        return "\n".join(parts)
