import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import aiohttp
import pytest
from aiohttp import web
from astrbot.api.message_components import At, File, Plain, Reply
from yunhu_plugin.api.models import ApiResponse
from yunhu_plugin.media import cdn as cdn_proxy


@pytest.mark.parametrize("chat_type", ["group", "bot"])
@pytest.mark.parametrize(
    "declared,expected",
    [
        ("test.md", "test.md"),
        ("%E8%A7%84%E5%AE%9A.md", "规定.md"),
        ("98f611dd09af4712a6a9a21864c71292.md", "测试文档.md"),
        (None, "测试文档.md"),
    ],
)
@pytest.mark.asyncio
async def test_receive_preserves_filename_in_component_path_and_storage(
    adapter_factory, message_payload, monkeypatch, chat_type, declared, expected
):
    async def response(request):
        return web.Response(
            body=b"body",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote("测试文档.md")
            },
        )

    app = web.Application()
    app.router.add_get("/hash.md", response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setitem(
        cdn_proxy._CDN_ROUTES,
        "file",
        {"base_url": f"http://127.0.0.1:{port}", "host": "127.0.0.1"},
    )
    adapter = adapter_factory()
    adapter._dl_session = aiohttp.ClientSession()
    adapter._cdn_proxy = cdn_proxy.CdnProxy(adapter._dl_session)
    message_payload["event"]["chat"] = {
        "chatId": "group-1" if chat_type == "group" else "bot-1",
        "chatType": chat_type,
    }
    message_payload["event"]["message"].update(
        contentType="file", content={"fileKey": "hash.md", "fileName": declared}
    )
    try:
        await adapter._process_message(message_payload)
        event = adapter._event_queue.get_nowait()
        file = event.get_messages()[0]
        assert file.name == Path(file.file_).name == expected
        assert event.message_str == f"[文件: {expected}]"
        assert event.get_extra("yunhu_stored_files")[0]["file_name"] == expected
        event.cleanup_temporary_local_files()
    finally:
        await adapter.terminate()
        await runner.cleanup()


@pytest.mark.parametrize("method", ["_download_builtin", "_download_custom"])
@pytest.mark.parametrize("kind", ["file", "image"])
@pytest.mark.asyncio
async def test_cdn_allows_html_files_but_rejects_html_instead_of_images(
    monkeypatch, method, kind
):
    body = b"<!DOCTYPE html><html>document content</html>"

    async def response(request):
        return web.Response(body=body)

    app = web.Application()
    app.router.add_get("/{path:.*}", response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setitem(
        cdn_proxy._CDN_ROUTES, kind, {"base_url": base, "host": "127.0.0.1"}
    )
    try:
        async with aiohttp.ClientSession() as session:
            proxy = cdn_proxy.CdnProxy(session, custom_proxy_base=base)
            data = await getattr(proxy, method)("document.html", kind)
            assert data == (body if kind == "file" else None)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_prefix_text_precedes_mentions_of_other_users(
    adapter_factory, message_payload
):
    adapter = adapter_factory(bot_id="11201781")
    message_payload["event"]["message"]["content"] = {
        "text": "*你能识别我@他了吗？",
        "at": [3141766],
    }
    message = await adapter._convert_to_abm(message_payload)
    assert isinstance(message.message[0], Plain)
    assert [c.qq for c in message.message if isinstance(c, At)] == ["3141766"]
    assert message.message_str.startswith("*")
    await adapter.terminate()


@pytest.mark.asyncio
async def test_unknown_identity_is_explicit_then_private_chat_learns_bot(
    adapter_factory, message_payload, caplog
):
    adapter = adapter_factory(bot_id="")
    assert adapter.client_self_id == ""
    group = await adapter._convert_to_abm(message_payload)
    assert not group.self_id
    assert "bot_id 未配置" in caplog.text
    private = copy.deepcopy(message_payload)
    private["event"]["chat"] = {"chatId": "11201781", "chatType": "bot"}
    await adapter._convert_to_abm(private)
    assert adapter.bot_id == "11201781"
    message_payload["event"]["message"]["content"]["at"] = ["11201781"]
    group = await adapter._convert_to_abm(message_payload)
    assert group.self_id == "11201781"
    await adapter.terminate()


@pytest.mark.parametrize("bot_id", ["yunhu_915e97adcccc0e99", "915e97adcccc0e99"])
def test_placeholder_id_cannot_be_configured_as_real_bot_id(adapter_factory, bot_id):
    with pytest.raises(ValueError, match="占位ID"):
        adapter_factory(bot_id=bot_id)


@pytest.mark.parametrize("chat_type", ["group", "bot"])
@pytest.mark.asyncio
async def test_relative_file_download_and_quoted_attachment_after_cleanup(
    adapter_factory, message_payload, monkeypatch, chat_type
):
    body = b"<html><body>test content</body></html>"
    received = []

    async def file_response(request):
        received.append(request.path)
        return web.Response(body=body)

    app = web.Application()
    app.router.add_get("/hash.html", file_response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setitem(
        cdn_proxy._CDN_ROUTES,
        "file",
        {"base_url": f"http://127.0.0.1:{port}", "host": "127.0.0.1"},
    )
    adapter = adapter_factory()
    adapter._dl_session = aiohttp.ClientSession()
    adapter._cdn_proxy = cdn_proxy.CdnProxy(adapter._dl_session)
    message_payload["event"]["chat"] = {
        "chatId": "group-1" if chat_type == "group" else "bot-1",
        "chatType": chat_type,
    }
    message_payload["event"]["message"].update(
        contentType="file", content={"fileUrl": "hash.html", "fileName": "文档.html"}
    )
    adapter._client.get_messages = AsyncMock(
        side_effect=AssertionError("quote should use cached metadata")
    )
    try:
        await adapter._process_message(message_payload)
        first = adapter._event_queue.get_nowait()
        component = first.get_messages()[0]
        assert isinstance(component, File)
        original_path = Path(component.file_)
        assert original_path.read_bytes() == body
        first.cleanup_temporary_local_files()
        assert not original_path.exists()
        quote = copy.deepcopy(message_payload)
        quote["event"]["message"].update(
            msgId="follow-up",
            parentId="msg-1",
            contentType="text",
            content={"text": "你能看到这个文件吗？"},
        )
        await adapter._process_message(quote)
        second = adapter._event_queue.get_nowait()
        reply = second.get_messages()[0]
        assert isinstance(reply, Reply) and isinstance(reply.chain[0], File)
        path = Path(reply.chain[0].file_)
        assert path != original_path and path.read_bytes() == body
        assert "文档.html" in reply.message_str
        assert str(path) in second._temporary_local_files
        assert received == ["/hash.html", "/hash.html"]
        second.cleanup_temporary_local_files()
    finally:
        await adapter.terminate()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_history_json_string_restores_quoted_file(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b"quoted file content"
    adapter._client.get_messages = AsyncMock(
        return_value=ApiResponse(
            1,
            data={
                "list": [
                    {
                        "msgId": "parent",
                        "contentType": "file",
                        "senderId": "u",
                        "content": json.dumps(
                            {"fileUrl": "hash.txt", "fileName": "引用.txt"}
                        ),
                    }
                ]
            },
        )
    )
    message_payload["event"]["message"]["parentId"] = "parent"
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    reply = event.get_messages()[0]
    assert isinstance(reply.chain[0], File)
    assert Path(reply.chain[0].file_).read_bytes() == b"quoted file content"
    event.cleanup_temporary_local_files()
    await adapter.terminate()


@pytest.mark.asyncio
async def test_quote_cache_is_scoped_to_chat(adapter_factory, message_payload):
    adapter = adapter_factory()
    await adapter._convert_to_abm(message_payload)
    adapter._client.get_messages = AsyncMock(
        return_value=ApiResponse(1, data={"list": []})
    )
    reply = await adapter._resolve_reply("msg-1", "other-group", "group")
    assert "暂不可用" in reply.message_str and not reply.chain
    adapter._client.get_messages.assert_awaited_once_with(
        "other-group", "group", before=0, after=0, message_id="msg-1"
    )
    await adapter.terminate()


@pytest.mark.asyncio
async def test_failed_file_download_is_explicit_and_never_passed_to_agent_as_file(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = None
    message_payload["event"]["chat"] = {"chatId": "bot-1", "chatType": "bot"}
    message_payload["event"]["message"].update(
        contentType="file",
        content={"fileUrl": "broken.html", "fileName": "broken.html"},
    )
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    assert not any(isinstance(c, File) for c in event.get_messages())
    assert "下载失败" in event.message_str
    await adapter.terminate()


@pytest.mark.asyncio
async def test_empty_file_is_valid_attachment(adapter_factory, message_payload):
    adapter = adapter_factory()
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b""
    message_payload["event"]["message"].update(
        contentType="file", content={"fileKey": "empty.txt", "fileName": "empty.txt"}
    )
    message = await adapter._convert_to_abm(message_payload)
    assert isinstance(message.message[0], File)
    assert Path(message.message[0].file_).stat().st_size == 0
    await adapter.terminate()
