"""云湖附件的临时缓存和 AstrBot 事件移交。"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import uuid
from typing import TYPE_CHECKING

from .names import safe_filename

if TYPE_CHECKING:
    from ..adapter.event import YunhuMessageEvent


class TempFileManager:
    """暂存尚未移交 AstrBot 事件的媒体；移交后由事件结束时清理。"""

    def __init__(self, base_dir: str = None, ttl: int = 600):
        if base_dir is None:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

                root = get_astrbot_temp_path()
            except ImportError:
                root = tempfile.gettempdir()
            base_dir = os.path.join(root, "yunhu_media", uuid.uuid4().hex)
        self.base_dir = os.path.abspath(base_dir)
        self.ttl = ttl
        self.closed = False
        self._file_records: dict[str, tuple[str, float]] = {}
        os.makedirs(self.base_dir, exist_ok=True)

    def put(
        self,
        data: bytes,
        suffix: str = ".bin",
        content_type: str = "application/octet-stream",
        filename: str = "",
    ) -> tuple[str, str]:
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
            suffix = ".bin"
        token = uuid.uuid4().hex
        if filename:
            directory = os.path.join(self.base_dir, token)
            os.makedirs(directory)
            path = os.path.join(directory, safe_filename(filename))
        else:
            path = os.path.join(self.base_dir, token + suffix)
        with open(path, "wb") as file:
            file.write(data)
        self._file_records[token] = (path, time.monotonic())
        self._cleanup()
        return token, path

    def _cleanup(self):
        now = time.monotonic()
        for token, (path, timestamp) in list(self._file_records.items()):
            if not os.path.exists(path) or now - timestamp > self.ttl:
                try:
                    os.unlink(path)
                    self._remove_empty_parent(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                self._file_records.pop(token, None)

    def transfer(self, path: str, event: YunhuMessageEvent):
        """移交后不再受 TTL 限制，避免删除处理中的附件。"""
        for token, (recorded, _) in list(self._file_records.items()):
            if recorded == path:
                event.track_temporary_local_file(path)
                self._file_records.pop(token)
                return

    def cleanup_all(self):
        self.closed = True
        for token, (path, _) in list(self._file_records.items()):
            try:
                os.unlink(path)
                self._remove_empty_parent(path)
            except FileNotFoundError:
                pass
            except OSError:
                continue
            self._file_records.pop(token, None)
        self._cleanup_empty_directories()
        try:
            os.rmdir(self.base_dir)  # 仅删除本实例的空目录
        except OSError:
            pass

    def _remove_empty_parent(self, path):
        parent = os.path.dirname(path)
        if parent != self.base_dir:
            try:
                os.rmdir(parent)
            except OSError:
                pass

    def _cleanup_empty_directories(self):
        # AstrBot只删除已移交的文件，唯一文件目录由本实例清理。
        if not os.path.isdir(self.base_dir):
            return
        for entry in os.scandir(self.base_dir):
            if entry.is_dir(follow_symlinks=False):
                try:
                    os.rmdir(entry.path)
                except OSError:
                    pass

    async def start_periodic_cleanup(self, interval: int = 300):
        while True:
            await asyncio.sleep(interval)
            self._cleanup()
            self._cleanup_empty_directories()

    @staticmethod
    def detect_image_suffix(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        return ".png"
