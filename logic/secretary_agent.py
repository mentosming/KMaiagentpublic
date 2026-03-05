"""
logic/secretary_agent.py — 秘書 Agent

職責：
  - Google Drive 文件管理（列出、讀取、建立、搜尋）
  - Telegram ↔ Drive 雙向文件傳輸
      上傳：Telegram 文件/圖片 → Drive Staging 資料夾（暫存）
      下載：Drive 文件 → 傳送給用戶 Telegram
  - 圖片 OCR（使用 Gemini Vision，無需額外 API Key）

架構規則：
    符合 AgentRegistry 統一建構簽名 __init__(self, registry, ...)
    透過 registry.get(name) 可呼叫其他 Agent（如日程師、天氣師）。

環境變數：
    GOOGLE_DRIVE_CREDENTIALS   - 服務帳號 JSON 路徑（與 CalendarAgent 共用）
    DRIVE_STAGING_FOLDER_ID    - Telegram 上傳暫存資料夾 ID（可選）
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

load_dotenv()

# Google Drive API 所需權限範圍
_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Telegram 上傳暫存資料夾
_STAGING_FOLDER_ID = os.getenv("DRIVE_STAGING_FOLDER_ID", None)

# 型別別名：發送文件給用戶的 callback
# 參數：(chat_id, filename, file_bytes, mime_type, caption)
SendFileFn = Callable[[str, str, bytes, str, str], Awaitable[None]]

# Google 憑證：支援 JSON 字串（雲端平台）或檔案路徑（本地）
_CREDENTIALS_RAW = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "credentials.json")



def _load_cred_data() -> dict:
    """
    解析憑證來源（與 CalendarAgent 相同邏輯）：
    - 若環境變數值以 '{' 開頭 → 直接當 JSON 字串解析（適合 Zeabur / Railway）
    - 否則視為檔案路徑，讀取該檔案
    """
    raw = _CREDENTIALS_RAW.strip()

    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_DRIVE_CREDENTIALS 的 JSON 格式有誤：{e}") from e

    if not os.path.exists(raw):
        raise FileNotFoundError(
            f"找不到 Google Drive 憑證檔：{raw}\n"
            "請設定 GOOGLE_DRIVE_CREDENTIALS 環境變數（JSON 字串或正確檔案路徑）"
        )
    with open(raw, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_drive_service() -> Any:
    """建立 Google Drive API Service 實例（支援 Service Account 和 OAuth2）。"""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    cred_data = _load_cred_data()

    if cred_data.get("type") == "service_account":
        try:
            creds = service_account.Credentials.from_service_account_info(
                cred_data, scopes=_SCOPES
            )
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            raise RuntimeError(f"Service Account 憑證建立失敗：{e}") from e

    if "refresh_token" in cred_data:
        try:
            creds = Credentials.from_authorized_user_info(cred_data, _SCOPES)
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            raise RuntimeError(f"OAuth2 憑證建立失敗：{e}") from e

    raise RuntimeError(
        "不支援的憑證格式。請提供 Service Account JSON 或含 refresh_token 的 OAuth2 JSON。"
    )


class SecretaryAgent:
    """
    秘書 Agent — 管理 Google Drive 文件與 Telegram 文件傳輸。

    符合 AgentRegistry 統一建構簽名，可被其他 Agent 查詢。
    """

    def __init__(
        self,
        registry: "AgentRegistry",
        send_file_fn: SendFileFn | None = None,
    ) -> None:
        """
        初始化秘書。

        參數：
            registry     - AgentRegistry 實例（統一規則）
            send_file_fn - 發送文件給 Telegram 用戶的 callback
        """
        self._registry = registry
        self._send_file_fn = send_file_fn
        self._service = _build_drive_service()
        self._staging_folder_id = _STAGING_FOLDER_ID

    # ------------------------------------------------------------------
    # 公開方法：列出文件
    # ------------------------------------------------------------------

    async def list_files(
        self,
        folder_id: str | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """
        列出 Google Drive 文件。

        參數：
            folder_id   - 指定資料夾（None 表示全部）
            query       - 額外篩選條件（Drive query 語法）
            max_results - 最多回傳數量

        回傳：[{id, name, mimeType, modifiedTime, size, webViewLink}]
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._list_files_sync(folder_id, query, max_results)
        )

    def _list_files_sync(
        self,
        folder_id: str | None,
        query: str | None,
        max_results: int,
    ) -> list[dict]:
        q_parts = ["trashed = false"]
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")
        if query:
            q_parts.append(f"name contains '{query}'")
        q = " and ".join(q_parts)

        result = self._service.files().list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
            orderBy="modifiedTime desc",
        ).execute()
        return result.get("files", [])

    # ------------------------------------------------------------------
    # 公開方法：搜尋文件（全文搜尋）
    # ------------------------------------------------------------------

    async def search_files(self, query: str, max_results: int = 10) -> list[dict]:
        """全文搜尋 Google Drive（搜尋文件名稱與內容）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._search_files_sync(query, max_results)
        )

    def _search_files_sync(self, query: str, max_results: int) -> list[dict]:
        q = f"fullText contains '{query}' and trashed = false"
        result = self._service.files().list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc",
        ).execute()
        return result.get("files", [])

    # ------------------------------------------------------------------
    # 公開方法：讀取文件內容
    # ------------------------------------------------------------------

    async def read_file(self, file_id: str) -> str:
        """
        讀取文件內容（純文字或 Google Docs 匯出為純文字）。

        回傳：文件文字內容（最多 8000 字元）
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._read_file_sync(file_id)
        )

    def _read_file_sync(self, file_id: str) -> str:
        from googleapiclient.errors import HttpError

        # 先查文件 MIME type
        meta = self._service.files().get(
            fileId=file_id, fields="mimeType,name"
        ).execute()
        mime = meta.get("mimeType", "")

        if "google-apps.document" in mime:
            # Google Docs → 匯出為純文字
            content = self._service.files().export(
                fileId=file_id, mimeType="text/plain"
            ).execute()
        elif mime.startswith("text/"):
            content = self._service.files().get_media(fileId=file_id).execute()
        else:
            return f"[無法直接讀取 {mime} 類型的文件，請使用 send_to_me 下載]"

        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        return text[:8000] + ("..." if len(text) > 8000 else "")

    # ------------------------------------------------------------------
    # 公開方法：建立文件
    # ------------------------------------------------------------------

    async def create_file(
        self,
        name: str,
        content: str,
        folder_id: str | None = None,
    ) -> dict:
        """
        在 Drive 建立純文字文件。

        回傳：{id, name, webViewLink}
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._create_file_sync(name, content, folder_id)
        )

    def _create_file_sync(
        self, name: str, content: str, folder_id: str | None
    ) -> dict:
        from googleapiclient.http import MediaInMemoryUpload

        metadata: dict = {"name": name, "mimeType": "text/plain"}
        if folder_id:
            metadata["parents"] = [folder_id]

        media = MediaInMemoryUpload(
            content.encode("utf-8"), mimetype="text/plain"
        )
        file = self._service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, webViewLink",
        ).execute()
        return {"id": file["id"], "name": file["name"], "webViewLink": file.get("webViewLink")}

    # ------------------------------------------------------------------
    # 公開方法：上傳二進位內容（Telegram 文件暫存）
    # ------------------------------------------------------------------

    async def upload_bytes(
        self,
        filename: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        folder_id: str | None = None,
    ) -> dict:
        """
        上傳二進位內容至 Drive。
        Telegram 文件/圖片接收後呼叫此方法暫存。

        回傳：{id, name, mimeType, webViewLink}
        """
        target_folder = folder_id or self._staging_folder_id
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._upload_bytes_sync(filename, data, mime_type, target_folder)
        )

    def _upload_bytes_sync(
        self,
        filename: str,
        data: bytes,
        mime_type: str,
        folder_id: str | None,
    ) -> dict:
        from googleapiclient.http import MediaInMemoryUpload

        metadata: dict = {"name": filename}
        if folder_id:
            metadata["parents"] = [folder_id]

        media = MediaInMemoryUpload(data, mimetype=mime_type, resumable=False)
        file = self._service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, mimeType, webViewLink",
        ).execute()
        return {
            "id": file["id"],
            "name": file["name"],
            "mimeType": file.get("mimeType"),
            "webViewLink": file.get("webViewLink"),
        }

    # ------------------------------------------------------------------
    # 公開方法：下載文件（Drive → Telegram）
    # ------------------------------------------------------------------

    async def download_bytes(self, file_id: str) -> tuple[str, bytes, str]:
        """
        從 Drive 下載文件內容。

        回傳：(filename, data_bytes, mime_type)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._download_bytes_sync(file_id)
        )

    def _download_bytes_sync(self, file_id: str) -> tuple[str, bytes, str]:
        meta = self._service.files().get(
            fileId=file_id, fields="name, mimeType"
        ).execute()
        filename = meta.get("name", "file")
        mime = meta.get("mimeType", "application/octet-stream")

        # Google Docs 系列：匯出為 Office 格式
        export_map = {
            "application/vnd.google-apps.document": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".docx",
            ),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xlsx",
            ),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                ".pptx",
            ),
        }

        if mime in export_map:
            export_mime, ext = export_map[mime]
            if not filename.endswith(ext):
                filename += ext
            data = self._service.files().export(
                fileId=file_id, mimeType=export_mime
            ).execute()
            return filename, data, export_mime
        else:
            data = self._service.files().get_media(fileId=file_id).execute()
            return filename, data, mime

    # ------------------------------------------------------------------
    # 公開方法：發送文件給用戶（Drive → Telegram）
    # ------------------------------------------------------------------

    async def send_to_user(self, file_id: str, chat_id: str, caption: str = "") -> dict:
        """
        從 Drive 下載文件並透過 Telegram 發送給用戶。

        參數：
            file_id - Drive 文件 ID
            chat_id - Telegram Chat ID
            caption - 附加說明文字

        回傳：{success, filename}
        """
        if not self._send_file_fn:
            return {"error": "send_file_fn 未設定，無法發送文件至 Telegram"}

        try:
            filename, data, mime = await self.download_bytes(file_id)
            await self._send_file_fn(chat_id, filename, data, mime, caption)
            return {"success": True, "filename": filename}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 公開方法：OCR（Gemini Vision 提取圖片文字）
    # ------------------------------------------------------------------

    async def ocr_image(self, file_id: str) -> str:
        """
        從 Drive 下載圖片並用 Gemini Vision 提取文字。

        回傳：圖片中的文字內容
        """
        import google.generativeai as genai

        try:
            _, data, mime = await self.download_bytes(file_id)

            # 用 Gemini Vision 分析（OCR 難度中等，用輕量模型即可）
            _model_name = os.getenv("GEMINI_OCR_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
            model = genai.GenerativeModel(_model_name)
            loop = asyncio.get_event_loop()
            response = await model.generate_content_async([
                    {
                        "mime_type": mime if mime.startswith("image/") else "image/jpeg",
                        "data": data,
                    },
                    "請提取並回傳圖片中的所有文字，保持原始格式與排版。若沒有文字，請說明圖片內容。",
                ])
            return response.text.strip()
        except Exception as e:
            return f"OCR 失敗：{e}"

    # ------------------------------------------------------------------
    # 靜態工具：格式化文件列表
    # ------------------------------------------------------------------

    @staticmethod
    def format_file(f: dict) -> str:
        """將文件 dict 格式化為可讀字串。"""
        name = f.get("name", "未知")
        fid = f.get("id", "")
        link = f.get("webViewLink", "")
        mtime = f.get("modifiedTime", "")[:10] if f.get("modifiedTime") else "未知"
        size = f.get("size")
        size_str = f"{int(size) // 1024}KB" if size else ""
        parts = [f"📄 **{name}**"]
        if size_str:
            parts.append(f"大小：{size_str}")
        parts.append(f"修改：{mtime}")
        parts.append(f"ID：`{fid}`")
        if link:
            parts.append(f"[開啟]({link})")
        return " | ".join(parts)
