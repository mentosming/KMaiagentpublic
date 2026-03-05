"""
logic/weather_agent.py — 天氣師 Agent

使用 Open-Meteo 免費 API（無需 API Key）取得天氣資訊。
預設地點：香港（緯度 22.32, 經度 114.17）
可透過環境變數 WEATHER_LAT / WEATHER_LON 覆蓋。

架構規則：
    符合 AgentRegistry 統一建構簽名 __init__(self, registry)
    可透過 registry.get(name) 呼叫其他 Agent。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

load_dotenv()

# 預設香港座標
_DEFAULT_LAT = float(os.getenv("WEATHER_LAT", "22.32"))
_DEFAULT_LON = float(os.getenv("WEATHER_LON", "114.17"))
_DEFAULT_LOCATION = os.getenv("WEATHER_LOCATION_NAME", "香港")

# Open-Meteo API 端點
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天氣代碼對應中文描述
_WMO_CODES: dict[int, str] = {
    0: "晴天☀️", 1: "大致晴朗🌤️", 2: "部分多雲⛅", 3: "陰天☁️",
    45: "有霧🌫️", 48: "凍霧🌫️",
    51: "毛毛雨🌦️", 53: "毛毛雨🌦️", 55: "毛毛雨🌦️",
    61: "小雨🌧️", 63: "中雨🌧️", 65: "大雨🌧️",
    71: "小雪❄️", 73: "中雪❄️", 75: "大雪❄️",
    80: "陣雨🌦️", 81: "陣雨🌦️", 82: "強陣雨⛈️",
    95: "雷暴⛈️", 96: "雷暴伴冰雹⛈️", 99: "強雷暴伴冰雹⛈️",
}


class WeatherAgent:
    """
    天氣師 Agent — 封裝 Open-Meteo 天氣 API。

    符合 AgentRegistry 統一建構簽名，可被其他 Agent 查詢。
    """

    def __init__(self, registry: "AgentRegistry") -> None:
        """
        初始化天氣師。

        參數：
            registry - AgentRegistry 實例（統一規則，即使暫時不需要也保留）
        """
        self._registry = registry
        self._default_lat = _DEFAULT_LAT
        self._default_lon = _DEFAULT_LON
        self._default_location = _DEFAULT_LOCATION

    # ------------------------------------------------------------------
    # 公開方法：取得當前天氣
    # ------------------------------------------------------------------

    async def get_weather(
        self,
        lat: float | None = None,
        lon: float | None = None,
        location_name: str | None = None,
    ) -> str:
        """
        取得指定地點的今日天氣摘要。

        參數：
            lat           - 緯度（預設使用 WEATHER_LAT 環境變數）
            lon           - 經度（預設使用 WEATHER_LON 環境變數）
            location_name - 地點名稱（用於顯示，不影響查詢）

        回傳：
            格式化的繁體中文天氣摘要字串
        """
        lat = lat or self._default_lat
        lon = lon or self._default_lon
        name = location_name or self._default_location

        params = {
            "latitude": lat,
            "longitude": lon,
            "current": [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "weather_code",
                "wind_speed_10m",
                "precipitation",
            ],
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "weather_code",
            ],
            "timezone": "Asia/Hong_Kong",
            "forecast_days": 1,
        }

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: self._fetch_weather(params)
            )
            return self._format_weather(data, name)
        except Exception as e:
            return f"⚠️ 無法取得天氣資料：{e}"

    # ------------------------------------------------------------------
    # 私有方法：HTTP 請求（同步，供 run_in_executor 使用）
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_weather(params: dict) -> dict:
        """使用 httpx 同步請求 Open-Meteo API。"""
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_FORECAST_URL, params=params)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # 私有方法：格式化天氣資料
    # ------------------------------------------------------------------

    @staticmethod
    def _format_weather(data: dict, location_name: str) -> str:
        """將 Open-Meteo 回傳資料格式化為繁體中文摘要。"""
        current = data.get("current", {})
        daily = data.get("daily", {})

        now_hk = datetime.now(tz=timezone(timedelta(hours=8)))
        time_str = now_hk.strftime("%m月%d日 %H:%M")

        # 當前天氣
        temp = current.get("temperature_2m", "N/A")
        feels_like = current.get("apparent_temperature", "N/A")
        humidity = current.get("relative_humidity_2m", "N/A")
        wind = current.get("wind_speed_10m", "N/A")
        weather_code = int(current.get("weather_code", 0))
        weather_desc = _WMO_CODES.get(weather_code, "未知天氣")

        # 今日預報
        temp_max = (daily.get("temperature_2m_max") or [None])[0]
        temp_min = (daily.get("temperature_2m_min") or [None])[0]
        rain_prob = (daily.get("precipitation_probability_max") or [None])[0]

        lines = [
            f"🌍 **{location_name} 天氣報告**（{time_str} HKT）",
            f"",
            f"**當前狀況**",
            f"• 天氣：{weather_desc}",
            f"• 氣溫：{temp}°C（體感 {feels_like}°C）",
            f"• 濕度：{humidity}%",
            f"• 風速：{wind} km/h",
        ]

        if temp_max is not None and temp_min is not None:
            lines += [
                f"",
                f"**今日預報**",
                f"• 溫度範圍：{temp_min}°C ~ {temp_max}°C",
            ]
        if rain_prob is not None:
            lines.append(f"• 降雨機率：{rain_prob}%")

        return "\n".join(lines)
