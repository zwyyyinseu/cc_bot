"""
飞书 REST API 客户端 —— httpx 实现，自动管理 tenant_access_token。
所有消息使用 interactive 卡片格式（schema 2.0，支持 markdown）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

import httpx

from config import config
import logging
log = logging.getLogger(__name__)

_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._token: str = ""
        self._token_expires: float = 0.0  # 过期时间戳
        self._token_lock = asyncio.Lock()  # 防止并发刷新 token

    async def close(self) -> None:
        await self._client.aclose()

    # ── Token 管理 ──────────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """确保 token 有效，过期前 5 分钟自动刷新。加锁防止并发刷新。"""
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        async with self._token_lock:
            # 双重检查：拿到锁后再次确认（可能已被其他协程刷新）
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """获取新的 tenant_access_token。调用方必须持有 _token_lock。"""
        resp = await self._client.post(
            f"{_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": config.FEISHU_APP_ID,
                "app_secret": config.FEISHU_APP_SECRET,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: code={data.get('code')} msg={data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200)
        log.info(f"token refreshed, expires in {data.get('expire', 7200)}s")
        return self._token

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """带自动 token 刷新的请求。401 或 1663 错误时刷新 token 并重试。"""
        token = await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = await self._client.request(method, url, headers=headers, **kwargs)

        # 401 → 刷新 token 重试
        if resp.status_code == 401:
            log.warning("got 401, refreshing token and retrying...")
            async with self._token_lock:
                token = await self._refresh_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._client.request(method, url, headers=headers, **kwargs)
            return resp

        # 1663 → token 过期但 HTTP 200，刷新并重试一次
        try:
            body_data = resp.json()
            if body_data.get("code") == 99991663:  # 1663 with prefix
                log.warning("got 1663 (token expired), refreshing and retrying...")
                async with self._token_lock:
                    token = await self._refresh_token()
                headers["Authorization"] = f"Bearer {token}"
                resp = await self._client.request(method, url, headers=headers, **kwargs)
        except Exception:
            pass

        return resp

    # ── 消息 API ────────────────────────────────────────────────────────────

    async def get_messages(self, container_id: str, page_size: int = 5) -> Optional[list[dict]]:
        """获取会话中的最新消息（按创建时间倒序）。
        返回 None 表示 API 错误（调用方应退避），返回 [] 表示无新消息。
        """
        resp = await self._request(
            "GET",
            f"{_BASE_URL}/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": container_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": page_size,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error(f"get_messages failed: code={data.get('code')} msg={data.get('msg')}")
            return None  # None = API error, distinct from [] = no messages
        items = data.get("data", {}).get("items", [])
        return items if items else []

    async def reply_message(self, message_id: str, card_json: str) -> Optional[str]:
        """回复指定消息（interactive 卡片），返回新消息 ID。"""
        resp = await self._request(
            "POST",
            f"{_BASE_URL}/im/v1/messages/{message_id}/reply",
            json={
                "msg_type": "interactive",
                "content": card_json,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error(f"reply_message failed: code={data.get('code')} msg={data.get('msg')}")
            return None
        return data.get("data", {}).get("message_id")

    async def update_card(self, message_id: str, card_json: str) -> bool:
        """PATCH 更新 interactive 卡片内容（流式刷新）。"""
        resp = await self._request(
            "PATCH",
            f"{_BASE_URL}/im/v1/messages/{message_id}",
            json={"content": card_json},
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error(f"update_card failed: code={data.get('code')} msg={data.get('msg')}")
            return False
        return True

    async def send_message(self, receive_id: str, card_json: str,
                           receive_id_type: str = "chat_id") -> Optional[str]:
        """向指定接收者发送新消息（interactive 卡片）。"""
        resp = await self._request(
            "POST",
            f"{_BASE_URL}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": card_json,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error(f"send_message failed: code={data.get('code')} msg={data.get('msg')}")
            return None
        return data.get("data", {}).get("message_id")

    # ── 卡片构建 ────────────────────────────────────────────────────────────

    @staticmethod
    def build_card(text: str) -> str:
        """将文本构建为 interactive 卡片 JSON（schema 2.0，支持 markdown）。"""
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "direction": "vertical",
                "elements": [
                    {"tag": "markdown", "content": text}
                ],
            },
        }
        return json.dumps(card, ensure_ascii=False)

    # ── 文本提取 ────────────────────────────────────────────────────────────

    @staticmethod
    def extract_text(content_str: str) -> str:
        """从飞书消息 content JSON 中提取纯文本，去除 @mention 占位符。"""
        try:
            obj = json.loads(content_str)
            text = obj.get("text", "")
        except Exception:
            text = content_str
        # HTTP 回调格式：<at user_id="...">name</at>
        text = re.sub(r"<at[^>]*>.*?</at>", "", text)
        # WS 长连接格式：@_user_1 @_user_2 等（防御性处理）
        text = re.sub(r"@_user_\d+\s*", "", text)
        return text.strip()

