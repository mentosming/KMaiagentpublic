"""
logic/worker_bus.py — Worker → Auditor 單向事件匯流排

設計原則：
- 所有 Worker 層（Director、Agents）在完成請求後，
  透過 WorkerBus.emit_nowait() 非阻斷地發送遙測事件。
- AuditorAgent 在後台 Task 中持續消費事件，完全解耦。
- Queue 上限 500 筆，超出時最舊事件被丟棄（防止記憶體溢出）。
"""

import asyncio
import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# 遙測事件資料結構
# ──────────────────────────────────────────────────────────────────

@dataclass
class TelemetryEvent:
    """
    Worker 層每次請求完成後發射的遙測事件。

    Attributes:
        user_id:      用戶唯一識別碼
        tokens_used:  本次 API 呼叫的總 Token 消耗
        latency_ms:   從收到請求到回覆完成的延遲（毫秒）
        tool_called:  本次呼叫觸發的工具名稱（若無則為 None）
        model:        使用的 Gemini 模型名稱
        timestamp:    事件發生的 Unix 時間戳（自動記錄）
        success:      本次請求是否成功完成（未拋出 Exception）
    """
    user_id: str
    session_id: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    tool_called: Optional[str] = None
    model: str = "unknown"
    success: bool = True
    timestamp: float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────────
# WorkerBus：單例事件匯流排
# ──────────────────────────────────────────────────────────────────

class _WorkerBus:
    """
    asyncio.Queue 封裝，作為 Worker ↔ Auditor 的單向資料管道。
    
    使用模式：
        # Worker 端（非阻斷）
        WorkerBus.emit_nowait(event)

        # Auditor 端（等待消費）
        event = await WorkerBus.consume()
    """

    _MAX_QUEUE_SIZE = 500

    def __init__(self) -> None:
        self._queue: collections.deque[TelemetryEvent] = collections.deque()
        self._event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dropped = 0

    def _setup_loop(self):
        """Bind event and loop precisely when the consumer starts."""
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()

    def emit_nowait(self, event: TelemetryEvent) -> None:
        """
        非阻斷地將遙測事件放入 Queue。
        跨執行緒與跨迴圈安全。
        """
        if len(self._queue) >= self._MAX_QUEUE_SIZE:
            # 丟棄最舊的事件
            try:
                self._queue.popleft()
                self._dropped += 1
                logger.warning(
                    f"WorkerBus Queue 已滿（上限 {self._MAX_QUEUE_SIZE}），"
                    f"丟棄最舊事件（累計丟棄 {self._dropped} 筆）"
                )
            except IndexError:
                pass
        
        self._queue.append(event)
        
        # 安全地喚醒消費端的 Event
        if self._loop is not None and self._event is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def consume(self) -> TelemetryEvent:
        """
        阻斷式消費事件（供 AuditorAgent 後台 Task 使用）。
        """
        if self._event is None:
            self._setup_loop()
            
        while not self._queue:
            await self._event.wait()
            self._event.clear()
            
        return self._queue.popleft()

    def task_done(self) -> None:
        """標記事件已處理（deque 版僅作 mock 佔位）"""
        pass

    @property
    def size(self) -> int:
        return len(self._queue)

    @property
    def dropped(self) -> int:
        """累計被丟棄的事件數量（因 Queue 滿而丟棄）。"""
        return self._dropped


# 全域單例
WorkerBus = _WorkerBus()
