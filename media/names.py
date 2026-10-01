"""文件显示名、HTTP文件名和安全本地basename。"""

import re
from pathlib import PurePath
from urllib.parse import unquote

from aiohttp.multipart import content_disposition_filename, parse_content_disposition


def safe_filename(value, fallback="file.bin", *, decode=False):
    name = str(value or "")
    if decode:
        try:
            decoded = unquote(name, encoding="utf-8", errors="strict")
            # 识别被URL编码的非英文名称，保留正常名称中的字面量%20等字符。
            if name.isascii() and not decoded.isascii():
                name = decoded
        except UnicodeError:
            pass
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f\x7f]', "_", name).strip().rstrip(". ")
    if name in ("", ".", ".."):
        return fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I):
        name = "_" + name
    suffix = PurePath(name).suffix
    suffix = suffix if len(suffix.encode("utf-8")) <= 32 else ""
    stem = name[: -len(suffix)] if suffix else name
    while len((stem + suffix).encode("utf-8")) > 220:
        stem = stem[:-1]
    return stem + suffix


def header_filename(headers):
    _, parameters = parse_content_disposition(headers.get("Content-Disposition", ""))
    name = content_disposition_filename(parameters)
    return safe_filename(name, decode=True) if name else ""


def received_filename(declared, downloaded, resource):
    declared = safe_filename(declared, "", decode=True)
    # 部分推送以CDN哈希作为fileName，HTTP元数据仍可能包含用户的原始名。
    generated = re.fullmatch(r"[a-fA-F0-9]{32,64}(?:\.[^.]*)?", declared or "")
    if downloaded and (not declared or generated):
        return safe_filename(downloaded)
    try:
        resource = unquote(str(resource or ""), encoding="utf-8", errors="strict")
    except UnicodeError:
        pass
    return declared or safe_filename(resource)
