# AstrBot 云湖平台适配器

> 将 AstrBot 接入[云湖](https://www.yhchat.com/)机器人平台的插件，直连云湖 HTTP API 与 WebSocket/Webhook 事件，新版本无需第三方 SDK。

---

## 功能特性

- **独立直连**：不再依赖任何外部 SDK，通过机器人 Token 直接与云湖服务端通信。
- **双模式连接**：
  - **WebSocket**（推荐）：主动连接云湖 WS 服务，无需公网 IP，资源占用低。
  - **Webhook**：在本地启动 HTTP 服务器接收推送，适合有公网 IP 或内网穿透的场景。
- **全消息类型**：支持文本、Markdown、图片、文件、视频的收发。
- **智能媒体处理**：
  - 内置 CDN 防盗链绕过（多级回退反代），将云湖媒体下载到本地，供所有插件/Agent 使用。
  - 临时文件自动管理，过期清理。
- **高级交互**：支持按钮组、看板、流式消息、消息编辑/撤回、批量发送。
- **回复线程**：可选开启线程回复，消息以子消息形式展示。
- **自动重连**：WebSocket 断线后指数退避自动重连，无需干预。
- **智能分段**：超长文本按段落边界自动切割，Markdown 内容自动识别并以 Markdown 格式发送。

---

## 目录结构

```
astrbot_plugin_yunhuAdapter/
├── main.py              # 插件入口
├── yunhu_adapter.py     # 平台适配器（连接管理、事件转换、媒体处理）
├── yunhu_event.py       # 消息事件（发送逻辑、多类型支持、分段等）
├── client.py            # 云湖 HTTP API 客户端（发消息、上传等）
├── cdn_proxy.py         # CDN 反代下载模块（防盗链绕过）
├── models.py            # 数据模型（事件/API 解析）
├── _conf_schema.json    # 配置项 Schema（管理面板展示用）
└── metadata.yaml        # 插件元数据
```

---

## 前置条件

| 依赖 | 说明 |
|------|------|
| AstrBot | 主框架，需提前安装并运行。 |
| 云湖机器人 Token | 在云湖控制台「Token」中获取。 |
| Python ≥ 3.10 | 运行环境。 |
| 网络环境 | 需能访问 `chat-go.jwzhd.com`（API）及 WebSocket 地址。 |

> 不需要安装任何云湖官方 SDK！

---

## 配置

在 AstrBot 管理面板 → 机器人 → 云湖适配器中填写参数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bot_token` | string | `""` | **必填**。从云湖控制台获取的机器人 Token。 |
| `connection_mode` | string | `"websocket"` | 连接模式：`websocket`（推荐）或 `webhook`。 |
| `websocket_url` | string | `"wss://ws.jwzhd.com/subscribe"` | WebSocket 服务地址（仅 `websocket` 模式）。 |
| `webhook_host` | string | `"0.0.0.0"` | Webhook 监听地址（仅 `webhook` 模式）。 |
| `webhook_port` | int | `6195` | Webhook 监听端口（仅 `webhook` 模式）。 |
| `webhook_path` | string | `"/webhook"` | Webhook 回调路径，需与云湖控制台设置一致（仅 `webhook` 模式）。 |
| `reply_in_thread` | bool | `false` | 开启后回复以线程方式展示。 |
| `media_ttl` | int | `600` | 媒体缓存时间（秒），过期自动清理。 |
| `custom_cdn_proxy` | string | `"https://yhcdn.yunhucdn.top"` | 自定义 CDN 反代基础地址，为空则只使用内置反代。 |

**WebSocket 模式配置示例：**
```json
{
  "bot_token": "your_bot_token",
  "connection_mode": "websocket",
  "websocket_url": "wss://ws.jwzhd.com/subscribe",
  "reply_in_thread": false
}
```
**Webhook 模式配置示例：**
```json
{
  "bot_token": "your_bot_token",
  "connection_mode": "webhook",
  "webhook_host": "0.0.0.0",
  "webhook_port": 6195,
  "webhook_path": "/webhook"
}
```
> Webhook 模式下需确保云湖平台能访问你配置的地址（公网 IP 或内网穿透），并将回调地址填写为 `http(s)://你的IP/域名:端口/webhook`。

---

## 部署步骤

### 1. 获取云湖机器人 Token
登录[云湖开放平台](https://www.yhchat.com/control)，创建或进入已有机器人，复制 **Bot Token**。

### 2. 配置插件
在 AstrBot 管理面板中填入 Token 并选择连接模式，推荐 **WebSocket** 模式（即开即用，无需额外网络配置）。

### 3. 启动 AstrBot
```bash
python main.py
```
启动日志中出现以下信息即表示连接成功：
```
[云湖] WebSocket 已连接
```

### 4. 测试
在云湖平台向机器人发送一条消息，若正常回复则部署成功。

---

## 消息类型支持

| 类型 | 接收 | 发送 |
|------|:----:|:----:|
| 纯文本 | ✅ | ✅ |
| Markdown | ✅ | ✅（自动识别并分段） |
| 图片 | ✅ | ✅（本地/远程URL/base64/imageKey） |
| 文件 | ✅ | ✅（本地/远程URL/fileKey） |
| 视频 | ✅ | ✅（本地/远程URL/videoKey） |
| 按钮交互 | ✅ | ✅（文本+按钮组） |
| 流式消息 | - | ✅（已封装，可直接调用） |

---

## 常见问题

**Q：WebSocket 模式下一直重连？**  
检查 `bot_token` 是否正确，确保网络可访问 `wss://ws.jwzhd.com/subscribe`。部分服务器可能需要配置代理。

**Q：Webhook 模式收不到事件？**  
- 确认云湖控制台的回调地址已填写为 `http://你的IP:端口/your_path`（如 `http://12.34.56.78:6195/webhook`）。  
- 检查防火墙/安全组是否放行了对应端口。  
- 使用内网穿透时需确保穿透工具稳定。

**Q：自定义 CDN 反代怎么用？**  
搭建自己的 CDN 反代服务（如 Nginx），确保能够正确代理云湖 CDN 资源，然后在 `custom_cdn_proxy` 中填入你的反代基础地址（如 `https://my-proxy.example.com`）。留空则自动回退到内置反代方案，Nginx 参考。
```nginx
    location ^~ /img/ {
        proxy_pass https://chat-img.jwznb.com/ ;
        proxy_set_header Host chat-img.jwznb.com;
        proxy_set_header Referer https://myapp.jwznb.com/ ;
        proxy_ssl_server_name on;
    }

    location ^~ /file/ {
        proxy_pass https://chat-file.jwznb.com/ ;
        proxy_set_header Host chat-file.jwznb.com;
        proxy_set_header Referer https://myapp.jwznb.com/ ;
        proxy_ssl_server_name on;
    }
    
    location ^~ /video/ {
        proxy_pass https://chat-video1.jwznb.com/ ;
        proxy_set_header Host chat-video1.jwznb.com;
        proxy_set_header Referer https://myapp.jwznb.com/ ;
        proxy_ssl_server_name on;
    }
    
    location ^~ /audio/ {
        proxy_pass https://chat-audio1.jwznb.com/ ;
        proxy_set_header Host chat-audio1.jwznb.com;
        proxy_set_header Referer https://myapp.jwznb.com/ ;
        proxy_ssl_server_name on;
    }
```

---

## 协议

MIT License
```