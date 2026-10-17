import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from astrbot.api.web import _request_context
from fastapi import FastAPI, Request
from yunhu_plugin.api.models import ApiResponse
from yunhu_plugin.main import YunhuPlugin
from yunhu_plugin.web import api as page_module


class Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = copy.deepcopy(dict(self))
        self.fail = False

    def save_config(self):
        if self.fail:
            raise OSError("cannot save")
        self.saved = copy.deepcopy(dict(self))


@pytest_asyncio.fixture
async def page_api(adapter_factory, tmp_path):
    one = adapter_factory(id="one", type="yunhu")
    two = adapter_factory(id="two", type="yunhu")
    content = tmp_path / "source.md"
    content.write_bytes("<script>alert(1)</script>\n文件正文".encode("utf-8"))
    record = one._file_store.add(content, "说明.md", "group", "100", "alice", "m1")
    other = two._file_store.add(content, "other.md", "user", "200", "bob", "m2")
    config = Config(platform=[one.config, two.config])
    context = SimpleNamespace(
        registered_web_apis=[],
        get_config=lambda: config,
        platform_manager=SimpleNamespace(get_insts=lambda: [one, two]),
    )
    context.register_web_api = lambda route, handler, methods, desc: (
        context.registered_web_apis.append((route, handler, methods, desc))
    )
    plugin = YunhuPlugin(context)
    app = FastAPI()

    def handler_route(handler):
        async def route(request: Request):
            async def payload(default=None):
                try:
                    return await request.json()
                except ValueError:
                    return default

            token = _request_context.set(
                SimpleNamespace(
                    username="admin"
                    if request.headers.get("authorization") == "test-session"
                    else None,
                    query=request.query_params,
                    json=payload,
                )
            )
            try:
                return await handler(**request.path_params)
            finally:
                _request_context.reset(token)

        return route

    for route, handler, methods, _ in context.registered_web_apis:
        app.add_api_route(
            route.replace("<file_id>", "{file_id}"),
            handler_route(handler),
            methods=methods,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": "test-session"},
    ) as client:
        yield SimpleNamespace(
            client=client,
            one=one,
            two=two,
            record=record,
            other=other,
            config=config,
            plugin=plugin,
            context=context,
        )
    await plugin.terminate()
    await one.terminate()
    await two.terminate()


ROOT = "/astrbot_plugin_yunhuAdapter"


@pytest.mark.asyncio
async def test_page_instances_and_filtered_file_catalog(page_api):
    p = page_api
    response = await p.client.get(ROOT + "/instances")
    assert {item["id"] for item in response.json()["instances"]} == {"one", "two"}
    assert "test-secret-token" not in response.text
    response = await p.client.get(
        ROOT + "/files", params={"instance_id": "one", "search": "说明", "limit": 1}
    )
    data = response.json()
    assert data["total"] == 1 and data["items"][0]["file_name"] == "说明.md"
    assert data["usage"]["max_bytes"] == 512 * 1024 * 1024
    assert data["conversations"] == [
        {"chat_type": "group", "chat_id": "100", "file_count": 1}
    ]
    assert "test-secret-token" not in json.dumps(data)
    assert (
        await p.client.get(
            ROOT + "/files", params={"instance_id": "one", "status": "pinned"}
        )
    ).json()["total"] == 0


@pytest.mark.parametrize(
    "query",
    [{"limit": "0"}, {"status": "unknown"}, {"offset": "-1"}, {"chat_type": "bot"}],
)
@pytest.mark.asyncio
async def test_file_filters_validate_before_database_access(page_api, query):
    response = await page_api.client.get(
        ROOT + "/files", params={"instance_id": "one", **query}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_preview_is_text_and_download_cleans_only_snapshot(page_api):
    p = page_api
    response = await p.client.get(
        f"{ROOT}/files/{p.record['file_id']}/preview", params={"instance_id": "one"}
    )
    assert response.json()["text"].startswith("<script>")
    assert response.headers["content-type"].startswith("application/json")
    response = await p.client.get(
        f"{ROOT}/files/{p.record['file_id']}/download", params={"instance_id": "one"}
    )
    assert response.content.decode() == "<script>alert(1)</script>\n文件正文"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "filename*=" in response.headers["content-disposition"]
    assert p.one._temp_manager._file_records == {}
    assert len(p.one._file_store.list("group", "100")) == 1


@pytest.mark.parametrize("path", ["preview", "download"])
@pytest.mark.asyncio
async def test_file_id_cannot_cross_selected_instance(page_api, path):
    p = page_api
    response = await p.client.get(
        f"{ROOT}/files/{p.other['file_id']}/{path}", params={"instance_id": "one"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_can_manage_files_in_selected_instance(page_api):
    p = page_api
    path = f"{ROOT}/files/{p.record['file_id']}/manage"
    assert (
        await p.client.post(path, json={"instance_id": "one", "action": "pin"})
    ).json()["pinned"] == 1
    assert (
        await p.client.post(path, json={"instance_id": "one", "action": "unpin"})
    ).json()["pinned"] == 0
    assert (
        await p.client.post(path, json={"instance_id": "two", "action": "delete"})
    ).status_code == 404
    assert (
        await p.client.post(path, json={"instance_id": "one", "action": "delete"})
    ).json()["deleted"]
    assert not p.one._file_store.list("group", "100")


@pytest.mark.asyncio
async def test_settings_persist_and_hot_apply_without_overwriting_secret(page_api):
    p = page_api
    result = await p.client.post(
        ROOT + "/settings/save",
        json={
            "instance_id": "one",
            "settings": {
                "file_retention_days": 30,
                "file_store_max_mb": 256,
                "file_store_max_count": 300,
                "bot_id": "11201781",
                "media_ttl": 100,
                "reply_in_thread": True,
            },
        },
    )
    assert result.status_code == 200 and result.json()["saved"]
    assert p.config.saved["platform"][0]["file_retention_days"] == 30
    assert p.one._file_store.retention_days == 30
    assert p.one._file_store.max_bytes == 256 * 1024 * 1024
    assert p.one._temp_manager.ttl == 100
    assert p.one.client_self_id == "11201781"
    assert p.one.reply_in_thread
    assert p.two._file_store.retention_days == 7
    assert p.config["platform"][0]["bot_token"] == "test-secret-token"
    assert "test-secret-token" not in result.text


@pytest.mark.parametrize(
    "settings",
    [
        {"bot_token": "x"},
        {"file_store_dir": "/arbitrary"},
        {"file_retention_days": -1},
        {"media_ttl": True},
        {"reply_in_thread": "false"},
        {"bot_id": "yunhu_hash"},
    ],
)
@pytest.mark.asyncio
async def test_settings_reject_unsupported_fields_or_values(page_api, settings):
    response = await page_api.client.post(
        ROOT + "/settings/save", json={"instance_id": "one", "settings": settings}
    )
    assert response.status_code == 400
    assert page_api.config.saved["platform"][0].get("file_retention_days", 7) == 7


@pytest.mark.asyncio
async def test_failed_config_save_restores_memory_and_running_policy(page_api):
    p = page_api
    original = copy.deepcopy(p.config["platform"][0])
    p.config.fail = True
    response = await p.client.post(
        ROOT + "/settings/save",
        json={"instance_id": "one", "settings": {"file_retention_days": 90}},
    )
    assert response.status_code == 500
    assert p.config["platform"][0] == original
    assert p.one._file_store.retention_days == 7


@pytest.mark.asyncio
async def test_operations_use_selected_bot_and_redact_remote_response(page_api):
    p = page_api
    p.one._client.gag_member = AsyncMock(
        return_value=ApiResponse(1, data={"echo": "test-secret-token"})
    )
    p.two._client.gag_member = AsyncMock()
    response = await p.client.post(
        ROOT + "/operations/run",
        json={
            "instance_id": "one",
            "chat_id": "123",
            "chat_type": "group",
            "operation": "mute",
            "arguments": {"user_id": "456", "duration": 600},
        },
    )
    assert response.json()["ok"]
    assert "test-secret-token" not in response.text
    p.one._client.gag_member.assert_awaited_once_with("123", "456", 600)
    p.two._client.gag_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_operation_does_not_need_fake_chat_target(page_api):
    p = page_api
    p.one._client.set_board_all = AsyncMock(return_value=ApiResponse(1))
    response = await p.client.post(
        ROOT + "/operations/run",
        json={
            "instance_id": "one",
            "operation": "global_board",
            "arguments": {"action": "set", "text": "notice"},
        },
    )
    assert response.json()["ok"]
    p.one._client.set_board_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_instance_and_operation_are_rejected(page_api):
    assert (
        await page_api.client.get(ROOT + "/settings", params={"instance_id": "other"})
    ).status_code == 404
    assert (
        await page_api.client.post(
            ROOT + "/operations/run",
            json={"instance_id": "one", "operation": "manage_file"},
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "path,method",
    [
        ("instances", "GET"),
        ("files", "GET"),
        ("settings", "GET"),
        ("settings/save", "POST"),
        ("cleanup", "POST"),
        ("operations/run", "POST"),
        ("files/00000000000000000000000000000000/preview", "GET"),
        ("files/00000000000000000000000000000000/download", "GET"),
        ("files/00000000000000000000000000000000/manage", "POST"),
    ],
)
@pytest.mark.asyncio
async def test_page_endpoints_require_dashboard_login(page_api, path, method):
    response = await page_api.client.request(
        method, f"{ROOT}/{path}", headers={"authorization": ""}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_plugin_unload_removes_only_its_registered_apis(page_api):
    p = page_api
    other = ("/other/ping", lambda: None, ["GET"], "other")
    p.context.registered_web_apis.append(other)
    await p.plugin.terminate()
    assert p.context.registered_web_apis == [other]


@pytest.mark.asyncio
async def test_cancelled_download_finishes_and_removes_snapshot(page_api, monkeypatch):
    p = page_api
    entered, release, copied = threading.Event(), threading.Event(), threading.Event()
    original = page_module.shutil.copyfile

    def slow_copy(source, target):
        entered.set()
        assert release.wait(3)
        try:
            return original(source, target)
        finally:
            copied.set()

    monkeypatch.setattr(page_module.shutil, "copyfile", slow_copy)
    token = _request_context.set(
        SimpleNamespace(username="admin", query={"instance_id": "one"})
    )
    task = asyncio.create_task(p.plugin.page_api.download(p.record["file_id"]))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        _request_context.reset(token)
    assert await asyncio.to_thread(copied.wait, 3)
    for _ in range(50):
        if not p.one._temp_manager._file_records:
            break
        await asyncio.sleep(0.01)
    assert p.one._temp_manager._file_records == {}
    assert p.one._file_store.record(p.record["file_id"])


@pytest.mark.asyncio
async def test_invalid_docx_preview_returns_readable_error(page_api, tmp_path):
    source = tmp_path / "broken.docx"
    source.write_bytes(b"not a zip archive")
    record = page_api.one._file_store.add(
        source, "broken.docx", "group", "100", "alice", "broken"
    )
    response = await page_api.client.get(
        f"{ROOT}/files/{record['file_id']}/preview", params={"instance_id": "one"}
    )
    assert response.status_code == 400
    assert "文件格式无效" in response.json()["message"]
