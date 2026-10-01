"""按平台实例和会话隔离的附件保留库；SQLite索引与受限容量清理。"""

import hashlib
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .names import safe_filename


class FileStore:
    def __init__(
        self,
        base_dir,
        instance_id,
        retention_days=7,
        max_bytes=512 * 1024 * 1024,
        max_files=1000,
        clock=None,
    ):
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._now = clock or time.time
        self._lock = threading.RLock()
        instance = hashlib.sha256(str(instance_id).encode()).hexdigest()[:24]
        self.base_dir = (Path(base_dir) / instance).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "index.sqlite3"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY, file_name TEXT NOT NULL,
                chat_type TEXT NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                message_id TEXT NOT NULL, size INTEGER NOT NULL,
                created_at REAL NOT NULL, expires_at REAL, pinned INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS files_chat ON files(chat_type,chat_id)"
            )
        self.cleanup()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.index_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, record):
        file_id, name = record["file_id"], record["file_name"]
        if not re.fullmatch(r"[a-f0-9]{32}", file_id) or safe_filename(name) != name:
            raise ValueError("文件库索引中的路径无效")
        path = self.base_dir / file_id / name
        if (
            path.parent.is_symlink()
            or path.is_symlink()
            or not path.resolve().is_relative_to(self.base_dir)
        ):
            raise ValueError("文件库路径超出本实例目录")
        return path

    def _remove(self, db, record):
        path = self._path(record)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()  # 仅删除该附件的空目录，禁止递归清理其他文件。
        except OSError:
            pass
        db.execute("DELETE FROM files WHERE file_id=?", (record["file_id"],))

    @staticmethod
    def _usage(db):
        row = db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM files").fetchone()
        return row[0], row[1]

    def _cleanup(self, db, incoming_bytes=0, incoming_count=0):
        for record in db.execute(
            "SELECT * FROM files ORDER BY created_at,file_id"
        ).fetchall():
            expired = not record["pinned"] and record["expires_at"] <= self._now()
            if expired or not self._path(record).is_file():
                self._remove(db, record)
        used, count = self._usage(db)
        for record in db.execute(
            "SELECT * FROM files WHERE pinned=0 ORDER BY created_at,file_id"
        ).fetchall():
            if (
                used + incoming_bytes <= self.max_bytes
                and count + incoming_count <= self.max_files
            ):
                break
            self._remove(db, record)
            used -= record["size"]
            count -= 1
        return used, count

    def cleanup(self):
        with self._lock, self._db() as db:
            used, count = self._cleanup(db)
            return {
                "used_bytes": used,
                "file_count": count,
                "max_bytes": self.max_bytes,
                "max_files": self.max_files,
            }

    def add(self, path, file_name, chat_type, chat_id, sender_id, message_id):
        if not self.retention_days:
            return None
        name = safe_filename(file_name)
        size = Path(path).stat().st_size
        with self._lock, self._db() as db:
            self._cleanup(db)
            existing = (
                db.execute(
                    "SELECT * FROM files WHERE chat_type=? AND chat_id=? AND message_id=? AND file_name=?",
                    (chat_type, chat_id, message_id, name),
                ).fetchone()
                if message_id
                else None
            )
            if existing and self._path(existing).is_file():
                return dict(existing)
            pinned = db.execute(
                "SELECT COALESCE(SUM(size),0),COUNT(*) FROM files WHERE pinned=1"
            ).fetchone()
            if size + pinned[0] > self.max_bytes or pinned[1] + 1 > self.max_files:
                raise ValueError(
                    "文件保留空间不足；请释放长期保留文件或提高文件库容量，当前附件仍可临时读取"
                )
            self._cleanup(db, size, 1)
            file_id = uuid.uuid4().hex
            record = {
                "file_id": file_id,
                "file_name": name,
                "chat_type": chat_type,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "message_id": message_id,
                "size": size,
                "created_at": self._now(),
                "expires_at": self._now() + self.retention_days * 86400,
                "pinned": 0,
            }
            destination = self._path(record)
            destination.parent.mkdir()
            try:
                shutil.copyfile(path, destination)
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                    tuple(record.values()),
                )
            except BaseException:
                destination.unlink(missing_ok=True)
                destination.parent.rmdir()
                raise
            return record

    def list(self, chat_type, chat_id, limit=100):
        with self._lock, self._db() as db:
            self._cleanup(db)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM files WHERE chat_type=? AND chat_id=? ORDER BY created_at DESC,file_id LIMIT ?",
                    (chat_type, chat_id, limit),
                )
            ]

    def _select(self, db, chat_type, chat_id, file_id="", file_name=""):
        rows = db.execute(
            "SELECT * FROM files WHERE chat_type=? AND chat_id=?", (chat_type, chat_id)
        ).fetchall()
        if file_id:
            rows = [r for r in rows if r["file_id"] == file_id]
        elif file_name:
            rows = [r for r in rows if r["file_name"] == safe_filename(file_name)]
        if len(rows) != 1:
            raise ValueError(
                "未找到唯一保留文件；请查看保留文件列表并指定file_id，同名文件不能猜测"
            )
        return rows[0]

    @contextmanager
    def open_file(self, chat_type, chat_id, file_id="", file_name=""):
        # 读取期间持有锁，定时清理不能删除正在被文件读取工具使用的保留副本。
        with self._lock, self._db() as db:
            self._cleanup(db)
            record = self._select(db, chat_type, chat_id, file_id, file_name)
            yield dict(record), self._path(record)

    def manage(self, chat_type, chat_id, file_id, action, sender_id, is_admin=False):
        if action not in ("pin", "unpin", "delete"):
            raise ValueError("action 必须为pin、unpin或delete")
        with self._lock, self._db() as db:
            self._cleanup(db)
            record = self._select(db, chat_type, chat_id, file_id)
            if record["sender_id"] != sender_id and not is_admin:
                raise PermissionError(
                    "只能管理自己上传的文件；群主、群管理员或AstrBot管理员可管理当前会话的文件"
                )
            if action == "delete":
                self._remove(db, record)
                return {"file_id": file_id, "deleted": True}
            pinned = int(action == "pin")
            expires = None if pinned else self._now() + self.retention_days * 86400
            db.execute(
                "UPDATE files SET pinned=?,expires_at=? WHERE file_id=?",
                (pinned, expires, file_id),
            )
            return {**dict(record), "pinned": pinned, "expires_at": expires}


def default_store_directory():
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data = Path(get_astrbot_data_path())
    except ImportError:
        data = Path.cwd() / "data"
    return data / "plugin_data" / "astrbot_plugin_yunhuAdapter" / "files"


def event_store(event):
    return event.get_extra("yunhu_file_store")


def retained_files(event, limit=100):
    store = event_store(event)
    return store.list(event._recv_type, event._recv_id, limit) if store else []
