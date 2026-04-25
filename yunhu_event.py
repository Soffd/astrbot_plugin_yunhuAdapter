"""
云湖平台消息事件

通过 YunhuClient 直接调用云湖 HTTP API 发送消息，
支持文本、Markdown、图片、文件、视频、按钮、撤回、编辑、看板等全部功能。
"""
import asyncio
import base64
import json
import logging
import os
import tempfile
import aiohttp
from typing import Optional, List

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata, MessageMember
from astrbot.api.message_components import Plain, Image, File, Video, Record

from .client import YunhuClient
from .models import Button, ButtonGroup, ApiResponse

logger = logging.getLogger("yunhu.event")

# 云湖 /bot/send 单次文本最大字节数
_MAX_TEXT_LEN = 3500

# 常见图片文件扩展名，用于判断 Plain 组件是否实为本地图片路径
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

import re as _re

# 各类 Markdown 特征的正则，按"命中任意一条即视为 Markdown"
_MD_PATTERNS = [
    _re.compile(r"^#{1,6}\s+\S", _re.MULTILINE),          # 标题：# 一级  ## 二级 …
    _re.compile(r"\*\*\S.*?\S\*\*"),                        # 粗体：**text**
    _re.compile(r"(?<!\*)\*(?!\*)\S.*?\S\*(?!\*)"),         # 斜体：*text*（排除 **）
    _re.compile(r"__\S.*?\S__"),                            # 粗体：__text__
    _re.compile(r"(?<!_)_(?!_)\S.*?\S_(?!_)"),             # 斜体：_text_（排除 __）
    _re.compile(r"`{1,3}[\s\S]+?`{1,3}"),                  # 行内/块代码：`code` 或 ```block```
    _re.compile(r"^```", _re.MULTILINE),                    # 代码块起始行
    _re.compile(r"^\s*[-*+]\s+\S", _re.MULTILINE),         # 无序列表：- item / * item / + item
    _re.compile(r"^\s*\d+\.\s+\S", _re.MULTILINE),         # 有序列表：1. item
    _re.compile(r"^\s*>\s+\S", _re.MULTILINE),             # 引用：> text
    _re.compile(r"\[.+?\]\(.+?\)"),                         # 链接：[text](url)
    _re.compile(r"!\[.*?\]\(.+?\)"),                        # 图片：![alt](url)
    _re.compile(r"^\|.*\|$", _re.MULTILINE),               # 表格行：| a | b |
    _re.compile(r"^\s*---+\s*$", _re.MULTILINE),           # 分隔线：---
]


def _looks_like_markdown(text: str) -> bool:
    """判断文本是否包含 Markdown 特征"""
    return any(p.search(text) for p in _MD_PATTERNS)


class YunhuMessageEvent(AstrMessageEvent):
    """
    云湖平台消息事件

    通过 YunhuClient 直接调用 HTTP API 发送消息，
    """

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: YunhuClient,
        recv_id: str,
        recv_type: str,
        parent_id: str = "",
        dl_session: aiohttp.ClientSession = None,
    ):
        super().__init__(
            message_str=message_str,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )
        self._client = client
        self._recv_id = recv_id
        self._recv_type = recv_type
        self._parent_id = parent_id
        self._dl_session = dl_session

    # 供 send_by_session 使用的类方法 

    @classmethod
    async def send_message(
        cls,
        client: YunhuClient,
        recv_id: str,
        recv_type: str,
        message_chain: MessageChain,
        dl_session: aiohttp.ClientSession = None,
        parent_id: str = "",
    ):
        """
        在没有事件实例的情况下发送消息链到云湖平台。

        此方法供 YunhuAdapter.send_by_session() 调用，用于处理
        AstrBot 内置 Agent 工具（如 send_message_to_user）主动
        发送消息的场景。

        Args:
            client: YunhuClient 实例
            recv_id: 接收者 ID（用户 ID 或群组 ID）
            recv_type: 接收者类型（"user" 或 "group"）
            message_chain: 消息链
            dl_session: 可复用的 aiohttp 会话，用于下载远程资源
            parent_id: 父消息 ID，用于消息串回复
        """
        from astrbot.api.platform import AstrBotMessage, MessageType

        # 构造一个最小化的 AstrBotMessage，仅用于满足父类初始化要求
        dummy_msg = AstrBotMessage()
        dummy_msg.type = MessageType.FRIEND_MESSAGE
        dummy_msg.self_id = ""
        dummy_msg.session_id = recv_id
        dummy_msg.message_id = ""
        dummy_msg.sender = MessageMember(user_id=recv_id)
        dummy_msg.message = []
        dummy_msg.message_str = ""
        dummy_msg.raw_message = {}

        # 构造一个最小化的 PlatformMetadata
        dummy_meta = PlatformMetadata(
            name="yunhu",
            description="云湖平台适配器",
            id="yunhu",
        )

        # 创建轻量级事件实例
        event = cls(
            message_str="",
            message_obj=dummy_msg,
            platform_meta=dummy_meta,
            session_id=recv_id,
            client=client,
            recv_id=recv_id,
            recv_type=recv_type,
            parent_id=parent_id,
            dl_session=dl_session,
        )

        # 直接调用各组件的发送方法，跳过 super().send() 避免标记事件已处理
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                await event._send_plain(comp)
            elif isinstance(comp, Image):
                await event._send_image(comp)
            elif isinstance(comp, File):
                await event._send_file(comp)
            elif isinstance(comp, Video):
                await event._send_video(comp)
            elif isinstance(comp, Record):
                await event._send_text("[语音消息]")

    # 核心发送方法 

    async def send(self, message: MessageChain):
        """
        发送消息链到云湖平台。

        处理逻辑：
        1. 遍历消息链中的每个组件
        2. 图片/文件/视频：先上传获取 key，再发送
        3. 文本：检测是否为 Markdown，自动选择 contentType
        4. 超长文本自动分段发送
        """
        for comp in message.chain:
            if isinstance(comp, Plain):
                await self._send_plain(comp)
            elif isinstance(comp, Image):
                await self._send_image(comp)
            elif isinstance(comp, File):
                await self._send_file(comp)
            elif isinstance(comp, Video):
                await self._send_video(comp)
            elif isinstance(comp, Record):
                # 云湖不支持语音消息，转为文本提示
                await self._send_text("[语音消息]")
        # 必须调用父类 send 方法，标记事件已处理，防止 LLM 重复响应
        await super().send(message)

    # 文本发送 

    async def _send_plain(self, comp: Plain):
        """发送文本组件，自动检测 Markdown"""
        text = comp.text
        if not text:
            return

        # 检查是否为本地图片路径
        ext = os.path.splitext(text.strip())[-1].lower()
        if ext in _IMAGE_EXTS and os.path.isfile(text.strip()):
            await self._send_image(Image(file=text.strip()))
            return

        # 检测 Markdown
        if _looks_like_markdown(text):
            await self._send_markdown(text)
        else:
            await self._send_text(text)

    async def _send_text(self, text: str):
        """发送纯文本消息，超长自动分段"""
        if not text:
            return

        # 按字节长度分段
        chunks = _split_text(text, _MAX_TEXT_LEN)
        for chunk in chunks:
            resp = await self._client.send_text(
                recv_id=self._recv_id,
                recv_type=self._recv_type,
                text=chunk,
                parent_id=self._parent_id,
            )
            if not resp.ok:
                logger.warning(f"[云湖] 文本发送失败: code={resp.code}, msg={resp.msg}")

    async def _send_markdown(self, text: str):
        """发送 Markdown 消息，超长自动分段"""
        if not text:
            return

        chunks = _split_markdown(text, _MAX_TEXT_LEN)
        for chunk in chunks:
            resp = await self._client.send_markdown(
                recv_id=self._recv_id,
                recv_type=self._recv_type,
                text=chunk,
                parent_id=self._parent_id,
            )
            if not resp.ok:
                logger.warning(f"[云湖] Markdown 发送失败: code={resp.code}, msg={resp.msg}")

    # 图片发送 

    async def _send_image(self, comp: Image):
        """
        发送图片消息。

        支持的格式：
          - data:image/xxx;base64,XXX  —— data URL
          - base64://XXXX              —— AstrBot 标准 base64 前缀格式
          - file:///path 或 /path      —— 本地文件路径
          - http(s)://...              —— 远程 URL，下载后上传
          - 裸 base64 字符串           —— 尝试解码，成功则上传
          - 短字符串                   —— 视为云湖 CDN imageKey 直接发送
        """
        url = comp.file if comp.file else (comp.url if comp.url else "")
        if not url:
            return

        # data URL
        if url.startswith("data:"):
            b64_str, filename = self._parse_data_url(url)
            if b64_str:
                try:
                    image_data = base64.b64decode(b64_str)
                    image_key = await self._upload_image_data(image_data, filename)
                    if image_key:
                        await self._send_image_by_key(image_key)
                except Exception as e:
                    logger.error(f"[云湖] data URL 图片解码失败: {e}")
            return

        # base64:// 前缀
        if url.startswith("base64://"):
            b64_str = url[len("base64://"):]
            try:
                image_data = base64.b64decode(b64_str)
                image_key = await self._upload_image_data(image_data, "image.png")
                if image_key:
                    await self._send_image_by_key(image_key)
            except Exception as e:
                logger.error(f"[云湖] base64 图片解码失败: {e}")
            return

        # 本地文件路径
        local_path = url
        if url.startswith("file://"):
            local_path = url[7:]
        if os.path.isfile(local_path):
            await self._send_image_by_file(local_path)
            return

        # HTTP(S) URL
        if url.startswith("http://") or url.startswith("https://"):
            # 远程 URL，下载后上传
            path = await self._download_to_temp(url)
            if path:
                await self._send_image_by_file(path)
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return

            # 下载失败，尝试作为 imageKey 直接发送
            await self._send_image_by_key(url)
            return

        # 裸 base64 字符串
        try:
            decoded = base64.b64decode(url, validate=True)
            if decoded[:4] in (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8"):
                image_key = await self._upload_image_data(decoded, "image.png")
                if image_key:
                    await self._send_image_by_key(image_key)
                return
        except Exception:
            pass

        # 短字符串，视为 imageKey
        await self._send_image_by_key(url)

    async def _send_image_by_file(self, file_path: str):
        """通过本地文件上传发送图片"""
        resp = await self._client.upload_image(file_path)
        if resp.ok and resp.data:
            image_key = resp.data.get("imageKey", "")
            if image_key:
                await self._send_image_by_key(image_key)
                return
        logger.warning(f"[云湖] 图片上传失败: code={resp.code}, msg={resp.msg}")

    async def _send_image_by_key(self, image_key: str):
        """通过 imageKey 发送图片"""
        resp = await self._client.send_image(
            recv_id=self._recv_id,
            recv_type=self._recv_type,
            image_key=image_key,
            parent_id=self._parent_id,
        )
        if not resp.ok:
            logger.warning(f"[云湖] 图片发送失败: code={resp.code}, msg={resp.msg}")

    async def _upload_image_data(
        self, image_data: bytes, filename: str = "image.png"
    ) -> Optional[str]:
        """将图片二进制数据上传到云湖，返回 imageKey"""
        try:
            # 写入临时文件
            fd, path = tempfile.mkstemp(suffix=os.path.splitext(filename)[-1])
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(image_data)
                resp = await self._client.upload_image(path)
                if resp.ok and resp.data:
                    return resp.data.get("imageKey", "")
                else:
                    logger.warning(f"[云湖] 图片上传失败: code={resp.code}, msg={resp.msg}")
                    return None
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[云湖] 图片上传异常: {e}")
            return None

    # 文件发送 

    async def _send_file(self, comp: File):
        """
        发送文件消息。

        支持的格式：
          - 本地文件路径
          - HTTP(S) URL（下载后上传）
          - 云湖 CDN fileKey（直接发送）
        """
        url = comp.file if comp.file else (comp.url if comp.url else "")
        if not url:
            return

        # 本地文件路径
        local_path = url
        if url.startswith("file://"):
            local_path = url[7:]
        if os.path.isfile(local_path):
            await self._send_file_by_path(local_path)
            return

        # HTTP(S) URL
        if url.startswith("http://") or url.startswith("https://"):
            # 远程 URL，下载后上传
            path = await self._download_to_temp(url)
            if path:
                await self._send_file_by_path(path)
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return

            # 下载失败，尝试作为 fileKey 直接发送
            await self._send_file_by_key(url)
            return

        # 短字符串，视为 fileKey
        await self._send_file_by_key(url)

    async def _send_file_by_path(self, file_path: str):
        """通过本地文件上传发送文件"""
        resp = await self._client.upload_file(file_path)
        if resp.ok and resp.data:
            file_key = resp.data.get("fileKey", "")
            if file_key:
                await self._send_file_by_key(file_key)
                return
        logger.warning(f"[云湖] 文件上传失败: code={resp.code}, msg={resp.msg}")

    async def _send_file_by_key(self, file_key: str):
        """通过 fileKey 发送文件"""
        resp = await self._client.send_file(
            recv_id=self._recv_id,
            recv_type=self._recv_type,
            file_key=file_key,
            parent_id=self._parent_id,
        )
        if not resp.ok:
            logger.warning(f"[云湖] 文件发送失败: code={resp.code}, msg={resp.msg}")

    # 视频发送 

    async def _send_video(self, comp: Video):
        """
        发送视频消息。

        支持的格式：
          - 本地文件路径
          - HTTP(S) URL（下载后上传）
          - 云湖 CDN videoKey（直接发送）
        """
        url = comp.file if comp.file else (comp.url if comp.url else "")
        if not url:
            return

        # 本地文件路径
        local_path = url
        if url.startswith("file://"):
            local_path = url[7:]
        if os.path.isfile(local_path):
            await self._send_video_by_path(local_path)
            return

        # HTTP(S) URL
        if url.startswith("http://") or url.startswith("https://"):
            # 远程 URL，下载后上传
            path = await self._download_to_temp(url)
            if path:
                await self._send_video_by_path(path)
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return

            # 下载失败，尝试作为 videoKey 直接发送
            await self._send_video_by_key(url)
            return

        # 短字符串，视为 videoKey
        await self._send_video_by_key(url)

    async def _send_video_by_path(self, file_path: str):
        """通过本地文件上传发送视频"""
        resp = await self._client.upload_video(file_path)
        if resp.ok and resp.data:
            video_key = resp.data.get("videoKey", "")
            if video_key:
                await self._send_video_by_key(video_key)
                return
        logger.warning(f"[云湖] 视频上传失败: code={resp.code}, msg={resp.msg}")

    async def _send_video_by_key(self, video_key: str):
        """通过 videoKey 发送视频"""
        resp = await self._client.send_video(
            recv_id=self._recv_id,
            recv_type=self._recv_type,
            video_key=video_key,
            parent_id=self._parent_id,
        )
        if not resp.ok:
            logger.warning(f"[云湖] 视频发送失败: code={resp.code}, msg={resp.msg}")

    # 高级功能 

    async def recall_message(self, msg_id: str, chat_id: str = "", chat_type: str = "") -> ApiResponse:
        """
        撤回消息

        Args:
            msg_id: 要撤回的消息 ID
            chat_id: 聊天 ID（群 ID 或用户 ID），不填则使用当前会话
            chat_type: 聊天类型（group/user），不填则根据当前会话推断
        """
        if not chat_id:
            chat_id = self._recv_id
        if not chat_type:
            chat_type = self._recv_type
        return await self._client.recall_message(msg_id, chat_id, chat_type)

    async def edit_message(
        self, msg_id: str, content_type: str, content: dict
    ) -> ApiResponse:
        """
        编辑已发送的消息

        Args:
            msg_id: 要编辑的消息 ID
            content_type: 消息类型（text/markdown/image/file/video）
            content: 消息内容字典
        """
        return await self._client.edit_message(
            msg_id=msg_id,
            recv_id=self._recv_id,
            recv_type=self._recv_type,
            content_type=content_type,
            content=content,
        )

    async def set_board(
        self,
        content_type: str,
        content: str,
        member_id: str = "",
        expire_time: int = 0,
    ) -> ApiResponse:
        """
        设置用户看板

        Args:
            content_type: 看板内容类型（text/markdown）
            content: 看板内容
            member_id: 目标用户 ID，不填则针对当前会话用户
            expire_time: 过期时间（秒），0 表示不过期
        """
        chat_id = self._recv_id
        chat_type = self._recv_type
        return await self._client.set_board(
            chat_id=chat_id,
            chat_type=chat_type,
            content_type=content_type,
            content=content,
            member_id=member_id,
            expire_time=expire_time,
        )

    async def dismiss_board(self, member_id: str = "") -> ApiResponse:
        """取消用户看板"""
        return await self._client.dismiss_board(
            chat_id=self._recv_id,
            chat_type=self._recv_type,
            member_id=member_id,
        )

    async def send_with_buttons(
        self,
        text: str,
        buttons: List[ButtonGroup],
        content_type: str = "text",
    ) -> ApiResponse:
        """
        发送带按钮的消息

        Args:
            text: 消息文本
            buttons: 按钮组列表
            content_type: 消息类型（text/markdown）
        """
        if content_type == "markdown":
            return await self._client.send_markdown(
                recv_id=self._recv_id,
                recv_type=self._recv_type,
                text=text,
                parent_id=self._parent_id,
                buttons=buttons,
            )
        else:
            return await self._client.send_text(
                recv_id=self._recv_id,
                recv_type=self._recv_type,
                text=text,
                parent_id=self._parent_id,
                buttons=buttons,
            )

    async def batch_send(
        self,
        recv_ids: list,
        content_type: str,
        content: dict,
    ) -> ApiResponse:
        """
        批量发送消息

        Args:
            recv_ids: 接收者 ID 列表
            content_type: 消息类型
            content: 消息内容
        """
        return await self._client.batch_send(
            recv_ids=recv_ids,
            recv_type=self._recv_type,
            content_type=content_type,
            content=content,
        )

    # 辅助方法 

    @staticmethod
    def _parse_data_url(url: str) -> tuple:
        """
        解析 data URL，返回 (base64_data, filename)
        """
        try:
            # data:image/png;base64,XXXX
            header, b64 = url.split(",", 1)
            mime = header.split(":")[1].split(";")[0]  # image/png
            suffix = mime.split("/")[-1]
            if suffix == "jpeg":
                suffix = "jpg"
            return b64, f"image.{suffix}"
        except Exception as e:
            logger.error(f"[云湖] data URL 解析失败: {e}")
            return None, "image.png"

    async def _download_to_temp(self, url: str) -> Optional[str]:
        """下载远程 URL 到临时文件，返回临时文件路径；失败返回 None"""
        try:
            session = self._dl_session or aiohttp.ClientSession()
            own_session = self._dl_session is None
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[云湖] 下载失败 status={resp.status}: {url[:80]}")
                        return None
                    suffix = os.path.splitext(url.split("?")[0])[-1] or ".bin"
                    fd, path = tempfile.mkstemp(suffix=suffix)
                    with os.fdopen(fd, "wb") as f:
                        f.write(await resp.read())
                return path
            finally:
                if own_session:
                    await session.close()
        except Exception as e:
            logger.error(f"[云湖] 下载失败 {url}: {e}")
            return None


# 文本分段工具 

def _split_text(text: str, max_len: int) -> list:
    """
    将超长文本按字节长度切割为多个分块，每块不超过 max_len 字节。
    尽量在换行符处切割，保持句子完整。
    """
    if len(text.encode("utf-8")) <= max_len:
        return [text]

    chunks = []
    lines = text.split("\n")
    current = ""

    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate.encode("utf-8")) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # 单行超长，强制硬切
            if len(line.encode("utf-8")) > max_len:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i:i + max_len])
                current = ""
            else:
                current = line

    if current:
        chunks.append(current)

    return chunks if chunks else [text[:max_len]]


def _split_markdown(text: str, max_len: int) -> list:
    """
    将超长 Markdown 文本按段落边界（空行）切割为多个分块，
    每块不超过 max_len 字节，尽量保持段落完整不被截断。

    如果单个段落本身超过 max_len，则按 max_len 强制截断（不可避免）。
    返回非空字符串列表。
    """
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        # 单段落超过上限，强制硬切
        if len(para) > max_len:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for i in range(0, len(para), max_len):
                chunks.append(para[i:i + max_len])
            continue

        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current.rstrip())
            current = para

    if current:
        chunks.append(current.rstrip())

    return chunks if chunks else [text[:max_len]]