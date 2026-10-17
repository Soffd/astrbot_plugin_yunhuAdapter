"""由Dashboard认证的管理API，不对Webhook端口暴露文件或管理权限。"""

import asyncio
import copy
import re
import shutil
import sqlite3
import threading
import zipfile
from functools import wraps
from xml.etree import ElementTree

from astrbot.api import logger
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.web import error_response, file_response, json_response, request

from ..adapter.event import YunhuMessageEvent
from ..media.attachments import _read_text
from ..services.operations import ACTIONS, GLOBAL_ACTIONS

PLUGIN_NAME = "astrbot_plugin_yunhuAdapter"
EDITABLE = {
    "file_retention_days",
    "file_store_max_mb",
    "file_store_max_count",
    "media_ttl",
    "reply_in_thread",
    "bot_id",
}
WEB_OPERATIONS = ACTIONS - {
    "context",
    "attachments",
    "stored_files",
    "manage_file",
    "read_attachment",
}


def endpoint(handler):
    @wraps(handler)
    async def authenticated(self, *args, **kwargs):
        if not request.username:
            return error_response("请登录AstrBot Dashboard后操作", status_code=401)
        try:
            return await handler(self, *args, **kwargs)
        except LookupError as error:
            return error_response(str(error), status_code=404)
        except (ValueError, TypeError) as error:
            return error_response(str(error), status_code=400)
        except PermissionError as error:
            return error_response(str(error), status_code=403)
        except (OSError, sqlite3.Error) as error:
            logger.warning("[云湖页面] 文件或配置操作失败 (%s)", type(error).__name__)
            return error_response(
                "文件或配置操作失败，请查看AstrBot日志", status_code=500
            )

    return authenticated


class YunhuPageAPI:
    def __init__(self, context, operations):
        self.context = context
        self.operations = operations
        self._settings_lock = asyncio.Lock()

    def register(self):
        for route, methods, handler in (
            ("instances", ["GET"], self.instances),
            ("files", ["GET"], self.files),
            ("files/<file_id>/preview", ["GET"], self.preview),
            ("files/<file_id>/download", ["GET"], self.download),
            ("files/<file_id>/manage", ["POST"], self.manage),
            ("settings", ["GET"], self.settings),
            ("settings/save", ["POST"], self.save_settings),
            ("cleanup", ["POST"], self.cleanup),
            ("operations/run", ["POST"], self.run_operation),
        ):
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, "云湖可视化管理"
            )

    def unregister(self):
        registry = self.context.registered_web_apis
        registry[:] = [
            item for item in registry if getattr(item[1], "__self__", None) is not self
        ]

    def _adapters(self):
        return [
            p
            for p in self.context.platform_manager.get_insts()
            if p.meta().name == "yunhu"
            and hasattr(getattr(p, "_file_store", None), "catalog")
        ]

    def _adapter(self, instance_id=None):
        instance_id = instance_id or request.query.get("instance_id", "")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("请选择云湖平台实例")
        adapter = next(
            (p for p in self._adapters() if p.meta().id == instance_id), None
        )
        if adapter is None:
            raise LookupError("云湖实例尚未载入，请在机器人管理中启用该实例")
        return adapter

    @staticmethod
    async def _payload():
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须为JSON对象")
        return payload

    @staticmethod
    def _integer(value, name, minimum=0, maximum=2**31 - 1):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{name}必须为整数")
        try:
            result = int(value)
        except ValueError:
            raise ValueError(f"{name}必须为整数") from None
        if not minimum <= result <= maximum:
            raise ValueError(f"{name}超出允许范围")
        return result

    @staticmethod
    def _redact(adapter, value):
        if isinstance(value, str):
            return adapter.get_client()._safe_error(RuntimeError(value))
        if isinstance(value, dict):
            return {
                key: YunhuPageAPI._redact(adapter, item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [YunhuPageAPI._redact(adapter, item) for item in value]
        return value

    @endpoint
    async def instances(self):
        instances = []
        for adapter in self._adapters():
            connected = (
                adapter._ws_connection is not None and not adapter._ws_connection.closed
                if adapter.connection_mode == "websocket"
                else adapter._webhook_runner is not None
            )
            instances.append(
                {
                    "id": adapter.meta().id,
                    "bot_id": adapter.client_self_id,
                    "mode": adapter.connection_mode,
                    "connected": connected,
                    "loaded": not adapter._stop_event.is_set(),
                }
            )
        return json_response(
            {"instances": instances, "version": "2.5.0"},
            headers={"Cache-Control": "no-store"},
        )

    @endpoint
    async def files(self):
        adapter = self._adapter()
        result = await asyncio.to_thread(
            adapter._file_store.catalog,
            search=str(request.query.get("search", ""))[:200],
            chat_type=request.query.get("chat_type", ""),
            chat_id=str(request.query.get("chat_id", ""))[:128],
            status=request.query.get("status", "all"),
            limit=self._integer(request.query.get("limit", "20"), "每页数量", 1, 100),
            offset=self._integer(request.query.get("offset", "0"), "分页位置"),
        )
        return json_response(result, headers={"Cache-Control": "no-store"})

    @endpoint
    async def preview(self, file_id):
        adapter = self._adapter()
        record = await asyncio.to_thread(adapter._file_store.record, file_id)

        def read():
            with adapter._file_store.open_file(
                record["chat_type"], record["chat_id"], file_id
            ) as (_, path):
                try:
                    return _read_text(path, record["file_name"], 32000)
                except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
                    raise ValueError(
                        "文件格式无效，无法预览；可下载原文件查看"
                    ) from None

        result = await asyncio.to_thread(read)
        return json_response(result, headers={"Cache-Control": "no-store"})

    @endpoint
    async def download(self, file_id):
        adapter = self._adapter()
        record = await asyncio.to_thread(adapter._file_store.record, file_id)
        _, snapshot = adapter._temp_manager.put(b"", ".bin")
        abandoned, finished = threading.Event(), threading.Event()

        def copy_file():
            try:
                with adapter._file_store.open_file(
                    record["chat_type"], record["chat_id"], file_id
                ) as (_, path):
                    shutil.copyfile(path, snapshot)
            finally:
                finished.set()
                if abandoned.is_set():
                    adapter._temp_manager.discard(snapshot)

        try:
            await asyncio.to_thread(copy_file)
            response = file_response(
                snapshot,
                filename=record["file_name"],
                content_type="application/octet-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except BaseException:
            abandoned.set()
            if finished.is_set():
                adapter._temp_manager.discard(snapshot)
            raise

        async def cleanup_snapshot():
            adapter._temp_manager.discard(snapshot)

        response.background = cleanup_snapshot
        return response

    @endpoint
    async def manage(self, file_id):
        payload = await self._payload()
        adapter = self._adapter(payload.get("instance_id"))
        record = await asyncio.to_thread(adapter._file_store.record, file_id)
        result = await asyncio.to_thread(
            adapter._file_store.manage,
            record["chat_type"],
            record["chat_id"],
            file_id,
            payload.get("action"),
            f"dashboard:{request.username}",
            True,
        )
        return json_response(result)

    @staticmethod
    def _settings(adapter):
        return {
            "instance_id": adapter.meta().id,
            "bot_id": adapter.bot_id,
            "file_retention_days": adapter._file_store.retention_days,
            "file_store_max_mb": adapter._file_store.max_bytes // (1024 * 1024),
            "file_store_max_count": adapter._file_store.max_files,
            "media_ttl": adapter.media_ttl,
            "reply_in_thread": adapter.reply_in_thread,
            "storage_path": str(adapter._file_store.base_dir),
            "mode": adapter.connection_mode,
        }

    @endpoint
    async def settings(self):
        return json_response(
            self._settings(self._adapter()), headers={"Cache-Control": "no-store"}
        )

    @endpoint
    async def save_settings(self):
        payload = await self._payload()
        adapter = self._adapter(payload.get("instance_id"))
        values = payload.get("settings")
        if not isinstance(values, dict) or not values or set(values) - EDITABLE:
            raise ValueError("保存参数无效，请仅提交页面支持的设置")
        clean = {}
        for key, value in values.items():
            if key == "reply_in_thread":
                if not isinstance(value, bool):
                    raise ValueError("线程回复必须为布尔值")
            elif key == "bot_id":
                if not isinstance(value, str) or (
                    value and not re.fullmatch(r"[0-9]{1,32}", value)
                ):
                    raise ValueError("机器人ID应为空或云湖真实数字ID")
            else:
                value = self._integer(
                    value, key, 0 if key == "file_retention_days" else 1
                )
            clean[key] = value
        async with self._settings_lock:
            config = self.context.get_config()
            target = next(
                (
                    p
                    for p in config.get("platform", [])
                    if p.get("id") == adapter.meta().id and p.get("type") == "yunhu"
                ),
                None,
            )
            if target is None:
                raise LookupError(
                    "无法找到实例的持久化配置，请在机器人管理中检查该实例"
                )
            original = copy.deepcopy(target)
            target.update(clean)
            try:
                await asyncio.to_thread(config.save_config)
            except BaseException:
                target.clear()
                target.update(original)
                raise
            current = self._settings(adapter)
            current.update(clean)
            await asyncio.to_thread(
                adapter._file_store.configure,
                current["file_retention_days"],
                current["file_store_max_mb"] * 1024 * 1024,
                current["file_store_max_count"],
            )
            adapter.config.update(clean)
            adapter.reply_in_thread = current["reply_in_thread"]
            adapter.media_ttl = current["media_ttl"]
            adapter._temp_manager.ttl = current["media_ttl"]
            if "bot_id" in clean:
                adapter.bot_id = adapter.client_self_id = clean["bot_id"]
                adapter._identity_warning_sent = False
        return json_response({"saved": True, "settings": self._settings(adapter)})

    @endpoint
    async def cleanup(self):
        payload = await self._payload()
        adapter = self._adapter(payload.get("instance_id"))
        usage = await asyncio.to_thread(adapter._file_store.cleanup)
        return json_response({"usage": usage, "message": "已按当前策略执行清理"})

    @endpoint
    async def run_operation(self):
        payload = await self._payload()
        adapter = self._adapter(payload.get("instance_id"))
        operation, arguments = payload.get("operation"), payload.get("arguments", {})
        chat_id, chat_type = payload.get("chat_id"), payload.get("chat_type")
        if operation not in WEB_OPERATIONS or not isinstance(arguments, dict):
            raise ValueError("请选择支持的功能并检查参数格式")
        if operation in GLOBAL_ACTIONS:
            chat_type, chat_id = "user", adapter.client_self_id or "dashboard"
        if (
            chat_type not in ("group", "user")
            or not isinstance(chat_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id)
        ):
            raise ValueError("请填写实际群/用户ID并选择会话类型")
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE
            if chat_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        message.sender = MessageMember(
            user_id=f"dashboard:{request.username}", nickname="Dashboard管理员"
        )
        message.self_id, message.message_id = adapter.client_self_id, ""
        message.group_id = chat_id if chat_type == "group" else ""
        message.session_id, message.raw_message = chat_id, {"source": "dashboard"}
        message.message, message.message_str = [], ""
        event = YunhuMessageEvent(
            "",
            message,
            adapter.meta(),
            chat_id,
            adapter.get_client(),
            chat_id,
            chat_type,
        )
        event.role = "admin"  # 此端点仅接受已认证Dashboard会话，不用于聊天事件。
        try:
            result = await self.operations.execute(event, operation, **arguments)
        finally:
            event.cleanup_temporary_local_files()
        return json_response(self._redact(adapter, result))
