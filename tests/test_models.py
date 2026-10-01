import copy
import json

import pytest
from yunhu_plugin.api.models import Button, ButtonGroup, parse_event


@pytest.mark.parametrize(
    "kind,action,field,value",
    [
        ("url", 1, "url", "https://example.com"),
        ("copy", 2, "value", "copy text"),
        ("callback", 3, "value", "confirm"),
    ],
)
def test_official_button_actions(kind, action, field, value):
    button = Button("按钮", type=kind, value=value)
    assert ButtonGroup([button]).to_dict() == [
        {"text": "按钮", "actionType": action, field: value}
    ]


def test_explicit_button_action():
    assert (
        Button("跳转", actionType=1, url="https://example.com").to_dict()["actionType"]
        == 1
    )
    with pytest.raises(ValueError):
        Button("无效", type="command").to_dict()


def test_instruction_aliases(message_payload):
    message_payload["header"]["eventType"] = "message.receive.instruction"
    message_payload["event"]["message"].update(instructionId=7, instructionName="help")
    event = parse_event(message_payload)
    assert event.message.commandId == 7
    assert event.message.commandName == "help"


@pytest.mark.parametrize(
    "kind",
    [
        "button.report.inline",
        "bot.shortcut.menu",
        "a2ui.button.report",
        "bot.setting",
        "group.join",
        "group.leave",
        "bot.followed",
        "bot.unfollowed",
    ],
)
def test_official_flat_events(kind):
    body = {
        "userId": "u",
        "senderId": "u",
        "chatId": "g",
        "chatType": "group",
        "recvId": "g",
        "recvType": "group",
        "msgId": "m",
        "value": "确认",
        "nickname": "张三",
        "menuId": "menu-1",
        "formContext": {"name": "张三"},
        "settingJson": "{}",
    }
    event = parse_event({"header": {"eventType": kind, "eventId": "e"}, "event": body})
    assert event.chat.chatId == "g"
    assert event.sender.senderId == "u"
    assert event.event_data == body
    if kind == "button.report.inline":
        assert event.button_report.value == "确认"
    if kind.startswith("group."):
        assert event.group_member.userId == "u"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "bad",
        {},
        {"header": []},
        {"header": {"eventType": "message.receive.normal"}, "event": []},
        {"header": {"eventType": "unknown"}},
        {"header": {"eventType": "button.report.inline"}, "event": {}},
    ],
)
def test_malformed_events_are_ignored(data):
    assert parse_event(data) is None


def test_legacy_wrapper(message_payload):
    original = copy.deepcopy(message_payload)
    wrapper = {"type": "message", "data": message_payload}
    event = parse_event(wrapper)
    assert event.message.msgId == "msg-1"
    assert event.raw is wrapper
    assert message_payload == original


def test_chat_falls_back_to_message(message_payload):
    del message_payload["event"]["chat"]
    assert parse_event(message_payload).chat.chatId == "group-1"


def test_json_string_message_content_is_parsed(message_payload):
    message_payload["event"]["message"]["content"] = json.dumps(
        {"fileUrl": "document.html", "fileName": "文档.html"}
    )
    assert parse_event(message_payload).message.content["fileUrl"] == "document.html"
    message_payload["event"]["message"]["content"] = "[1,2]"
    assert parse_event(message_payload) is None
