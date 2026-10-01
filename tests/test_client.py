import asyncio
import time

import pytest
import pytest_asyncio
from aiohttp import web
from yunhu_plugin.api.client import YunhuClient
from yunhu_plugin.api.models import Button, ButtonGroup


@pytest_asyncio.fixture
async def api_server():
    requests = []
    received = asyncio.Event()

    async def handler(request):
        entry = {
            "path": request.path,
            "query": dict(request.query),
            "time": time.monotonic(),
        }
        if request.path.endswith("send-stream"):
            entry["chunks"] = []
            assert request.headers["Transfer-Encoding"] == "chunked"
            requests.append(entry)
            async for chunk in request.content.iter_any():
                entry["chunks"].append(chunk)
                received.set()
        elif request.content_type == "multipart/form-data":
            form = await request.multipart()
            part = await form.next()
            entry.update(
                field=part.name, filename=part.filename, body=bytes(await part.read())
            )
            requests.append(entry)
        else:
            entry["json"] = await request.json() if request.method == "POST" else None
            requests.append(entry)
        return web.json_response(
            {"code": 1, "message": "success", "data": {"imageKey": "key"}}
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = YunhuClient("token&?中文", base_url=f"http://127.0.0.1:{port}")
    try:
        yield client, requests, received, app
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_send_buttons_mentions_and_token_encoding(api_server):
    client, requests, *_ = api_server
    response = await client.send_text(
        "g",
        "group",
        "@张三 你好",
        parent_id="parent",
        buttons=[ButtonGroup([Button("确认", value="yes")])],
        at=["u", "u"],
    )
    assert response.ok and response.msg == "success"
    assert requests[0]["query"]["token"] == "token&?中文"
    assert requests[0]["json"] == {
        "recvId": "g",
        "recvType": "group",
        "contentType": "text",
        "parentId": "parent",
        "content": {
            "text": "@张三 你好",
            "at": ["u"],
            "buttons": [[{"text": "确认", "actionType": 3, "value": "yes"}]],
        },
    }


@pytest.mark.asyncio
async def test_message_list_official_query(api_server):
    client, requests, *_ = api_server
    await client.get_messages("g", "group", before=2, after=3, message_id="m")
    assert requests[0]["query"] == {
        "token": client.token,
        "chat-id": "g",
        "chat-type": "group",
        "message-id": "m",
        "before": "2",
        "after": "3",
    }
    await client.get_messages("u", "user", limit=7)
    assert requests[1]["query"]["before"] == "7"
    assert "limit" not in requests[1]["query"]


@pytest.mark.parametrize(
    "method,args,path,payload",
    [
        (
            "batch_send",
            (["u"], "user", "text", {"text": "hello"}),
            "/bot/batch_send",
            {
                "recvIds": ["u"],
                "recvType": "user",
                "contentType": "text",
                "content": {"text": "hello"},
            },
        ),
        (
            "gag_member",
            ("g", "u", 0),
            "/group/gag-member",
            {"groupId": "g", "userId": "u", "gag": 0},
        ),
        (
            "remove_member",
            ("g", "u"),
            "/group/remove-member",
            {"groupId": "g", "userId": "u"},
        ),
        (
            "set_message_type_limit",
            ("g", "text,image"),
            "/group/msg-type-limit",
            {"groupId": "g", "type": "text,image"},
        ),
        ("list_tags", ("g",), "/group/tag/list", {"groupId": "g"}),
        (
            "delete_tag",
            ("g", "VIP"),
            "/group/tag/delete",
            {"groupId": "g", "tag": "VIP"},
        ),
        (
            "add_user_tag",
            ("g", "u", "VIP"),
            "/group/tag/user-relate",
            {"groupId": "g", "userId": "u", "tag": "VIP"},
        ),
        (
            "remove_user_tag",
            ("g", "u", "VIP"),
            "/group/tag/user-relate-cancel",
            {"groupId": "g", "userId": "u", "tag": "VIP"},
        ),
        (
            "recall_message",
            ("m", "g", "group"),
            "/bot/recall",
            {"msgId": "m", "chatId": "g", "chatType": "group"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_official_paths_and_payloads(api_server, method, args, path, payload):
    client, requests, *_ = api_server
    assert (await getattr(client, method)(*args)).ok
    assert requests[0]["path"] == path
    assert requests[0]["json"] == payload


@pytest.mark.asyncio
async def test_boards_and_edit_tag(api_server):
    client, requests, *_ = api_server
    await client.set_board(
        "g", "group", "html", "<b>hi</b>", member_id="u", expire_time=1800000000
    )
    assert requests[-1]["json"]["expireTime"] == 1800000000
    await client.set_board_all(content_type="markdown", content="hi", expire_time=0)
    assert requests[-1]["json"] == {
        "contentType": "markdown",
        "content": "hi",
        "expireTime": 0,
    }
    await client.edit_tag("g", "VIP", sort=0, desc="")
    assert requests[-1]["json"] == {"groupId": "g", "tag": "VIP", "sort": 0, "desc": ""}


@pytest.mark.parametrize("kind", ["image", "file", "video"])
@pytest.mark.asyncio
async def test_real_multipart_upload(api_server, tmp_path, kind):
    client, requests, *_ = api_server
    path = tmp_path / "文件.bin"
    path.write_bytes(b"media contents")
    response = await getattr(client, f"upload_{kind}")(str(path))
    assert response.ok
    assert requests[0]["field"] == kind
    assert requests[0]["body"] == b"media contents"
    assert client._session is not None


@pytest.mark.asyncio
async def test_upload_size_limit_before_network(api_server, tmp_path):
    client, requests, *_ = api_server
    path = tmp_path / "large.png"
    with path.open("wb") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    response = await client.upload_image(str(path))
    assert response.code == 1002
    assert requests == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("规定.md", "规定.md"),
        ("测试 文档+100%.md", "测试 文档+100%.md"),
        ("test.md", "test.md"),
        ("%E8%A7%84%E5%AE%9A.md", "规定.md"),
    ],
)
@pytest.mark.asyncio
async def test_multipart_upload_preserves_original_unicode_filename(
    api_server, tmp_path, name, expected
):
    client, requests, *_ = api_server
    path = tmp_path / "source.md"
    path.write_text("body", encoding="utf-8")
    assert (await client.upload_file(str(path), filename=name)).ok
    assert requests[0]["filename"] == expected
    assert requests[0]["body"] == b"body"


@pytest.mark.asyncio
async def test_stream_delivers_before_eof(api_server):
    client, requests, received, _ = api_server
    stream = await client.send_stream("u", "user", "markdown")
    await stream.write("第一段🙂")
    await asyncio.wait_for(received.wait(), 2)
    assert not stream._task.done()
    await stream.write("第二段")
    assert (await asyncio.wait_for(stream.write_eof(), 2)).ok
    assert b"".join(requests[0]["chunks"]).decode() == "第一段🙂第二段"
    assert requests[0]["query"]["recvId"] == "u"
    with pytest.raises(RuntimeError):
        await stream.write("closed")


@pytest.mark.asyncio
async def test_stream_cancel_and_client_close(api_server):
    client, _, received, _ = api_server
    stream = await client.send_stream("u", "user")
    await stream.write("partial")
    await asyncio.wait_for(received.wait(), 2)
    session = client._session
    await client.close()
    assert stream._task.done()
    assert session.closed


@pytest.mark.asyncio
async def test_rate_limit_concurrent_requests(api_server):
    client, requests, *_ = api_server
    await asyncio.gather(*(client.send_text("u", "user", str(i)) for i in range(11)))
    times = sorted(item["time"] for item in requests)
    assert times[-1] - times[0] >= 0.9


@pytest.mark.asyncio
async def test_invalid_http_response_is_not_success():
    app = web.Application()

    async def bad_response(request):
        return web.Response(text="<html>error</html>", status=502)

    app.router.add_post("/bad", bad_response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = YunhuClient("secret", base_url=f"http://127.0.0.1:{port}")
    try:
        response = await client._post("/bad", {})
        assert not response.ok and "502" in response.msg
    finally:
        await client.close()
        await runner.cleanup()


def test_error_redacts_encoded_token():
    client = YunhuClient("token&中文")
    error = RuntimeError(
        "url https://example.com/api?token=token%26%E4%B8%AD%E6%96%87&other=1"
    )
    assert "token%26" not in client._safe_error(error)
    assert "[redacted]" in client._safe_error(error)
