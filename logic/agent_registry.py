"""
logic/agent_registry.py — Agent 共享登記表

架構規則：
    每個 Agent 初始化時都注入此 Registry，
    可透過 registry.get(name) 直接呼叫其他 Agent 的方法，
    無需繞回 Director（避免循環依賴）。

使用方式：
    class MyAgent:
        def __init__(self, registry: AgentRegistry):
            self._registry = registry

    # 在方法中呼叫其他 Agent：
        calendar = self._registry.get("calendar")
        events = await calendar.list_events(...)
"""

from __future__ import annotations

from typing import Any


class AgentRegistry:
    """
    輕量 Agent 登記表 — 系統基礎設施。

    Director 在啟動時建立唯一實例，並將所有 Agent 登記進來。
    每個 Agent 持有此實例的參考，可隨時查詢其他 Agent。
    """

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 登記 / 查詢
    # ------------------------------------------------------------------

    def register(self, name: str, agent: Any) -> None:
        """
        登記一個 Agent 實例。

        參數：
            name  - Agent 識別名（如 "calendar"、"weather"、"reminder"）
            agent - Agent 實例
        """
        self._agents[name] = agent

    def get(self, name: str) -> Any:
        """
        取得已登記的 Agent 實例。

        若找不到則拋出 KeyError，呼叫方應自行處理。
        """
        if name not in self._agents:
            raise KeyError(
                f"AgentRegistry：找不到 Agent '{name}'。"
                f"已登記：{list(self._agents.keys())}"
            )
        return self._agents[name]

    def all(self) -> dict[str, Any]:
        """回傳所有已登記 Agent 的 dict（name → instance）。"""
        return dict(self._agents)

    def __repr__(self) -> str:
        return f"AgentRegistry(agents={list(self._agents.keys())})"
