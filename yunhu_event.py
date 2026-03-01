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

# 云湖 /bot/send 单次文本最大字节数
_MAX_TEXT_LEN = 3500

# 常见图片文件扩展名，用于判断 Plain 组件是否实为本地图片路径
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _normalize_local_path(path: str) -> str:
    """
    将各种 file:// 格式路径统一转换为绝对路径字符串。
    无论 file:// 后有几个斜杠，都正确解析：
      file:////tmp/a.jpg  -> /tmp/a.jpg  (AstrBot 生成的四斜杠格式)
      file:///tmp/a.jpg   -> /tmp/a.jpg
      file://tmp/a.jpg    -> /tmp/a.jpg
      /tmp/a.jpg          -> /tmp/a.jpg  (裸路径原样返回)
    """
    if path.startswith("file://"):
        rest = path[7:]                  
        path = "/" + rest.lstrip("/") 
    return path


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
    def _read_local_file_b64(path: str) -> Optional[tuple]:
        """
        读取本地文件并返回base64字符串, 文件名。
        path 可以是 file:///xxx、file://xxx 或裸绝对/相对路径。
        失败返回 None。
        """
        path = _normalize_local_path(path)
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

    @staticmethod
    def _parse_data_url(url: str) -> Optional[tuple]:
        """
        解析 data:image/xxx;base64,XXXX 格式，返回base64字符串, 文件名。
        失败返回 None。
        """
        try:
            header, b64 = url.split(",", 1)
            mime = header.split(";")[0].split(":")[1]   # image/png
            ext_map = {
                "image/png": ".png", "image/jpeg": ".jpg",
                "image/gif": ".gif", "image/webp": ".webp",
            }
            suffix = ext_map.get(mime, ".png")
            return b64, f"image{suffix}"
        except Exception as e:
            logger.error(f"[云湖] data URL 解析失败: {e}")
            return None

    def _build_image_payload(self, url: str) -> Optional[dict]:
        """
        根据 url 类型构造 send_image 的 WS payload。
        返回 None 表示无法处理（跳过）。

        支持的格式：
          - data:image/xxx;base64,XXX  —— data URL
          - base64://XXXX              —— AstrBot 标准 base64 前缀格式
          - file:///path 或 /path      —— 本地文件路径
          - http(s)://...              —— 远程 URL，由 SDK 侧下载后上传
          - 裸 base64 字符串           —— 尝试解码，成功则作为 image_data 上传
          - 短字符串                   —— 视为云湖 CDN imageKey 直接发送
        """
        payload = {
            "action": "send_image",
            "recv_id": self._recv_id,
            "recv_type": self._recv_type,
            "parent_id": self._parent_id,
        }

        if url.startswith("data:"):
            # data:image/png;base64,XXX —— 直接提取 base64，避免触发 413
            result = self._parse_data_url(url)
            if not result:
                return None
            payload["image_data"], payload["filename"] = result

        elif url.startswith("base64://"):
            # 提取纯 base64 数据通过上传接口发送
            b64_data = url[len("base64://"):]
            # 补齐 padding
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += "=" * (4 - missing_padding)
            payload["image_data"] = b64_data
            payload["filename"] = "image.png"

        elif url.startswith("file://") or url.startswith("/"):
            # 本地文件（file:// 前缀 或 裸绝对路径）
            local_path = _normalize_local_path(url)
            result = self._read_local_file_b64(local_path)
            if not result:
                return None
            payload["image_data"], payload["filename"] = result

        elif url.startswith("http"):
            # 远程 URL，让 SDK 侧下载再上传
            payload["download_url"] = url

        else:
            # 如果是较长字符串，尝试作为裸 base64 解码，成功则通过上传接口发送，
            # 避免将大量数据直接塞入 imageKey 字段发往 /bot/send 触发 413。
            # 短字符串（≤200字符）才视为云湖 CDN imageKey，但这只是妥协办法。
            if len(url) > 200:
                import base64 as _b64
                try:
                    b64_data = url
                    missing_padding = len(b64_data) % 4
                    if missing_padding:
                        b64_data += "=" * (4 - missing_padding)
                    _b64.b64decode(b64_data, validate=True) 
                    logger.info("[云湖] 检测到裸 base64 图片数据，将通过上传接口发送")
                    payload["image_data"] = b64_data
                    payload["filename"] = "image.png"
                except Exception:
                    logger.warning(f"[云湖] 无法识别的图片格式，作为 imageKey 尝试发送: {url[:60]}...")
                    payload["image_key"] = url
            else:
                # 短字符串视为云湖 CDN imageKey
                payload["image_key"] = url

        return payload

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

            # Plain 文本
            if isinstance(component, Plain):
                text = component.text or ""

                # 部分插件会把图片 data URL 或本地图片路径写进 Plain，
                # 以文本直接发送会触发 413 或发出无意义内容，转为图片发送。
                if text.startswith("data:image/"):
                    logger.info("[云湖] Plain 中检测到 data URL，转为图片发送")
                    payload = self._build_image_payload(text)
                    if payload:
                        if await self._send_with_retry(payload):
                            sent_count += 1
                    continue

                local_path = _normalize_local_path(text)
                ext = os.path.splitext(local_path)[-1].lower()
                if ext in _IMAGE_EXTS and os.path.exists(local_path):
                    logger.info(f"[云湖] Plain 中检测到本地图片路径，转为图片发送: {local_path}")
                    payload = self._build_image_payload(local_path)
                    if payload:
                        if await self._send_with_retry(payload):
                            sent_count += 1
                    continue

                # 普通文本分块发送，避免超长文本触发 413，开启了分段回复也不用管？
                chunks = [text[j:j+_MAX_TEXT_LEN] for j in range(0, max(len(text), 1), _MAX_TEXT_LEN)]
                for chunk_idx, chunk in enumerate(chunks):
                    logger.info(f"[云湖][诊断C] 发送文本分块 {chunk_idx+1}/{len(chunks)}: {chunk[:60]!r}")
                    if await self._send_with_retry({
                        "action": "send_text",
                        "recv_id": self._recv_id,
                        "recv_type": self._recv_type,
                        "content": chunk,
                        "parent_id": self._parent_id,
                    }):
                        sent_count += 1

            # Image
            elif isinstance(component, Image):
                url = component.file or ""
                payload = self._build_image_payload(url)
                if payload is None:
                    logger.error(f"[云湖] 图片构造 payload 失败，跳过: {url[:80]}")
                    continue
                if await self._send_with_retry(payload):
                    sent_count += 1

            # File / Record
            elif isinstance(component, (File, Record)):
                file_url = getattr(component, "file", "") or ""
                payload = {
                    "action": "send_file",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "parent_id": self._parent_id,
                }

                if file_url.startswith("file://") or file_url.startswith("/"):
                    result = self._read_local_file_b64(file_url)
                    if result:
                        payload["file_data"], payload["filename"] = result
                    else:
                        logger.error(f"[云湖] 文件读取失败，跳过: {file_url}")
                        continue
                elif file_url.startswith("http"):
                    payload["download_url"] = file_url
                else:
                    payload["file_key"] = file_url

                if await self._send_with_retry(payload):
                    sent_count += 1

            # Video
            elif isinstance(component, Video):
                video_url = getattr(component, "file", "") or ""
                payload = {
                    "action": "send_video",
                    "recv_id": self._recv_id,
                    "recv_type": self._recv_type,
                    "parent_id": self._parent_id,
                }

                if video_url.startswith("file://") or video_url.startswith("/"):
                    result = self._read_local_file_b64(video_url)
                    if result:
                        payload["video_data"], payload["filename"] = result
                    else:
                        logger.error(f"[云湖] 视频文件读取失败，跳过: {video_url}")
                        continue
                elif video_url.startswith("http"):
                    payload["download_url"] = video_url
                else:
                    payload["video_key"] = video_url

                if await self._send_with_retry(payload):
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