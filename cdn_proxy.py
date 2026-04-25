"""
云湖 CDN 内置反向代理模块

实现多级回退的 CDN 资源下载策略，绕过云湖 CDN 的防盗链机制。

下载优先级：
  1. 内置反代：直接向云湖 CDN 发起请求，设置正确的 Host 和 Referer 头
  2. 自定义反代：用户在配置中指定的第三方反代地址
  3. 备用反代：chat-webp.000434.xyz（仅支持图片，稳定性不佳）

内置反代基于云湖 CDN 的实际域名映射：
  图片 -> chat-img.jwznb.com
  文件 -> chat-file.jwznb.com
  视频 -> chat-video1.jwznb.com
  音频 -> chat-audio1.jwznb.com

所有请求均携带 Referer: https://myapp.jwznb.com/ 以通过防盗链校验。
"""

import asyncio
import logging
import aiohttp
from typing import Optional

# 尝试从 astrbot 获取 logger，降级到标准 logging
try:
    from astrbot import logger as _logger
except ImportError:
    _logger = logging.getLogger("yunhu.cdn_proxy")


# 云湖 CDN 域名映射（内置反代用） 

_CDN_ROUTES = {
    "image": {
        "base_url": "https://chat-img.jwznb.com",
        "host": "chat-img.jwznb.com",
    },
    "file": {
        "base_url": "https://chat-file.jwznb.com",
        "host": "chat-file.jwznb.com",
    },
    "video": {
        "base_url": "https://chat-video1.jwznb.com",
        "host": "chat-video1.jwznb.com",
    },
    "audio": {
        "base_url": "https://chat-audio1.jwznb.com",
        "host": "chat-audio1.jwznb.com",
    },
}

# 自定义反代 URL 中的类型路径映射
_PROXY_TYPE_MAP = {
    "image": "image",
    "file": "file",
    "video": "video",
    "audio": "audio",
}

# 备用图片反代（社区提供，仅支持图片，稳定性不佳）
_BACKUP_IMAGE_PROXY = "https://chat-webp.000434.xyz"

# 通用浏览器 User-Agent
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# 云湖应用 Referer（通过防盗链校验的关键）
_APP_REFERER = "https://myapp.jwznb.com/"

# 各级下载超时（秒）
_BUILTIN_TIMEOUT = 60
_CUSTOM_TIMEOUT = 60
_BACKUP_TIMEOUT = 30


class CdnProxy:
    """
    云湖 CDN 多级回退下载器

    下载优先级：
      1. 内置反代（直接请求 CDN，设置正确的 Host 和 Referer）
      2. 自定义反代（用户配置的第三方反代地址）
      3. 备用反代（chat-webp.000434.xyz，仅图片）

    使用方式：
      proxy = CdnProxy(dl_session, custom_proxy_base="https://yhcdn.yunhucdn.top")
      data = await proxy.download("abc123.mp4", "video")
    """

    def __init__(
        self,
        dl_session: aiohttp.ClientSession,
        custom_proxy_base: str = "",
    ):
        """
        Args:
            dl_session: aiohttp 会话（由适配器提供，复用连接池）
            custom_proxy_base: 自定义反代基础地址，如 https://yhcdn.yunhucdn.top
                               插件会自动拼接 /{类型}/{key}。
                               留空表示不使用自定义反代。
        """
        self._session = dl_session
        self._custom_proxy_base = (
            custom_proxy_base.rstrip("/") if custom_proxy_base else ""
        )

    # 公开接口 

    async def download(self, key: str, kind: str) -> Optional[bytes]:
        """
        多级回退下载 CDN 资源

        Args:
            key: CDN 资源 key（通常包含扩展名，如 abc123.mp4）
            kind: 资源类型（image / file / video / audio）

        Returns:
            下载到的二进制数据；全部方式失败返回 None
        """
        if not key:
            return None

        # 内置反代
        data = await self._download_builtin(key, kind)
        if data is not None:
            _logger.info("[云湖] 内置反代下载成功: %s/%s", kind, _truncate_key(key))
            return data

        # 自定义反代
        data = await self._download_custom(key, kind)
        if data is not None:
            _logger.info("[云湖] 自定义反代下载成功: %s/%s", kind, _truncate_key(key))
            return data

        # 备用反代（仅图片）
        if kind == "image":
            data = await self._download_backup_image(key)
            if data is not None:
                _logger.info("[云湖] 备用反代下载成功: image/%s", _truncate_key(key))
                return data

        _logger.warning("[云湖] 所有下载方式均失败: %s/%s", kind, _truncate_key(key))
        return None

    # 内置反代 

    async def _download_builtin(self, key: str, kind: str) -> Optional[bytes]:
        """
        内置反代：直接请求 CDN，设置正确的 Host 和 Referer 头绕过防盗链。
        """
        route = _CDN_ROUTES.get(kind)
        if not route:
            _logger.debug("[云湖] 内置反代不支持的资源类型: %s", kind)
            return None

        url = route["base_url"] + "/" + key
        headers = {
            "Host": route["host"],
            "Referer": _APP_REFERER,
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=_BUILTIN_TIMEOUT)
            async with self._session.get(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    _logger.debug(
                        "[云湖] 内置反代下载失败 status=%s: %s",
                        resp.status,
                        _truncate_url(url),
                    )
                    return None

                data = await resp.read()

                if _is_html_response(data):
                    _logger.debug(
                        "[云湖] 内置反代返回 HTML 页面: %s", _truncate_url(url)
                    )
                    return None

                return data

        except asyncio.TimeoutError:
            _logger.debug("[云湖] 内置反代下载超时: %s", _truncate_url(url))
            return None
        except Exception as e:
            _logger.debug("[云湖] 内置反代下载异常: %s - %s", _truncate_url(url), e)
            return None

    # 自定义反代 

    async def _download_custom(self, key: str, kind: str) -> Optional[bytes]:
        """
        自定义反代：通过用户配置的反代地址下载。

        URL 格式：{custom_proxy_base}/{type}/{key}
        例如：https://yhcdn.yunhucdn.top/video/abc123.mp4
        """
        if not self._custom_proxy_base:
            return None

        proxy_type = _PROXY_TYPE_MAP.get(kind, kind)
        url = self._custom_proxy_base + "/" + proxy_type + "/" + key

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=_CUSTOM_TIMEOUT)
            async with self._session.get(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    _logger.debug(
                        "[云湖] 自定义反代下载失败 status=%s: %s",
                        resp.status,
                        _truncate_url(url),
                    )
                    return None

                data = await resp.read()

                if _is_html_response(data):
                    _logger.debug(
                        "[云湖] 自定义反代返回 HTML 页面: %s", _truncate_url(url)
                    )
                    return None

                return data

        except asyncio.TimeoutError:
            _logger.debug("[云湖] 自定义反代下载超时: %s", _truncate_url(url))
            return None
        except Exception as e:
            _logger.debug("[云湖] 自定义反代下载异常: %s - %s", _truncate_url(url), e)
            return None

    # 备用反代（仅图片） 

    async def _download_backup_image(self, key: str) -> Optional[bytes]:
        """
        备用反代：通过 chat-webp.000434.xyz 下载图片。

        该反代仅支持图片，且稳定性不佳，仅作为最后手段。
        """
        url = _BACKUP_IMAGE_PROXY + "/" + key

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,"
                "image/*,*/*;q=0.8"
            ),
            "Referer": "https://chat-go.jwzhd.com/",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=_BACKUP_TIMEOUT)
            async with self._session.get(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    _logger.debug(
                        "[云湖] 备用反代下载失败 status=%s: %s",
                        resp.status,
                        _truncate_url(url),
                    )
                    return None

                data = await resp.read()

                if _is_html_response(data):
                    _logger.debug(
                        "[云湖] 备用反代返回 HTML 页面: %s", _truncate_url(url)
                    )
                    return None

                return data

        except asyncio.TimeoutError:
            _logger.debug("[云湖] 备用反代下载超时: %s", _truncate_url(url))
            return None
        except Exception as e:
            _logger.debug("[云湖] 备用反代下载异常: %s - %s", _truncate_url(url), e)
            return None


# 工具函数 


def _is_html_response(data: bytes) -> bool:
    """检查响应数据是否为 HTML 页面（错误页面或人机验证）"""
    if not data:
        return False
    stripped = data.lstrip()
    return stripped.startswith(b"<html") or stripped.startswith(b"<!DOCTYPE html")


def _truncate_key(key: str, max_len: int = 40) -> str:
    """截断过长的 key 用于日志输出"""
    if len(key) <= max_len:
        return key
    return key[:max_len] + "..."


def _truncate_url(url: str, max_len: int = 80) -> str:
    """截断过长的 URL 用于日志输出"""
    if len(url) <= max_len:
        return url
    return url[:max_len] + "..."
