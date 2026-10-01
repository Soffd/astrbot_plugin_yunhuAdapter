import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import At, File, Plain, Reply
from yunhu_plugin.main import YunhuPlugin
from yunhu_plugin.media.attachments import attachment_info, read_attachment
from yunhu_plugin.services.operations import YunhuOperations, parse_command


def local_file(tmp_path, name, body):
    path = tmp_path / name
    path.write_bytes(body)
    component = File(name=name, file=str(path))
    component.get_file = AsyncMock(return_value=str(path))
    return component


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "gb18030"])
async def test_attachment_read_preserves_encoding_and_reports_truncation(
    event_factory, tmp_path, encoding
):
    event = event_factory()
    component = local_file(tmp_path, "内容.txt", "你好云湖\n正文".encode(encoding))
    event.message_obj.message = [component]
    result = await read_attachment(event, max_chars=4)
    assert result["text"] == "你好云湖"
    assert result["truncated"] is True
    assert result["source"] == "current"
    assert result["size"] == len("你好云湖\n正文".encode(encoding))


@pytest.mark.asyncio
async def test_attachment_selection_scopes_current_and_quoted_files(
    event_factory, tmp_path
):
    event = event_factory()
    current = local_file(tmp_path, "当前.html", b"<html>current</html>")
    quoted = local_file(tmp_path, "引用.txt", b"quoted text")
    event.message_obj.message = [current, Reply(id="parent", chain=[quoted])]
    assert attachment_info(event) == [
        {"file_name": "当前.html", "source": "current"},
        {"file_name": "引用.txt", "source": "quoted"},
    ]
    with pytest.raises(ValueError, match="唯一附件"):
        await read_attachment(event)
    with pytest.raises(ValueError, match="唯一附件"):
        await read_attachment(event, file_name=str(tmp_path / "引用.txt"))
    result = await read_attachment(event, source="quoted")
    assert result["text"] == "quoted text"
    assert result["source"] == "quoted"
    current.get_file.assert_not_awaited()
    action, arguments = parse_command("读文件 当前.html")
    result = await YunhuOperations().execute(event, action, **arguments)
    assert result["ok"] and result["data"]["text"] == "<html>current</html>"
    assert parse_command("文件列表") == ("attachments", {})


@pytest.mark.asyncio
async def test_docx_read_extracts_paragraphs(event_factory, tmp_path):
    path = tmp_path / "内容.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>第二段</w:t></w:r></w:p></w:body></w:document>",
        )
    component = File(name=path.name, file=str(path))
    component.get_file = AsyncMock(return_value=str(path))
    event = event_factory()
    event.message_obj.message = [component]
    result = await read_attachment(event)
    assert result["text"] == "第一段\n第二段"
    assert result["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,body,expected",
    [("附件.pdf", b"%PDF-binary", "解析/OCR"), ("附件.txt", b"\x00binary", "二进制")],
)
async def test_unreadable_file_returns_clear_tool_failure(
    event_factory, tmp_path, name, body, expected
):
    event = event_factory()
    event.message_obj.message = [local_file(tmp_path, name, body)]
    result = await YunhuOperations().execute(event, "read_attachment")
    assert not result["ok"] and expected in result["message"]


@pytest.mark.asyncio
async def test_llm_hook_adds_mentions_and_attachment_body_without_changing_prompt(
    event_factory, tmp_path
):
    event = event_factory()
    event.message_obj.self_id = "11201781"
    event.message_str = "*你能看到这个文件吗"
    current = local_file(tmp_path, "当前.html", "<html>当前内容</html>".encode())
    quoted = local_file(tmp_path, "引用.txt", "引用内容".encode())
    event.message_obj.message = [
        Plain(event.message_str),
        At("3141766"),
        current,
        Reply(id="parent", chain=[quoted]),
    ]
    req = SimpleNamespace(prompt=event.message_str, extra_user_content_parts=[])
    plugin = YunhuPlugin(None)
    await plugin.enrich_yunhu_inputs(event, req)
    assert event.message_str == req.prompt == "*你能看到这个文件吗"
    assert len(req.extra_user_content_parts) == 3
    metadata = json.loads(req.extra_user_content_parts[0].text.split("\n")[1])
    assert metadata["mentioned_user_ids"] == ["3141766"]
    assert metadata["bot_id"] == "11201781"
    assert "当前内容" in req.extra_user_content_parts[1].text
    assert "引用内容" in req.extra_user_content_parts[2].text
    await plugin.enrich_yunhu_inputs(event, req)
    assert len(req.extra_user_content_parts) == 3


@pytest.mark.asyncio
async def test_llm_hook_obeys_total_text_limit(event_factory, tmp_path):
    event = event_factory()
    event.message_obj.self_id = "11201781"
    event.message_obj.message = [
        local_file(tmp_path, f"{i}.txt", b"x" * 10000) for i in range(3)
    ]
    req = SimpleNamespace(extra_user_content_parts=[])
    await YunhuPlugin(None).enrich_yunhu_inputs(event, req)
    parts = [
        json.loads(part.text.split("\n")[2])
        for part in req.extra_user_content_parts[1:]
    ]
    assert len(parts) == 2
    assert sum(len(part["text"]) for part in parts) == 16000
    assert all(part["truncated"] for part in parts)
