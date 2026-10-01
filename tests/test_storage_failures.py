import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Plain
from yunhu_plugin.media.store import FileStore
from yunhu_plugin.services.llm_inputs import enrich_yunhu_inputs
from yunhu_plugin.services.operations import YunhuOperations


@pytest.mark.asyncio
async def test_storage_failure_does_not_abort_llm_request(event_factory):
    event = event_factory()
    event.message_obj.self_id = "bot-1"
    event.message_obj.message = [Plain("hello")]

    def broken_list(*args):
        raise sqlite3.OperationalError("full")

    event.set_extra("yunhu_file_store", SimpleNamespace(list=broken_list))
    req = SimpleNamespace(extra_user_content_parts=[])
    await enrich_yunhu_inputs(event, req)
    assert "文件库暂不可用" in req.extra_user_content_parts[0].text
    result = await YunhuOperations().execute(event, "stored_files")
    assert not result["ok"] and "OperationalError" in result["message"]


def test_archive_keeps_zip_bytes_without_treating_it_as_text(tmp_path):
    store = FileStore(tmp_path / "store", "instance")
    source = tmp_path / "archive.zip"
    body = b"PK\x03\x04\x00binary"
    source.write_bytes(body)
    record = store.add(source, "源码.zip", "user", "u1", "u1", "m1")
    with store.open_file("user", "u1", record["file_id"]) as (_, path):
        assert path.name == "源码.zip" and path.read_bytes() == body


def test_invalid_index_cannot_clean_outside_managed_directory(tmp_path):
    store = FileStore(tmp_path / "store", "instance")
    source = tmp_path / "source.md"
    source.write_bytes(b"data")
    record = store.add(source, "test.md", "group", "g1", "u1", "m1")
    outside = tmp_path / "valuable.md"
    outside.write_bytes(b"valuable")
    with store._db() as db:
        db.execute(
            "UPDATE files SET file_name=?,expires_at=0 WHERE file_id=?",
            (str(outside), record["file_id"]),
        )
    with pytest.raises(ValueError, match="路径无效"):
        store.cleanup()
    assert outside.read_bytes() == b"valuable"


@pytest.mark.asyncio
async def test_late_event_cleanup_after_termination_removes_temp_directories(
    adapter_factory, message_payload
):
    adapter = adapter_factory()
    adapter._cdn_proxy = AsyncMock()
    adapter._cdn_proxy.download.return_value = b"document"
    message_payload["event"]["message"].update(
        contentType="file", content={"fileKey": "hash.md", "fileName": "test.md"}
    )
    await adapter._process_message(message_payload)
    event = adapter._event_queue.get_nowait()
    root = Path(adapter._temp_manager.base_dir)
    await adapter.terminate()
    assert root.exists()
    event.cleanup_temporary_local_files()
    assert not root.exists()
    assert adapter._file_store.list("group", "group-1")
