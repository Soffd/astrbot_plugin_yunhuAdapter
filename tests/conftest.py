"""单元测试用 AstrBot 边界替身；HTTP 集成测试使用真实 aiohttp 客户端和服务器。"""

import importlib
import logging
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("yunhu_plugin")
package.__path__ = [str(ROOT)]
sys.modules["yunhu_plugin"] = package


def module(name):
    result = types.ModuleType(name)
    result.__path__ = []
    sys.modules[name] = result
    return result


for name in ("astrbot", "astrbot.api", "astrbot.core", "astrbot.core.platform"):
    module(name)
sys.modules["astrbot"].logger = logging.getLogger("astrbot.test")
platform_api = module("astrbot.api.platform")
event_api = module("astrbot.api.event")
components = module("astrbot.api.message_components")
base_event_module = module("astrbot.core.platform.astr_message_event")
module("astrbot.core.utils")
media_utils = module("astrbot.core.utils.media_utils")
media_utils.is_file_uri = lambda reference: reference.startswith("file:")
media_utils.file_uri_to_path = lambda reference: url2pathname(urlparse(reference).path)
filter_api = module("astrbot.api.event.filter")
event_api.filter = filter_api


def handler_decorator(attribute, value):
    def decorate(handler):
        setattr(handler, attribute, value)
        return handler

    return decorate


filter_api.command = lambda name, **kwargs: handler_decorator("command_name", name)
filter_api.llm_tool = lambda name: handler_decorator("tool_name", name)
filter_api.on_llm_request = lambda: handler_decorator("event_hook", "llm_request")
module("astrbot.core.agent")
agent_message = module("astrbot.core.agent.message")


@dataclass
class TextPart:
    text: str


agent_message.TextPart = TextPart
module("astrbot.core.star")
module("astrbot.core.star.filter")
command_module = module("astrbot.core.star.filter.command")


class GreedyStr(str):
    pass


command_module.GreedyStr = GreedyStr
star_api = module("astrbot.api.star")


class Star:
    def __init__(self, context):
        self.context = context


star_api.Star = Star
star_api.Context = type("Context", (), {})
star_api.register = lambda *args, **kwargs: lambda cls: cls


class MessageType(Enum):
    FRIEND_MESSAGE = "FriendMessage"
    GROUP_MESSAGE = "GroupMessage"
    OTHER_MESSAGE = "OtherMessage"


@dataclass
class PlatformMetadata:
    name: str
    description: str
    id: str
    support_streaming_message: bool = True
    support_proactive_message: bool = True


@dataclass
class MessageMember:
    user_id: str
    nickname: str = ""


@dataclass
class MessageSesion:
    platform_name: str
    message_type: MessageType
    session_id: str

    @property
    def platform_id(self):
        return self.platform_name


class AstrBotMessage:
    def __init__(self):
        self.group_id = ""


class Platform:
    def __init__(self, config, event_queue):
        self.config = config
        self._event_queue = event_queue

    def commit_event(self, event):
        self._event_queue.put_nowait(event)

    async def send_by_session(self, session, chain):
        self.metrics_called = True


class AstrMessageEvent:
    def __init__(self, message_str, message_obj, platform_meta, session_id):
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session_id = session_id
        self._temporary_local_files = []
        self._extras = {}
        self._has_send_oper = False
        self.call_llm = False
        self.role = "member"

    def get_messages(self):
        """AstrBot 的同步接口：获取当前事件的消息链。"""
        return getattr(self.message_obj, "message", [])

    def get_platform_id(self):
        return self.platform_meta.id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def is_admin(self):
        return self.role == "admin"

    def plain_result(self, text):
        return MessageChain([Plain(text)])

    def stop_event(self):
        self.stopped = True

    def track_temporary_local_file(self, path):
        if path not in self._temporary_local_files:
            self._temporary_local_files.append(path)

    def cleanup_temporary_local_files(self):
        for path in self._temporary_local_files:
            Path(path).unlink(missing_ok=True)
        self._temporary_local_files.clear()

    def set_extra(self, name, value):
        self._extras[name] = value

    def get_extra(self, name):
        return self._extras.get(name)

    def should_call_llm(self, value):
        self.call_llm = value

    async def send(self, message):
        self._has_send_oper = True

    async def send_streaming(self, generator, use_fallback=False):
        self._has_send_oper = True


@dataclass
class Plain:
    text: str


@dataclass
class At:
    qq: str
    name: str = ""


class AtAll(At):
    def __init__(self):
        super().__init__("all")


@dataclass
class Reply:
    id: str
    chain: list = field(default_factory=list)
    sender_id: str = ""
    sender_nickname: str = ""
    message_str: str = ""
    time: int = 0


class Media:
    def __init__(self, file="", url=""):
        self.file = file
        self.url = url

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=Path(path).resolve().as_uri())

    async def convert_to_file_path(self):
        raise NotImplementedError("在测试中 mock AstrBot 的媒体解析边界")


class Image(Media):
    pass


class Record(Media):
    pass


class Video(Media):
    pass


class File:
    def __init__(self, name, file="", url=""):
        self.name, self.file_, self.url = name, file, url

    @property
    def file(self):
        raise AssertionError("异步代码必须使用 get_file，不应访问 File.file")

    async def get_file(self):
        raise NotImplementedError("在测试中 mock AstrBot 的文件解析边界")


@dataclass
class MessageChain:
    chain: list = field(default_factory=list)
    use_markdown_: bool = None
    type: str = None


for obj in (Platform, AstrBotMessage, MessageMember, MessageType, PlatformMetadata):
    setattr(platform_api, obj.__name__, obj)
platform_api.register_platform_adapter = lambda *args, **kwargs: lambda cls: cls
for obj in (AstrMessageEvent, MessageChain):
    setattr(event_api, obj.__name__, obj)
for obj in (Plain, At, AtAll, Reply, Image, Record, Video, File):
    setattr(components, obj.__name__, obj)
base_event_module.MessageSesion = MessageSesion

models = importlib.import_module("yunhu_plugin.api.models")
client_module = importlib.import_module("yunhu_plugin.api.client")
adapter_module = importlib.import_module("yunhu_plugin.adapter.adapter")
event_module = importlib.import_module("yunhu_plugin.adapter.event")


@pytest.fixture
def adapter_factory(tmp_path, monkeypatch):
    original = adapter_module.TempFileManager
    count = 0

    class TestTempFileManager(original):
        def __init__(self, ttl):
            nonlocal count
            count += 1
            super().__init__(str(tmp_path / f"media-{count}"), ttl)

    monkeypatch.setattr(adapter_module, "TempFileManager", TestTempFileManager)

    def create(**config):
        import asyncio

        return adapter_module.YunhuAdapter(
            {
                "bot_token": "test-secret-token",
                "id": "yunhu-1",
                "bot_id": "bot-1",
                "file_store_dir": str(tmp_path / "saved-files"),
                **config,
            },
            {},
            asyncio.Queue(),
        )

    return create


@pytest.fixture
def message_payload():
    return {
        "version": "1.0",
        "header": {
            "eventId": "event-1",
            "eventTime": 1716000000000,
            "eventType": "message.receive.normal",
        },
        "event": {
            "sender": {
                "senderId": "user-1",
                "senderType": "user",
                "senderNickname": "张三",
                "senderUserLevel": "administrator",
            },
            "chat": {"chatId": "group-1", "chatType": "group"},
            "message": {
                "msgId": "msg-1",
                "parentId": "",
                "sendTime": 1716000000000,
                "chatId": "group-1",
                "chatType": "group",
                "contentType": "text",
                "content": {"text": "你好", "at": ["bot-1"]},
            },
        },
    }


@pytest.fixture
def fake_client():
    result = AsyncMock(spec=client_module.YunhuClient)
    response = models.ApiResponse(
        1,
        data={"imageKey": "image-key", "fileKey": "file-key", "videoKey": "video-key"},
    )
    for method in (
        "send_message",
        "send_html",
        "upload_image",
        "upload_file",
        "upload_video",
    ):
        getattr(result, method).return_value = response
    return result


@pytest.fixture
def event_factory(fake_client):
    def create(parent_id="", recv_type="group"):
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE
            if recv_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        message.sender = MessageMember("user-1")
        return event_module.YunhuMessageEvent(
            "",
            message,
            PlatformMetadata("yunhu", "云湖", "yunhu-1"),
            "group-1",
            fake_client,
            "group-1",
            recv_type,
            parent_id,
        )

    return create
