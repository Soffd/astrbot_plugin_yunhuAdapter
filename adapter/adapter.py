"""云湖平台适配器：Webhook / WebSocket 接收、消息转换及生命周期管理。"""

import asyncio
import copy
import hmac
import json
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from yarl import URL

from ..api.client import YunhuClient
from ..api.models import (
    MESSAGE_EVENTS,
    NOTICE_EVENTS,
    YunhuEvent,
    parse_content,
    parse_event,
)
from ..media.cdn import CdnProxy, DownloadedMedia
from ..media.names import header_filename, received_filename
from ..media.store import FileStore, default_store_directory
from ..media.temp_files import TempFileManager
from .config import CONFIG_METADATA, DEFAULT_CONFIG
from .event import YunhuMessageEvent


@register_platform_adapter(
    "yunhu", "云湖", default_config_tmpl=DEFAULT_CONFIG, config_metadata=CONFIG_METADATA
)
class YunhuAdapter(Platform):
    def __init__(
        self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ):
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        config = {**DEFAULT_CONFIG, **platform_config}
        self.bot_token = config["bot_token"].strip()
        self.bot_id = str(config["bot_id"] or "").strip()
        if re.fullmatch(r"(?:yunhu_)?[a-f0-9]{16}", self.bot_id):
            raise ValueError("bot_id 不能填写旧版占位ID，请填写云湖机器人真实数字ID")
        self.connection_mode = config["connection_mode"].lower().strip()
        if self.connection_mode not in ("webhook", "websocket"):
            raise ValueError("connection_mode 必须为 webhook 或 websocket")
        self.webhook_host = config["webhook_host"]
        self.webhook_port = int(config["webhook_port"])
        self.webhook_path = config["webhook_path"]
        if not self.webhook_path.startswith("/"):
            raise ValueError("webhook_path 必须以 / 开头")
        self.webhook_secret = config["webhook_secret"]
        websocket_url = config["websocket_url"]
        if websocket_url is None:
            websocket_url = ""
        if not isinstance(websocket_url, str):
            raise ValueError("websocket_url 必须为字符串，留空使用默认地址")
        # AstrBot 已保存的空值会覆盖配置模板，必须显式恢复默认地址。
        self.websocket_url = websocket_url.strip() or DEFAULT_CONFIG["websocket_url"]
        if self.connection_mode == "websocket":
            try:
                url = URL(self.websocket_url)
                valid_url = url.scheme in ("ws", "wss") and bool(url.host)
                url.port  # 校验端口，避免带有无效端口的地址进入重连循环。
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError(
                    "websocket_url 必须为完整的 ws:// 或 wss:// 地址，"
                    "例如 wss://ws.jwzhd.com/subscribe；留空使用默认地址"
                )
        self.reply_in_thread = config["reply_in_thread"]
        self.media_ttl = max(1, int(config["media_ttl"]))
        self.custom_cdn_proxy = config["custom_cdn_proxy"]
        # Token 不能进入 AstrBotMessage、日志或会话标识。
        self.client_self_id = self.bot_id
        self._client = YunhuClient(self.bot_token)
        self._temp_manager = TempFileManager(ttl=self.media_ttl)
        retention = int(config["file_retention_days"])
        capacity = int(config["file_store_max_mb"])
        count = int(config["file_store_max_count"])
        if retention < 0 or capacity < 1 or count < 1:
            raise ValueError("file_retention_days必须非负，文件库容量和数量必须大于0")
        self._file_store = FileStore(
            config["file_store_dir"] or default_store_directory(),
            config.get("id") or self.bot_token,
            retention_days=retention,
            max_bytes=capacity * 1024 * 1024,
            max_files=count,
        )
        self._cdn_proxy = None
        self._dl_session = None
        self._webhook_app = None
        self._webhook_runner = None
        self._ws_session = None
        self._ws_connection = None
        self._ws_listen_task = None
        self._ws_running = False
        self._cleanup_task = None
        self._tasks: set[asyncio.Task] = set()
        self._pending_ids: set[str] = set()
        self._seen_ids: OrderedDict[str, float] = OrderedDict()
        self._message_cache: OrderedDict[tuple[str, str, str], tuple[float, dict]] = (
            OrderedDict()
        )
        self._identity_warning_sent = False
        self._stop_event = asyncio.Event()
        self._terminate_lock = asyncio.Lock()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="yunhu",
            description="云湖平台适配器",
            id=self.config.get("id") or "yunhu",
            support_streaming_message=True,
            support_proactive_message=True,
        )

    def get_client(self) -> YunhuClient:
        return self._client

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ):
        if session.platform_id != self.meta().id:
            return
        recv_type = (
            "group" if session.message_type == MessageType.GROUP_MESSAGE else "user"
        )
        await YunhuMessageEvent.send_message(
            self._client, session.session_id, recv_type, message_chain, self._dl_session
        )
        await super().send_by_session(session, message_chain)

    async def run(self):
        if not self.bot_token:
            raise ValueError("请配置云湖机器人 bot_token")
        self._stop_event.clear()
        try:
            self._dl_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
            self._cdn_proxy = CdnProxy(self._dl_session, self.custom_cdn_proxy)
            self._cleanup_task = asyncio.create_task(self._cleanup_media())
            if self.connection_mode == "websocket":
                await self._start_websocket()
            else:
                await self._start_webhook()
            await self._stop_event.wait()
        finally:
            await self.terminate()

    async def terminate(self):
        self._stop_event.set()
        async with self._terminate_lock:
            await self._stop_websocket()
            if self._webhook_runner:
                await self._webhook_runner.cleanup()
                self._webhook_runner = None
            tasks = list(self._tasks)
            if self._cleanup_task:
                tasks.append(self._cleanup_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            self._pending_ids.clear()
            self._seen_ids.clear()
            self._message_cache.clear()
            self._cleanup_task = None
            await self._client.close()
            if self._dl_session:
                await self._dl_session.close()
                self._dl_session = None
            self._cdn_proxy = None
            self._temp_manager.cleanup_all()

    async def _cleanup_media(self):
        while True:
            await asyncio.sleep(max(60, min(300, self.media_ttl // 2)))
            self._temp_manager._cleanup()
            self._temp_manager._cleanup_empty_directories()
            try:
                await asyncio.to_thread(self._file_store.cleanup)
            except (OSError, sqlite3.Error, ValueError) as error:
                logger.warning("[云湖] 文件库清理失败 (%s)", type(error).__name__)

    async def _start_webhook(self):
        self._webhook_app = web.Application()
        self._webhook_app.router.add_post(self.webhook_path, self._handle_webhook)
        self._webhook_runner = web.AppRunner(self._webhook_app, access_log=None)
        await self._webhook_runner.setup()
        await web.TCPSite(
            self._webhook_runner, self.webhook_host, self.webhook_port
        ).start()
        logger.info(
            "[云湖] Webhook 已启动，监听端口 %s，路径 %s",
            self.webhook_port,
            self.webhook_path,
        )

    async def _handle_webhook(self, request: web.Request):
        if self.webhook_secret:
            secret = request.headers.get("X-Yunhu-Secret") or request.query.get(
                "secret", ""
            )
            if not hmac.compare_digest(secret.encode(), self.webhook_secret.encode()):
                return web.json_response(
                    {"code": -1, "msg": "unauthorized"}, status=403
                )
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"code": -1, "msg": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"code": -1, "msg": "expected object"}, status=400)
        if data.get("type") == "verify":
            return web.json_response({"code": 1, "msg": "ok"})
        try:
            self._dispatch_event(data)
        except asyncio.QueueFull:
            return web.json_response({"code": -1, "msg": "busy"}, status=503)
        return web.json_response({"code": 1, "msg": "ok"})

    async def _start_websocket(self):
        self._ws_running = True
        self._ws_session = aiohttp.ClientSession()
        logger.info(
            "[云湖] WebSocket 订阅地址: %s",
            URL(self.websocket_url)
            .with_query(None)
            .with_fragment(None)
            .with_user(None),
        )
        self._ws_listen_task = asyncio.create_task(self._ws_listen_loop())

    async def _stop_websocket(self):
        self._ws_running = False
        if self._ws_listen_task:
            self._ws_listen_task.cancel()
            await asyncio.gather(self._ws_listen_task, return_exceptions=True)
            self._ws_listen_task = None
        if self._ws_connection:
            await self._ws_connection.close()
            self._ws_connection = None
        if self._ws_session:
            await self._ws_session.close()
            self._ws_session = None

    async def _ws_listen_loop(self):
        delay = 2
        while self._ws_running:
            connected_at = time.monotonic()
            try:
                # 保留自定义地址已有的查询参数，并正确编码 Token。
                url = URL(self.websocket_url).update_query(token=self.bot_token)
                async with self._ws_session.ws_connect(url, heartbeat=30) as ws:
                    self._ws_connection = ws
                    logger.info("[云湖] WebSocket 已连接")
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await self._handle_ws_message(json.loads(message.data))
                            except (ValueError, TypeError):
                                logger.warning("[云湖] 忽略无效 WebSocket 数据")
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            break
            except asyncio.CancelledError:
                raise
            except (
                aiohttp.ClientError,
                OSError,
                ValueError,
                asyncio.TimeoutError,
            ) as error:
                logger.warning(
                    "[云湖] WebSocket 连接失败 (%s): %s",
                    type(error).__name__,
                    self._client._safe_error(error) or "未返回详细原因",
                )
            finally:
                self._ws_connection = None
            if self._ws_running:
                if time.monotonic() - connected_at > 60:
                    delay = 2
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _handle_ws_message(self, data: dict):
        while len(self._tasks) >= 64:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        self._dispatch_event(data)

    def _dispatch_event(self, data: dict):
        event = parse_event(data)
        if event is None or self._stop_event.is_set():
            return
        now = time.monotonic()
        while self._seen_ids and next(iter(self._seen_ids.values())) < now - 300:
            self._seen_ids.popitem(last=False)
        key = event.event_id or (
            event.message.msgId if event.event_type in MESSAGE_EVENTS else ""
        )
        if key and (key in self._seen_ids or key in self._pending_ids):
            return
        if len(self._tasks) >= 64:
            raise asyncio.QueueFull
        if key:
            self._pending_ids.add(key)
        task = asyncio.create_task(self._process_message(data, event, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_message(
        self, raw_data: dict, parsed: YunhuEvent = None, key: str = ""
    ):
        try:
            parsed = parsed or parse_event(raw_data)
            message = await self._convert_to_abm(raw_data, parsed)
            if message:
                await self._handle_msg(message, raw_data, parsed)
                if key:
                    self._seen_ids[key] = time.monotonic()
                    while len(self._seen_ids) > 4096:
                        self._seen_ids.popitem(last=False)
        except Exception as error:
            logger.error("[云湖] 处理事件失败: %s", self._client._safe_error(error))
        finally:
            self._pending_ids.discard(key)

    async def _convert_to_abm(self, raw_data: dict, parsed: YunhuEvent = None):
        event = parsed or parse_event(raw_data)
        if not event or not event.sender or not event.chat or not event.message:
            return None
        if event.event_type in MESSAGE_EVENTS and event.sender.senderType == "bot":
            return None
        if not self.bot_id:
            identified = event.event_data.get("botId") or raw_data.get("botId")
            if not identified and isinstance(raw_data.get("header"), dict):
                identified = raw_data["header"].get("botId")
            if event.event_type in MESSAGE_EVENTS and event.chat.chatType == "bot":
                identified = event.chat.chatId
            if identified:
                self.bot_id = str(identified)
                self.client_self_id = str(identified)
            elif event.chat.chatType == "group" and not self._identity_warning_sent:
                logger.warning(
                    "[云湖] bot_id 未配置，群聊中无法确认哪个 @ 是机器人；"
                    "请填写云湖机器人数字ID，或先私聊机器人以自动识别"
                )
                self._identity_warning_sent = True
        message = AstrBotMessage()
        message.self_id = self.client_self_id
        message.sender = MessageMember(
            user_id=event.sender.senderId,
            nickname=event.sender.senderNickname or event.sender.senderId,
        )
        message.message_id = event.message.msgId
        message.timestamp = event.message.sendTime // 1000  # 云湖毫秒 -> AstrBot 秒
        message.raw_message = raw_data
        message.type = (
            MessageType.GROUP_MESSAGE
            if event.chat.chatType == "group"
            else MessageType.FRIEND_MESSAGE
        )
        message.group_id = event.chat.chatId if event.chat.chatType == "group" else ""
        message.session_id = message.group_id or event.sender.senderId
        if event.event_type in NOTICE_EVENTS:
            message.type = MessageType.OTHER_MESSAGE
        if event.event_type in MESSAGE_EVENTS:
            self._cache_message(
                event,
                message.group_id or event.sender.senderId,
                "group" if message.group_id else "user",
            )
        message.message, message.message_str = await self._content_components(
            event.message.contentType, event.message.content
        )
        logger.debug(
            "[云湖] 接收结构: self_id=%s chat_type=%s content_type=%s content_fields=%s parent_id=%s",
            message.self_id or "未识别",
            event.chat.chatType,
            event.message.contentType,
            sorted(event.message.content),
            event.message.parentId or "无",
        )
        if not message.message and event.message.commandName:
            message.message_str = "/" + event.message.commandName
            message.message = [Plain(text=message.message_str)]
        if event.message.parentId:
            reply = await self._resolve_reply(
                event.message.parentId,
                message.group_id or event.sender.senderId,
                "group" if message.group_id else "user",
            )
            message.message.insert(0, reply)
        return message if message.message else None

    async def _content_components(self, content_type: str, content: dict):
        components = []
        text = str(content.get("text") or "")
        if content_type in ("text", "markdown", "html"):
            if text:
                components.append(Plain(text=text))
        elif content_type in ("image", "file", "video", "audio"):
            media = await self._resolve_media(
                content.get(f"{content_type}Key") or "",
                content.get(f"{content_type}Url") or "",
                content_type,
                content.get("fileName") or "",
            )
            if media:
                components.append(media)
            if text:
                components.append(Plain(text=text))
            if isinstance(media, Plain):
                text = "\n".join(part for part in (text, media.text) if part)
            text = text or (
                f"[文件: {media.name if isinstance(media, File) else content.get('fileName') or 'file'}]"
                if content_type == "file"
                else {"image": "[图片]", "video": "[视频]", "audio": "[语音]"}.get(
                    content_type, "[文件]"
                )
            )
            if not components:
                components.append(Plain(text=text))
        else:
            text = text or f"[{content_type}]"
            components.append(Plain(text=text))
        mentions = content.get("at") or []
        if isinstance(mentions, list):
            # 云湖 at 是元数据，不能当作消息首段；首个@他人会阻止 AstrBot 的前缀唤醒。
            components.extend(
                At(qq=str(user)) for user in mentions if isinstance(user, (str, int))
            )
        return components, text

    def _cache_message(self, event: YunhuEvent, chat_id: str, chat_type: str):
        now = time.monotonic()
        key = (chat_type, chat_id, event.message.msgId)
        self._message_cache.pop(key, None)
        self._message_cache[key] = (
            now,
            {
                "msgId": event.message.msgId,
                "senderId": event.sender.senderId,
                "senderNickname": event.sender.senderNickname,
                "contentType": event.message.contentType,
                "content": copy.deepcopy(event.message.content),
                "sendTime": event.message.sendTime,
            },
        )
        while self._message_cache and (
            len(self._message_cache) > 256
            or next(iter(self._message_cache.values()))[0] < now - 600
        ):
            self._message_cache.popitem(last=False)

    async def _resolve_reply(
        self, message_id: str, chat_id: str, chat_type: str
    ) -> Reply:
        cached = self._message_cache.get((chat_type, chat_id, message_id))
        if cached and time.monotonic() - cached[0] <= 600:
            items = [cached[1]]
        else:
            response = await self._client.get_messages(
                chat_id, chat_type, before=0, after=0, message_id=message_id
            )
            items = (
                response.data.get("list") or []
                if response.ok and isinstance(response.data, dict)
                else []
            )
            if not response.ok:
                logger.warning(
                    "[云湖] 引用消息查询失败 (code=%s): %s",
                    response.code,
                    self._client._safe_error(RuntimeError(response.msg)),
                )
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("msgId")) == message_id:
                    try:
                        content = parse_content(item.get("content") or {})
                    except ValueError:
                        logger.warning("[云湖] 引用消息 content 格式无效")
                        break
                    chain, text = await self._content_components(
                        item.get("contentType") or "text", content
                    )
                    return Reply(
                        id=message_id,
                        chain=chain,
                        sender_id=item.get("senderId") or "",
                        sender_nickname=item.get("senderNickname") or "",
                        message_str=text,
                        time=int(item.get("sendTime") or 0) // 1000,
                    )
        return Reply(
            id=message_id, message_str=f"[引用消息原内容暂不可用，消息ID: {message_id}]"
        )

    @staticmethod
    def _extract_key_from_url(url: str) -> str:
        return urlparse(url).path.lstrip("/") if url else ""

    async def _resolve_media(self, key: str, url: str, kind: str, filename: str = ""):
        data = None
        downloaded_name = ""
        key, url = self._normalize_media_source(key, url, kind)
        if not key and urlparse(url).hostname in {
            "chat-img.jwznb.com",
            "chat-file.jwznb.com",
            "chat-video1.jwznb.com",
            "chat-audio1.jwznb.com",
        }:
            key = self._extract_key_from_url(url)
        if key and self._cdn_proxy:
            result = (
                await self._cdn_proxy.download(key, kind, with_metadata=True)
                if kind == "file"
                else await self._cdn_proxy.download(key, kind)
            )
            if isinstance(result, DownloadedMedia):
                data, downloaded_name = result.data, result.filename
            else:
                data = result
        if data is None and url and self._dl_session:
            try:
                async with self._dl_session.get(
                    url, headers={"Referer": "https://myapp.jwznb.com/"}
                ) as response:
                    if response.status == 200:
                        data = await response.read()
                        downloaded_name = header_filename(response.headers)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
        safe_name = received_filename(
            filename, downloaded_name, Path(urlparse(url or key).path).name
        )
        if data is not None:
            suffix = (
                TempFileManager.detect_image_suffix(data)
                if kind == "image"
                else (
                    Path(safe_name if filename else urlparse(url or key).path).suffix
                    or {"video": ".mp4", "audio": ".ogg"}.get(kind, ".bin")
                )
            )
            _, path = self._temp_manager.put(
                data, suffix, filename=safe_name if kind == "file" else ""
            )
            if kind == "file":
                return File(name=safe_name, file=path)
            return {"image": Image, "video": Video, "audio": Record}[
                kind
            ].fromFileSystem(path)
        url = url or CdnProxy.url_for(key, kind)
        if not url:
            if kind == "file":
                return Plain(
                    text=f"[文件: {safe_name}；缺少可用下载地址，暂时无法读取]"
                )
            return None
        if kind == "file":
            # 核心收集 File 时会立即调用 get_file；失败文件不能转交通用下载器再报错。
            logger.warning("[云湖] 文件接收失败，未获得本地附件: %s", safe_name)
            return Plain(
                text=f"[文件: {safe_name}；下载失败，暂时无法读取，请重新发送]"
            )
        return {"image": Image, "video": Video, "audio": Record}[kind](
            file=url, url=url
        )

    @staticmethod
    def _normalize_media_source(key: str, url: str, kind: str) -> tuple[str, str]:
        key, url = str(key or "").strip(), str(url or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url and not urlparse(url).scheme:
            # 云湖部分文件推送只提供 hash.ext，不能把它作为相对URL交给核心。
            if re.match(r"^(?:chat-[^/]+\.jwznb\.com|[^/]+\.yunhucdn\.[^/]+)/", url):
                url = "https://" + url
            else:
                key = key or url.lstrip("/")
                url = CdnProxy.url_for(key, kind)
        if url and urlparse(url).scheme not in ("http", "https"):
            url = ""
        if key.startswith(("https://", "http://")):
            url = url or key
            key = ""
        if not url:
            url = CdnProxy.url_for(key, kind)
        return key, url

    async def _resolve_image(self, image_key: str, image_url: str):
        return await self._resolve_media(image_key, image_url, "image")

    async def _resolve_file(self, file_key: str, file_url: str, file_name: str):
        return await self._resolve_media(file_key, file_url, "file", file_name)

    async def _resolve_video(self, video_key: str, video_url: str):
        return await self._resolve_media(video_key, video_url, "video")

    async def _handle_msg(
        self, message: AstrBotMessage, raw_data: dict, parsed: YunhuEvent = None
    ):
        parsed = parsed or parse_event(raw_data)
        recv_type = "group" if message.group_id else "user"
        event = YunhuMessageEvent(
            message.message_str,
            message,
            self.meta(),
            message.session_id,
            self._client,
            message.group_id or message.sender.user_id,
            recv_type,
            parsed.message.msgId
            if self.reply_in_thread and parsed.event_type in MESSAGE_EVENTS
            else "",
            self._dl_session,
        )
        event.set_extra("yunhu_event_type", parsed.event_type)
        event.set_extra("yunhu_event_data", parsed.event_data)
        event.set_extra("yunhu_sender_level", parsed.sender.senderUserLevel)
        event.set_extra("yunhu_command_id", parsed.message.commandId)
        event.set_extra("yunhu_command_name", parsed.message.commandName)
        event.set_extra("yunhu_file_store", self._file_store)
        event.set_extra("yunhu_temp_manager", self._temp_manager)
        stored, failures = [], []
        for component in message.message:
            if not isinstance(component, File):
                continue
            try:
                record = await asyncio.to_thread(
                    self._file_store.add,
                    component.file_,
                    component.name,
                    recv_type,
                    message.group_id or message.sender.user_id,
                    message.sender.user_id,
                    message.message_id,
                )
                if record:
                    stored.append(record)
            except (OSError, sqlite3.Error, ValueError) as error:
                failures.append(
                    {
                        "file_name": component.name,
                        "reason": str(error)
                        if isinstance(error, ValueError)
                        else type(error).__name__,
                    }
                )
                logger.warning(
                    "[云湖] 附件保留失败: %s (%s)", component.name, type(error).__name__
                )
        event.set_extra("yunhu_stored_files", stored)
        event.set_extra("yunhu_file_store_failures", failures)
        event.set_extra(
            "yunhu_mentions",
            [str(comp.qq) for comp in message.message if isinstance(comp, At)],
        )
        if parsed.event_type in NOTICE_EVENTS:
            event.should_call_llm(False)
        self.commit_event(event)
        self._track_media(message.message, event)

    def _track_media(self, chain, event):
        for component in chain:
            if isinstance(component, Reply):
                self._track_media(component.chain or [], event)
            elif isinstance(component, (Image, Record, Video, File)):
                source = (
                    getattr(component, "file_", "")
                    if isinstance(component, File)
                    else getattr(component, "file", "")
                )
                # fromFileSystem 生成标准 file URI；以登记表定位路径，避免自行解析 URI。
                for path, _ in list(self._temp_manager._file_records.values()):
                    if source in (path, Path(path).as_uri()):
                        self._temp_manager.transfer(path, event)
                        break
