"""
云湖机器人 API 客户端
封装所有云湖开放平台接口，直接通过 HTTP API 与云湖平台通信。
本客户端内嵌于 AstrBot 适配器中，无需额外运行 WebSDK 服务。
"""
import asyncio
import os
import aiohttp
import logging
from typing import Optional, Union, List

from .models import (
    SendMessageRequest, BatchSendRequest, EditMessageRequest,
    RecallMessageRequest, BoardRequest, ApiResponse, ButtonGroup,
)

logger = logging.getLogger("yunhu.client")

BASE_URL = "https://chat-go.jwzhd.com/open-apis/v1"


class YunhuClient:
    """云湖机器人 API 客户端"""

    def __init__(self, token: str, timeout: int = 10, upload_timeout: int = 120):
        self.token = token
        # 普通 API 请求超时（发消息、查消息等）
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        # 上传专用超时，视频/文件可能较大，需要更长时间
        self.upload_timeout = aiohttp.ClientTimeout(
            total=upload_timeout,
            connect=10,
            sock_connect=10,
            sock_read=upload_timeout,
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # 内部请求方法 

    async def _post(self, path: str, payload: dict) -> ApiResponse:
        session = await self._get_session()
        url = f"{BASE_URL}{path}?token={self.token}"
        try:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                return ApiResponse(
                    code=data.get("code", -1),
                    msg=data.get("msg", ""),
                    data=data.get("data"),
                )
        except asyncio.TimeoutError:
            logger.error(f"[云湖Client] 请求超时: {path}")
            return ApiResponse(code=-1, msg="请求超时")
        except Exception as e:
            logger.error(f"[云湖Client] 请求异常: {path} - {e}")
            return ApiResponse(code=-1, msg=str(e))

    async def _get(self, path: str, params: dict) -> ApiResponse:
        session = await self._get_session()
        url = f"{BASE_URL}{path}?token={self.token}"
        try:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                return ApiResponse(
                    code=data.get("code", -1),
                    msg=data.get("msg", ""),
                    data=data.get("data"),
                )
        except asyncio.TimeoutError:
            logger.error(f"[云湖Client] 请求超时: {path}")
            return ApiResponse(code=-1, msg="请求超时")
        except Exception as e:
            logger.error(f"[云湖Client] 请求异常: {path} - {e}")
            return ApiResponse(code=-1, msg=str(e))

    async def _upload(self, path: str, file_path: str, field_name: str = "file") -> ApiResponse:
        """上传文件（图片/文件/视频）

        Args:
            path: API 路径，如 /image/upload
            file_path: 本地文件路径
            field_name: 表单字段名，图片为 image，视频为 video，文件为 file
        """
        session = await self._get_session()
        url = f"{BASE_URL}{path}?token={self.token}"
        try:
            # 使用上传专用超时
            upload_session = aiohttp.ClientSession(timeout=self.upload_timeout)
            try:
                with open(file_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field(field_name, f, filename=os.path.basename(file_path))
                    async with upload_session.post(url, data=form) as resp:
                        data = await resp.json()
                        return ApiResponse(
                            code=data.get("code", -1),
                            msg=data.get("msg", ""),
                            data=data.get("data"),
                        )
            finally:
                await upload_session.close()
        except asyncio.TimeoutError:
            logger.error(f"[云湖Client] 上传超时: {path} - {file_path}")
            return ApiResponse(code=-1, msg="上传超时")
        except Exception as e:
            logger.error(f"[云湖Client] 上传异常: {path} - {e}")
            return ApiResponse(code=-1, msg=str(e))

    # 发送消息 

    async def send_text(
        self,
        recv_id: str,
        recv_type: str,
        text: str,
        parent_id: str = "",
        buttons: List[ButtonGroup] = None,
    ) -> ApiResponse:
        """发送文本消息"""
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": "text",
            "content": {"text": text},
        }
        if parent_id:
            payload["parentId"] = parent_id
        if buttons:
            payload["buttons"] = [bg.to_dict() for bg in buttons]
        return await self._post("/bot/send", payload)

    async def send_markdown(
        self,
        recv_id: str,
        recv_type: str,
        text: str,
        parent_id: str = "",
        buttons: List[ButtonGroup] = None,
    ) -> ApiResponse:
        """发送 Markdown 消息"""
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": "markdown",
            "content": {"text": text},
        }
        if parent_id:
            payload["parentId"] = parent_id
        if buttons:
            payload["buttons"] = [bg.to_dict() for bg in buttons]
        return await self._post("/bot/send", payload)

    async def send_image(
        self,
        recv_id: str,
        recv_type: str,
        image_key: str,
        parent_id: str = "",
        buttons: List[ButtonGroup] = None,
    ) -> ApiResponse:
        """发送图片消息（使用已上传的 imageKey）"""
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": "image",
            "content": {"imageKey": image_key},
        }
        if parent_id:
            payload["parentId"] = parent_id
        if buttons:
            payload["buttons"] = [bg.to_dict() for bg in buttons]
        return await self._post("/bot/send", payload)

    async def send_file(
        self,
        recv_id: str,
        recv_type: str,
        file_key: str,
        parent_id: str = "",
        buttons: List[ButtonGroup] = None,
    ) -> ApiResponse:
        """发送文件消息（使用已上传的 fileKey）"""
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": "file",
            "content": {"fileKey": file_key},
        }
        if parent_id:
            payload["parentId"] = parent_id
        if buttons:
            payload["buttons"] = [bg.to_dict() for bg in buttons]
        return await self._post("/bot/send", payload)

    async def send_video(
        self,
        recv_id: str,
        recv_type: str,
        video_key: str,
        parent_id: str = "",
        buttons: List[ButtonGroup] = None,
    ) -> ApiResponse:
        """发送视频消息（使用已上传的 videoKey）"""
        payload = {
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": "video",
            "content": {"videoKey": video_key},
        }
        if parent_id:
            payload["parentId"] = parent_id
        if buttons:
            payload["buttons"] = [bg.to_dict() for bg in buttons]
        return await self._post("/bot/send", payload)

    # 流式消息 

    async def send_stream(
        self,
        recv_id: str,
        recv_type: str,
        content_type: str = "text",
    ) -> Optional[aiohttp.ClientResponse]:
        """
        发送流式消息（返回原始 response，调用者需自行写入数据并关闭）。

        用法:
            resp = await client.send_stream(recv_id, recv_type, "text")
            if resp:
                await resp.write("第一段内容".encode())
                await resp.write("第二段内容".encode())
                await resp.write_eof()

        注意：流式消息需要使用 chunked transfer encoding。
        """
        session = await self._get_session()
        url = (
            f"{BASE_URL}/bot/send-stream"
            f"?token={self.token}"
            f"&recvId={recv_id}"
            f"&recvType={recv_type}"
            f"&contentType={content_type}"
        )
        try:
            # 使用 chunked 传输
            resp = await session.post(
                url,
                data=aiohttp.streamer.AsyncIterablePayload(
                    _StreamIterator(), chunk_size=1024
                ),
                headers={"Transfer-Encoding": "chunked", "Content-Type": "text/plain"},
            )
            return resp
        except Exception as e:
            logger.error(f"[云湖Client] 流式消息发送失败: {e}")
            return None

    # 批量发送 

    async def batch_send(
        self,
        recv_ids: list,
        recv_type: str,
        content_type: str,
        content: dict,
    ) -> ApiResponse:
        """批量发送消息"""
        payload = {
            "recvIds": recv_ids,
            "recvType": recv_type,
            "contentType": content_type,
            "content": content,
        }
        return await self._post("/bot/batch-send", payload)

    # 编辑消息 

    async def edit_message(
        self,
        msg_id: str,
        recv_id: str,
        recv_type: str,
        content_type: str,
        content: dict,
    ) -> ApiResponse:
        """编辑已发送的消息"""
        payload = {
            "msgId": msg_id,
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": content_type,
            "content": content,
        }
        return await self._post("/bot/edit", payload)

    # 撤回消息 

    async def recall_message(
        self, msg_id: str, chat_id: str, chat_type: str
    ) -> ApiResponse:
        """撤回消息"""
        payload = {
            "msgId": msg_id,
            "chatId": chat_id,
            "chatType": chat_type,
        }
        return await self._post("/bot/recall", payload)

    # 获取消息列表 

    async def get_messages(
        self,
        chat_id: str,
        chat_type: str,
        before: str = "",
        after: str = "",
        limit: int = 20,
    ) -> ApiResponse:
        """获取消息列表"""
        params = {
            "chatId": chat_id,
            "chatType": chat_type,
        }
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if limit:
            params["limit"] = str(limit)
        return await self._get("/bot/messages", params)

    # 上传文件 

    async def upload_image(self, file_path: str) -> ApiResponse:
        """上传图片，返回 imageKey"""
        return await self._upload("/image/upload", file_path, field_name="image")

    async def upload_file(self, file_path: str) -> ApiResponse:
        """上传文件，返回 fileKey"""
        return await self._upload("/file/upload", file_path, field_name="file")

    async def upload_video(self, file_path: str) -> ApiResponse:
        """上传视频，返回 videoKey"""
        return await self._upload("/video/upload", file_path, field_name="video")

    # 看板 

    async def set_board(
        self,
        chat_id: str,
        chat_type: str,
        content_type: str,
        content: str,
        member_id: str = "",
        expire_time: int = 0,
    ) -> ApiResponse:
        """设置用户看板"""
        payload = {
            "chatId": chat_id,
            "chatType": chat_type,
            "contentType": content_type,
            "content": content,
        }
        if member_id:
            payload["memberId"] = member_id
        if expire_time:
            payload["expireTime"] = expire_time
        return await self._post("/bot/board", payload)

    async def set_board_all(
        self,
        chat_type: str,
        content_type: str,
        content: str,
        expire_time: int = 0,
    ) -> ApiResponse:
        """设置全部看板"""
        payload = {
            "chatType": chat_type,
            "contentType": content_type,
            "content": content,
        }
        if expire_time:
            payload["expireTime"] = expire_time
        return await self._post("/bot/board-all", payload)

    async def dismiss_board(
        self, chat_id: str, chat_type: str, member_id: str = ""
    ) -> ApiResponse:
        """取消用户看板"""
        payload = {"chatId": chat_id, "chatType": chat_type}
        if member_id:
            payload["memberId"] = member_id
        return await self._post("/bot/board-dismiss", payload)

    async def dismiss_board_all(self) -> ApiResponse:
        """取消全部看板"""
        return await self._post("/bot/board-all-dismiss", {})

    # 连接测试 

    async def test_connection(self) -> tuple[bool, str]:
        """测试 Token 是否有效，返回 (成功, 消息)"""
        resp = await self._get("/bot/messages", {
            "chatId": "test", "chatType": "user", "before": "1"
        })
        if resp.code == 1003:
            return False, "Token 无效（未授权）"
        elif resp.code == -1:
            return False, f"连接失败: {resp.msg}"
        else:
            # code 1002 (参数有误) 也说明 token 是有效的，服务器有响应
            return True, f"连接成功（code={resp.code}）"


# 流式消息辅助 

class _StreamIterator:
    """
    用于 aiohttp streamer 的异步迭代器。
    调用者通过 write() 方法注入数据块，通过 close() 结束流。
    """

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def write(self, data: bytes):
        """写入一段数据"""
        if self._closed:
            return
        await self._queue.put(data)

    async def close(self):
        """关闭流"""
        self._closed = True
        await self._queue.put(None)  # sentinel

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self._queue.get()
        if chunk is None:
            raise StopAsyncIteration
        return chunk
