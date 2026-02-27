# -*- coding: utf-8 -*-
import asyncio
import base64
import json
import logging
import os
import aiohttp
from typing import Optional

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain, Image, File, Video, Record

logger = logging.getLogger("yunhu.event")


class YunhuMessageEvent(AstrMessageEvent):

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        ws: "aiohttp.ClientWebSocketResponse",
        recv_id: str,
        recv_type: str,
        parent_id: str = "",
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._ws = ws
        self._recv_id = recv_id
        self._recv_type = recv_type
        self._parent_id = parent_id

    async def _send_ws(self, payload: dict) -> bool:
        if not self._ws or self._ws.closed:
            logger.error(f"[云湖] WS 连接已断开，无法发送 | ws={self._ws} closed={getattr(self._ws, 'closed', 'N/A')}")
            return False
        try:
            await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
            logger.debug(f"[云湖] WS 发送成功: {payload.get('action')}")
            return True
        except Exception as e:
            logger.error(f"[云湖] WS 发送异常: {e}", exc_info=True)
            return False

    async def _send_with_retry(self, payload: dict, max_retries: int = 3) -> bool:
        for attempt in range(max_retries):
            success = await self._send_ws(payload)
            if success:
                return True
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"[云湖] 重试 {attempt+1}/{max_retries}，等待 {wait_time}s")
                await asyncio.sleep(wait_time)
        logger.error(f"[云湖] 重试 {max_retries} 次后仍失败")
        return False

    @staticmethod
    def _read_local_file_b64(path: str) -> Optional[tuple[str, str]]:
        """
        读取本地文件并返回 (base64字符串, 文件名)。
        path 可以是 file:///xxx 或裸路径。
        失败返回 None。
        """
        if path.startswith("file:///"):
            path = path[7:]   # 保留开头的 /，变成绝对路径
        try:
            with open(path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode("ascii")
            filename = os.path.basename(path)
            logger.info(f"[云湖] 本地文件读取成功: {path} ({len(data)} bytes)")
            return b64, filename
        except Exception as e:
            logger.error(f"[云湖] 本地文件读取失败: {path}: {e}")
            return None

    async def send(self, message: MessageChain):
        logger.info(
            f"[云湖][诊断C] send() 被调用 | "
            f"chain长度={len(message.chain)} "
            f"recv={self._recv_type}:{self._recv_id} "
            f"ws_exists={self._ws is not None} "
            f"ws_closed={self._ws.closed if self._ws else 'N/A'}"
        )

        if not self._ws or self._ws.closed:
            logger.error("[云湖][诊断C] WS 已断开，跳过发送")
            await super().send(message)
            return

        sent_count = 0
        for i, component in enumerate(message.chain):
            logger.info(f"[云湖][诊断C] 处理第{i}个组件: {type(component).__name__}")

            if isinstance(component, Plain):
                logger.info(f"[云湖][诊断C] 发送文本: {component.text!r}")
                success = await self._send_with_retry({
                    "action": "send_text",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "content": component.text,
                    "parent_id": self._parent_id,
                })
                if success:
                    sent_count += 1

            elif isinstance(component, Image):
                url: str = component.file or ""
                payload = {
                    "action": "send_image",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "parent_id": self._parent_id,
                }

                if url.startswith("file:///") or (url.startswith("/") and os.path.exists(url)):
                    # 本地文件读成 base64 随 WS 发送，SDK 那边负责上传
                    result = self._read_local_file_b64(url)
                    if result:
                        b64, filename = result
                        payload["image_data"] = b64
                        payload["filename"] = filename
                    else:
                        logger.error(f"[云湖] 图片文件读取失败，跳过: {url}")
                        continue
                elif url.startswith("http"):
                    # 远程 URL让 SDK 侧去下载
                    payload["download_url"] = url
                else:
                    # 已经是 imageKey
                    payload["image_key"] = url

                success = await self._send_with_retry(payload)
                if success:
                    sent_count += 1

            elif isinstance(component, (File, Record)):
                file_url: str = getattr(component, "file", "") or ""
                payload = {
                    "action": "send_file",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "parent_id": self._parent_id,
                }

                if file_url.startswith("file:///") or (file_url.startswith("/") and os.path.exists(file_url)):
                    result = self._read_local_file_b64(file_url)
                    if result:
                        b64, filename = result
                        payload["file_data"] = b64
                        payload["filename"] = filename
                    else:
                        logger.error(f"[云湖] 文件读取失败，跳过: {file_url}")
                        continue
                elif file_url.startswith("http"):
                    payload["download_url"] = file_url
                else:
                    payload["file_key"] = file_url

                success = await self._send_with_retry(payload)
                if success:
                    sent_count += 1

            elif isinstance(component, Video):
                video_url: str = getattr(component, "file", "") or ""
                payload = {
                    "action": "send_video",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "parent_id": self._parent_id,
                }

                if video_url.startswith("file:///") or (video_url.startswith("/") and os.path.exists(video_url)):
                    result = self._read_local_file_b64(video_url)
                    if result:
                        b64, filename = result
                        payload["video_data"] = b64
                        payload["filename"] = filename
                    else:
                        logger.error(f"[云湖] 视频文件读取失败，跳过: {video_url}")
                        continue
                elif video_url.startswith("http"):
                    payload["download_url"] = video_url
                else:
                    payload["video_key"] = video_url

                success = await self._send_with_retry(payload)
                if success:
                    sent_count += 1

            else:
                logger.debug(f"[云湖] 未处理组件: {type(component).__name__}")

        logger.info(f"[云湖][诊断C] send() 完成 | sent_count={sent_count}/{len(message.chain)}")
        await super().send(message)

    @staticmethod
    async def _download_tmp(url: str) -> Optional[str]:
        import tempfile
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    suffix = os.path.splitext(url.split("?")[0])[-1] or ".bin"
                    fd, path = tempfile.mkstemp(suffix=suffix)
                    with os.fdopen(fd, "wb") as f:
                        f.write(await resp.read())
            return path
        except Exception as e:
            logger.error(f"[云湖] 下载失败 {url}: {e}")
            return None