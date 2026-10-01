"""AstrBot 消息链发送与云湖事件辅助 API。"""

import logging
import os
import re
from pathlib import Path

import aiohttp
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

from ..api.client import YunhuClient, ensure_success
from ..api.models import ApiResponse, ButtonGroup
from ..media.names import safe_filename

logger = logging.getLogger("yunhu.event")
_MAX_TEXT_LEN = 3500
_MD_PATTERNS = [
    re.compile(r"^#{1,6}\s+\S", re.MULTILINE),
    re.compile(r"\*\*.+?\*\*|__.+?__|`.+?`|\[.+?\]\(.+?\)"),
    re.compile(r"^\s*(?:```|[-*+]\s+|\d+\.\s+|>\s+|\|.*\||---+\s*$)", re.MULTILINE),
]


def _looks_like_markdown(text: str) -> bool:
    return any(pattern.search(text) for pattern in _MD_PATTERNS)


def _split_utf8(text: str, max_len: int, separator: str) -> list[str]:
    """严格按 UTF-8 字节切分，保留所有字符及空白，优先在段落/行末切分。"""
    if max_len < 4:
        raise ValueError("max_len 至少为 4，才能容纳完整 UTF-8 字符")
    chunks = []
    start, position, size, boundary = 0, 0, 0, 0
    while position < len(text):
        width = len(text[position].encode("utf-8"))
        if size + width > max_len:
            end = boundary if boundary > start else position
            chunks.append(text[start:end])
            start = position = end
            size, boundary = 0, end
            continue
        size += width
        position += 1
        if text[max(start, position - len(separator)) : position] == separator:
            boundary = position
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def _split_text(text: str, max_len: int) -> list[str]:
    return _split_utf8(text, max_len, "\n")


def _split_markdown(text: str, max_len: int) -> list[str]:
    return _split_utf8(text, max_len, "\n\n")


class YunhuMessageEvent(AstrMessageEvent):
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
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._client = client
        self._recv_id = recv_id
        self._recv_type = recv_type
        self._parent_id = parent_id
        self._dl_session = dl_session

    @property
    def client(self) -> YunhuClient:
        return self._client

    def cleanup_temporary_local_files(self):
        super().cleanup_temporary_local_files()
        manager = self.get_extra("yunhu_temp_manager")
        if manager:
            manager._cleanup_empty_directories()
            if manager.closed:
                try:
                    os.rmdir(manager.base_dir)
                except OSError:
                    pass

    @classmethod
    async def send_message(
        cls,
        client: YunhuClient,
        recv_id: str,
        recv_type: str,
        message_chain: MessageChain,
        dl_session=None,
        parent_id: str = "",
    ):
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE
            if recv_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        message.self_id = ""
        message.group_id = recv_id if recv_type == "group" else ""
        message.session_id = recv_id
        message.message_id = ""
        message.sender = MessageMember(user_id=recv_id)
        message.message = []
        message.message_str = ""
        message.raw_message = {}
        event = cls(
            "",
            message,
            PlatformMetadata(name="yunhu", description="云湖", id="yunhu"),
            recv_id,
            client,
            recv_id,
            recv_type,
            parent_id,
            dl_session,
        )
        try:
            await event._send_chain(message_chain)
        finally:
            event.cleanup_temporary_local_files()

    async def send(self, message: MessageChain):
        await self._send_chain(message)
        await super().send(message)

    async def _send_chain(self, message: MessageChain):
        previous_parent = self._parent_id
        replies = [
            component for component in message.chain if isinstance(component, Reply)
        ]
        if replies:
            self._parent_id = str(replies[0].id)
        text_parts, mentions = [], []

        async def flush():
            if text_parts:
                await self._send_plain(
                    Plain(text="".join(text_parts)),
                    mentions,
                    getattr(message, "use_markdown_", None),
                )
                text_parts.clear()
                mentions.clear()

        try:
            for component in message.chain:
                if isinstance(component, Plain):
                    text_parts.append(component.text)
                elif isinstance(component, AtAll) or (
                    isinstance(component, At) and str(component.qq) == "all"
                ):
                    # 官方 API 没有 @全员 参数，保留可见文本。
                    text_parts.append("@全体成员 ")
                elif isinstance(component, At):
                    text_parts.append(f"@{component.name or component.qq} ")
                    mentions.append(str(component.qq))
                elif isinstance(component, Reply):
                    continue
                elif isinstance(component, (Image, File, Video, Record)):
                    await flush()
                    await self._send_media(component)
                else:
                    logger.warning(
                        "[云湖] 不支持的消息组件: %s", type(component).__name__
                    )
            await flush()
        finally:
            self._parent_id = previous_parent

    async def _send_plain(
        self, component: Plain, at: list[str] = None, markdown: bool = None
    ):
        text = component.text
        if not text:
            return
        if Path(text.strip()).suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
        }:
            if os.path.isfile(text.strip()):
                await self._send_media(Image.fromFileSystem(text.strip()))
                return
        use_markdown = _looks_like_markdown(text) if markdown is None else markdown
        await self._send_text_content(
            text, "markdown" if use_markdown else "text", at=at
        )

    async def _send_text_content(
        self,
        text: str,
        content_type: str = "text",
        buttons: list[ButtonGroup] = None,
        at: list[str] = None,
    ) -> ApiResponse:
        split = _split_markdown if content_type == "markdown" else _split_text
        response = ApiResponse(1)
        chunks = split(text, _MAX_TEXT_LEN)
        for index, chunk in enumerate(chunks):
            response = ensure_success(
                await self._client.send_message(
                    self._recv_id,
                    self._recv_type,
                    content_type,
                    {"text": chunk},
                    self._parent_id,
                    buttons=buttons if index == len(chunks) - 1 else None,
                    at=list(at) if at else None,
                )
            )
        return response

    async def _send_text(self, text: str):
        return await self._send_text_content(text)

    async def _send_markdown(self, text: str):
        return await self._send_text_content(text, "markdown")

    async def _send_media(self, component):
        is_file = isinstance(component, File)
        references = (
            [getattr(component, "file_", ""), component.url]
            if is_file
            else [
                getattr(component, "file", ""),
                getattr(component, "url", ""),
                getattr(component, "path", ""),
            ]
        )
        source = (
            (getattr(component, "file_", "") or component.url)
            if is_file
            else (getattr(component, "file", "") or getattr(component, "url", ""))
        ) or ""
        kind = (
            "image"
            if isinstance(component, Image)
            else "video"
            if isinstance(component, Video)
            else "file"
        )
        # 保留已上传 CDN key 的兼容入口，其余引用交给 AstrBot 媒体工具解析。
        key_pattern = r"[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9]+)?"
        if (
            not isinstance(component, Record)
            and re.fullmatch(key_pattern, source)
            and len(source) < 128
            and not os.path.isfile(source)
        ):
            key = source
        else:
            path = (
                await component.get_file()
                if is_file
                else await component.convert_to_file_path()
            )
            if not path or not os.path.isfile(path):
                raise ValueError("媒体解析后未得到可上传的本地文件")
            original_file = False
            for reference in references:
                if not reference:
                    continue
                local = (
                    file_uri_to_path(reference) if is_file_uri(reference) else reference
                )
                try:
                    if os.path.isfile(local) and os.path.samefile(local, path):
                        original_file = True
                        break
                except (OSError, ValueError):
                    pass
            if not original_file:
                self.track_temporary_local_file(path)
            if kind == "image":
                response = await self._client.upload_image(path)
            elif kind == "video":
                response = await self._client.upload_video(path)
            else:
                response = await self._client.upload_file(
                    path,
                    filename=safe_filename(
                        getattr(component, "name", "") or os.path.basename(path),
                        decode=True,
                    ),
                )
            ensure_success(response)
            key = (
                response.data.get(f"{kind}Key", "")
                if isinstance(response.data, dict)
                else ""
            )
            if not key:
                raise ValueError(f"云湖上传成功响应缺少 {kind}Key")
        ensure_success(
            await self._client.send_message(
                self._recv_id,
                self._recv_type,
                kind,
                {f"{kind}Key": key},
                self._parent_id,
            )
        )

    async def send_streaming(self, generator, use_fallback: bool = False):
        """以 chunked 请求体发送文本增量；媒体保持顺序，线程回复使用分段模式。"""
        stream = None
        fallback = []
        fallback_markdown = None

        async def finish_stream():
            nonlocal stream
            if stream:
                ensure_success(await stream.write_eof())
                stream = None

        try:
            async for message in generator:
                if getattr(message, "type", None) == "break":
                    await finish_stream()
                    continue
                if use_fallback or self._parent_id:
                    fallback.extend(message.chain)
                    fallback_markdown = getattr(message, "use_markdown_", None)
                    continue
                # 需要 parentId/@ 的消息链使用普通发送 API。
                if any(isinstance(comp, (At, Reply)) for comp in message.chain):
                    await finish_stream()
                    await self._send_chain(message)
                    continue
                for component in message.chain:
                    if isinstance(component, Plain) and component.text:
                        if stream is None:
                            content_type = (
                                "text"
                                if getattr(message, "use_markdown_", None) is False
                                else "markdown"
                            )
                            stream = await self._client.send_stream(
                                self._recv_id, self._recv_type, content_type
                            )
                        await stream.write(component.text)
                    elif not isinstance(component, Plain):
                        await finish_stream()
                        await self._send_chain(MessageChain([component]))
            await finish_stream()
            if fallback:
                await self._send_chain(
                    MessageChain(fallback, use_markdown_=fallback_markdown)
                )
            await super().send_streaming(generator, use_fallback)
        finally:
            if stream:
                await stream.abort()

    async def send_html(self, text: str) -> ApiResponse:
        return ensure_success(
            await self._client.send_html(
                self._recv_id, self._recv_type, text, self._parent_id
            )
        )

    async def recall_message(
        self, msg_id: str, chat_id: str = "", chat_type: str = ""
    ) -> ApiResponse:
        return await self._client.recall_message(
            msg_id, chat_id or self._recv_id, chat_type or self._recv_type
        )

    async def edit_message(
        self, msg_id: str, content_type: str, content: dict
    ) -> ApiResponse:
        return await self._client.edit_message(
            msg_id, self._recv_id, self._recv_type, content_type, content
        )

    async def get_message_history(
        self, message_id: str = "", before: int = 20, after: int = 0
    ) -> ApiResponse:
        """查询云湖历史消息；get_messages() 保留为框架的同步消息链接口。"""
        return await self._client.get_messages(
            self._recv_id,
            self._recv_type,
            before=before,
            after=after,
            message_id=message_id,
        )

    async def set_board(
        self, content_type: str, content: str, member_id: str = "", expire_time: int = 0
    ) -> ApiResponse:
        """expire_time 是秒级 Unix 时间戳，0 为不过期。"""
        return await self._client.set_board(
            self._recv_id,
            self._recv_type,
            content_type,
            content,
            member_id,
            expire_time,
        )

    async def set_board_all(
        self, content_type: str, content: str, expire_time: int = 0
    ) -> ApiResponse:
        return await self._client.set_board_all(
            content_type=content_type, content=content, expire_time=expire_time
        )

    async def dismiss_board(self, member_id: str = "") -> ApiResponse:
        return await self._client.dismiss_board(
            self._recv_id, self._recv_type, member_id
        )

    async def dismiss_board_all(self) -> ApiResponse:
        return await self._client.dismiss_board_all()

    async def send_with_buttons(
        self, text: str, buttons: list[ButtonGroup], content_type: str = "text"
    ) -> ApiResponse:
        return await self._send_text_content(text, content_type, buttons=buttons)

    async def batch_send(
        self, recv_ids: list, content_type: str, content: dict
    ) -> ApiResponse:
        return await self._client.batch_send(
            recv_ids, self._recv_type, content_type, content
        )
