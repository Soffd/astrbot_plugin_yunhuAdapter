# -*- coding: utf-8 -*-
"""
云湖平台适配器（AstrBot 插件）

通过 WebSocket 连接到云湖 SDK，接收平台事件并提交给 AstrBot 处理；
AstrBot 处理结果经由同一 WS 连接发回 SDK，由 SDK 调用云湖 HTTP API 发送。
"""
import asyncio
import json
import logging
import os
import tempfile

import aiohttp

from astrbot.api.platform import (
    Platform, AstrBotMessage, MessageMember,
    PlatformMetadata, MessageType, register_platform_adapter,
)
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, File, Video
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot import logger

from .yunhu_event import YunhuMessageEvent

# ─────────────────────────────────────────────────────────────────────────────

# 云湖媒体资源下载基础地址
_YUNHU_CDN_BASE = "https://chat-go.jwzhd.com/open-apis/v1"


def _yunhu_media_url(media_type: str, key: str, token: str = "") -> str:
    """
    将云湖媒体 Key 转换为可访问的 HTTP 下载 URL，供 AstrBot 下载使用。
    media_type: "image" | "file" | "video"
    如果 key 已经是完整 HTTP URL 则直接返回。
    """
    if not key:
        return ""
    if key.startswith("http"):
        if token and "token=" not in key:
            sep = "&" if "?" in key else "?"
            return f"{key}{sep}token={token}"
        return key
    url = f"{_YUNHU_CDN_BASE}/{media_type}/download?key={key}"
    if token:
        url += f"&token={token}"
    return url


RECONNECT_DELAY = 5   # 断线重连等待秒数
RECV_TIMEOUT    = 60  # 心跳超时（SDK 每 30s 发一次 ping）

# 媒体下载后缀映射
_MEDIA_SUFFIX = {"image": ".jpg", "file": ".bin", "video": ".mp4"}


async def _download_media(url: str, media_type: str, bot_token: str = "") -> str:
    """
    从云湖 CDN 下载媒体文件到本地临时文件，返回绝对路径。
    下载失败时返回空字符串，调用方回退到直接使用原始 URL。

    云湖 CDN (chat-img.jwznb.com 等) 需要携带
    Referer: https://myapp.jwznb.com 请求头才能访问，token 鉴权对此域名无效。
    """
    if not url:
        return ""
    headers = {
        "Referer": "https://myapp.jwznb.com",
    }
    try:
        suffix = _MEDIA_SUFFIX.get(media_type, ".bin")
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[云湖] 媒体下载失败 status={resp.status} url={url}")
                    return ""
                ct = resp.headers.get("Content-Type", "")
                if "jpeg" in ct or "jpg" in ct:
                    suffix = ".jpg"
                elif "png" in ct:
                    suffix = ".png"
                elif "gif" in ct:
                    suffix = ".gif"
                elif "webp" in ct:
                    suffix = ".webp"
                elif "mp4" in ct:
                    suffix = ".mp4"
                raw_bytes = await resp.read()
                fd, path = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(raw_bytes)
                logger.info(f"[云湖] 媒体下载成功: {path} ({len(raw_bytes)} bytes)")
                return path
    except Exception as e:
        logger.warning(f"[云湖] 媒体下载异常: {e}，将回退到 URL 模式")
        return ""


async def _download_image_as_base64(url: str, filename: str = "image.jpg") -> str:
    """
    使用云湖 CDN 所需的 Referer 请求头下载图片，返回 "base64://XXXX" 格式字符串。
    下载失败时返回空字符串，调用方应降级到原始 URL。
    """
    import base64 as _base64

    if not url:
        return ""

    headers = {"Referer": "https://myapp.jwznb.com"}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[云湖] 图片预下载失败 status={resp.status} url={url[:80]}"
                    )
                    return ""
                raw_bytes = await resp.read()
                b64_str = _base64.b64encode(raw_bytes).decode("ascii")
                logger.info(
                    f"[云湖] 图片预下载成功: {filename} ({len(raw_bytes)} bytes)"
                )
                return f"base64://{b64_str}"
    except Exception as e:
        logger.warning(f"[云湖] 图片预下载异常: {e}，url={url[:80]}")
        return ""


@register_platform_adapter(
    "yunhu",
    "云湖平台适配器（通过 WebSocket 桥接 SDK）",
    default_config_tmpl={
        "ws_url": "ws://127.0.0.1:8080/ws",
        "ws_token": "",
        "bot_token": "",
        "reply_in_thread": False,
    },
)
class YunhuAdapter(Platform):
    """
    云湖平台适配器

    配置项:
        ws_url         — SDK WS 桥接地址，如 ws://192.168.1.100:8080/ws
        ws_token       — 鉴权 token（与 SDK 启动时 --ws-token 一致，留空则不鉴权）
        reply_in_thread— 是否以 parentId 方式回复原消息（线程模式）
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        
        self.config = platform_config
        self.settings = platform_settings

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False

    # 必须实现的接口

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata("yunhu", "云湖平台适配器", "yunhu")

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        await super().send_by_session(session, message_chain)

    # 主循环

    async def run(self):
        """连接 SDK WS，断线自动重连"""
        self._running = True
        ws_url = self.config.get("ws_url", "ws://127.0.0.1:8080/ws")
        ws_token = self.config.get("ws_token", "")

        if ws_token:
            sep = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{sep}token={ws_token}"

        while self._running:
            try:
                logger.info(f"[云湖] 正在连接 SDK WS: {ws_url}")
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    ws_url,
                    heartbeat=30,
                    receive_timeout=RECV_TIMEOUT,
                    max_msg_size=100 * 1024 * 1024,  # 100 MB，支持发送大文件 base64，但仍受云湖 API 限制，等以后云湖升级后再调整
                )
                logger.info("[云湖] WS 连接成功，开始接收事件")
                await self._receive_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[云湖] WS 连接异常: {e}，{RECONNECT_DELAY}s 后重连…")
            finally:
                await self._close_session()
                if self._running:
                    await asyncio.sleep(RECONNECT_DELAY)

        logger.info("[云湖] 适配器已停止")

    async def _receive_loop(self):
        """持续从 WS 读取事件并处理"""
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._on_raw_event(msg.data)
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                logger.info("[云湖] WS 连接已关闭")
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning(f"[云湖] WS 错误: {msg.data}")
                break

    async def _close_session(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None

    # 事件处理

    async def _on_raw_event(self, raw: str):
        """解析 SDK 推来的原始 JSON 事件"""
        try:
            data = json.loads(raw)
        except Exception as e:
            logger.warning(f"[云湖] 事件 JSON 解析失败: {e}")
            return

        # 只处理消息事件
        header = data.get("header", {})
        event_type = header.get("eventType", "")

        if event_type in ("message.receive.normal", "message.receive.instruction"):
            abm = await self._convert_message(data)
            if abm:
                await self._handle_msg(abm, data)
        else:
            logger.debug(f"[云湖] 忽略事件类型: {event_type}")

    async def _convert_message(self, data: dict) -> AstrBotMessage | None:
        """将云湖事件原始 JSON 转换为 AstrBotMessage，媒体文件下载到本地临时文件供 AstrBot 使用"""
        _bot_token = self.config.get("bot_token", "")
        try:
            event_data = data.get("event", {})
            sender_data = event_data.get("sender", {})
            chat_data = event_data.get("chat", {})
            msg_data = event_data.get("message", {})
            content = msg_data.get("content", {})
            content_type = msg_data.get("contentType", "text")
            chat_type = chat_data.get("chatType", "bot")   # "bot"=私聊 / "group"=群聊
            chat_id = chat_data.get("chatId", "")
            sender_id = sender_data.get("senderId", "")
            sender_nick = sender_data.get("senderNickname", sender_id)

            abm = AstrBotMessage()

            # 判断消息类型（群 or 私聊）
            if chat_type == "group":
                abm.type = MessageType.GROUP_MESSAGE
                abm.group_id = chat_id
            else:
                abm.type = MessageType.FRIEND_MESSAGE

            abm.self_id = data.get("header", {}).get("appId", "yunhu_bot")
            abm.sender = MessageMember(user_id=sender_id, nickname=sender_nick)
            abm.session_id = chat_id or sender_id
            abm.message_id = msg_data.get("msgId", "")
            abm.raw_message = data

            # 构建消息链
            chain = []
            if content_type == "text":
                text = content.get("text", "")
                abm.message_str = text
                chain.append(Plain(text=text))

            elif content_type == "image":
                abm.message_str = "[图片]"
                cdn_url = content.get("imageUrl") or content.get("url") or ""
                if cdn_url:
                    # 云湖 CDN 需要携带 Referer 才能访问（无此 Header 会返回 403）。
                    filename = os.path.basename(cdn_url.split("?")[0]) or "image.jpg"
                    b64_file = await _download_image_as_base64(cdn_url, filename)
                    if b64_file:
                        img = Image(file=b64_file)
                        chain.append(img)
                        logger.info(f"[云湖] 图片以 base64:// 写入消息链: {filename}")

                    else:
                        logger.warning(f"[云湖] base64 下载失败，尝试本地临时文件降级: {cdn_url[:80]}")
                        local_path = await _download_media(cdn_url, "image")
                        if local_path:
                            file_uri = "file:///" + local_path.lstrip("/")
                            img = Image(file=file_uri)
                            chain.append(img)
                            logger.info(f"[云湖] 图片以 file:/// 写入消息链: {file_uri}")
                        else:
                            logger.warning(f"[云湖] 本地下载也失败，最终降级为 CDN URL: {cdn_url[:80]}")
                            img = Image(file=cdn_url)
                            chain.append(img)
                else:
                    logger.warning(f"[云湖] 图片无可用地址，原始 content={content!r}")
                    chain.append(Plain(text="[图片（无法获取）]"))

            elif content_type == "file":
                cdn_url = content.get("fileUrl") or content.get("url") or ""
                file_name = (
                    content.get("fileName")
                    or content.get("file_name")
                    or content.get("name")
                    or os.path.basename(cdn_url.split("?")[0])
                    or "unknown_file"
                )
                abm.message_str = f"[文件:{file_name}]"
                if cdn_url:
                    chain.append(File(file=cdn_url, name=file_name))
                else:
                    logger.warning(f"[云湖] 文件无可用地址，原始 content={content!r}")
                    chain.append(Plain(text=f"[文件:{file_name}（无法获取）]"))

            elif content_type == "video":
                abm.message_str = "[视频]"
                cdn_url = content.get("videoUrl") or content.get("url") or ""
                if cdn_url:
                    filename = os.path.basename(cdn_url.split("?")[0]) or "video.mp4"
                    chain.append(Video(file=cdn_url, filename=filename))
                else:
                    logger.warning(f"[云湖] 视频无可用地址，原始 content={content!r}")
                    chain.append(Plain(text="[视频（无法获取）]"))


            elif content_type == "markdown":
                text = content.get("text", "")
                abm.message_str = text
                chain.append(Plain(text=text))

            else:
                abm.message_str = f"[{content_type}]"
                chain.append(Plain(text=abm.message_str))

            abm.message = chain
            return abm

        except Exception as e:
            logger.error(f"[云湖] 消息转换失败: {e}", exc_info=True)
            return None

    async def _handle_msg(self, abm: AstrBotMessage, raw_data: dict):
        """构造 YunhuMessageEvent 并提交到 AstrBot 事件队列"""
        event_data = raw_data.get("event", {})
        chat_data = event_data.get("chat", {})
        msg_data = event_data.get("message", {})

        chat_type = chat_data.get("chatType", "bot")

        if chat_type == "group":
            recv_id = chat_data.get("chatId", "")
            recv_type = "group"
        else:
            recv_id = abm.sender.user_id
            recv_type = "user"

        # 是否使用线程回复（parentId）
        parent_id = ""
        if self.config.get("reply_in_thread", False):
            parent_id = msg_data.get("msgId", "")

        event = YunhuMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            ws=self._ws,
            recv_id=recv_id,
            recv_type=recv_type,
            parent_id=parent_id,
        )

        self.commit_event(event)