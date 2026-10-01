"""异步直连云湖开放 API，不重试可能产生重复消息的写操作。"""

import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional

import aiohttp

from ..media.names import safe_filename
from .models import ApiResponse, ButtonGroup

logger = logging.getLogger("yunhu.client")
BASE_URL = "https://chat-go.jwzhd.com/open-apis/v1"


class YunhuAPIError(RuntimeError):
    def __init__(self, response: ApiResponse):
        self.response = response
        super().__init__(f"云湖 API 失败: code={response.code}, msg={response.msg}")


def ensure_success(response: ApiResponse) -> ApiResponse:
    if not response.ok:
        raise YunhuAPIError(response)
    return response


class YunhuClient:
    def __init__(
        self,
        token: str,
        timeout: int = 10,
        upload_timeout: int = 120,
        base_url: str = BASE_URL,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.upload_timeout = aiohttp.ClientTimeout(total=upload_timeout, connect=10)
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_times = defaultdict(deque)
        self._rate_locks = defaultdict(asyncio.Lock)
        self._streams: set[YunhuStream] = set()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        for stream in list(self._streams):
            await stream.abort()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _throttle(self, path: str):
        # 官方消息 API 每个接口每秒最多 10 次；滑动窗口包含并发调用。
        async with self._rate_locks[path]:
            timestamps = self._request_times[path]
            now = time.monotonic()
            while timestamps and now - timestamps[0] >= 1:
                timestamps.popleft()
            if len(timestamps) >= 10:
                await asyncio.sleep(max(0, 1 - (now - timestamps[0])))
                now = time.monotonic()
                while timestamps and now - timestamps[0] >= 1:
                    timestamps.popleft()
            timestamps.append(now)

    def _safe_error(self, error: Exception) -> str:
        message = (
            str(error).replace(self.token, "[redacted]") if self.token else str(error)
        )
        return re.sub(r"([?&]token=)[^&\s'\"<>]*", r"\1[redacted]", message)

    async def _decode_response(self, response: aiohttp.ClientResponse) -> ApiResponse:
        try:
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("响应 JSON 必须为对象")
            result = ApiResponse(
                int(data.get("code", -1)),
                str(data.get("msg") or data.get("message") or ""),
                data.get("data"),
            )
            if response.status >= 400 and result.ok:
                return ApiResponse(-1, f"HTTP {response.status}")
            return result
        except (ValueError, TypeError):
            return ApiResponse(-1, f"HTTP {response.status}: 无效 JSON 响应")

    async def _request(
        self, method: str, path: str, params: dict = None, **kwargs
    ) -> ApiResponse:
        await self._throttle(path)
        session = await self._get_session()
        try:
            async with session.request(
                method,
                self.base_url + path,
                params={**(params or {}), "token": self.token},
                **kwargs,
            ) as response:
                return await self._decode_response(response)
        except asyncio.TimeoutError:
            return ApiResponse(-1, "请求超时")
        except (aiohttp.ClientError, OSError) as error:
            message = self._safe_error(error)
            logger.error("[云湖Client] %s 请求失败: %s", path, message)
            return ApiResponse(-1, message)

    async def _post(self, path: str, payload: dict) -> ApiResponse:
        return await self._request("POST", path, json=payload)

    async def _get(self, path: str, params: dict) -> ApiResponse:
        return await self._request("GET", path, params=params)

    async def _upload(
        self, path: str, file_path: str, field_name: str = "file", filename: str = ""
    ) -> ApiResponse:
        limit = (10 if field_name == "image" else 20) * 1024 * 1024
        try:
            if os.path.getsize(file_path) > limit:
                return ApiResponse(
                    1002, f"{field_name} 上传超过 {limit // 1024 // 1024}MB 上限"
                )
            with open(file_path, "rb") as file:
                # 云湖会原样保存multipart的filename；默认quote_fields会把中文变成%XX。
                form = aiohttp.FormData(quote_fields=False)
                form.add_field(
                    field_name,
                    file,
                    filename=safe_filename(
                        filename or os.path.basename(file_path), decode=True
                    ),
                    content_type="application/octet-stream",
                )
                return await self._request(
                    "POST", path, data=form, timeout=self.upload_timeout
                )
        except OSError as error:
            return ApiResponse(-1, self._safe_error(error))

    async def send_message(
        self,
        recv_id: str,
        recv_type: str,
        content_type: str,
        content: dict,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
        at: list[str] = None,
    ) -> ApiResponse:
        content = dict(content)
        if buttons:
            content["buttons"] = [group.to_dict() for group in buttons]
        if at and recv_type == "group":
            content["at"] = list(dict.fromkeys(str(user) for user in at))
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": content_type,
            "content": content,
        }
        if parent_id:
            payload["parentId"] = parent_id
        return await self._post("/bot/send", payload)

    async def send_text(
        self,
        recv_id: str,
        recv_type: str,
        text: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
        at: list[str] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "text", {"text": text}, parent_id, buttons, at
        )

    async def send_markdown(
        self,
        recv_id: str,
        recv_type: str,
        text: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
        at: list[str] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "markdown", {"text": text}, parent_id, buttons, at
        )

    async def send_html(
        self,
        recv_id: str,
        recv_type: str,
        text: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "html", {"text": text}, parent_id, buttons
        )

    async def send_image(
        self,
        recv_id: str,
        recv_type: str,
        image_key: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "image", {"imageKey": image_key}, parent_id, buttons
        )

    async def send_file(
        self,
        recv_id: str,
        recv_type: str,
        file_key: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "file", {"fileKey": file_key}, parent_id, buttons
        )

    async def send_video(
        self,
        recv_id: str,
        recv_type: str,
        video_key: str,
        parent_id: str = "",
        buttons: list[ButtonGroup] = None,
    ) -> ApiResponse:
        return await self.send_message(
            recv_id, recv_type, "video", {"videoKey": video_key}, parent_id, buttons
        )

    async def send_stream(
        self, recv_id: str, recv_type: str, content_type: str = "text"
    ) -> "YunhuStream":
        """立即返回可写请求流；write_eof() 返回最终 ApiResponse。"""
        if content_type not in ("text", "markdown"):
            raise ValueError("流式消息仅支持 text 和 markdown")
        stream = YunhuStream(
            self,
            {"recvId": recv_id, "recvType": recv_type, "contentType": content_type},
        )
        self._streams.add(stream)
        stream._task.add_done_callback(lambda task: self._streams.discard(stream))
        return stream

    async def batch_send(
        self, recv_ids: list, recv_type: str, content_type: str, content: dict
    ) -> ApiResponse:
        return await self._post(
            "/bot/batch_send",
            {
                "recvIds": recv_ids,
                "recvType": recv_type,
                "contentType": content_type,
                "content": content,
            },
        )

    async def edit_message(
        self,
        msg_id: str,
        recv_id: str,
        recv_type: str,
        content_type: str,
        content: dict,
    ) -> ApiResponse:
        return await self._post(
            "/bot/edit",
            {
                "msgId": msg_id,
                "recvId": recv_id,
                "recvType": recv_type,
                "contentType": content_type,
                "content": content,
            },
        )

    async def recall_message(
        self, msg_id: str, chat_id: str, chat_type: str
    ) -> ApiResponse:
        return await self._post(
            "/bot/recall", {"msgId": msg_id, "chatId": chat_id, "chatType": chat_type}
        )

    async def get_messages(
        self,
        chat_id: str,
        chat_type: str,
        before: int = None,
        after: int = 0,
        limit: int = 20,
        message_id: str = "",
    ) -> ApiResponse:
        """before/after 是条数，message_id 是查询锚点；limit 兼容旧调用。"""
        params = {
            "chat-id": chat_id,
            "chat-type": chat_type,
            "before": str(limit if before is None else int(before)),
            "after": str(int(after)),
        }
        if message_id:
            params["message-id"] = message_id
        return await self._get("/bot/messages", params)

    async def upload_image(self, file_path: str) -> ApiResponse:
        return await self._upload("/image/upload", file_path, "image")

    async def upload_file(self, file_path: str, filename: str = "") -> ApiResponse:
        return await self._upload("/file/upload", file_path, "file", filename)

    async def upload_video(self, file_path: str) -> ApiResponse:
        return await self._upload("/video/upload", file_path, "video")

    async def set_board(
        self,
        chat_id: str,
        chat_type: str,
        content_type: str,
        content: str,
        member_id: str = "",
        expire_time: int = 0,
    ) -> ApiResponse:
        """expire_time 是秒级 Unix 时间戳，0 为不过期。"""
        payload = {
            "chatId": chat_id,
            "chatType": chat_type,
            "contentType": content_type,
            "content": content,
            "expireTime": expire_time,
        }
        if member_id:
            payload["memberId"] = member_id
        return await self._post("/bot/board", payload)

    async def set_board_all(
        self,
        chat_type: str = "",
        content_type: str = "text",
        content: str = "",
        expire_time: int = 0,
    ) -> ApiResponse:
        # chat_type 仅保留兼容性；全局看板的官方接口不接受 chatType。
        return await self._post(
            "/bot/board-all",
            {
                "contentType": content_type,
                "content": content,
                "expireTime": expire_time,
            },
        )

    async def dismiss_board(
        self, chat_id: str, chat_type: str, member_id: str = ""
    ) -> ApiResponse:
        payload = {"chatId": chat_id, "chatType": chat_type}
        if member_id:
            payload["memberId"] = member_id
        return await self._post("/bot/board-dismiss", payload)

    async def dismiss_board_all(self) -> ApiResponse:
        return await self._post("/bot/board-all-dismiss", {})

    async def gag_member(
        self, group_id: str, user_id: str, duration: int = 600
    ) -> ApiResponse:
        return await self._post(
            "/group/gag-member",
            {"groupId": group_id, "userId": user_id, "gag": duration},
        )

    async def remove_member(self, group_id: str, user_id: str) -> ApiResponse:
        return await self._post(
            "/group/remove-member", {"groupId": group_id, "userId": user_id}
        )

    async def set_message_type_limit(
        self, group_id: str, types: str = ""
    ) -> ApiResponse:
        return await self._post(
            "/group/msg-type-limit", {"groupId": group_id, "type": types}
        )

    async def create_tag(
        self, group_id: str, tag: str, color: str = "", desc: str = "", sort: int = 0
    ) -> ApiResponse:
        return await self._post(
            "/group/tag/create",
            {
                "groupId": group_id,
                "tag": tag,
                "color": color,
                "desc": desc,
                "sort": sort,
            },
        )

    async def list_tags(self, group_id: str) -> ApiResponse:
        return await self._post("/group/tag/list", {"groupId": group_id})

    async def edit_tag(
        self,
        group_id: str,
        tag: str,
        *,
        new_tag: str = None,
        color: str = None,
        desc: str = None,
        sort: int = None,
    ) -> ApiResponse:
        payload = {"groupId": group_id, "tag": tag}
        payload.update(
            {
                key: value
                for key, value in {
                    "newTag": new_tag,
                    "color": color,
                    "desc": desc,
                    "sort": sort,
                }.items()
                if value is not None
            }
        )
        return await self._post("/group/tag/edit", payload)

    async def delete_tag(self, group_id: str, tag: str) -> ApiResponse:
        return await self._post("/group/tag/delete", {"groupId": group_id, "tag": tag})

    async def add_user_tag(self, group_id: str, user_id: str, tag: str) -> ApiResponse:
        return await self._post(
            "/group/tag/user-relate",
            {"groupId": group_id, "userId": user_id, "tag": tag},
        )

    async def remove_user_tag(
        self, group_id: str, user_id: str, tag: str
    ) -> ApiResponse:
        return await self._post(
            "/group/tag/user-relate-cancel",
            {"groupId": group_id, "userId": user_id, "tag": tag},
        )

    async def test_connection(self) -> tuple[bool, str]:
        response = await self.get_messages("test", "user", before=1)
        if response.code in (1, 1002):
            return (
                True,
                f"服务器已响应（code={response.code}，参数错误不能证明会话权限）",
            )
        return False, response.msg or f"code={response.code}"


class YunhuStream:
    """带背压的可写异步请求体；请求在后台读取队列，不等待响应再写入。"""

    def __init__(self, client: YunhuClient, params: dict):
        self._client = client
        self._queue = asyncio.Queue(maxsize=16)
        self._closed = False
        self._task = asyncio.create_task(self._run(params))

    async def _body(self):
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    async def _run(self, params: dict) -> ApiResponse:
        return await self._client._request(
            "POST",
            "/bot/send-stream",
            params=params,
            data=self._body(),
            chunked=True,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=300),
        )

    async def _put(self, chunk):
        if self._task.done():
            ensure_success(await self._task)
            raise RuntimeError("流式请求已结束")
        put = asyncio.create_task(self._queue.put(chunk))
        try:
            done, _ = await asyncio.wait(
                (put, self._task), return_when=asyncio.FIRST_COMPLETED
            )
            if put in done:
                await put
                return
            if self._task in done:
                ensure_success(await self._task)
                raise RuntimeError("流式请求提前结束")
            await put
        finally:
            if not put.done():
                put.cancel()
                await asyncio.gather(put, return_exceptions=True)

    async def write(self, data: bytes | str):
        if self._closed:
            raise RuntimeError("不能写入已关闭的流")
        if data:
            await self._put(data.encode("utf-8") if isinstance(data, str) else data)

    async def write_eof(self) -> ApiResponse:
        if not self._closed:
            self._closed = True
            if not self._task.done():
                await self._put(None)
        return await self._task

    async def close(self) -> ApiResponse:
        return await self.write_eof()

    async def abort(self):
        self._closed = True
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
