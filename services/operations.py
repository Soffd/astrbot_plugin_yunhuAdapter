"""指令与 Agent Tools 共用的云湖操作入口、会话作用域和权限校验。"""

import asyncio
import json
import re
import shlex
import sqlite3

from astrbot.api.message_components import At, Reply

from ..adapter.event import YunhuMessageEvent
from ..api.client import YunhuAPIError
from ..api.models import ApiResponse, Button, ButtonGroup
from ..media.attachments import attachment_info, read_attachment
from ..media.store import event_store, retained_files

GROUP_ACTIONS = {
    "mute",
    "remove",
    "message_types",
    "tags",
    "create_tag",
    "edit_tag",
    "delete_tag",
    "user_tag",
}
SESSION_WRITES = {"edit_message", "recall", "board", "dismiss_board"}
GLOBAL_ACTIONS = {"global_board", "batch_send"}
ACTIONS = (
    GROUP_ACTIONS
    | SESSION_WRITES
    | GLOBAL_ACTIONS
    | {
        "context",
        "history",
        "send",
        "buttons",
        "read_attachment",
        "attachments",
        "stored_files",
        "manage_file",
    }
)
MESSAGE_TYPES = {
    "text",
    "image",
    "markdown",
    "file",
    "post",
    "expression",
    "html",
    "video",
    "audio",
    "liveAudio",
}
HELP = """云湖功能（/yunhu 同样可用）：
/云湖 信息
/云湖 文件列表
/云湖 读文件 [文件名]（当前/引用优先，其次保留文件）
/云湖 保留列表 [条数]
/云湖 读保留文件 文件ID
/云湖 保留文件 文件ID（长期保留，仍占容量）
/云湖 取消保留 文件ID（恢复按保留天数过期）
/云湖 删除文件 文件ID（仅上传者或当前会话管理员）
/云湖 禁言 用户ID [秒数]（默认600；0解禁；-1永久）
/云湖 解禁 用户ID
/云湖 移除 用户ID
/云湖 消息类型 text,image（填 不限 恢复全部类型）
/云湖 标签列表
/云湖 标签创建 标签 [#RRGGBB] [描述] [排序]
/云湖 标签修改 标签 '{"new_tag":"新标签","color":"#FF5733"}'
/云湖 标签删除 标签
/云湖 标签添加 用户ID 标签
/云湖 标签移除 用户ID 标签
/云湖 历史 [条数] [消息ID] [后续条数]
/云湖 撤回 [消息ID]（省略时使用引用的消息）
/云湖 编辑 消息ID 新内容（消息ID填 引用 可使用引用消息）
/云湖 看板 内容
/云湖 看板取消
/云湖 发送 内容
/云湖 按钮 内容 '[[{"text":"确认","type":"callback","value":"yes"}]]'
/云湖 全局看板 内容（仅AstrBot管理员）
/云湖 全局看板取消（仅AstrBot管理员）
/云湖 批量发送 user或group ID1,ID2 内容（仅AstrBot管理员）
含空格的标签或参数用引号包裹。用户ID填 @ 或 引用 时，使用消息中唯一的@对象或引用发送者；排除机器人自身。群管理限本群群主、管理员或AstrBot管理员。"""


def _text(value, name, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} 必须为{'字符串' if allow_empty else '非空字符串'}")
    return value


def _integer(value, name, minimum=0, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} 必须为整数")
    try:
        result = int(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} 必须为整数") from None
    if isinstance(value, float) and value != result:
        raise ValueError(f"{name} 必须为整数")
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _content_type(value):
    if value not in ("text", "markdown", "html"):
        raise ValueError("content_type 必须为 text、markdown 或 html")
    return value


def _id(value, name):
    value = _text(value, name).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{name} 必须为实际ID，不能使用昵称")
    return value


def _target_user(event, user_id):
    user_id = _text(user_id, "user_id", allow_empty=True).strip()
    if user_id in ("", "@", "引用"):
        mentions = {
            str(component.qq)
            for component in event.get_messages()
            if isinstance(component, At)
            and str(component.qq) not in (str(event.message_obj.self_id), "all", "0")
        }
        if user_id != "引用" and mentions:
            if len(mentions) != 1:
                raise ValueError("存在多个@对象，请明确提供用户ID")
            user_id = next(iter(mentions))
        else:
            senders = {
                str(component.sender_id)
                for component in event.get_messages()
                if isinstance(component, Reply)
                and component.sender_id
                and str(component.sender_id) != str(event.message_obj.self_id)
            }
            if len(senders) != 1:
                raise ValueError("请提供用户ID，或@一名成员、引用该成员的消息")
            user_id = next(iter(senders))
    user_id = _id(user_id.removeprefix("@"), "user_id")
    if user_id == str(event.message_obj.self_id):
        raise ValueError("不能以机器人自身为群管理目标")
    return user_id


def _target_message(event, message_id):
    message_id = _text(message_id, "message_id", allow_empty=True).strip()
    if message_id in ("", "引用"):
        replies = {
            str(c.id) for c in event.get_messages() if isinstance(c, Reply) and c.id
        }
        if len(replies) != 1:
            raise ValueError("请提供消息ID或引用要操作的消息")
        message_id = next(iter(replies))
    return _id(message_id, "message_id")


class YunhuOperations:
    async def execute(self, event, operation, **arguments):
        try:
            if not isinstance(event, YunhuMessageEvent):
                raise ValueError("该功能仅适用于云湖平台会话")
            if operation not in ACTIONS:
                raise ValueError("未知云湖操作，请使用 /云湖 帮助")
            group = event._recv_type == "group"
            if operation in GROUP_ACTIONS and not group:
                raise ValueError("该功能需要在云湖群聊中使用")
            if operation in GLOBAL_ACTIONS:
                if not event.is_admin():
                    raise PermissionError("该操作仅限 AstrBot 管理员")
            elif group and operation in (GROUP_ACTIONS - {"tags"}) | SESSION_WRITES:
                if not event.is_admin() and event.get_extra(
                    "yunhu_sender_level"
                ) not in (
                    "owner",
                    "administrator",
                ):
                    raise PermissionError(
                        "该操作仅限当前云湖群的群主、管理员或 AstrBot 管理员"
                    )
            try:
                response = await getattr(self, f"_op_{operation}")(event, **arguments)
            except YunhuAPIError as error:
                response = error.response
            result = {
                "ok": response.ok,
                "code": response.code,
                "message": "操作成功"
                if response.ok
                else response.msg or "云湖接口执行失败",
                "data": response.data,
            }
            if not response.ok and operation in GROUP_ACTIONS - {"tags"}:
                result["hint"] = (
                    "检查云湖中授予机器人的对应群管理权限，并确认机器人仍在群中"
                )
            return result
        except (ValueError, PermissionError) as error:
            return {"ok": False, "message": str(error)}
        except (OSError, sqlite3.Error) as error:
            return {"ok": False, "message": f"文件库操作失败 ({type(error).__name__})"}
        except TypeError:
            return {"ok": False, "message": "参数格式不正确，请使用 /云湖 帮助查看格式"}

    @staticmethod
    def render(event, result, as_json=False):
        if as_json:
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        else:
            text = result["message"]
            if "code" in result and not result["ok"]:
                text += f"（code={result['code']}）"
            if result.get("hint"):
                text += "\n" + result["hint"]
            if result.get("data") not in (None, {}, []):
                text += "\n" + json.dumps(result["data"], ensure_ascii=False, indent=2)
        if isinstance(event, YunhuMessageEvent):
            text = event.client._safe_error(RuntimeError(text))
        return text

    async def _op_context(self, event):
        return ApiResponse(
            1,
            data={
                "platform_id": event.get_platform_id(),
                "chat_id": event._recv_id,
                "chat_type": event._recv_type,
                "sender_id": event.get_sender_id(),
                "bot_id": event.message_obj.self_id,
                "sender_level": event.get_extra("yunhu_sender_level") or "unknown",
                "astrbot_admin": event.is_admin(),
                "message_id": event.message_obj.message_id,
                "mentions": [
                    str(c.qq) for c in event.get_messages() if isinstance(c, At)
                ],
                "replies": [
                    {"message_id": str(c.id), "sender_id": str(c.sender_id or "")}
                    for c in event.get_messages()
                    if isinstance(c, Reply)
                ],
                "attachments": attachment_info(event),
                "stored_files": await asyncio.to_thread(retained_files, event, 20),
                "file_store_failures": event.get_extra("yunhu_file_store_failures")
                or [],
            },
        )

    async def _op_attachments(self, event):
        return ApiResponse(
            1,
            data={
                "current": attachment_info(event),
                "stored": await asyncio.to_thread(retained_files, event),
                "retention_failures": event.get_extra("yunhu_file_store_failures")
                or [],
            },
        )

    async def _op_stored_files(self, event, limit=100):
        return ApiResponse(
            1,
            data=await asyncio.to_thread(
                retained_files, event, _integer(limit, "limit", 1, 1000)
            ),
        )

    async def _op_manage_file(self, event, file_id, action):
        store = event_store(event)
        if not store:
            raise ValueError("当前会话没有可用的文件保留库")
        is_admin = event.is_admin() or (
            event._recv_type == "group"
            and event.get_extra("yunhu_sender_level") in ("owner", "administrator")
        )
        result = await asyncio.to_thread(
            store.manage,
            event._recv_type,
            event._recv_id,
            _id(file_id, "file_id"),
            action,
            event.get_sender_id(),
            is_admin,
        )
        return ApiResponse(1, data=result)

    async def _op_read_attachment(
        self, event, file_name="", source="all", max_chars=16000, file_id=""
    ):
        return ApiResponse(
            1,
            data=await read_attachment(
                event,
                file_name,
                source,
                _integer(max_chars, "max_chars", 1, 64000),
                file_id,
            ),
        )

    async def _op_mute(self, event, user_id="", duration=600):
        return await event.client.gag_member(
            event._recv_id,
            _target_user(event, user_id),
            _integer(duration, "duration", -1),
        )

    async def _op_remove(self, event, user_id=""):
        return await event.client.remove_member(
            event._recv_id, _target_user(event, user_id)
        )

    async def _op_message_types(self, event, types=""):
        types = _text(types, "types", allow_empty=True).strip()
        values = [s.strip() for s in types.split(",")] if types else []
        if any(value not in MESSAGE_TYPES for value in values):
            raise ValueError("消息类型无效；支持 " + ",".join(sorted(MESSAGE_TYPES)))
        return await event.client.set_message_type_limit(
            event._recv_id, ",".join(dict.fromkeys(values))
        )

    async def _op_tags(self, event):
        return await event.client.list_tags(event._recv_id)

    async def _op_create_tag(self, event, tag, color="", description="", sort=0):
        self._validate_color(color, allow_empty=True)
        return await event.client.create_tag(
            event._recv_id,
            _text(tag, "tag").strip(),
            color,
            _text(description, "description", allow_empty=True),
            _integer(sort, "sort", -(2**63)),
        )

    @staticmethod
    def _validate_color(color, allow_empty=False):
        if (
            not isinstance(color, str)
            or not (allow_empty and color == "")
            and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color)
        ):
            raise ValueError("颜色必须为 #RRGGBB 格式")

    async def _op_edit_tag(self, event, tag, changes):
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - {"new_tag", "color", "description", "sort"}
        ):
            raise ValueError(
                "changes 必须为非空对象，只能包含 new_tag、color、description、sort"
            )
        fields = dict(changes)
        if "new_tag" in fields:
            fields["new_tag"] = _text(fields["new_tag"], "new_tag").strip()
        if "color" in fields:
            self._validate_color(fields["color"])
        if "description" in fields:
            fields["desc"] = _text(
                fields.pop("description"), "description", allow_empty=True
            )
        if "sort" in fields:
            fields["sort"] = _integer(fields["sort"], "sort", -(2**63))
        return await event.client.edit_tag(
            event._recv_id, _text(tag, "tag").strip(), **fields
        )

    async def _op_delete_tag(self, event, tag):
        return await event.client.delete_tag(event._recv_id, _text(tag, "tag").strip())

    async def _op_user_tag(self, event, action, tag, user_id=""):
        if action not in ("add", "remove"):
            raise ValueError("action 必须为 add 或 remove")
        method = (
            event.client.add_user_tag
            if action == "add"
            else event.client.remove_user_tag
        )
        return await method(
            event._recv_id, _target_user(event, user_id), _text(tag, "tag").strip()
        )

    async def _op_history(self, event, before=20, after=0, message_id=""):
        before, after = (
            _integer(before, "before", maximum=100),
            _integer(after, "after", maximum=100),
        )
        message_id = _text(message_id, "message_id", allow_empty=True).strip()
        if message_id:
            message_id = _target_message(event, message_id)
        elif after:
            raise ValueError("查询后续消息时请提供 message_id")
        return await event.get_message_history(message_id, before, after)

    async def _op_recall(self, event, message_id=""):
        return await event.recall_message(_target_message(event, message_id))

    async def _op_edit_message(self, event, text, message_id="", content_type="text"):
        return await event.edit_message(
            _target_message(event, message_id),
            _content_type(content_type),
            {"text": _text(text, "text")},
        )

    async def _op_board(
        self, event, text, content_type="text", expire_time=0, member_id=""
    ):
        if member_id:
            if event._recv_type != "group":
                raise ValueError("member_id 仅适用于群看板")
            member_id = _target_user(event, member_id)
        return await event.set_board(
            _content_type(content_type),
            _text(text, "text"),
            member_id,
            _integer(expire_time, "expire_time"),
        )

    async def _op_dismiss_board(self, event, member_id=""):
        if member_id:
            if event._recv_type != "group":
                raise ValueError("member_id 仅适用于群看板")
            member_id = _target_user(event, member_id)
        return await event.dismiss_board(member_id)

    async def _op_global_board(
        self, event, action, text="", content_type="text", expire_time=0
    ):
        if action == "dismiss":
            return await event.client.dismiss_board_all()
        if action != "set":
            raise ValueError("action 必须为 set 或 dismiss")
        return await event.client.set_board_all(
            content_type=_content_type(content_type),
            content=_text(text, "text"),
            expire_time=_integer(expire_time, "expire_time"),
        )

    async def _op_send(self, event, text, content_type="text"):
        return await event._send_text_content(
            _text(text, "text"), _content_type(content_type)
        )

    async def _op_buttons(self, event, text, buttons):
        if isinstance(buttons, str):
            try:
                buttons = json.loads(buttons)
            except ValueError:
                raise ValueError("buttons 必须为有效的JSON二维数组字符串") from None
        if not isinstance(buttons, list) or not 1 <= len(buttons) <= 10:
            raise ValueError("buttons 必须为包含1到10行的二维数组")
        groups = []
        for row in buttons:
            if not isinstance(row, list) or not 1 <= len(row) <= 5:
                raise ValueError("每行必须包含1到5个按钮对象")
            parsed = []
            for item in row:
                if not isinstance(item, dict) or set(item) - {
                    "text",
                    "type",
                    "value",
                    "url",
                }:
                    raise ValueError("按钮只能包含 text、type、value、url")
                button = Button(
                    _text(item.get("text"), "按钮文字"),
                    type=item.get("type", "callback"),
                    value=_text(item.get("value", ""), "按钮值", allow_empty=True),
                    url=_text(item.get("url", ""), "按钮链接", allow_empty=True),
                )
                button.to_dict()
                if button.type == "url" and not re.match(
                    r"^https?://[^/\s]+", button.url or button.value
                ):
                    raise ValueError("URL按钮必须提供完整的 http:// 或 https:// 链接")
                parsed.append(button)
            groups.append(ButtonGroup(parsed))
        return await event.send_with_buttons(_text(text, "text"), groups)

    async def _op_batch_send(
        self, event, receiver_ids, receiver_type, text, content_type="text"
    ):
        if receiver_type not in ("user", "group"):
            raise ValueError("receiver_type 必须为 user 或 group")
        if not isinstance(receiver_ids, list) or not 1 <= len(receiver_ids) <= 100:
            raise ValueError("receiver_ids 必须为包含1到100个ID的数组")
        ids = list(dict.fromkeys(_id(value, "接收者ID") for value in receiver_ids))
        return await event.client.batch_send(
            ids,
            receiver_type,
            _content_type(content_type),
            {"text": _text(text, "text")},
        )


# 每个指令声明对应操作、位置参数和必需参数数量；两种入口复用 execute。
COMMANDS = {
    "信息": ("context", [], 0),
    "文件列表": ("attachments", [], 0),
    "读文件": ("read_attachment", ["file_name"], 0),
    "保留列表": ("stored_files", ["limit"], 0),
    "读保留文件": ("read_attachment", ["file_id"], 1),
    "保留文件": ("manage_file", ["file_id"], 1),
    "取消保留": ("manage_file", ["file_id"], 1),
    "删除文件": ("manage_file", ["file_id"], 1),
    "禁言": ("mute", ["user_id", "duration"], 0),
    "解禁": ("mute", ["user_id"], 0),
    "移除": ("remove", ["user_id"], 0),
    "消息类型": ("message_types", ["types"], 1),
    "标签列表": ("tags", [], 0),
    "标签创建": ("create_tag", ["tag", "color", "description", "sort"], 1),
    "标签修改": ("edit_tag", ["tag", "changes"], 2),
    "标签删除": ("delete_tag", ["tag"], 1),
    "标签添加": ("user_tag", ["user_id", "tag"], 2),
    "标签移除": ("user_tag", ["user_id", "tag"], 2),
    "历史": ("history", ["before", "message_id", "after"], 0),
    "撤回": ("recall", ["message_id"], 0),
    "编辑": ("edit_message", ["message_id", "text"], 2),
    "看板": ("board", ["text"], 1),
    "看板取消": ("dismiss_board", [], 0),
    "发送": ("send", ["text"], 1),
    "按钮": ("buttons", ["text", "buttons"], 2),
    "全局看板": ("global_board", ["text"], 1),
    "全局看板取消": ("global_board", [], 0),
    "批量发送": ("batch_send", ["receiver_type", "receiver_ids", "text"], 3),
}


def parse_command(arguments):
    values = shlex.split(arguments)
    if not values or values[0] in ("帮助", "help"):
        return None, {}
    name = values.pop(0)
    if name not in COMMANDS:
        raise ValueError("未知子指令，请使用 /云湖 帮助")
    action, fields, required = COMMANDS[name]
    if fields and fields[-1] == "text" and len(values) > len(fields):
        values[len(fields) - 1 :] = [" ".join(values[len(fields) - 1 :])]
    if not required <= len(values) <= len(fields):
        raise ValueError("参数数量不正确，请使用 /云湖 帮助")
    params = dict(zip(fields, values))
    if name == "读保留文件":
        params["source"] = "stored"
    if name in ("保留文件", "取消保留", "删除文件"):
        params["action"] = {
            "保留文件": "pin",
            "取消保留": "unpin",
            "删除文件": "delete",
        }[name]
    if name == "解禁":
        params["duration"] = 0
    if name in ("标签添加", "标签移除"):
        params["action"] = "add" if name == "标签添加" else "remove"
    if name in ("全局看板", "全局看板取消"):
        params["action"] = "set" if name == "全局看板" else "dismiss"
    if params.get("types") == "不限":
        params["types"] = ""
    for key in ("changes", "buttons"):
        if key in params:
            try:
                params[key] = json.loads(params[key])
            except ValueError:
                raise ValueError(
                    f"{key} 必须为有效JSON，请用单引号包裹JSON参数"
                ) from None
    if "receiver_ids" in params:
        params["receiver_ids"] = params["receiver_ids"].split(",")
    return action, params
