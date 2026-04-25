"""
云湖平台数据模型
包含事件模型、API请求/响应模型、按钮模型等
"""
from dataclasses import dataclass, field
from typing import Optional, Any, List


# API 响应 

@dataclass
class ApiResponse:
    code: int
    msg: str = ""
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.code == 1


# 事件模型 

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
    chatType: str  # "bot" or "group"


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

    @property
    def text(self) -> str:
        return self.content.get("text", "")

    @property
    def imageKey(self) -> str:
        return self.content.get("imageKey", "")

    @property
    def imageUrl(self) -> str:
        return self.content.get("imageUrl", "")

    @property
    def fileKey(self) -> str:
        return self.content.get("fileKey", "")

    @property
    def fileUrl(self) -> str:
        return self.content.get("fileUrl", "")

    @property
    def fileName(self) -> str:
        return self.content.get("fileName", "")

    @property
    def videoKey(self) -> str:
        return self.content.get("videoKey", "")

    @property
    def videoUrl(self) -> str:
        return self.content.get("videoUrl", "")


# 按钮模型 

@dataclass
class Button:
    """
    云湖消息按钮
    按钮类型:
      - url: 跳转链接按钮
      - callback: 回调按钮（点击后触发 button.report.inline 事件）
      - command: 指令按钮（点击后自动发送指令消息）
    """
    text: str
    type: str = "callback"  # url / callback / command
    value: str = ""
    url: str = ""
    actionType: int = 1  # 1=回调, 2=跳转URL

    def to_dict(self) -> dict:
        d = {"text": self.text}
        if self.type == "url" or self.actionType == 2:
            d["actionType"] = 2
            d["url"] = self.url or self.value
        else:
            d["actionType"] = 1
            d["value"] = self.value or self.text
        return d


@dataclass
class ButtonGroup:
    """按钮组，一行最多5个按钮"""
    buttons: List[Button] = field(default_factory=list)

    def to_dict(self) -> list:
        return [btn.to_dict() for btn in self.buttons]


# 按钮点击事件 

@dataclass
class ButtonReportEvent:
    """按钮汇报事件 (button.report.inline)"""
    msgId: str
    recvId: str
    recvType: str  # "bot"
    time: int
    userId: str
    value: str


# 群事件 

@dataclass
class GroupMemberEvent:
    """群成员加入/离开事件"""
    chatId: str
    chatType: str
    userId: str
    time: int = 0


# 机器人关注事件 

@dataclass
class BotFollowEvent:
    """机器人关注/取关事件"""
    userId: str
    time: int = 0


# 统一事件 

@dataclass
class YunhuEvent:
    """云湖平台统一事件"""
    event_id: str
    event_time: int
    event_type: str
    raw: dict
    sender: Optional[Sender] = None
    chat: Optional[Chat] = None
    message: Optional[Message] = None
    # 按钮汇报事件字段
    button_report: Optional[ButtonReportEvent] = None
    # 群成员事件字段
    group_member: Optional[GroupMemberEvent] = None
    # 机器人关注事件字段
    bot_follow: Optional[BotFollowEvent] = None


def parse_event(data: dict) -> Optional[YunhuEvent]:
    """解析云湖推送事件 JSON 为 YunhuEvent 对象"""
    try:
        header = data.get("header", {})
        event_type = header.get("eventType", "")

        # 按钮汇报事件结构不同
        if event_type == "button.report.inline":
            return YunhuEvent(
                event_id=header.get("eventId", ""),
                event_time=header.get("eventTime", 0),
                event_type=event_type,
                raw=data,
                button_report=ButtonReportEvent(
                    msgId=data.get("msgId", ""),
                    recvId=data.get("recvId", ""),
                    recvType=data.get("recvType", "bot"),
                    time=data.get("time", 0),
                    userId=data.get("userId", ""),
                    value=data.get("value", ""),
                ),
            )

        # 群成员加入/离开事件
        if event_type in ("group.join", "group.leave"):
            event_data = data.get("event", {})
            chat_data = event_data.get("chat", {})
            return YunhuEvent(
                event_id=header.get("eventId", ""),
                event_time=header.get("eventTime", 0),
                event_type=event_type,
                raw=data,
                chat=Chat(
                    chatId=chat_data.get("chatId", ""),
                    chatType=chat_data.get("chatType", "group"),
                ),
                group_member=GroupMemberEvent(
                    chatId=chat_data.get("chatId", ""),
                    chatType=chat_data.get("chatType", "group"),
                    userId=event_data.get("member", {}).get("userId", ""),
                    time=event_data.get("time", 0),
                ),
            )

        # 机器人关注/取关事件
        if event_type in ("bot.followed", "bot.unfollowed"):
            event_data = data.get("event", {})
            return YunhuEvent(
                event_id=header.get("eventId", ""),
                event_time=header.get("eventTime", 0),
                event_type=event_type,
                raw=data,
                bot_follow=BotFollowEvent(
                    userId=event_data.get("userId", ""),
                    time=event_data.get("time", 0),
                ),
            )

        # 普通消息/指令消息事件
        event_data = data.get("event", {})
        sender_data = event_data.get("sender", {})
        chat_data = event_data.get("chat", {})
        msg_data = event_data.get("message", {})

        sender = Sender(
            senderId=sender_data.get("senderId", ""),
            senderType=sender_data.get("senderType", "user"),
            senderUserLevel=sender_data.get("senderUserLevel", "member"),
            senderNickname=sender_data.get("senderNickname", ""),
            senderAvatarUrl=sender_data.get("senderAvatarUrl", ""),
        )

        chat = Chat(
            chatId=chat_data.get("chatId", ""),
            chatType=chat_data.get("chatType", "bot"),
        )

        message = Message(
            msgId=msg_data.get("msgId", ""),
            parentId=msg_data.get("parentId", ""),
            sendTime=msg_data.get("sendTime", 0),
            chatId=msg_data.get("chatId", ""),
            chatType=msg_data.get("chatType", ""),
            contentType=msg_data.get("contentType", "text"),
            content=msg_data.get("content", {}),
            commandId=msg_data.get("commandId", 0),
            commandName=msg_data.get("commandName", ""),
        )

        return YunhuEvent(
            event_id=header.get("eventId", ""),
            event_time=header.get("eventTime", 0),
            event_type=event_type,
            raw=data,
            sender=sender,
            chat=chat,
            message=message,
        )
    except Exception as e:
        import logging
        logging.getLogger("yunhu.models").error(f"解析事件失败: {e}, data={data}")
        return None


# API 请求模型 

@dataclass
class SendMessageRequest:
    recvId: str
    recvType: str
    contentType: str
    content: dict
    parentId: str = ""
    buttons: List[ButtonGroup] = field(default_factory=list)


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