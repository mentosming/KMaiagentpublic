"""
logic/image_artist_agent.py — 圖片師 Agent

職責：
    1. generate_image()   — 根據文字 Prompt 生成全新圖片（Imagen 4）
    2. transform_image()  — 接收圖片 bytes，根據指示改造/風格化圖片（Gemini Vision + 重新生成）
    3. describe_image()   — 描述圖片內容（Gemini Vision）
    4. send_to_user()     — 透過注入的 callback 將圖片推播給 Telegram 用戶

架構規則：
    符合 AgentRegistry 統一建構簽名 __init__(self, registry, send_photo_fn)
    可透過 registry.get(name) 呼叫其他 Agent（如秘書獲取 Drive 圖片）。

環境變數：
    GEMINI_IMAGE_MODEL        — 生成圖片模型（預設 imagen-4.0-generate-001）
    GEMINI_IMAGE_VISION_MODEL — 圖片理解/改造模型（預設 gemini-3-pro-image-preview）
"""

from __future__ import annotations

import asyncio
import io
import os
from typing import TYPE_CHECKING, Awaitable, Callable

import google.generativeai as genai
from dotenv import load_dotenv

if TYPE_CHECKING:
    from logic.agent_registry import AgentRegistry

load_dotenv()

# ------------------------------------------------------------------
# 模型設定
# ------------------------------------------------------------------

_IMAGE_GEN_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")
_IMAGE_VISION_MODEL = os.getenv("GEMINI_IMAGE_VISION_MODEL", "gemini-3-pro-image-preview")


class ImageArtistAgent:
    """
    圖片師 Agent — 封裝 Google Imagen 4 圖片生成與 Gemini Vision 圖片改造。

    符合 AgentRegistry 統一建構簽名，可被其他 Agent 查詢。
    """

    def __init__(
        self,
        registry: "AgentRegistry",
        send_photo_fn: "Callable[[str, bytes, str], Awaitable[None]] | None" = None,
    ) -> None:
        """
        初始化圖片師。

        參數：
            registry      - AgentRegistry 實例
            send_photo_fn - Telegram 圖片推播 callback(chat_id, image_bytes, caption)
        """
        self._registry = registry
        self._send_photo_fn = send_photo_fn

        # Imagen 4 生成模型（只支援 text → image）
        try:
            self._gen_model = genai.ImageGenerationModel(_IMAGE_GEN_MODEL)
        except Exception:
            self._gen_model = None  # 若 API 不支援，fallback 到 Vision 模型

        # Gemini Vision 模型（multimodal：text + image → text + image）
        self._vision_model = genai.GenerativeModel(_IMAGE_VISION_MODEL)

    # ------------------------------------------------------------------
    # 公開方法 1：文字生成圖片
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        number_of_images: int = 1,
    ) -> bytes | None:
        """
        根據文字 Prompt 生成圖片。

        參數：
            prompt         - 圖片描述（建議使用英文可獲得最佳效果）
            aspect_ratio   - 長寬比：1:1 / 16:9 / 4:3 / 3:4 / 9:16
            number_of_images - 生成數量（預設 1）

        回傳：
            PNG 圖片 bytes，失敗返回 None
        """
        loop = asyncio.get_event_loop()

        # 優先使用 Imagen 4
        if self._gen_model is not None:
            try:
                result = await self._gen_model.generate_images_async(
                        prompt=prompt,
                        number_of_images=number_of_images,
                        aspect_ratio=aspect_ratio,
                        safety_filter_level="block_only_high",
                        person_generation="allow_adult",
                    )
                if result.images:
                    img = result.images[0]
                    # 轉換為 bytes
                    buf = io.BytesIO()
                    img._pil_image.save(buf, format="PNG")
                    return buf.getvalue()
            except Exception as e:
                print(f"⚠️ Imagen 4 生成失敗，嘗試 Gemini Vision: {e}")

        # Fallback：使用 Gemini Vision 生成圖片
        return await self._generate_via_vision(prompt)

    # ------------------------------------------------------------------
    # 公開方法 2：改造圖片
    # ------------------------------------------------------------------

    async def transform_image(
        self,
        image_bytes: bytes,
        instruction: str,
        mime_type: str = "image/jpeg",
    ) -> bytes | None:
        """
        根據指示改造圖片。

        策略（依次嘗試）：
          1. gemini-2.0-flash-exp-image-generation — 原生圖片輸入+輸出（最直接）
          2. gemini-3-pro-image-preview — 嘗試圖片輸入+輸出
          3. fallback：Vision 描述原圖 → Imagen 4 重新生成

        參數：
            image_bytes - 原圖 bytes
            instruction - 改造指示（如「改成卡通風格」）
            mime_type   - 圖片 MIME 類型

        回傳：
            改造後圖片 bytes，全部失敗時 raise Exception
        """
        loop = asyncio.get_event_loop()
        last_error = "未知錯誤"

        # ── 策略 1：gemini-2.0-flash-exp-image-generation ────────────
        try:
            flash_img_model = genai.GenerativeModel(
                "gemini-2.0-flash-exp-image-generation"
            )
            transform_prompt = (
                f"Transform this image according to the following instruction. "
                f"Output the transformed image directly.\n"
                f"Instruction: {instruction}"
            )
            response = await flash_img_model.generate_content_async(
                    [
                        {"mime_type": mime_type, "data": image_bytes},
                        transform_prompt,
                    ],
                    generation_config=genai.GenerationConfig(
                        temperature=1,
                        top_p=0.95,
                        top_k=40,
                        candidate_count=1,
                    ),
                )
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data and \
                        part.inline_data.mime_type.startswith("image/"):
                    print(f"✅ [策略1] 圖片改造成功（gemini-2.0-flash-exp）")
                    return part.inline_data.data
            print("⚠️ [策略1] 未返回圖片資料，嘗試策略2...")
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ [策略1] gemini-2.0-flash-exp 失敗：{e}")

        # ── 策略 2：gemini-3-pro-image-preview（圖片輸入+輸出）──────
        try:
            transform_prompt2 = (
                f"請根據以下指示改造這張圖片，直接輸出改造後的圖片：\n{instruction}"
            )
            response2 = await self._vision_model.generate_content_async(
                    [
                        {"mime_type": mime_type, "data": image_bytes},
                        transform_prompt2,
                    ],
                )
            for part in response2.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data and \
                        part.inline_data.mime_type.startswith("image/"):
                    print(f"✅ [策略2] 圖片改造成功（gemini-3-pro-image-preview）")
                    return part.inline_data.data
            print("⚠️ [策略2] 未返回圖片資料，嘗試 fallback 描述+重新生成...")
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ [策略2] gemini-3-pro-image-preview 失敗：{e}")

        # ── 策略 3：Vision 描述 + Imagen 4 重新生成（Fallback）───────
        return await self._transform_via_describe_and_generate(
            image_bytes, instruction, mime_type, last_error
        )

    async def _transform_via_describe_and_generate(
        self,
        image_bytes: bytes,
        instruction: str,
        mime_type: str,
        prev_error: str = "",
    ) -> bytes | None:
        """
        Fallback：先用 gemini-3-pro-image-preview 描述原圖，
        再用 Imagen 4 依改造指示重新生成。
        """
        loop = asyncio.get_event_loop()
        vision_prompt = (
            f"Analyze this image in detail and generate an English image generation prompt "
            f"that produces a modified version according to: {instruction}\n\n"
            f"Output only the English prompt text, no explanations."
        )
        response = await self._vision_model.generate_content_async([
                {"mime_type": mime_type, "data": image_bytes},
                vision_prompt,
            ])
        new_prompt = response.text.strip()
        print(f"🎨 [策略3 Fallback] 生成 Prompt：{new_prompt[:120]}...")
        result = await self.generate_image(prompt=new_prompt)
        if result:
            return result
        raise RuntimeError(
            f"所有改造策略均失敗。最後錯誤：{prev_error or '描述+重新生成失敗'}"
        )


    # ------------------------------------------------------------------
    # 公開方法 3：描述圖片（純 Vision）
    # ------------------------------------------------------------------

    async def describe_image(
        self,
        image_bytes: bytes,
        question: str = "請詳細描述這張圖片的內容。",
        mime_type: str = "image/jpeg",
    ) -> str:
        """
        使用 Gemini Vision 描述或分析圖片。

        參數：
            image_bytes - 圖片 bytes
            question    - 分析指示或問題
            mime_type   - 圖片 MIME 類型

        回傳：
            繁體中文描述文字
        """
        loop = asyncio.get_event_loop()
        try:
            response = await self._vision_model.generate_content_async([
                    {"mime_type": mime_type, "data": image_bytes},
                    question,
                ])
            return response.text.strip()
        except Exception as e:
            return f"⚠️ 無法分析圖片：{e}"

    # ------------------------------------------------------------------
    # 公開方法 4：推播圖片給 Telegram 用戶
    # ------------------------------------------------------------------

    async def send_to_user(
        self,
        chat_id: str,
        image_bytes: bytes,
        caption: str = "",
    ) -> dict:
        """
        透過注入的 callback 將圖片推播給 Telegram 用戶。

        參數：
            chat_id     - Telegram Chat ID
            image_bytes - 圖片 bytes（PNG/JPEG）
            caption     - 圖片說明文字

        回傳：
            成功/失敗 dict
        """
        if not self._send_photo_fn:
            return {"error": "圖片推播 callback 未設定"}
        if not image_bytes:
            return {"error": "沒有可傳送的圖片"}

        try:
            await self._send_photo_fn(chat_id, image_bytes, caption)
            return {"success": True, "message": "圖片已傳送"}
        except Exception as e:
            return {"error": f"圖片傳送失敗：{e}"}

    # ------------------------------------------------------------------
    # 私有方法：使用 Gemini Vision 生成圖片（Imagen fallback）
    # ------------------------------------------------------------------

    async def _generate_via_vision(self, prompt: str) -> bytes | None:
        """
        使用 Gemini Vision 生成圖片（Imagen 不可用時的備用方案）。
        仅支援支援 image output 的模型如 gemini-2.0-flash-exp-image-generation。
        """
        loop = asyncio.get_event_loop()
        try:
            # 使用支援圖片輸出的 Gemini 模型
            flash_model = genai.GenerativeModel("gemini-2.0-flash-exp-image-generation")
            response = await flash_model.generate_content_async(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        response_modalities=["IMAGE", "TEXT"],
                    ),
                )
            # 從回應中提取圖片
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    return part.inline_data.data
            return None
        except Exception as e:
            print(f"⚠️ Gemini Vision 圖片生成也失敗：{e}")
            return None
