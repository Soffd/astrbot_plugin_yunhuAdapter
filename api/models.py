"""云湖开放平台数据模型，字段名与官方事件保持一致。"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ApiResponse:
    code: int
    msg: str = ""
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.code == 1


@dataclass
class Sender:
    senderId: str
    senderType: str = "user"
    senderUserLevel: str = "member"
    senderNickname: str = ""
    senderAvatarUrl: str = ""


@dataclass
class Chat:
    chatId: str
    chatType: str


@dataclass
class MessageContent:
    text: str = ""
    imageKey: str = ""
    imageUrl: str = ""
    fileKey: str = ""
    fileUrl: str = ""
    fileName: str = ""
    videoKey: str = ""
    videoUrl: str = ""
    audioKey: str = ""
    audioUrl: str = ""
    at: list[str] = field(default_factory=list)


@dataclass
class Message:
    msgId: str
    parentId: str
    sendTime: int
    chatId: str
    chatType: str
    contentType: str
    content: dict
    commandId: int = 0
    commandName: str = ""

    def __getattr__(self, name: str):
        if name in MessageContent.__dataclass_fields__:
            return self.content.get(name, [] if name == "at" else "")
        raise AttributeError(name)


@dataclass
class Button:
    """按钮动作：1 跳转 URL，2 复制，3 点击汇报。"""

    text: str
    type: str = "callback"  # url / copy / callback
    value: str = ""
    url: str = ""
    actionType: Optional[int] = None

    def to_dict(self) -> dict:
        action = self.actionType
        if action is None:
            try:
                action = {"url": 1, "copy": 2, "callback": 3}[self.type]
            except KeyError:
                raise ValueError(f"不支持的按钮类型: {self.type}") from None
        if action not in (1, 2, 3):
            raise ValueError("actionType 必须为 1、2 或 3")
        result = {"text": self.text, "actionType": action}
        result["url" if action == 1 else "value"] = (
            (self.url or self.value) if action == 1 else (self.value or self.text)
        )
        return result


@dataclass
class ButtonGroup:
    buttons: list[Button] = field(default_factory=list)

    def to_dict(self) -> list:
        return [button.to_dict() for button in self.buttons]


@dataclass
class ButtonReportEvent:
    msgId: str
    recvId: str
    recvType: str
    time: int
    userId: str
    value: str


@dataclass
class GroupMemberEvent:
    chatId: str
    chatType: str
    userId: str
    time: int = 0


@dataclass
class BotFollowEvent:
    userId: str
    time: int = 0


MESSAGE_EVENTS = {"message.receive.normal", "message.receive.instruction"}
INTERACTION_EVENTS = {"button.report.inline", "bot.shortcut.menu", "a2ui.button.report"}
NOTICE_EVENTS = {
    "group.join",
    "group.leave",
    "bot.followed",
    "bot.unfollowed",
    "bot.setting",
}


@dataclass
class YunhuEvent:
    event_id: str
    event_time: int
    event_type: str
    raw: dict
    sender: Optional[Sender] = None
    chat: Optional[Chat] = None
    message: Optional[Message] = None
    button_report: Optional[ButtonReportEvent] = None
    group_member: Optional[GroupMemberEvent] = None
    bot_follow: Optional[BotFollowEvent] = None
    event_data: dict = field(default_factory=dict)


def parse_content(value) -> dict:
    """事件与历史接口可能返回对象或 JSON 字符串形式的正文。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("消息 content 必须为对象或 JSON 对象字符串")
    return value


def parse_event(data: dict) -> Optional[YunhuEvent]:
    """解析官方 v1.0 信封及旧版 type=message 信封；无效数据返回 None。"""
    if not isinstance(data, dict):
        return None
    try:
        header = data.get("header") or {}
        body = data.get("event", data.get("data", {}))
        if not isinstance(header, dict) or not isinstance(body, dict):
            return None
        if data.get("type") == "message" and "header" in body:
            event = parse_event(body)
            if event:
                event.raw = data
            return event
        kind = header.get("eventType") or (
            "message.receive.normal" if data.get("type") == "message" else ""
        )
        if kind not in MESSAGE_EVENTS | INTERACTION_EVENTS | NOTICE_EVENTS:
            return None
        event = YunhuEvent(
            event_id=str(header.get("eventId") or ""),
            event_time=int(header.get("eventTime") or 0),
            event_type=kind,
            raw=data,
            event_data=body,
        )
        if kind in MESSAGE_EVENTS:
            sender, chat, msg = (
                body.get(key) or {} for key in ("sender", "chat", "message")
            )
            if not all(isinstance(value, dict) for value in (sender, chat, msg)):
                return None
            content = parse_content(msg.get("content") or {})
            if (
                not isinstance(content, dict)
                or not sender.get("senderId")
                or not msg.get("msgId")
            ):
                return None
            event.sender = Sender(
                senderId=str(sender["senderId"]),
                senderType=sender.get("senderType", "user"),
                senderUserLevel=sender.get("senderUserLevel", "member"),
                senderNickname=sender.get("senderNickname") or "",
                senderAvatarUrl=sender.get("senderAvatarUrl") or "",
            )
            event.chat = Chat(
                str(chat.get("chatId") or msg.get("chatId") or ""),
                chat.get("chatType") or msg.get("chatType") or "bot",
            )
            event.message = Message(
                str(msg["msgId"]),
                str(msg.get("parentId") or ""),
                int(msg.get("sendTime") or event.event_time),
                event.chat.chatId,
                event.chat.chatType,
                msg.get("contentType") or "text",
                content,
                int(msg.get("commandId") or msg.get("instructionId") or 0),
                msg.get("commandName") or msg.get("instructionName") or "",
            )
        else:
            # 非消息事件的字段直接位于 event 内。
            source = body or data  # 兼容早期顶层按钮事件
            user_id = str(source.get("userId") or source.get("senderId") or "")
            chat_type = source.get("recvType") or source.get("chatType") or "bot"
            chat_id = str(
                source.get("recvId")
                or source.get("chatId")
                or source.get("groupId")
                or ""
            )
            event.sender = Sender(
                user_id or "system",
                source.get("senderType") or "user",
                senderNickname=source.get("nickname") or "",
                senderAvatarUrl=source.get("avatarUrl") or "",
            )
            event.chat = Chat(chat_id or user_id, chat_type)
            timestamp = int(
                source.get("time") or source.get("sendTime") or event.event_time
            )
            if kind == "button.report.inline":
                event.button_report = ButtonReportEvent(
                    str(source.get("msgId") or ""),
                    chat_id,
                    chat_type,
                    timestamp,
                    user_id,
                    str(source.get("value") or ""),
                )
            elif kind in ("group.join", "group.leave"):
                event.group_member = GroupMemberEvent(
                    chat_id, chat_type, user_id, timestamp
                )
            elif kind in ("bot.followed", "bot.unfollowed"):
                event.bot_follow = BotFollowEvent(user_id, timestamp)
            text_field = {
                "button.report.inline": "value",
                "a2ui.button.report": "actionName",
                "bot.shortcut.menu": "menuId",
            }.get(kind)
            text = (
                str(source.get(text_field) or f"[{kind}]")
                if text_field
                else f"[{kind}]"
            )
            event.message = Message(
                str(source.get("msgId") or event.event_id),
                "",
                timestamp,
                event.chat.chatId,
                chat_type,
                "text",
                {"text": text},
            )
            event.event_data = source
            if kind != "bot.setting" and not user_id:
                return None
        if event.chat.chatType == "group" and not event.chat.chatId:
            return None
        return event
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass
class SendMessageRequest:
    recvId: str
    recvType: str
    contentType: str
    content: dict
    parentId: str = ""
    buttons: list[ButtonGroup] = field(default_factory=list)


@dataclass
class BatchSendRequest:
    recvIds: list
    recvType: str
    contentType: str
    content: dict


@dataclass
class EditMessageRequest:
    msgId: str
    recvId: str
    recvType: str
    contentType: str
    content: dict


@dataclass
class RecallMessageRequest:
    msgId: str
    chatId: str
    chatType: str


@dataclass
class BoardRequest:
    chatId: str
    chatType: str
    contentType: str
    content: str
    memberId: str = ""
    expireTime: int = 0
