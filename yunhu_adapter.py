"""
云湖平台适配器（AstrBot 插件）

支持 Webhook 和 WebSocket 两种连接模式，通过 YunhuClient HTTP API 发送消息。
与 AstrBot 共用同一个服务进程，脱离外部云湖 WebSDK。

架构说明
────────
1. Webhook 模式：在本地启动 HTTP 服务，接收云湖平台推送的事件
2. WebSocket 模式：主动连接云湖 WebSocket 服务，接收实时事件推送
3. YunhuClient：直接调用云湖 HTTP API 发送消息、上传文件等
4. TempFileManager：将媒体文件保存到本地 temp 目录，供 AstrBot 其他插件/agent 访问
5. CdnProxy：内置 CDN 反向代理，绕过防盗链下载资源

图片/文件/视频处理策略
────────────────────
由于云湖 CDN 启用了防盗链机制，直接请求会被拒绝。
本适配器采用多级回退下载策略：
  1. 内置反代：直接请求 CDN，设置正确的 Host 和 Referer 头
  2. 自定义反代：用户在配置中指定的第三方反代地址（如自建 nginx 反代）
  3. 备用反代：chat-webp.000434.xyz（仅支持图片，稳定性不佳）
下载成功后，媒体保存到本地 temp 目录，以文件路径形式写入消息链，
所有插件和 agent 都能像处理普通本地文件一样处理云湖媒体。
temp 目录中的文件会定期自动清理（默认 10 分钟）。
"""
import asyncio
import json
import os
import time
import uuid
import random
import aiohttp
from aiohttp import web
from urllib.parse import urlparse
from astrbot.api.platform import (
    Platform, AstrBotMessage, MessageMember,
    PlatformMetadata, MessageType, register_platform_adapter,
)
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, File, Video
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot import logger

from .yunhu_event import YunhuMessageEvent
from .client import YunhuClient
from .cdn_proxy import CdnProxy


# 临时文件管理器 

class TempFileManager:
    """
    临时文件磁盘管理器
    将从 CDN 下载的图片/文件/视频保存到本地 temp 目录中，
    以文件路径形式提供给 AstrBot 其他插件和 agent 使用。
    文件在 TTL 过期后自动清理。
    """

    def __init__(self, base_dir: str = None, ttl: int = 600):
        if base_dir is None:
            base_dir = os.path.join(os.getcwd(), "temp", "yunhu_media")
        self.base_dir = base_dir
        self.ttl = ttl
        self._file_records: dict[str, tuple[str, float]] = {}  # token → (file_path, timestamp)
        os.makedirs(self.base_dir, exist_ok=True)

    def put(self, data: bytes, suffix: str = ".bin", content_type: str = "application/octet-stream") -> tuple[str, str]:
        """
        保存媒体数据到磁盘，返回 (token, file_path)

        Args:
            data: 文件二进制数据
            suffix: 文件后缀名，如 .png, .mp4, .bin
            content_type: MIME 类型（记录用）

        Returns:
            (token, file_path): token 用于内部管理，file_path 是磁盘上的绝对路径
        """
        token = uuid.uuid4().hex[:12]
        filename = f"{token}{suffix}"
        file_path = os.path.join(self.base_dir, filename)

        with open(file_path, "wb") as f:
            f.write(data)

        self._file_records[token] = (file_path, time.time())
        logger.debug(f"[云湖] 媒体已保存到磁盘: {file_path}")

        # 触发清理
        self._cleanup()

        return token, file_path

    def _cleanup(self):
        """清理过期文件"""
        now = time.time()
        expired_tokens = [
            token for token, (_, ts) in self._file_records.items()
            if now - ts > self.ttl
        ]
        for token in expired_tokens:
            file_path, _ = self._file_records.pop(token)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"[云湖] 已清理过期媒体文件: {file_path}")
            except Exception as e:
                logger.warning(f"[云湖] 清理过期文件失败: {file_path} - {e}")

    def cleanup_all(self):
        """清理所有文件"""
        for token, (file_path, _) in list(self._file_records.items()):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
        self._file_records.clear()
        logger.info("[云湖] 已清理所有临时媒体文件")

    async def start_periodic_cleanup(self, interval: int = 300):
        """
        启动定期清理任务

        Args:
            interval: 清理间隔（秒），默认 300 秒（5 分钟）
        """
        while True:
            await asyncio.sleep(interval)
            try:
                self._cleanup()
            except Exception as e:
                logger.error(f"[云湖] 定期清理任务异常: {e}")

    @staticmethod
    def detect_image_suffix(data: bytes) -> str:
        """根据文件头魔数检测图片后缀"""
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        elif data[:4] == b"RIFF":
            return ".webp"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        return ".png"


# 适配器 

@register_platform_adapter(
    "yunhu",
    "云湖",
    default_config_tmpl={
        "bot_token": "",
        "connection_mode": "websocket",
        "webhook_host": "0.0.0.0",
        "webhook_port": 6195,
        "webhook_path": "/webhook",
        "websocket_url": "wss://ws.jwzhd.com/subscribe",
        "reply_in_thread": False,
        "media_ttl": 600,
        "custom_cdn_proxy": "https://yhcdn.yunhucdn.top",
    },
    config_metadata={
        "bot_token": {
            "description": "云湖机器人 Bot Token",
            "type": "string",
            "hint": "从云湖官网控制台获取的机器人 Token，用于调用 API 和下载媒体资源。必填。",
            "obvious_hint": True,
        },
        "connection_mode": {
            "description": "连接模式（webhook / websocket）",
            "type": "string",
            "hint": "与云湖平台的连接方式。websocket：主动连接云湖 WebSocket 服务，无需公网 IP，推荐使用；webhook：在本地启动 HTTP 服务接收推送，需要公网 IP 或内网穿透。",
            "obvious_hint": True,
        },
        "webhook_host": {
            "description": "Webhook 监听地址",
            "type": "string",
            "hint": "云湖平台推送事件的 HTTP 监听地址，一般填 0.0.0.0 即可。需确保云湖平台能访问到此地址。仅在 webhook 模式下生效。",
        },
        "webhook_port": {
            "description": "Webhook 监听端口",
            "type": "int",
            "hint": "云湖平台推送事件的 HTTP 监听端口。需确保端口未被占用且云湖平台能访问到此端口。仅在 webhook 模式下生效。",
        },
        "webhook_path": {
            "description": "Webhook 路径",
            "type": "string",
            "hint": "云湖平台推送事件的 URL 路径，例如 /webhook。需与云湖官网控制台配置的回调地址一致。仅在 webhook 模式下生效。",
        },
        "websocket_url": {
            "description": "WebSocket 服务地址",
            "type": "string",
            "hint": "云湖 WebSocket 订阅地址，默认 wss://ws.jwzhd.com/subscribe。仅在 websocket 模式下生效。",
        },
        "reply_in_thread": {
            "description": "以线程方式回复消息",
            "type": "bool",
            "hint": "开启后回复会带上 parentId，将在原消息下方显示为子消息（线程）",
        },
        "media_ttl": {
            "description": "临时媒体缓存时间（秒）",
            "type": "int",
            "hint": "从云湖 CDN 下载的图片/文件/视频在本地缓存的时间，过期后自动清理。默认 600 秒（10 分钟）。",
        },
        "custom_cdn_proxy": {
            "description": "自定义 CDN 反代地址",
            "type": "string",
            "hint": "自建 CDN 反向代理的基础地址。留空则不使用自定义反代，仅依赖内置反代和备用反代。",
            "obvious_hint": False,
        },
    },
)
class YunhuAdapter(Platform):
    """
    云湖平台适配器

    支持 Webhook 和 WebSocket 两种连接模式：
    - Webhook 模式：在本地启动 HTTP 服务，接收云湖平台推送的事件
    - WebSocket 模式：主动连接云湖 WebSocket 服务，接收实时事件推送（推荐，性能开销小且无需公网 IP）
    """

    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue):
        super().__init__(platform_config, event_queue)

        # 配置项
        self.bot_token = platform_config.get("bot_token", "")
        self.connection_mode = platform_config.get("connection_mode", "websocket").lower().strip()
        self.webhook_host = platform_config.get("webhook_host", "0.0.0.0")
        self.webhook_port = int(platform_config.get("webhook_port", 6195))
        self.webhook_path = platform_config.get("webhook_path", "/webhook")
        self.websocket_url = platform_config.get("websocket_url", "wss://ws.jwzhd.com/subscribe")
        self.reply_in_thread = platform_config.get("reply_in_thread", False)
        self.media_ttl = int(platform_config.get("media_ttl", 600))
        self.custom_cdn_proxy = platform_config.get("custom_cdn_proxy", "")

        # YunhuClient
        self._client = YunhuClient(self.bot_token)

        # 临时文件管理器
        self._temp_manager = TempFileManager(ttl=self.media_ttl)

        # CDN 下载器
        self._cdn_proxy: CdnProxy | None = None

        # HTTP 下载会话
        self._dl_session: aiohttp.ClientSession | None = None

        # Webhook 服务器
        self._webhook_app: web.Application | None = None
        self._webhook_runner: web.AppRunner | None = None
        self._webhook_verified = False

        # WebSocket 相关
        self._ws_session: aiohttp.ClientSession | None = None
        self._ws_connection: aiohttp.ClientWebSocketResponse | None = None
        self._ws_reconnect_task: asyncio.Task | None = None
        self._ws_listen_task: asyncio.Task | None = None
        self._ws_running = False

        # 定期清理任务
        self._cleanup_task: asyncio.Task | None = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            id="yunhu",
            name="yunhu_webhook" if self.connection_mode == "webhook" else "yunhu_websocket",
            description=f"云湖平台适配器（{'Webhook' if self.connection_mode == 'webhook' else 'WebSocket'} 模式）",
        )

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        """
        通过会话信息主动发送消息链到云湖平台。

        此方法覆写 Platform 基类的空实现，用于支持 AstrBot 内置 Agent
        工具（如 send_message_to_user）主动发送消息的场景。

        当 Agent 使用 send_message_to_user 工具发送文件、图片等消息时，
        AstrBot 核心会调用此方法将消息路由到对应的平台适配器。

        Args:
            session: 消息会话信息，包含平台标识、消息类型和会话 ID
            message_chain: 待发送的消息链
        """
        # 只处理本平台的会话
        if session.platform_id != self.meta().id:
            return

        # 从 MessageSesion 解析接收者信息
        recv_id = session.session_id
        if session.message_type == MessageType.GROUP_MESSAGE:
            recv_type = "group"
        else:
            recv_type = "user"

        logger.debug(
            f"[云湖] send_by_session: recv_id={recv_id}, recv_type={recv_type}, "
            f"chain_len={len(message_chain.chain)}"
        )

        try:
            await YunhuMessageEvent.send_message(
                client=self._client,
                recv_id=recv_id,
                recv_type=recv_type,
                message_chain=message_chain,
                dl_session=self._dl_session,
            )
        except Exception as e:
            logger.error(f"[云湖] send_by_session 发送失败: {e}")

        # 调用基类方法上传 metrics
        await super().send_by_session(session, message_chain)

    # 生命周期 

    async def run(self):
        """启动适配器"""
        # 创建 HTTP 下载会话
        self._dl_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"Referer": "https://chat-go.jwzhd.com/"},
        )

        # 初始化 CDN 下载器
        self._cdn_proxy = CdnProxy(
            dl_session=self._dl_session,
            custom_proxy_base=self.custom_cdn_proxy,
        )

        # 启动定期清理任务
        self._cleanup_task = asyncio.create_task(
            self._temp_manager.start_periodic_cleanup(interval=max(60, self.media_ttl // 2))
        )

        # 根据连接模式启动
        if self.connection_mode == "websocket":
            await self._start_websocket()
        else:
            await self._start_webhook()

    async def terminate(self):
        """停止适配器"""
        # 停止 WebSocket
        await self._stop_websocket()

        # 停止 Webhook
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
            self._webhook_runner = None

        # 停止定期清理
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # 关闭 HTTP 下载会话
        if self._dl_session and not self._dl_session.closed:
            await self._dl_session.close()
            self._dl_session = None

        # 清理所有临时文件
        self._temp_manager.cleanup_all()

    # Webhook 模式 

    async def _start_webhook(self):
        """启动 Webhook 服务器"""
        self._webhook_app = web.Application()
        self._webhook_app.router.add_post(self.webhook_path, self._handle_webhook)
        self._webhook_runner = web.AppRunner(self._webhook_app)
        await self._webhook_runner.setup()
        site = web.TCPSite(self._webhook_runner, self.webhook_host, self.webhook_port)
        await site.start()
        logger.info(
            f"[云湖] Webhook 服务已启动: "
            f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
        )

    async def _handle_webhook(self, request: web.Request):
        """处理云湖平台推送的 Webhook 事件"""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"code": -1, "msg": "invalid json"})

        # 首次收到云湖推送，记录验证成功
        if not self._webhook_verified:
            self._webhook_verified = True
            logger.info("[云湖] Webhook 验证成功，已收到云湖平台推送")

        # 检查顶层 type 字段
        event_type = data.get("type", "")
        if event_type == "message":
            asyncio.create_task(self._process_message(data))
            return web.json_response({"code": 1, "msg": "ok"})
        elif event_type == "verify":
            logger.info("[云湖] 收到验证事件，已确认")
            return web.json_response({"code": 1, "msg": "ok"})

        # 检查 header.eventType
        header = data.get("header", {})
        header_event_type = header.get("eventType", "")
        if header_event_type:
            if header_event_type in ("message.receive.normal", "message.receive.instruction"):
                asyncio.create_task(self._process_message(data))
            elif header_event_type == "button.report.inline":
                logger.debug("[云湖] 收到按钮点击事件")
            elif header_event_type in ("group.join", "group.leave"):
                logger.debug(f"[云湖] 收到群成员事件: {header_event_type}")
            elif header_event_type in ("bot.followed", "bot.unfollowed"):
                logger.debug(f"[云湖] 收到机器人关注事件: {header_event_type}")
            else:
                logger.debug(f"[云湖] 忽略事件类型: {header_event_type}")
        else:
            logger.debug(f"[云湖] 无法识别的事件格式: {str(data)[:200]}")

        return web.json_response({"code": 1, "msg": "ok"})


    # WebSocket 模式 

    async def _start_websocket(self):
        """启动 WebSocket 连接"""
        self._ws_running = True
        self._ws_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        # 启动 WebSocket 监听任务
        self._ws_listen_task = asyncio.create_task(self._ws_listen_loop())
        logger.info("[云湖] WebSocket 模式已启动")

    async def _stop_websocket(self):
        """停止 WebSocket 连接"""
        self._ws_running = False

        # 关闭 WebSocket 连接
        if self._ws_connection and not self._ws_connection.closed:
            await self._ws_connection.close()
            self._ws_connection = None

        # 取消监听任务
        if self._ws_listen_task and not self._ws_listen_task.done():
            self._ws_listen_task.cancel()
            try:
                await self._ws_listen_task
            except asyncio.CancelledError:
                pass
            self._ws_listen_task = None

        # 关闭 WebSocket 会话
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
            self._ws_session = None

    async def _ws_listen_loop(self):
        """
        WebSocket 监听主循环，包含自动重连逻辑。

        重连策略：
        - 首次重连等待 2 秒
        - 之后每次重连等待时间翻倍，最大 60 秒
        - 连接成功后重置等待时间
        """
        reconnect_delay = 2
        max_delay = 60

        while self._ws_running:
            try:
                ws_url = f"{self.websocket_url}?token={self.bot_token}"
                logger.info(f"[云湖] 正在连接 WebSocket: {self.websocket_url}")

                async with self._ws_session.ws_connect(
                    ws_url,
                    heartbeat=30,  # 每 30 秒发送心跳
                ) as ws:
                    self._ws_connection = ws
                    # 连接成功，重置重连延迟
                    reconnect_delay = 2
                    logger.info("[云湖] WebSocket 已连接")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_ws_message(data)
                            except json.JSONDecodeError:
                                logger.warning(f"[云湖] WebSocket 收到非 JSON 消息: {msg.data[:100]}")
                            except Exception as e:
                                logger.error(f"[云湖] WebSocket 消息处理异常: {e}")

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"[云湖] WebSocket 错误: {ws.exception()}")
                            break

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                            logger.info("[云湖] WebSocket 连接关闭")
                            break

                    self._ws_connection = None

            except asyncio.CancelledError:
                logger.info("[云湖] WebSocket 监听任务被取消")
                break
            except Exception as e:
                logger.error(f"[云湖] WebSocket 连接异常: {e}")

            self._ws_connection = None

            if self._ws_running:
                logger.info(f"[云湖] WebSocket 将在 {reconnect_delay} 秒后重连...")
                try:
                    await asyncio.sleep(reconnect_delay)
                except asyncio.CancelledError:
                    break
                reconnect_delay = min(reconnect_delay * 2, max_delay)

    async def _handle_ws_message(self, data: dict):
        """
        处理 WebSocket 收到的消息
        """
        event_type = data.get("type", "")

        # 如果有 type 字段
        if event_type == "message":
            asyncio.create_task(self._process_message(data))
            return

        # 标准 WebSocket 事件格式
        header = data.get("header", {})
        ws_event_type = header.get("eventType", "")

        if ws_event_type:
            # 有 header 的事件格式，直接作为消息处理
            asyncio.create_task(self._process_message(data))
        else:
            logger.debug(f"[云湖] WebSocket 忽略未知消息: {str(data)[:200]}")

    # 消息处理 

    async def _process_message(self, raw_data: dict):
        """处理消息事件"""
        try:
            abm = await self._convert_to_abm(raw_data)
            if abm:
                await self._handle_msg(abm, raw_data)
        except Exception as e:
            logger.error(f"[云湖] 处理消息异常: {e}")

    # 消息转换 

    async def _convert_to_abm(self, raw_data: dict) -> AstrBotMessage | None:
        """将云湖事件数据转换为 AstrBotMessage"""
        from .models import parse_event

        event = parse_event(raw_data)
        if not event:
            return None

        sender = event.sender
        chat = event.chat
        msg = event.message

        abm = AstrBotMessage()
        abm.self_id = self.bot_token
        abm.sender = MessageMember(
            user_id=sender.senderId,
            nickname=sender.senderNickname or sender.senderId,
        )
        abm.message_id = msg.msgId
        abm.timestamp = msg.sendTime
        abm.raw_message = raw_data

        # 判断消息类型
        chat_type = chat.chatType if chat else "bot"
        if chat_type == "group":
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = chat.chatId
            abm.session_id = chat.chatId
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = sender.senderId

        # 解析消息内容
        abm.message = []
        content_type = msg.contentType

        if content_type == "text":
            text = msg.text
            if text:
                abm.message.append(Plain(text=text))
                abm.message_str = text

        elif content_type == "image":
            img_comp = await self._resolve_image(msg.imageKey, msg.imageUrl)
            if img_comp:
                abm.message.append(img_comp)
            # 图片消息可能同时包含文本
            if msg.text:
                abm.message.append(Plain(text=msg.text))
            abm.message_str = msg.text or "[图片]"

        elif content_type == "file":
            file_comp = await self._resolve_file(msg.fileKey, msg.fileUrl, msg.fileName)
            if file_comp:
                abm.message.append(file_comp)
            abm.message_str = f"[文件: {msg.fileName or msg.fileKey}]"

        elif content_type == "video":
            video_comp = await self._resolve_video(msg.videoKey, msg.videoUrl)
            if video_comp:
                abm.message.append(video_comp)
            abm.message_str = "[视频]"

        elif content_type == "markdown":
            text = msg.text
            if text:
                abm.message.append(Plain(text=text))
                abm.message_str = text

        else:
            # 未知类型，尝试提取文本
            text = msg.text
            if text:
                abm.message.append(Plain(text=text))
                abm.message_str = text
            else:
                abm.message_str = f"[{content_type}]"

        if not abm.message:
            return None

        return abm

    # CDN 媒体解析 

    @staticmethod
    def _extract_key_from_url(url: str) -> str:
        """从完整 URL 中提取文件 key（取路径最后一段）"""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            return os.path.basename(parsed.path) or ""
        except Exception:
            return ""

    async def _resolve_image(self, image_key: str, image_url: str) -> Image | None:
        """
        解析图片消息：通过多级回退策略下载图片并保存到本地 temp 目录。

        下载优先级：内置反代 → 自定义反代 → 备用反代(chat-webp.000434.xyz)
        下载成功后保存到磁盘，以文件路径形式提供给 AstrBot。
        """
        # 提取 key：优先使用 imageKey，否则从 imageUrl 中提取
        key = image_key or self._extract_key_from_url(image_url)
        if key:
            logger.debug(f"[云湖] 图片下载 key={key[:40]}")

        # 通过 CdnProxy 多级回退下载
        if key and self._cdn_proxy:
            data = await self._cdn_proxy.download(key, "image")
            if data:
                suffix = self._temp_manager.detect_image_suffix(data)
                token, file_path = self._temp_manager.put(data, suffix=suffix)
                logger.info(f"[云湖] 图片已保存到本地: {file_path}")
                return Image(file=file_path)

        # 全部下载方式均失败，回退到 imageUrl
        fallback_url = image_url
        if fallback_url:
            logger.warning(f"[云湖] 图片下载失败，回退 URL: {fallback_url[:80]}")
            return Image(file=fallback_url)

        logger.error("[云湖] 图片下载失败且无回退 URL")
        return None

    async def _resolve_file(self, file_key: str, file_url: str, file_name: str) -> File | None:
        """
        解析文件消息：通过多级回退策略下载文件并保存到本地 temp 目录。

        下载优先级：内置反代 → 自定义反代（文件不支持备用反代）
        下载成功后保存到磁盘，以文件路径形式提供给 AstrBot。
        """
        # 提取 key
        key = file_key or self._extract_key_from_url(file_url)
        if key:
            logger.debug(f"[云湖] 文件下载 key={key[:40]}")

        # 通过 CdnProxy 多级回退下载
        if key and self._cdn_proxy:
            data = await self._cdn_proxy.download(key, "file")
            if data:
                # 尝试从文件名获取后缀
                suffix = ".bin"
                if file_name:
                    _, ext = os.path.splitext(file_name)
                    if ext:
                        suffix = ext
                token, file_path = self._temp_manager.put(data, suffix=suffix)
                # 如果有文件名，重命名文件以保留原始文件名
                if file_name:
                    final_path = os.path.join(self._temp_manager.base_dir, f"{token}_{file_name}")
                    try:
                        os.rename(file_path, final_path)
                        file_path = final_path
                    except Exception:
                        pass
                logger.info(f"[云湖] 文件已保存到本地: {file_path} ({file_name})")
                return File(file=file_path, name=file_name or "file")

        # 全部下载方式均失败，回退到 fileUrl
        fallback_url = file_url
        if fallback_url:
            logger.warning(f"[云湖] 文件下载失败，回退 URL: {fallback_url[:80]}")
            return File(file=fallback_url, name=file_name or "file")

        logger.error("[云湖] 文件下载失败且无回退 URL")
        return None

    async def _resolve_video(self, video_key: str, video_url: str) -> Video | None:
        """
        解析视频消息：通过多级回退策略下载视频并保存到本地 temp 目录。

        下载优先级：内置反代 → 自定义反代（视频不支持备用反代）
        下载成功后保存到磁盘，以文件路径形式提供给 AstrBot。
        """
        # 提取 key
        key = video_key or self._extract_key_from_url(video_url)
        if key:
            logger.debug(f"[云湖] 视频下载 key={key[:40]}")

        # 通过 CdnProxy 多级回退下载
        if key and self._cdn_proxy:
            data = await self._cdn_proxy.download(key, "video")
            if data:
                token, file_path = self._temp_manager.put(data, suffix=".mp4")
                logger.info(f"[云湖] 视频已保存到本地: {file_path}")
                return Video(file=file_path)

        # 全部下载方式均失败，回退到 videoUrl
        fallback_url = video_url
        if fallback_url:
            logger.warning(f"[云湖] 视频下载失败，回退 URL: {fallback_url[:80]}")
            return Video(file=fallback_url)

        logger.error("[云湖] 视频下载失败且无回退 URL")
        return None

    # 提交事件给 AstrBot 

    async def _handle_msg(self, abm: AstrBotMessage, raw_data: dict):
        """将 AstrBotMessage 包装为 YunhuMessageEvent 并提交给 AstrBot"""
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

        parent_id = msg_data.get("msgId", "") if self.reply_in_thread else ""

        event = YunhuMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            client=self._client,
            recv_id=recv_id,
            recv_type=recv_type,
            parent_id=parent_id,
            dl_session=self._dl_session,
        )
        self.commit_event(event)
