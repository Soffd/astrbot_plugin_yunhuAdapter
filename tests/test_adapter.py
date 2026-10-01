import asyncio
import copy
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply
from astrbot.api.platform import MessageType
from astrbot.core.platform.astr_message_event import MessageSesion
from yunhu_plugin.adapter import adapter as adapter_module
from yunhu_plugin.adapter import config as config_module
from yunhu_plugin.api.models import ApiResponse


def test_schema_loads_after_working_directory_changes(tmp_path, monkeypatch):
    import importlib

    monkeypatch.chdir(tmp_path)
    config = importlib.reload(config_module)
    assert config.DEFAULT_CONFIG["websocket_url"] == "wss://ws.jwzhd.com/subscribe"
    assert "bot_id" in config.CONFIG_METADATA


@pytest.mark.asyncio
async def test_message_identity_timestamp_and_mentions(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    message = await adapter._convert_to_abm(message_payload)
    assert message.self_id == "bot-1"
    assert message.timestamp == 1716000000
    assert message.session_id == "group-1"
    assert message.sender.nickname == "张三"
    assert isinstance(message.message[0], Plain)
    assert any(isinstance(c, At) and c.qq == "bot-1" for c in message.message)
    await adapter._handle_msg(message, message_payload)
    event = adapter._event_queue.get_nowait()
    assert event.platform_meta.name == "yunhu"
    assert event.platform_meta.id == "yunhu-1"
    assert event.get_extra("yunhu_sender_level") == "administrator"
    assert event.role == "member"  # 群管理员不能因此获得 AstrBot 全局管理员权限
    assert adapter.bot_token not in repr(message.__dict__)
    await adapter.terminate()


@pytest.mark.asyncio
async def test_private_identity_auto_discovery(adapter_factory, message_payload):
    adapter = adapter_factory(bot_id="")
    assert adapter.client_self_id != adapter.bot_token
    message_payload["event"]["chat"] = {"chatId": "bot-auto", "chatType": "bot"}
    message = await adapter._convert_to_abm(message_payload)
    assert message.self_id == "bot-auto"
    assert message.session_id == "user-1"
    assert message.type == MessageType.FRIEND_MESSAGE
    await adapter.terminate()


@pytest.mark.parametrize(
    "kind",
    ["group.join", "group.leave", "bot.followed", "bot.unfollowed", "bot.setting"],
)
@pytest.mark.asyncio
async def test_notice_events_reach_plugins(adapter_factory, kind):
    adapter = adapter_factory()
    payload = {
        "header": {"eventType": kind, "eventId": "e"},
        "event": {
            "userId": "u",
            "chatId": "g",
            "chatType": "group",
            "settingJson": "{}",
        },
    }
    await adapter._process_message(payload)
    event = adapter._event_queue.get_nowait()
    assert event.message_obj.type == MessageType.OTHER_MESSAGE
    assert event.call_llm is False
    assert event.get_extra("yunhu_event_type") == kind
    assert event._recv_type == "group"
    await adapter.terminate()


@pytest.mark.parametrize(
    "kind,text",
    [
        ("button.report.inline", "confirm"),
        ("bot.shortcut.menu", "menu-1"),
        ("a2ui.button.report", "submit"),
    ],
)
@pytest.mark.asyncio
async def test_interaction_events_route_to_clicking_user(adapter_factory, kind, text):
    adapter = adapter_factory()
    payload = {
        "header": {"eventType": kind, "eventId": "e"},
        "event": {
            "recvId": "bot-1",
            "recvType": "bot",
            "senderId": "u",
            "userId": "u",
            "value": "confirm" if kind == "button.report.inline" else "",
            "menuId": "menu-1" if kind == "bot.shortcut.menu" else "",
            "actionName": "submit",
            "formContext": {"name": "张三"},
        },
    }
    await adapter._process_message(payload)
    event = adapter._event_queue.get_nowait()
    assert event.message_str == text
    assert event._recv_id == "u"
    assert event.get_extra("yunhu_event_data")["formContext"] == {"name": "张三"}
    await adapter.terminate()


@pytest.mark.asyncio
async def test_deduplication_in_flight_and_after_commit(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    adapter._dispatch_event(message_payload)
    adapter._dispatch_event(copy.deepcopy(message_payload))
    await asyncio.gather(*list(adapter._tasks))
    adapter._dispatch_event(message_payload)
    assert adapter._event_queue.qsize() == 1
    assert not adapter._pending_ids
    await adapter.terminate()


@pytest.mark.asyncio
async def test_failed_conversion_can_be_retried(adapter_factory, message_payload):
    adapter = adapter_factory()
    convert = adapter._convert_to_abm
    adapter._convert_to_abm = AsyncMock(side_effect=RuntimeError("temporary"))
    adapter._dispatch_event(message_payload)
    await asyncio.gather(*list(adapter._tasks))
    adapter._convert_to_abm = convert
    adapter._dispatch_event(message_payload)
    await asyncio.gather(*list(adapter._tasks))
    assert adapter._event_queue.qsize() == 1
    await adapter.terminate()


@pytest.mark.parametrize(
    "kind,component", [("image", Image), ("file", File), ("audio", Record)]
)
@pytest.mark.asyncio
async def test_media_ownership_and_safe_filename(
    adapter_factory, message_payload, monkeypatch, kind, component
):
    adapter = adapter_factory()
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b"\x89PNG\r\n\x1a\ncontents"
    message_payload["event"]["message"].update(
        contentType=kind,
        content={f"{kind}Key": "media-key", "fileName": "../../恶意\\文件.txt"},
    )
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    assert isinstance(event.message_obj.message[0], component)
    assert len(event._temporary_local_files) == 1
    path = Path(event._temporary_local_files[0])
    assert path.is_relative_to(Path(adapter._temp_manager.base_dir))
    assert path.exists()
    assert adapter._temp_manager._file_records == {}
    if kind == "file":
        assert event.message_obj.message[0].name == "文件.txt"
        assert path.name == "文件.txt"
    adapter._temp_manager._cleanup()
    assert path.exists()
    event.cleanup_temporary_local_files()
    assert not path.exists()
    await adapter.terminate()


@pytest.mark.asyncio
async def test_reply_chain_and_html(adapter_factory, message_payload):
    adapter = adapter_factory()
    adapter._client.get_messages = AsyncMock(
        return_value=ApiResponse(
            1,
            data={
                "list": [
                    {
                        "msgId": "parent",
                        "senderId": "u",
                        "sendTime": 1716000000000,
                        "contentType": "text",
                        "content": {"text": "被引用内容"},
                    },
                ]
            },
        )
    )
    message_payload["event"]["message"].update(
        parentId="parent", contentType="html", content={"text": "<b>你好</b>"}
    )
    message = await adapter._convert_to_abm(message_payload)
    assert isinstance(message.message[0], Reply)
    assert message.message[0].chain[0].text == "被引用内容"
    assert message.message_str == "<b>你好</b>"
    assert adapter._client.get_messages.call_args.kwargs["message_id"] == "parent"
    await adapter.terminate()


@pytest.mark.asyncio
async def test_proactive_routing_uses_instance_id(adapter_factory, fake_client):
    adapter = adapter_factory(id="unique-instance")
    adapter._client = fake_client
    await adapter.send_by_session(
        MessageSesion("wrong-instance", MessageType.GROUP_MESSAGE, "g"),
        MessageChain([Plain("hi")]),
    )
    fake_client.send_message.assert_not_called()
    await adapter.send_by_session(
        MessageSesion("unique-instance", MessageType.GROUP_MESSAGE, "g"),
        MessageChain([Plain("hi")]),
    )
    assert fake_client.send_message.call_args.args[:2] == ("g", "group")
    assert adapter.metrics_called
    await adapter.terminate()


@pytest.mark.asyncio
async def test_webhook_validation_and_lifecycle(adapter_factory, message_payload):
    adapter = adapter_factory(
        connection_mode="webhook",
        webhook_host="127.0.0.1",
        webhook_port=0,
        webhook_secret="secret",
    )
    task = asyncio.create_task(adapter.run())
    for _ in range(100):
        if adapter._webhook_runner and adapter._webhook_runner.sites:
            site = next(iter(adapter._webhook_runner.sites))
            if site._server:
                break
        await asyncio.sleep(0.01)
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/webhook"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=message_payload) as response:
            assert response.status == 403
        async with session.post(url + "?secret=secret", json=[]) as response:
            assert response.status == 400
        async with session.post(
            url + "?secret=secret", json=message_payload
        ) as response:
            assert response.status == 200
        assert (
            await asyncio.wait_for(adapter._event_queue.get(), 2)
        ).message_str == "你好"
    assert not task.done()  # run 持续运行，直到 terminate
    dl_session = adapter._dl_session
    await adapter._client._get_session()
    api_session = adapter._client._session
    await adapter.terminate()
    await asyncio.wait_for(task, 2)
    assert dl_session.closed and api_session.closed
    assert not adapter._tasks
    assert adapter._webhook_runner is None


def test_invalid_configuration(adapter_factory):
    with pytest.raises(ValueError):
        adapter_factory(connection_mode="invalid")


@pytest.mark.parametrize(
    "address",
    [
        "?token=secret",
        "ws.jwzhd.com/subscribe",
        "/subscribe",
        "https://example.com/subscribe",
        "wss://",
        "wss://[invalid",
        "wss://example.com:bad/subscribe",
        "wss://example.com:999999/subscribe",
        42,
    ],
)
def test_invalid_websocket_address_fails_before_connect(adapter_factory, address):
    with pytest.raises(ValueError, match="websocket_url") as error:
        adapter_factory(websocket_url=address)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "url_mode", ["custom", "missing", "empty", "whitespace", "null"]
)
@pytest.mark.asyncio
async def test_websocket_protocol_and_resource_cleanup(
    adapter_factory, message_payload, monkeypatch, url_mode
):
    query = {}

    async def subscribe(request):
        query.update(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("invalid json")
        await ws.send_json([])
        await ws.send_json({"type": "message", "data": message_payload})
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/subscribe", subscribe)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    endpoint = f"ws://127.0.0.1:{port}/subscribe?existing=kept"
    monkeypatch.setitem(adapter_module.DEFAULT_CONFIG, "websocket_url", endpoint)
    configs = {
        "custom": {"websocket_url": f"  {endpoint}  "},
        "missing": {},
        "empty": {"websocket_url": ""},
        "whitespace": {"websocket_url": " \t\r\n "},
        "null": {"websocket_url": None},
    }
    adapter = adapter_factory(
        connection_mode="websocket",
        bot_token="token&中文",
        **configs[url_mode],
    )
    task = asyncio.create_task(adapter.run())
    try:
        event = await asyncio.wait_for(adapter._event_queue.get(), 2)
        assert event.message_str == "你好"
        assert query == {"existing": "kept", "token": "token&中文"}
        session, connection = adapter._ws_session, adapter._ws_connection
        assert not task.done()
        await adapter.terminate()
        await asyncio.wait_for(task, 2)
        assert session.closed and connection.closed
        assert adapter._ws_listen_task is None
    finally:
        await adapter.terminate()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_websocket_handshake_error_keeps_reason_and_redacts_token(
    adapter_factory, caplog
):
    async def reject(request):
        return web.Response(status=403, text="forbidden")

    app = web.Application()
    app.router.add_get("/subscribe", reject)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    adapter = adapter_factory(
        bot_token="token&中文",
        websocket_url=f"ws://127.0.0.1:{port}/subscribe",
    )
    caplog.set_level("INFO", logger="astrbot.test")
    task = asyncio.create_task(adapter.run())
    try:

        async def wait_for_warning():
            while "WebSocket 连接失败" not in caplog.text:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_warning(), 2)
        assert "WSServerHandshakeError" in caplog.text
        assert "403" in caplog.text
        assert f"ws://127.0.0.1:{port}/subscribe" in caplog.text
        assert "[redacted]" in caplog.text
        assert "token&中文" not in caplog.text
        assert "token%26" not in caplog.text
    finally:
        await adapter.terminate()
        await asyncio.wait_for(task, 2)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_pending_tasks_are_cancelled(adapter_factory, message_payload):
    adapter = adapter_factory()
    entered = asyncio.Event()

    async def slow_conversion(*args):
        entered.set()
        await asyncio.Event().wait()

    adapter._convert_to_abm = slow_conversion
    adapter._dispatch_event(message_payload)
    task = next(iter(adapter._tasks))
    await entered.wait()
    await adapter.terminate()
    assert task.cancelled()
    assert not adapter._pending_ids
