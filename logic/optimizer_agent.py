"""
logic/optimizer_agent.py — 系統優化師 (Optimizer Agent)

職責：
1. 作為 24/7 背景心跳機制的一部分，定時分析系統日誌與回饋。
2. 提出優化提案（AST/檔案修改）並發送至 Telegram 等待管理員批准。
3. 執行程式碼修改，並在修改前使用 Git 建立 Commit 作為備份。
4. 提供 /rollback 機制，若發生錯誤可立即恢復上一個穩定狀態。
"""

import json
import logging
import os
import subprocess
from typing import Any, Awaitable, Callable, Optional

import google.generativeai as genai
from logic.agent_registry import AgentRegistry
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Admin 權限設定
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [x.strip() for x in ADMIN_IDS_STR.split(",") if x.strip()]

GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

_OPTIMIZER_PROMPT = """你現在是 Nexus-OS 的「系統優化師」（Optimizer Agent）。
你的職責是分析過去的對話日誌與錯誤紀錄，主動尋找可以優化的地方（例如：效能瓶頸、異常錯誤、程式碼簡化等），並提供對應的修改方案。

【嚴格規則】
1. 若沒有發現明顯可以優化的地方，請明確表示「目前系統運作良好，無需修改」。
2. 若發現可優化的項目，請給出具體的「檔案路徑」與「修改內容的完整程式碼替換（Replacement Chunks）」。
3. 產生提案時，必須非常謹慎，不可隨意刪除核心邏輯。
4. 回覆必須是嚴格的 JSON 格式：
{
  "has_optimization": true/false,
  "reason": "發現了甚麼問題以及為什麼要修改...",
  "target_file": "logic/some_agent.py",
  "replacements": [
    {"search": "舊程式碼...", "replace": "新程式碼..."}
  ]
}
"""

class OptimizerAgent:
    def __init__(
        self, 
        registry: AgentRegistry, 
        send_approval_fn: Optional[Callable[[str, dict], Awaitable[None]]] = None
    ) -> None:
        self._registry = registry
        self._send_approval_fn = send_approval_fn
        
        # 建立 Gemini Pro 實例，限定回傳 JSON
        self._model = genai.GenerativeModel(
            model_name=os.getenv("GEMINI_MODEL_PRO", "gemini-3.1-pro-preview"),
            system_instruction=_OPTIMIZER_PROMPT,
            generation_config={"response_mime_type": "application/json"}
        )
        
        # 暫存正在等待批准的提案
        self.pending_proposal: dict | None = None

    async def analyze_system(self) -> dict:
        """主動分析系統狀態，若發現可優化點則生成提案並通知 Admin。"""
        if not ADMIN_IDS:
            logger.warning("未設定 ADMIN_IDS，優化提案將無法送達。")
            return {"error": "未設定 ADMIN_IDS。"}

        # 這裡模擬撈取系統日誌
        # 未來可擴充為從資料庫讀取 UsageLog 或 Error Logs
        analysis_context = "近期系統日誌：無嚴重的 Exception。請評估是否需要重構或效能最佳化。若無，請直接回傳 has_optimization: false。"
        
        try:
            response = await self._model.generate_content_async(
                f"請分析以下運行狀態並提出優化提案：\n{analysis_context}"
            )
            result = json.loads(response.text)
            
            if result.get("has_optimization"):
                self.pending_proposal = result
                await self.propose_optimization(result)
                
            return result
        except Exception as e:
            logger.error(f"分析系統時發生錯誤: {e}")
            return {"error": str(e)}

    async def propose_optimization(self, proposal: dict) -> None:
        """向所有 Admin 發送審批推播。"""
        if not self._send_approval_fn:
            logger.warning("未設定 send_approval_fn，無法發送審批請求。")
            return
            
        for admin_id in ADMIN_IDS:
            try:
                await self._send_approval_fn(admin_id, proposal)
            except Exception as e:
                logger.error(f"傳送優化提案給 {admin_id} 失敗：{e}")

    async def apply_optimization(self) -> str:
        """執行等待中的優化提案（檔案操作），並先進行 Git 備份。"""
        if not self.pending_proposal:
            return "沒有等待批准的優化提案。"
            
        target_file = self.pending_proposal.get("target_file", "")
        replacements = self.pending_proposal.get("replacements", [])
        reason = self.pending_proposal.get("reason", "自動優化")
        
        if not target_file:
            return "❌ 目標檔案未提供。"

        abs_path = os.path.abspath(target_file)
        if not os.path.exists(abs_path):
            return f"❌ 找不到目標檔案：{target_file}"
            
        # 1. 備份：Git Commit
        backup_res = self._git_backup(f"Auto-backup before optimization: {reason}")
        if "Error" in backup_res:
             # 如果備份失敗可能只是因為沒有變更，這裡容忍繼續
            logger.warning(f"Git 備份可能有問題：{backup_res}")
            
        # 2. 應用修改
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            for rep in replacements:
                search_text = rep.get("search", "")
                replace_text = rep.get("replace", "")
                if search_text and search_text in content:
                    content = content.replace(search_text, replace_text)
                else:
                    logger.warning(f"目標取代字串未找到: {search_text[:30]}...")
                    
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
                
            self.pending_proposal = None
            
            # 修改後也做一次 commit 紀錄
            self._git_backup(f"Applied optimization: {reason}")
            
            return f"✅ 已成功優化 `{target_file}`。"
        except Exception as e:
            logger.error(f"覆寫檔案失敗：{e}")
            self.rollback_optimization() # 若發生異常主動還原
            return f"❌ 寫入檔案時發生錯誤：{e}，已自動還原至上一版本。"

    def rollback_optimization(self) -> str:
        """還原至 Git 上一個穩定的 Commit。"""
        try:
            # 放棄所有尚未 commit 的修改，還原回 HEAD
            # 如果我們剛才已經 commit 了優化版本，要還原必須回退一個 commit
            # git reset --hard HEAD~1
            result = subprocess.run(
                ["git", "reset", "--hard", "HEAD~1"], 
                capture_output=True, text=True, check=True
            )
            return f"✅ 已成功還原至上一版本 (HEAD~1)。\n\nGit 訊息：`{result.stdout.strip()}`"
        except subprocess.CalledProcessError as e:
            logger.error(f"Git rollback 失敗: {e.stderr}")
            return f"❌ 還原失敗：{e.stderr}"

    def _git_backup(self, commit_msg: str) -> str:
        """產生一個 Git commit 作為備份，若有配置 Token 可嘗試 push。"""
        try:
            subprocess.run(["git", "add", "."], capture_output=True, text=True, check=True)
            res = subprocess.run(
                ["git", "commit", "-m", commit_msg], 
                capture_output=True, text=True
            )
            # nothing to commit 是正常的
            
            if GITHUB_TOKEN and GITHUB_REPO:
                # 這裡可加入推送邏輯 (git push)
                pass
                
            return "Git commit completed."
        except Exception as e:
            return f"Error: {str(e)}"
