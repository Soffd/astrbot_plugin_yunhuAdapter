"""
云湖平台适配器插件入口

通过 Webhook 直接接收云湖平台推送的事件，
通过 HTTP API 直接发送消息，与 AstrBot 共用同一个服务进程。
"""
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_yunhuAdapter",
    "Yuki Soffd",
    "云湖平台适配器，通过 Webhook 直连云湖平台，无需额外运行 WebSDK",
    "2.0.0",
)
class YunhuPlugin(Star):
    def __init__(self, context: Context):
        from .yunhu_adapter import YunhuAdapter