"""仅读取当前事件或引用链中的附件，不接受任意本地路径。"""

import asyncio
import codecs
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from astrbot.api.message_components import File, Reply

from .store import event_store

_MAX_BYTES = 2 * 1024 * 1024
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".json",
    ".csv",
    ".tsv",
    ".log",
    ".yaml",
    ".yml",
    ".xml",
    ".py",
    ".js",
    ".ts",
    ".css",
    ".sql",
    ".ini",
    ".conf",
    ".toml",
    ".sh",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".rs",
}


def attachment_components(event, source="all"):
    if source not in ("all", "current", "quoted"):
        raise ValueError("source 必须为 all、current 或 quoted")
    result = []
    for component in event.get_messages():
        if isinstance(component, File) and source in ("all", "current"):
            result.append(("current", component))
        elif isinstance(component, Reply) and source in ("all", "quoted"):
            result.extend(
                ("quoted", child)
                for child in component.chain or []
                if isinstance(child, File)
            )
    return result


def attachment_info(event):
    return [
        {"file_name": component.name or "file", "source": source}
        for source, component in attachment_components(event)
    ]


def _read_text(path, file_name, max_chars):
    size = os.path.getsize(path)
    suffix = Path(file_name or path).suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > _MAX_BYTES:
                raise ValueError("DOCX正文超过2MiB，无法直接读取")
            root = ElementTree.fromstring(archive.read(info))
        paragraphs = []
        for paragraph in root.iter(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
        ):
            paragraphs.append(
                "".join(
                    node.text or ""
                    for node in paragraph.iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                    )
                )
            )
        text = "\n".join(paragraphs)
        truncated = len(text) > max_chars
    else:
        if suffix and suffix not in _TEXT_SUFFIXES:
            raise ValueError(
                "此工具支持文本、HTML、代码和DOCX文件；PDF、图片或其他二进制文件需使用对应解析/OCR功能"
            )
        with open(path, "rb") as file:
            data = file.read(_MAX_BYTES + 1)
        clipped = len(data) > _MAX_BYTES
        data = data[:_MAX_BYTES]
        encodings = ["utf-8-sig", "gb18030"]
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encodings = ["utf-32"]
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encodings = ["utf-16"]
        elif b"\x00" in data:
            raise ValueError("文件包含二进制内容，无法作为文本读取")
        text = None
        for encoding in encodings:
            try:
                decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
                text = decoder.decode(data, final=not clipped)
                break
            except UnicodeError:
                continue
        if text is None:
            raise ValueError("文件编码无法识别，支持UTF-8、带BOM的UTF-16/32和GB18030")
        truncated = clipped or len(text) > max_chars
    return {
        "file_name": file_name,
        "size": size,
        "text": text[:max_chars],
        "truncated": truncated,
    }


async def read_attachment(
    event, file_name="", source="all", max_chars=16000, file_id=""
):
    if not isinstance(file_name, str):
        raise ValueError("file_name 必须为字符串")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= 64000
    ):
        raise ValueError("max_chars 必须为1到64000之间的整数")
    if source not in ("all", "current", "quoted", "stored"):
        raise ValueError("source 必须为all、current、quoted或stored")
    if not isinstance(file_id, str):
        raise ValueError("file_id 必须为字符串")
    if file_id and source not in ("all", "stored"):
        raise ValueError("file_id 仅适用于保留文件，请使用source=stored")
    candidates = attachment_components(event, source) if source != "stored" else []
    if file_name:
        candidates = [
            (origin, component)
            for origin, component in candidates
            if component.name == file_name
        ]
    if file_id or source == "stored" or (source == "all" and not candidates):
        store = event_store(event)
        if store:

            def read_stored():
                with store.open_file(
                    event._recv_type, event._recv_id, file_id, file_name
                ) as (record, path):
                    return {
                        **_read_text(path, record["file_name"], max_chars),
                        "source": "stored",
                        "file_id": record["file_id"],
                        "pinned": bool(record["pinned"]),
                        "expires_at": record["expires_at"],
                    }

            try:
                return await asyncio.to_thread(read_stored)
            except (
                OSError,
                sqlite3.Error,
                zipfile.BadZipFile,
                KeyError,
                ElementTree.ParseError,
            ) as error:
                raise ValueError(f"附件读取失败 ({type(error).__name__})") from None
        if source == "stored" or file_id:
            raise ValueError("当前会话没有可用的文件保留库")
    if len(candidates) != 1:
        names = json.dumps(attachment_info(event), ensure_ascii=False)
        raise ValueError(
            "未找到唯一附件，请指定 file_name 和 source；本消息附件: " + names
        )
    origin, component = candidates[0]
    try:
        path = await component.get_file()
        if not path or not os.path.isfile(path):
            raise ValueError("附件未下载成功，暂时无法读取")
        result = await asyncio.to_thread(
            _read_text, path, component.name or Path(path).name, max_chars
        )
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise ValueError(f"附件读取失败 ({type(error).__name__})") from None
    result["source"] = origin
    return result


async def attachment_prompt_parts(event):
    """小型文本附件自动进入请求；大附件和其他格式由读取工具显式处理。"""
    results = []
    total = 0
    for origin, component in attachment_components(event):
        if Path(component.name or "").suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            path = await component.get_file()
            if (
                not path
                or not os.path.isfile(path)
                or os.path.getsize(path) > _MAX_BYTES
            ):
                continue
            result = await asyncio.to_thread(
                _read_text, path, component.name, min(8000, 16000 - total)
            )
        except (OSError, ValueError):
            continue
        result["source"] = origin
        results.append(result)
        total += len(result["text"])
        if total >= 16000:
            break
    return results
