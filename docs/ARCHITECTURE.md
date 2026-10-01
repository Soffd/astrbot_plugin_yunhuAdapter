# 模块结构

```text
astrbot_plugin_yunhuAdapter/
├── main.py                 # 插件注册、指令/工具/LLM钩子声明
├── _conf_schema.json       # AstrBot 配置表单与默认值
├── metadata.yaml           # 插件元数据
├── api/
│   ├── client.py           # HTTP API、上传、流式请求、限速与错误
│   └── models.py           # 云湖事件、消息、按钮和API响应模型
├── adapter/
│   ├── adapter.py          # Webhook/WebSocket、消息转换、事件提交
│   ├── config.py           # 从根目录schema加载配置
│   └── event.py            # AstrBot消息链发送与云湖事件辅助接口
├── media/
│   ├── cdn.py              # 云湖CDN下载与回退
│   ├── attachments.py      # 当前/引用附件选择和正文读取
│   ├── names.py            # 原始文件名、HTTP元数据与安全basename
│   ├── store.py            # 持久化副本、SQLite索引、会话隔离与容量清理
│   └── temp_files.py       # 临时文件缓存、移交和清理
├── services/
│   ├── operations.py       # 指令解析、操作执行、权限和会话校验
│   └── llm_inputs.py       # 提及信息、附件元数据和正文补充
├── docs/
│   ├── ARCHITECTURE.md
│   ├── CHANGELOG.md
│   └── FILE_STORAGE.md
├── tests/                  # 单元测试与本地HTTP/WebSocket集成测试
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── README.md
├── LICENSE
└── logo.png
```

`api` 处理云湖协议，不依赖 AstrBot。`adapter` 将协议转换成 AstrBot 消息及事件；`media` 负责资源下载和附件处理。`services` 在事件会话范围内提供操作与模型输入。`main.py` 只声明框架入口、工具参数和帮助文案，并调用功能模块。

AstrBot v4.28.2按处理函数的 `__module__` 绑定插件实例。因此 `@filter.command`、`@filter.llm_tool` 和 `@filter.on_llm_request` 保留在 `main.py`，业务实现放入子模块。直接把带装饰器的处理函数移到其他包再导入，会使框架无法按入口模块绑定这些函数，详见 [v4.28.2插件加载源码](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/star_manager.py)。

配置和元数据仍在插件根目录，供 AstrBot 发现。`adapter/config.py` 从该根目录读取 `_conf_schema.json`，不依赖当前工作目录。各Python子目录都有 `__init__.py`，内部通过相对导入连接。

收到附件后，适配器下载工作副本并移交给 AstrBot 事件清理，文件消息另存保留副本到文件库。引用缓存只保存会话内的资源元数据；引用时重新下载。正文读取接受当前/引用附件，以及本会话文件库中的文件名或文件ID，不能用任意本地路径读取其他文件。文件保留、容量和重启策略见 [文件保存策略](FILE_STORAGE.md)。

在插件包内导入扩展接口时使用新路径：

```python
from .api.client import YunhuClient
from .api.models import ApiResponse, Button, ButtonGroup
from .adapter.event import YunhuMessageEvent
```

更新现有部署时应同步完整目录，清理旧根目录的 `client.py`、`models.py`、`yunhu_adapter.py`、`yunhu_event.py`、`cdn_proxy.py`、`attachments.py`、`operations.py`，然后重启 AstrBot。
