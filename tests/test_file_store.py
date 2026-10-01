import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import File, Plain
from yunhu_plugin.media.attachments import read_attachment
from yunhu_plugin.media.store import FileStore
from yunhu_plugin.services.llm_inputs import enrich_yunhu_inputs
from yunhu_plugin.services.operations import YunhuOperations, parse_command


def create_store(tmp_path, now, **kwargs):
    return FileStore(tmp_path / "store", "instance-1", clock=lambda: now[0], **kwargs)


def save(
    store,
    tmp_path,
    message_id="m1",
    sender="u1",
    chat="g1",
    kind="group",
    body=b"content",
):
    path = tmp_path / "source.txt"
    path.write_bytes(body)
    return store.add(path, "test.md", kind, chat, sender, message_id)


def test_store_retains_original_names_across_restart_and_scopes_sessions(tmp_path):
    now = [1000]
    store = create_store(tmp_path, now)
    first = save(store, tmp_path)
    now[0] += 1
    second = save(store, tmp_path, message_id="m2", body=b"second")
    assert first["file_id"] != second["file_id"]
    again = create_store(tmp_path, now)
    assert len(again.list("group", "g1")) == 2
    assert again.list("group", "g2") == []
    assert again.list("user", "g1") == []
    other = FileStore(tmp_path / "store", "instance-2")
    assert other.list("group", "g1") == []
    with pytest.raises(ValueError, match="唯一"):
        with again.open_file("group", "g1", file_name="test.md"):
            pass
    with again.open_file("group", "g1", file_id=first["file_id"]) as (record, path):
        assert path.name == record["file_name"] == "test.md"
        assert path.read_bytes() == b"content"


def test_expiry_and_pinned_files_obey_quota_without_silent_pinned_deletion(tmp_path):
    now = [1000]
    store = create_store(tmp_path, now, retention_days=1, max_bytes=12)
    first = save(store, tmp_path, body=b"first!")
    pinned = store.manage("group", "g1", first["file_id"], "pin", "u1")
    assert pinned["pinned"] and pinned["expires_at"] is None
    now[0] += 1
    second = save(store, tmp_path, "m2", body=b"second")
    now[0] += 1
    third = save(store, tmp_path, "m3", body=b"third!")
    assert {r["file_id"] for r in store.list("group", "g1")} == {
        first["file_id"],
        third["file_id"],
    }
    assert second["file_id"] not in {r["file_id"] for r in store.list("group", "g1")}
    with pytest.raises(ValueError, match="空间不足"):
        save(store, tmp_path, "too-large", body=b"1234567")
    assert len(store.list("group", "g1")) == 2
    now[0] += 86401
    assert [r["file_id"] for r in store.list("group", "g1")] == [first["file_id"]]
    released = store.manage("group", "g1", first["file_id"], "unpin", "u1")
    assert not released["pinned"] and released["expires_at"] == now[0] + 86400
    now[0] += 86401
    assert store.list("group", "g1") == []


def test_count_limit_includes_zero_byte_files_and_disabled_retention(tmp_path):
    now = [1000]
    store = create_store(tmp_path, now, max_files=1)
    first = save(store, tmp_path, body=b"")
    now[0] += 1
    second = save(store, tmp_path, "m2", body=b"")
    assert [r["file_id"] for r in store.list("group", "g1")] == [second["file_id"]]
    assert first["file_id"] != second["file_id"]
    disabled = FileStore(tmp_path / "disabled", "instance", retention_days=0)
    assert save(disabled, tmp_path) is None
    assert disabled.list("group", "g1") == []


def test_management_requires_uploader_or_current_chat_admin(tmp_path):
    now = [1000]
    store = create_store(tmp_path, now)
    record = save(store, tmp_path)
    with pytest.raises(PermissionError):
        store.manage("group", "g1", record["file_id"], "delete", "other")
    with pytest.raises(ValueError):
        store.manage("group", "g2", record["file_id"], "delete", "u1", True)
    assert store.manage("group", "g1", record["file_id"], "delete", "admin", True)[
        "deleted"
    ]
    assert store.list("group", "g1") == []


def test_cleanup_waits_for_active_store_reader(tmp_path):
    now = [1000]
    store = create_store(tmp_path, now, retention_days=1)
    record = save(store, tmp_path)
    attempted = threading.Event()
    completed = threading.Event()

    def clean():
        attempted.set()
        store.cleanup()
        completed.set()

    with store.open_file("group", "g1", record["file_id"]) as (_, path):
        now[0] += 86401
        thread = threading.Thread(target=clean)
        thread.start()
        assert attempted.wait(2)
        assert not completed.wait(0.05)
        assert path.is_file()
    thread.join(timeout=2)
    assert completed.is_set() and not path.exists()


@pytest.mark.parametrize("chat_type", ["group", "bot"])
@pytest.mark.asyncio
async def test_receive_cleanup_and_restart_still_read_by_original_name(
    adapter_factory, message_payload, chat_type
):
    adapter = adapter_factory(id="persistent")
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b"document content"
    message_payload["event"]["chat"] = {
        "chatId": "group-1" if chat_type == "group" else "bot-1",
        "chatType": chat_type,
    }
    message_payload["event"]["message"].update(
        contentType="file", content={"fileKey": "hash.md", "fileName": "test.md"}
    )
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    component = event.get_messages()[0]
    assert isinstance(component, File)
    assert Path(component.file_).name == component.name == "test.md"
    record = event.get_extra("yunhu_stored_files")[0]
    event.cleanup_temporary_local_files()
    assert not Path(component.file_).exists()
    await adapter.terminate()
    restarted = adapter_factory(id="persistent")
    message_payload["event"]["message"].update(
        msgId="next", contentType="text", content={"text": "能读取test.md吗？"}
    )
    await restarted._process_message(message_payload)
    next_event = restarted._event_queue.get_nowait()
    result = await read_attachment(next_event, file_name="test.md")
    assert result["text"] == "document content" and result["source"] == "stored"
    req = SimpleNamespace(extra_user_content_parts=[])
    await enrich_yunhu_inputs(next_event, req)
    assert "test.md" in req.extra_user_content_parts[0].text
    operations = YunhuOperations()
    action, params = parse_command(f"保留文件 {record['file_id']}")
    assert (await operations.execute(next_event, action, **params))["ok"]
    listed = await operations.execute(next_event, "stored_files")
    assert listed["data"][0]["pinned"]
    with pytest.raises(ValueError):
        await read_attachment(next_event, file_name="test.md", source="current")
    await restarted.terminate()


@pytest.mark.asyncio
async def test_retention_full_keeps_current_file_readable_and_explains_failure(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    adapter._file_store.max_bytes = 2
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b"content too large"
    message_payload["event"]["message"].update(
        contentType="file", content={"fileKey": "hash.md", "fileName": "test.md"}
    )
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    assert not event.get_extra("yunhu_stored_files")
    assert "空间不足" in event.get_extra("yunhu_file_store_failures")[0]["reason"]
    component = event.get_messages()[0]
    component.get_file = AsyncMock(return_value=component.file_)
    assert (await read_attachment(event))["text"] == "content too large"
    event.cleanup_temporary_local_files()
    await adapter.terminate()


@pytest.mark.asyncio
async def test_file_management_tool_denies_other_member(event_factory, tmp_path):
    now = [1000]
    store = create_store(tmp_path, now)
    record = save(store, tmp_path, sender="other", chat="group-1")
    event = event_factory()
    event.message_obj.message = [Plain("命令")]
    event.set_extra("yunhu_file_store", store)
    result = await YunhuOperations().execute(
        event, "manage_file", file_id=record["file_id"], action="delete"
    )
    assert not result["ok"] and "自己上传" in result["message"]
    event.set_extra("yunhu_sender_level", "administrator")
    assert (
        await YunhuOperations().execute(
            event, "manage_file", file_id=record["file_id"], action="delete"
        )
    )["ok"]
