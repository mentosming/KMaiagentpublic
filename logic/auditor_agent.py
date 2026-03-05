"""
logic/auditor_agent.py — 監控層 (Auditor Agent) 雛形

負責在後台持續消費 WorkerBus 傳來的 TelemetryEvent，
並非阻斷地寫入資料庫（UsageLog 等），實踐雙層意識隔離。
"""

import asyncio
import logging

from database import AsyncSessionLocal, UsageLog
from logic.agent_registry import AgentRegistry
from logic.worker_bus import WorkerBus

logger = logging.getLogger(__name__)

class AuditorAgent:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def start_consuming(self) -> None:
        """
        後台無窮迴圈，持續消費 WorkerBus 中的事件並寫入資料庫。
        此 Task 應在系統啟動時（如 Director 初始化時）被建立。
        """
        logger.info("🛡️ AuditorAgent 開始在背景消費遙測事件...")
        while True:
            try:
                # 阻斷式等待新事件（不佔用 CPU）
                event = await WorkerBus.consume()
                
                async with AsyncSessionLocal() as session:
                    log = UsageLog(
                        user_id=event.user_id,
                        session_id=event.session_id,
                        model=event.model,
                        prompt_tokens=event.prompt_tokens,
                        completion_tokens=event.completion_tokens,
                        total_tokens=event.total_tokens,
                        tool_called=event.tool_called,
                    )
                    session.add(log)
                    await session.commit()
                    
            except asyncio.CancelledError:
                logger.info("🛡️ AuditorAgent 消費終止")
                break
            except Exception as e:
                logger.error(f"🛡️ AuditorAgent 處理事件失敗: {e}")
            finally:
                # 告知 Queue 該事件處理完畢
                try:
                    WorkerBus.task_done()
                except ValueError:
                    # 如果 task_done 呼叫次數大於 put，會拋出 ValueError
                    pass
