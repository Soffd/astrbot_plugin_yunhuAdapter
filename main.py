# -*- coding: utf-8 -*-
"""
云湖平台适配器插件入口
"""
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_yunhuAdapter",
    "Yuki Soffd",
    "云湖平台适配器，通过 WebSocket 与云湖 SDK 通信",
    "1.0.0",
)
class YunhuPlugin(Star):
    def __init__(self, context: Context):
        # 导入即自动注册适配器（装饰器 @register_platform_adapter 生效）
        from .yunhu_adapter import YunhuAdapter  # noqa: F401
