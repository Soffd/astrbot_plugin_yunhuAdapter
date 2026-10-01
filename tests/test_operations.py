import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from astrbot.api.message_components import At, Plain, Reply
from yunhu_plugin.api.client import YunhuClient
from yunhu_plugin.api.models import ApiResponse
from yunhu_plugin.main import YunhuPlugin
from yunhu_plugin.services.operations import (
    GROUP_ACTIONS,
    HELP,
    YunhuOperations,
    parse_command,
)


@pytest.fixture
def operation_event(event_factory):
    event = event_factory()
    client = YunhuClient("secret-for-tools")
    for name in (
        "gag_member",
        "remove_member",
        "set_message_type_limit",
        "list_tags",
        "create_tag",
        "edit_tag",
        "delete_tag",
        "add_user_tag",
        "remove_user_tag",
        "get_messages",
        "recall_message",
        "edit_message",
        "set_board",
        "dismiss_board",
        "set_board_all",
        "dismiss_board_all",
        "send_message",
        "batch_send",
    ):
        setattr(
            client,
            name,
            AsyncMock(return_value=ApiResponse(1, data={"msgId": "sent-1"})),
        )
    event._client = client
    event.message_obj.group_id = "group-1"
    event.message_obj.self_id = "bot-1"
    event.message_obj.message_id = "incoming-1"
    event.message_obj.message = [Plain("命令"), At("bot-1"), At("user-2")]
    event.set_extra("yunhu_sender_level", "administrator")
    return event


GROUP_WRITES = [
    ("mute", {"user_id": "user-2"}),
    ("remove", {"user_id": "user-2"}),
    ("message_types", {"types": "text"}),
    ("create_tag", {"tag": "VIP"}),
    ("edit_tag", {"tag": "VIP", "changes": {"new_tag": "SVIP"}}),
    ("delete_tag", {"tag": "VIP"}),
    ("user_tag", {"action": "add", "tag": "VIP", "user_id": "user-2"}),
    ("recall", {"message_id": "parent-1"}),
    ("edit_message", {"message_id": "parent-1", "text": "new"}),
    ("board", {"text": "notice"}),
    ("dismiss_board", {}),
]


@pytest.mark.parametrize("action,params", GROUP_WRITES)
@pytest.mark.parametrize("level", ["member", "unknown", "admin"])
@pytest.mark.asyncio
async def test_group_writes_reject_non_admin_before_network(
    operation_event, action, params, level
):
    operation_event.set_extra("yunhu_sender_level", level)
    result = await YunhuOperations().execute(operation_event, action, **params)
    assert not result["ok"] and "仅限" in result["message"]
    assert all(
        not method.called
        for method in vars(operation_event.client).values()
        if isinstance(method, AsyncMock)
    )


@pytest.mark.parametrize("action", sorted(GROUP_ACTIONS))
@pytest.mark.asyncio
async def test_group_actions_reject_private_even_for_global_admin(
    operation_event, action
):
    operation_event._recv_type = "user"
    operation_event.role = "admin"
    result = await YunhuOperations().execute(operation_event, action)
    assert not result["ok"] and "群聊" in result["message"]


@pytest.mark.parametrize("level", ["owner", "administrator", "member"])
@pytest.mark.asyncio
async def test_only_astrbot_admin_can_perform_global_actions(operation_event, level):
    operation_event.set_extra("yunhu_sender_level", level)
    operations = YunhuOperations()
    result = await operations.execute(
        operation_event, "global_board", action="set", text="global"
    )
    assert not result["ok"]
    result = await operations.execute(
        operation_event,
        "batch_send",
        receiver_ids=["user-2"],
        receiver_type="user",
        text="hello",
    )
    assert not result["ok"]
    operation_event.client.set_board_all.assert_not_awaited()
    operation_event.client.batch_send.assert_not_awaited()
    operation_event.role = "admin"
    result = await operations.execute(
        operation_event,
        "global_board",
        action="set",
        text="global",
        expire_time=1800000000,
    )
    assert result["ok"]
    operation_event.client.set_board_all.assert_awaited_once_with(
        content_type="text", content="global", expire_time=1800000000
    )


@pytest.mark.parametrize(
    "level,role",
    [("owner", "member"), ("administrator", "member"), ("member", "admin")],
)
@pytest.mark.asyncio
async def test_authorized_moderation_uses_current_group_and_actual_mentions(
    operation_event, level, role
):
    operation_event.set_extra("yunhu_sender_level", level)
    operation_event.role = role
    result = await YunhuOperations().execute(operation_event, "mute", duration=600)
    assert result["ok"]
    operation_event.client.gag_member.assert_awaited_once_with("group-1", "user-2", 600)
    assert operation_event.role == role


@pytest.mark.parametrize("duration", [True, -2, 1.5, "1.5", 2**63, None])
@pytest.mark.asyncio
async def test_invalid_mute_duration_does_not_send(operation_event, duration):
    result = await YunhuOperations().execute(operation_event, "mute", duration=duration)
    assert not result["ok"]
    operation_event.client.gag_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_mentions_cannot_select_arbitrary_member(operation_event):
    operation_event.message_obj.message.append(At("user-3"))
    operations = YunhuOperations()
    assert not (await operations.execute(operation_event, "remove"))["ok"]
    operation_event.client.remove_member.assert_not_awaited()
    assert (await operations.execute(operation_event, "remove", user_id="user-3"))["ok"]
    operation_event.client.remove_member.assert_awaited_once_with("group-1", "user-3")


@pytest.mark.asyncio
async def test_targets_from_reply_and_missing_message_id(operation_event):
    operation_event.message_obj.message = [
        At("bot-1"),
        Reply(id="parent-1", sender_id="user-4"),
    ]
    operations = YunhuOperations()
    assert (await operations.execute(operation_event, "mute", duration=0))["ok"]
    operation_event.client.gag_member.assert_awaited_once_with("group-1", "user-4", 0)
    assert (await operations.execute(operation_event, "recall"))["ok"]
    operation_event.client.recall_message.assert_awaited_once_with(
        "parent-1", "group-1", "group"
    )
    operation_event.message_obj.message = [Plain("撤回")]
    assert not (await operations.execute(operation_event, "recall"))["ok"]
    assert operation_event.client.recall_message.await_count == 1


@pytest.mark.asyncio
async def test_tag_changes_reject_scope_injection_and_preserve_omitted_fields(
    operation_event,
):
    operations = YunhuOperations()
    assert not (
        await operations.execute(
            operation_event,
            "edit_tag",
            tag="VIP",
            changes={"group_id": "other", "new_tag": "SVIP"},
        )
    )["ok"]
    operation_event.client.edit_tag.assert_not_awaited()
    assert (
        await operations.execute(
            operation_event,
            "edit_tag",
            tag="VIP",
            changes={"description": "", "sort": 0},
        )
    )["ok"]
    operation_event.client.edit_tag.assert_awaited_once_with(
        "group-1", "VIP", desc="", sort=0
    )
    assert not (
        await operations.execute(
            operation_event, "mute", user_id="user-2", group_id="other"
        )
    )["ok"]
    operation_event.client.gag_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_failure_is_reported_and_secrets_redacted(operation_event):
    operation_event.client.gag_member.return_value = ApiResponse(
        -1, "permission denied secret-for-tools"
    )
    result = await YunhuOperations().execute(operation_event, "mute")
    rendered = YunhuOperations.render(operation_event, result, as_json=True)
    assert json.loads(rendered)["ok"] is False
    assert json.loads(rendered)["code"] == -1
    assert "权限" in json.loads(rendered)["hint"]
    assert "permission denied" in rendered and "secret-for-tools" not in rendered
    operation_event.client.send_message.return_value = ApiResponse(
        1003, "invalid token"
    )
    result = await YunhuOperations().execute(
        operation_event, "buttons", text="test", buttons=[[{"text": "ok"}]]
    )
    assert not result["ok"] and result["code"] == 1003


@pytest.mark.asyncio
async def test_board_expiry_history_and_button_payloads(operation_event):
    operations = YunhuOperations()
    assert (
        await operations.execute(
            operation_event, "board", text="notice", expire_time=1800000000
        )
    )["ok"]
    operation_event.client.set_board.assert_awaited_once_with(
        "group-1", "group", "text", "notice", "", 1800000000
    )
    assert (
        await operations.execute(
            operation_event, "history", before=5, after=2, message_id="parent-1"
        )
    )["ok"]
    operation_event.client.get_messages.assert_awaited_once_with(
        "group-1", "group", before=5, after=2, message_id="parent-1"
    )
    assert (
        await operations.execute(
            operation_event,
            "buttons",
            text="test",
            buttons=[[{"text": "ok", "type": "callback", "value": "confirm"}]],
        )
    )["ok"]
    buttons = operation_event.client.send_message.call_args.kwargs["buttons"]
    assert buttons[0].to_dict() == [{"text": "ok", "actionType": 3, "value": "confirm"}]
    assert isinstance(operation_event.get_messages(), list)


@pytest.mark.asyncio
async def test_command_and_tools_share_permission_checks(operation_event):
    plugin = YunhuPlugin(None)
    outputs = [
        result
        async for result in plugin.yunhu_command(operation_event, "禁言 user-2 600")
    ]
    assert "操作成功" in outputs[0].chain[0].text
    assert operation_event.stopped and not operation_event.call_llm
    result = json.loads(await plugin.yunhu_mute_member(operation_event, "user-2", 600))
    assert result["ok"]
    assert (
        operation_event.client.gag_member.call_args_list[0]
        == operation_event.client.gag_member.call_args_list[1]
    )
    operation_event.set_extra("yunhu_sender_level", "member")
    denied = json.loads(await plugin.yunhu_remove_member(operation_event, "user-2"))
    assert not denied["ok"]
    operation_event.client.remove_member.assert_not_awaited()
    outputs = [result async for result in plugin.yunhu_command(operation_event, "帮助")]
    assert outputs[0].chain[0].text == HELP
    assert not json.loads(await plugin.yunhu_context(object()))["ok"]


@pytest.mark.parametrize(
    "command,action,params",
    [
        ("解禁 user-2", "mute", {"user_id": "user-2", "duration": 0}),
        ("消息类型 不限", "message_types", {"types": ""}),
        (
            '标签创建 "VIP 用户" "#FF5733" "测试描述" 2',
            "create_tag",
            {
                "tag": "VIP 用户",
                "color": "#FF5733",
                "description": "测试描述",
                "sort": "2",
            },
        ),
        (
            '标签修改 VIP \'{"new_tag":"SVIP"}\'',
            "edit_tag",
            {"tag": "VIP", "changes": {"new_tag": "SVIP"}},
        ),
        (
            "批量发送 user user-2,user-3 hello world",
            "batch_send",
            {
                "receiver_type": "user",
                "receiver_ids": ["user-2", "user-3"],
                "text": "hello world",
            },
        ),
    ],
)
def test_command_parsing_handles_quotes_json_and_remaining_text(
    command, action, params
):
    assert parse_command(command) == (action, params)


@pytest.mark.parametrize(
    "command",
    [
        "未知",
        "标签删除",
        "标签修改 VIP 'bad json'",
        '标签创建 "未闭合',
        "禁言 u 3 extra",
    ],
)
def test_bad_commands_are_rejected(command):
    with pytest.raises(ValueError):
        parse_command(command)


@pytest.mark.asyncio
async def test_command_and_agent_tools_reach_real_http_api(operation_event):
    requests = []

    async def handle(request):
        requests.append((request.path, dict(request.query), await request.json()))
        return web.json_response({"code": 1, "msg": "success", "data": {}})

    app = web.Application()
    app.router.add_post("/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = YunhuClient("token&中文", base_url=f"http://127.0.0.1:{port}")
    operation_event._client = client
    plugin = YunhuPlugin(None)
    try:
        outputs = [
            result
            async for result in plugin.yunhu_command(operation_event, "解禁 user-2")
        ]
        assert "操作成功" in outputs[0].chain[0].text
        assert json.loads(
            await plugin.yunhu_user_tag(operation_event, "add", "VIP", "user-2")
        )["ok"]
        assert json.loads(
            await plugin.yunhu_send_buttons(
                operation_event,
                "请选择",
                '[[{"text":"确认","type":"callback","value":"yes"}]]',
            )
        )["ok"]
        assert requests[:2] == [
            (
                "/group/gag-member",
                {"token": "token&中文"},
                {"groupId": "group-1", "userId": "user-2", "gag": 0},
            ),
            (
                "/group/tag/user-relate",
                {"token": "token&中文"},
                {"groupId": "group-1", "userId": "user-2", "tag": "VIP"},
            ),
        ]
        assert requests[2][2]["content"]["buttons"] == [
            [{"text": "确认", "actionType": 3, "value": "yes"}]
        ]
    finally:
        await client.close()
        await runner.cleanup()
