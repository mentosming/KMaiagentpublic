"""
logic/crawler_agent.py — 爬虫師 Agent (Pre-Cache 架構)

功能：
    fetch(url)              — 抓取 URL 並回傳清理後純文字
    search(query)           — DuckDuckGo 搜尋 (自動 fallback)
    search_news(query)      — Google News RSS 即時新聞搜尋
    summarize(url, prompt)  — 抓取後用 Gemini 詮釋 (含失敗 fallback)
    
    trending_lihkg()        — 連登 API 24h 熱門 (修正 headers)
    trending_threads()      — Google News RSS 抓 Threads 熱門
    trending_facebook()     — Google News RSS 抓 Facebook 熱門
    
    get_cached_trending()   — 讀取預取快取 (毫秒級回傳)
    refresh_cache()         — 背景強制刷新所有平台快取寫入 DB

架構規則：
    符合 AgentRegistry 統一建構簽名 __init__(self, registry, ...)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any
import xml.etree.ElementTree as ET

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

load_dotenv()

# LIHKG API
_LIHKG_HOT_API = "https://lihkg.com/api_v2/thread/hot?cat_id=1&page=1&type=now&count={count}"
_LIHKG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://lihkg.com/category/1",
}

# Gemini 模型（OCR/Summary 使用）
_GEMINI_MODEL = os.getenv("GEMINI_CRAWLER_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))

# 網頁抓取最大字元數（避免 token 爆炸）
_MAX_CONTENT_CHARS = 12_000


class CrawlerAgent:
    """
    爬虫師 Agent — 預取快取架構，支援即時新聞。
    """

    def __init__(
        self,
        registry: "AgentRegistry",
    ) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # 基礎：網頁抓取
    # ------------------------------------------------------------------

    async def fetch(self, url: str, timeout: int = 15) -> str:
        """
        抓取網頁，回傳去除 HTML 標籤後的純文字。
        自動截斷超過 _MAX_CONTENT_CHARS 的內容。
        """
        headers = {
            "User-Agent": _LIHKG_HEADERS["User-Agent"],
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            # 移除 script / style
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # 壓縮空白行
            lines = [l for l in text.splitlines() if l.strip()]
            text = "\n".join(lines)
        except ImportError:
            text = html  # bs4 未安裝時回傳原始 HTML

        if len(text) > _MAX_CONTENT_CHARS:
            text = text[:_MAX_CONTENT_CHARS] + "\n\n[內容已截斷]"
        return text

    # ------------------------------------------------------------------
    # 基礎：DuckDuckGo 搜尋 (具備 fallback)
    # ------------------------------------------------------------------

    async def search(self, query: str, max_results: int = 8) -> list[dict]:
        """
        DuckDuckGo 搜尋，回傳 [{title, href, body}] 清單。
        整合意圖拆解與標題過濾機制，提升資訊密度。
        """
        loop = asyncio.get_event_loop()
        
        # 1. 意圖拆解
        intent_data = await self._decompose_intent(query)
        refined_query = intent_data.get("search_query", query)
        time_limit = intent_data.get("time_limit")
        
        print(f"🕷️ 爬虫師：意圖拆解 [{query}] -> [{refined_query}] (時間限制: {time_limit})")

        def _ddg_search(limit: str | None) -> list[dict]:
            from ddgs import DDGS
            try:
                with DDGS() as ddgs:
                    return list(ddgs.text(
                        refined_query,
                        max_results=max_results,
                        timelimit=limit,
                    ))
            except Exception as e:
                print(f"⚠️ DDG 搜尋失敗: {e}")
                return []

        # 2. 擴大抓取範圍 (為了後續過濾)
        fetch_limit = max(15, max_results * 2)
        
        results = []
        if time_limit:
            results = await loop.run_in_executor(None, lambda: _ddg_search(time_limit))
            
        if not results and time_limit == "d":
            print(f"🕷️ 爬虫師：DDG 'd' 無結果，降級搜尋 'w' ({refined_query})")
            results = await loop.run_in_executor(None, lambda: _ddg_search("w"))
            
        if not results:
            if time_limit:
                print(f"🕷️ 爬虫師：DDG '{time_limit}' 仍無結果，降級搜尋不限時間 ({refined_query})")
            results = await loop.run_in_executor(None, lambda: _ddg_search(None))
            
        results = results or []
        
        if not results:
            return []
            
        # 3. 標題過濾
        filtered_results = await self._filter_titles(query, results, max_results)
        return filtered_results

    # ------------------------------------------------------------------
    # 新功能：Google News RSS 即時新聞搜尋
    # ------------------------------------------------------------------

    async def search_news(self, query: str, count: int = 8) -> list[dict]:
        """
        透過 Google News RSS 搜尋即時新聞。
        整合意圖拆解與標題過濾，確保高度相關性。
        """
        import urllib.parse
        
        # 1. 意圖拆解
        intent_data = await self._decompose_intent(query)
        refined_query = intent_data.get("search_query", query)
        
        print(f"🕷️ 爬虫師 (News)：意圖拆解 [{query}] -> [{refined_query}]")
        
        encoded_query = urllib.parse.quote(refined_query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            
            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if not channel:
                return []
                
            # 擴大抓取範圍
            fetch_limit = max(15, count * 2)
            results = []
            for item in channel.findall("item")[:fetch_limit]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pubDate = item.findtext("pubDate", "")
                source = item.find("source")
                source_name = source.text if source is not None else ""
                
                results.append({
                    "title": title,
                    "url": link,
                    "summary": f"{source_name} - {pubDate}",
                    "body": f"{source_name} - {pubDate}"  # 相容 filter_titles 的 body 欄位
                })
                
            if not results:
                return []
                
            # 3. 標題過濾
            return await self._filter_titles(query, results, count)
            
        except Exception as e:
            print(f"⚠️ search_news 失敗: {e}")
            # Fallback 到 DDG (DDG search 已內建意圖拆解與過濾)
            ddg_results = await self.search(f"{query} 新聞", max_results=count)
            return [{"title": r.get("title", ""), "url": r.get("href", ""), "summary": r.get("body", "")} for r in ddg_results]

    # ------------------------------------------------------------------
    # Gemini 詮釋 (具備 Fallback)
    # ------------------------------------------------------------------

    async def summarize(self, url: str, prompt: str = "") -> str:
        """
        抓取網頁後用 Gemini 依 prompt 歸納。
        若抓取網頁失敗，自動 Fallback 用 DDG 去搜這個 URL 來取得 Snippet 歸納。
        """
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            genai.configure(api_key=api_key)
            
        if not prompt:
            prompt = "請用繁體中文扼要總結這個網頁的主要內容。"

        content = ""
        try:
            content = await self.fetch(url)
        except Exception as e:
            print(f"⚠️ summarize fetch 失敗: {e}，嘗試 DDG Fallback")
            # 抓取失敗時，用 DDG 搜這個 URL 取 snippet
            fallback_res = await self.search(url, max_results=3)
            if fallback_res:
                content = f"[無法直接抓取網頁，以下是搜尋引擎摘要]\n" + "\n".join([r.get('body', '') for r in fallback_res])
            else:
                return f"❌ 無法讀取網頁內容，且搜尋引擎無摘要（錯誤：{str(e)[:50]}）"

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

        async def _generate_async(key):
            if key:
                genai.configure(api_key=key)
            model = genai.GenerativeModel(_GEMINI_MODEL)
            return await model.generate_content_async(f"{prompt}\n\n---\n\n{content[:8000]}")

        response = await _generate_async(api_key)
        return response.text.strip()

    # ------------------------------------------------------------------
    # 意圖拆解與標題過濾 (Intent Decomposition & Title Filtering)
    # ------------------------------------------------------------------

    async def _decompose_intent(self, query: str) -> dict:
        """
        使用 LLM 解析模糊搜尋指令，輸出擴充後的搜尋關鍵字與時間限制。
        回傳範例: {"search_query": "香港 警察 新聞", "time_limit": "d"}
        """
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            genai.configure(api_key=api_key)
        
        prompt = f"""請對使用者的搜尋查詢 "{query}" 進行意圖拆解。
        目標：產生適合餵給搜尋引擎 (DuckDuckGo / Google News) 的精確關鍵字，並判斷時間敏感度。
        
        請以純 JSON 格式輸出，不要加 markdown code block:
        {{
            "search_query": "經過擴充或優化的搜尋關鍵字組合",
            "time_limit": "如果需要最新資訊請用 'd' (一天內)，近期請用 'w' (一週內)，否則用 null"
        }}
        """
        
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

        async def _generate_async(key):
            if key:
                genai.configure(api_key=key)
            model = genai.GenerativeModel(_GEMINI_MODEL)
            return await model.generate_content_async(prompt)

        try:
            response = await _generate_async(api_key)
            
            text = response.text.strip()
            # 移除可能存在的 markdown wrapper
            if text.startswith("```json"):
                text = text[7:-3].strip()
            elif text.startswith("```"):
                text = text[3:-3].strip()
                
            result = json.loads(text)
            return {
                "search_query": result.get("search_query", query),
                "time_limit": result.get("time_limit")
            }
        except Exception as e:
            print(f"⚠️ 意圖拆解失敗 ({e})，使用原始查詢")
            return {"search_query": query, "time_limit": None}

    async def _filter_titles(self, original_query: str, search_results: list[dict], max_return: int) -> list[dict]:
        """
        使用 LLM 評估初步搜尋結果的相關性，篩選出最符合使用者原始意圖的 N 筆。
        """
        if not search_results or len(search_results) <= max_return:
            return search_results

        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            genai.configure(api_key=api_key)
        
        # 準備餵給 LLM 的候選清單
        candidates_text = ""
        for i, res in enumerate(search_results):
            title = res.get("title", "")
            snippet = res.get("body", res.get("summary", ""))
            candidates_text += f"[{i}] {title}\n摘要: {snippet}\n\n"
            
        prompt = f"""使用者的原始搜尋意圖是："{original_query}"
        以下是搜尋引擎初步返回的 {len(search_results)} 筆結果。
        為了極大化資訊密度並過濾內容農場/無關結果，請挑選出「最符合使用者意圖」且「最具資訊價值」的結果。
        
        請挑出最多 {max_return} 筆，並以純 JSON 格式回傳挑選的索引陣列 (例如: [0, 2, 4])，不要加 markdown:
        
        候選清單：
        {candidates_text}
        """
        
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

        async def _generate_async(key):
            if key:
                genai.configure(api_key=key)
            model = genai.GenerativeModel(_GEMINI_MODEL)
            return await model.generate_content_async(prompt)

        try:
            response = await _generate_async(api_key)
            
            text = response.text.strip()
            if text.startswith("```json"):
                text = text[7:-3].strip()
            elif text.startswith("```"):
                text = text[3:-3].strip()
                
            selected_indices = json.loads(text)
            
            if not isinstance(selected_indices, list):
                selected_indices = [0]
                
            # 防呆：確保索引有效
            valid_indices = [i for i in selected_indices if isinstance(i, int) and 0 <= i < len(search_results)]
            
            # 限制回傳數量
            valid_indices = valid_indices[:max_return]
            
            if not valid_indices:
                return search_results[:max_return]
                
            filtered = [search_results[i] for i in valid_indices]
            
            # 若選出的數量不足 max_return，自動從原清單補上尚未選取的
            if len(filtered) < max_return:
                remaining_needed = max_return - len(filtered)
                unselected = [res for i, res in enumerate(search_results) if i not in valid_indices]
                filtered.extend(unselected[:remaining_needed])
                
            return filtered
            
        except Exception as e:
            print(f"⚠️ 標題過濾失敗 ({e})，直接回傳前 N 筆")
            return search_results[:max_return]

    # ------------------------------------------------------------------
    # 爬取連結
    # ------------------------------------------------------------------

    async def extract_links(self, url: str) -> list[dict]:
        """從網頁提取所有超連結，回傳 [{text, href}]。"""
        headers = {"User-Agent": _LIHKG_HEADERS["User-Agent"]}
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if href.startswith("http") and text:
                    links.append({"text": text[:80], "href": href})
            return links[:50]
        except ImportError:
            return []

    # ------------------------------------------------------------------
    # 熱門話題：連登 LIHKG
    # ------------------------------------------------------------------

    async def trending_lihkg(self, count: int = 10) -> list[dict]:
        """透過 LIHKG 公開 API 抓取即時熱門帖子。"""
        url = _LIHKG_HOT_API.format(count=count)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=_LIHKG_HEADERS)
                resp.raise_for_status()
                data = resp.json()

            items = data.get("response", {}).get("items", [])
            results = []
            for item in items[:count]:
                results.append({
                    "title": item.get("title", ""),
                    "replies": item.get("no_of_reply", 0),
                    "likes": item.get("like_count", 0),
                    "thread_id": item.get("thread_id", ""),
                    "url": f"https://lihkg.com/thread/{item.get('thread_id', '')}/page/1",
                })
            return results
        except Exception as e:
            print(f"⚠️ LIHKG 抓取失敗: {e}")
            return []

    # ------------------------------------------------------------------
    # 熱門話題：Threads & Facebook (改用 Google News)
    # ------------------------------------------------------------------

    async def trending_threads(self, count: int = 8) -> list[dict]:
        """使用 Google News RSS 搜尋 Threads 熱門。"""
        return await self.search_news("Threads 香港", count=count)

    async def trending_facebook(self, count: int = 8) -> list[dict]:
        """使用 Google News RSS 搜尋 Facebook 熱話。"""
        return await self.search_news("Facebook 香港 熱話", count=count)

    # ------------------------------------------------------------------
    # 熱門話題：三平台合併抓取 (底層行為，不直接對外)
    # ------------------------------------------------------------------

    async def _fetch_trending_all_live(self, count: int = 5) -> str:
        """
        並行抓取 LIHKG + Threads + Facebook 熱門，
        用 Gemini 歸納成一則結果（此方法為 refresh_cache 所用，耗時長）。
        """
        import google.generativeai as genai

        now_hk = datetime.now(tz=timezone(timedelta(hours=8)))
        date_str = now_hk.strftime("%Y年%m月%d日 %H:%M")

        # 並行抓取三平台
        lihkg_task = asyncio.create_task(self.trending_lihkg(count))
        threads_task = asyncio.create_task(self.trending_threads(count))
        fb_task = asyncio.create_task(self.trending_facebook(count))

        lihkg_results, threads_results, fb_results = await asyncio.gather(
            lihkg_task, threads_task, fb_task,
            return_exceptions=True,
        )

        # 組裝 prompt 素材
        sections: list[str] = [f"📊 三平台熱門話題彙整（{date_str}）\n"]

        if isinstance(lihkg_results, list) and lihkg_results:
            sections.append("【連登 LIHKG 熱帖】")
            for i, item in enumerate(lihkg_results[:count], 1):
                sections.append(f"{i}. {item['title']} （{item['replies']} 回覆）")
        else:
            sections.append("【連登 LIHKG】無資料")

        if isinstance(threads_results, list) and threads_results:
            sections.append("\n【Threads 熱門】")
            for i, item in enumerate(threads_results[:count], 1):
                sections.append(f"{i}. {item['title']}")
        else:
            sections.append("\n【Threads】無資料")

        if isinstance(fb_results, list) and fb_results:
            sections.append("\n【Facebook 熱話】")
            for i, item in enumerate(fb_results[:count], 1):
                sections.append(f"{i}. {item['title']}")
        else:
            sections.append("\n【Facebook】無資料")

        raw_data = "\n".join(sections)

        # Gemini 潤稿
        model = genai.GenerativeModel(_GEMINI_MODEL)
        try:
            response = await model.generate_content_async(
                "請根據以下資料，用繁體中文寫一則簡潔的「今日三平台熱門話題」早報，"
                "加上適當 emoji，控制在 400 字以內。直接輸出內容：\n\n" + raw_data
            )
            return response.text.strip()
        except Exception:
            return raw_data

    # ------------------------------------------------------------------
    # Pre-Cache 架構：讀取與刷新
    # ------------------------------------------------------------------

    async def get_cached_trending(self, platform: str = "all", max_age_hours: int = 4) -> str:
        """
        【主入口】讀取熱門話題快取。若過期或無資料，則觸發即時抓取並更新。
        毫秒級回傳，大幅提升使用者體驗。
        """
        from database import AsyncSessionLocal, CrawlCache
        from sqlalchemy import select
        
        # 1. 嘗試從 DB 讀取
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CrawlCache)
                .where(CrawlCache.platform == platform)
                .order_by(CrawlCache.fetched_at.desc())
                .limit(1)
            )
            cache_row = result.scalar_one_or_none()
            
            # 若有快取且未過期，直接回傳
            now_utc = datetime.now(timezone.utc)
            if cache_row and cache_row.fetched_at:
                age_hours = (now_utc - cache_row.fetched_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                if age_hours < max_age_hours:
                    print(f"🕷️ 爬虫師：命中 {platform} 快取（{age_hours:.1f} 小時前）")
                    return cache_row.content

        # 2. 無有效快取，進行即時抓取
        print(f"🕷️ 爬虫師：{platform} 無快取或已過期，執行即時抓取...")
        
        if platform == "lihkg":
            items = await self.trending_lihkg(5)
            content = "\n\n".join([f"{i+1}. {r['title']}（{r['replies']} 回覆）\n🔗 {r['url']}" for i, r in enumerate(items)])
        elif platform == "threads":
            items = await self.trending_threads(5)
            content = self.format_search_results([{"title": r["title"], "href": r["url"], "body": r["summary"]} for r in items])
        elif platform == "facebook":
            items = await self.trending_facebook(5)
            content = self.format_search_results([{"title": r["title"], "href": r["url"], "body": r["summary"]} for r in items])
        else:
            content = await self._fetch_trending_all_live(5)

        # 3. 寫入快取
        if content and "無資料" not in content and "失敗" not in content:
            async with AsyncSessionLocal() as session:
                new_cache = CrawlCache(platform=platform, content=content, fetched_at=now_utc)
                session.add(new_cache)
                await session.commit()
                
        return content

    async def refresh_cache(self) -> None:
        """
        背景強制刷新所有平台快取，寫入 DB。
        此方法供 APScheduler 每 2 小時背景呼叫。
        """
        print(f"🔄 爬虫師：開始背景刷新快取...")
        
        platforms = ["lihkg", "threads", "facebook", "all"]
        for p in platforms:
            try:
                # 這裡調用 get_cached_trending 並將 max_age_hours 設為 0 以強制刷新
                await self.get_cached_trending(platform=p, max_age_hours=0)
            except Exception as e:
                print(f"⚠️ 背景刷新 {p} 失敗: {e}")
                
        print(f"✅ 爬虫師：背景快取刷新完成")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def format_search_results(results: list[dict]) -> str:
        """將搜尋結果格式化為可讀文字。"""
        if not results:
            return "沒有找到相關結果。"
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("href", "")
            body = r.get("body", r.get("summary", ""))[:120]
            lines.append(f"**{i}. {title}**\n{body}\n🔗 {url}")
        return "\n\n".join(lines)
