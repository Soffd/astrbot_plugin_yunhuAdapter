import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from yunhu_plugin.adapter.event import YunhuMessageEvent, _split_markdown, _split_text
from yunhu_plugin.api.client import YunhuAPIError
from yunhu_plugin.api.models import ApiResponse, Button, ButtonGroup


def test_framework_get_messages_stays_synchronous(event_factory, fake_client):
    event = event_factory()
    assert not inspect.iscoroutinefunction(event.get_messages)
    assert YunhuMessageEvent.get_messages is AstrMessageEvent.get_messages
    chain = [At(qq="bot-1"), Plain("你好"), Reply(id="parent")]
    event.message_obj.message = chain
    assert event.get_messages() is chain
    assert list(event.get_messages()) == chain
    fake_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_history_query_has_separate_async_method(event_factory, fake_client):
    response = ApiResponse(1, data={"list": [{"msgId": "parent"}]})
    fake_client.get_messages.return_value = response
    event = event_factory()
    result = await event.get_message_history(message_id="parent", before=5, after=2)
    assert result is response
    fake_client.get_messages.assert_awaited_once_with(
        "group-1", "group", message_id="parent", before=5, after=2
    )


@pytest.mark.parametrize("split", [_split_text, _split_markdown])
@pytest.mark.parametrize(
    "text", ["中" * 5000, "🙂" * 2000, "\n\n hello \n" * 700, "a" * 10000, ""]
)
def test_utf8_chunks_preserve_text(split, text):
    chunks = split(text, 3500)
    assert "".join(chunks) == text
    assert all(0 < len(chunk.encode()) <= 3500 for chunk in chunks)


@pytest.mark.asyncio
async def test_reply_mentions_and_adjacent_text(event_factory, fake_client):
    event = event_factory()
    await event.send(
        MessageChain(
            [Reply(id="parent"), At(qq="u", name="张三"), Plain("你好"), Plain("世界")]
        )
    )
    fake_client.send_message.assert_awaited_once()
    args = fake_client.send_message.call_args
    assert args.args == (
        "group-1",
        "group",
        "text",
        {"text": "@张三 你好世界"},
        "parent",
    )
    assert args.kwargs["at"] == ["u"]
    assert event._parent_id == ""
    assert event._has_send_oper


@pytest.mark.parametrize("override,expected", [(True, "markdown"), (False, "text")])
@pytest.mark.asyncio
async def test_markdown_override(event_factory, fake_client, override, expected):
    await event_factory().send(
        MessageChain([Plain("**bold**")], use_markdown_=override)
    )
    assert fake_client.send_message.call_args.args[2] == expected


@pytest.mark.parametrize(
    "kind,upload,expected",
    [
        (Image, "upload_image", "image"),
        (File, "upload_file", "file"),
        (Video, "upload_video", "video"),
        (Record, "upload_file", "file"),
    ],
)
@pytest.mark.asyncio
async def test_media_uses_astrbot_resolver(
    event_factory, fake_client, tmp_path, kind, upload, expected
):
    path = tmp_path / "local.bin"
    path.write_bytes(b"content")
    component = (
        kind(name="文件.txt", file=path.as_uri())
        if kind is File
        else kind(file=path.as_uri())
    )
    method = "get_file" if kind is File else "convert_to_file_path"
    setattr(component, method, AsyncMock(return_value=str(path)))
    await event_factory().send(MessageChain([component]))
    getattr(component, method).assert_awaited_once()
    getattr(fake_client, upload).assert_awaited_once()
    assert fake_client.send_message.call_args.args[2] == expected
    assert path.exists()  # 不删除用户提供的本地附件


@pytest.mark.parametrize(
    "name,expected",
    [("%E8%A7%84%E5%AE%9A.md", "规定.md"), ("进度100%20.md", "进度100%20.md")],
)
@pytest.mark.parametrize("recv_type", ["group", "user"])
@pytest.mark.asyncio
async def test_send_file_decodes_encoded_display_name(
    event_factory, fake_client, tmp_path, recv_type, name, expected
):
    path = tmp_path / "hash.md"
    path.write_bytes(b"content")
    component = File(name=name, file=str(path))
    component.get_file = AsyncMock(return_value=str(path))
    await event_factory(recv_type=recv_type).send(MessageChain([component]))
    fake_client.upload_file.assert_awaited_once_with(str(path), filename=expected)


@pytest.mark.asyncio
async def test_failure_does_not_mark_send_success(event_factory, fake_client):
    event = event_factory()
    fake_client.send_message.return_value = ApiResponse(1007, "frequency limit")
    with pytest.raises(YunhuAPIError):
        await event.send(MessageChain([Plain("hello")]))
    assert not event._has_send_oper


@pytest.mark.asyncio
async def test_button_segmentation_only_adds_buttons_to_last_part(
    event_factory, fake_client
):
    await event_factory().send_with_buttons(
        "中" * 2000, [ButtonGroup([Button("确认")])]
    )
    calls = fake_client.send_message.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["buttons"] is None
    assert calls[-1].kwargs["buttons"]


@pytest.mark.asyncio
async def test_streaming_break_and_cancellation(event_factory, fake_client):
    event = event_factory()
    streams = [AsyncMock(), AsyncMock()]
    for stream in streams:
        stream.write_eof.return_value = ApiResponse(1)
    fake_client.send_stream.side_effect = streams

    async def generator():
        yield MessageChain([Plain("first")])
        yield MessageChain(type="break")
        yield MessageChain([Plain("second")])

    await event.send_streaming(generator())
    assert fake_client.send_stream.await_count == 2
    streams[0].write.assert_awaited_once_with("first")
    streams[1].write.assert_awaited_once_with("second")
    streams[0].write_eof.assert_awaited_once()
    assert event._has_send_oper


@pytest.mark.asyncio
async def test_stream_generator_error_aborts_request(event_factory, fake_client):
    stream = AsyncMock()
    fake_client.send_stream.return_value = stream

    async def generator():
        yield MessageChain([Plain("partial")])
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await event_factory().send_streaming(generator())
    stream.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_stream_fallback_preserves_parent(event_factory, fake_client):
    async def generator():
        yield MessageChain([Plain("你")])
        yield MessageChain([Plain("好")])

    await event_factory(parent_id="parent").send_streaming(generator())
    fake_client.send_stream.assert_not_called()
    assert fake_client.send_message.call_args.args[3:] == ({"text": "你好"}, "parent")


@pytest.mark.asyncio
async def test_converted_local_audio_is_cleaned_but_original_is_kept(
    event_factory, tmp_path
):
    original = tmp_path / "original.mp3"
    converted = tmp_path / "converted.wav"
    original.write_bytes(b"mp3")
    converted.write_bytes(b"wav")
    component = Record.fromFileSystem(str(original))
    component.convert_to_file_path = AsyncMock(return_value=str(converted))
    event = event_factory()
    await event.send(MessageChain([component]))
    assert event._temporary_local_files == [str(converted)]
    event.cleanup_temporary_local_files()
    assert original.exists() and not converted.exists()
