# AstrBot 云湖平台适配器

> 将 AstrBot 接入[云湖](https://www.yhchat.com/)机器人平台的插件，通过 WebSocket 与云湖 SDK 桥接通信。
注意！此插件无法独立使用！
---

## 更新说明

## v1.1.1
- 兼容性修订

## v1.1.0 （本次需要同时更新 [yunhubot-websdk](https://github.com/Soffd/yunhubot-websdk "yunhubot-websdk") 才能正常使用）
- 修复发送一些插件提交的图片返回 413 的错误。
- 修复无法识别 markdown 消息的错误。
- 修复无法获取图片的错误。
- 添加了机器人 token 配置，现在没什么用，为以后准备的。

## 功能特性

- **双向通信**：通过 WebSocket 连接云湖 SDK，实时接收平台消息并将 AstrBot 的回复发回云湖。
- **多消息类型**：支持文本、图片、文件、视频的收发。
- **群聊 & 私聊**：自动识别群聊（`chatType=group`）和私聊（`chatType=bot`），正确路由消息。
- **线程回复模式**：可选开启，开启后回复将作为原消息的子消息（线程）展示。
- **自动重连**：WS 断线后每 5 秒自动尝试重连，无需手动干预。
- **发送重试**：每条消息最多重试 3 次（指数退避），提升发送可靠性。
- **本地 & 远程媒体**：本地文件通过 Base64 随 WS 发送由 SDK 上传；远程 URL 直接透传给 SDK 下载，避免双重传输。

---

## 目录结构

```
astrbot_plugin_yunhuAdapter/
├── main.py              # 插件入口，注册 YunhuPlugin
├── yunhu_adapter.py     # 平台适配器，处理连接、事件接收与消息转换
├── yunhu_event.py       # 消息事件类，负责将 MessageChain 发回云湖 SDK
├── _conf_schema.json    # 配置项描述（供 AstrBot 管理界面展示）
└── metadata.yaml        # 插件元数据
```

---

## 前置条件

| 依赖 | 说明 |
|------|------|
| AstrBot | 主框架，需提前安装并运行 |
| 云湖 SDK | 云湖官方提供的 SDK，负责与云湖 HTTP API 对接，并暴露 WS 桥接端口。推荐用我做的配套使用[yunhubot-websdk](https://github.com/Soffd/yunhubot-websdk "yunhubot-websdk") |
| Python ≥ 3.10 | `aiohttp`、`asyncio` 等依赖 |

---

## 安装

### 方式一：通过 AstrBot 插件市场（推荐）

在 AstrBot 管理面板的插件市场中搜索 **astrbot_plugin_yunhuAdapter** 并安装。

### 方式二：手动安装

```bash
# 进入 AstrBot 插件目录
cd /path/to/AstrBot/data/plugins/

# 克隆仓库
git clone https://github.com/Soffd/astrbot_plugin_yunhuAdapter.git
```

安装完成后重启 AstrBot 即可加载插件。

---

## 配置

在 AstrBot 管理面板 → 平台配置 → 云湖适配器中填写以下参数，或直接编辑配置文件：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `ws_url` | string | `ws://127.0.0.1:8080/ws` | 云湖 SDK 暴露的 WebSocket 桥接地址 |
| `ws_token` | string | `""` | WS 鉴权 Token，与 SDK 启动参数 `--ws-token` 保持一致，不需要鉴权则留空 |
| `reply_in_thread` | bool | `false` | 开启后回复带 `parentId`，在原消息下方以线程形式展示 |

**配置示例：**

```json
{
  "ws_url": "ws://127.0.0.1:8080/ws",
  "ws_token": "your_secret_token",
  "reply_in_thread": false
}
```

---

## 部署步骤

### 1. 配置云湖 SDK

按云湖官方文档启动 SDK，并开启 WS 桥接模式

SDK 启动后会在指定端口暴露 WebSocket 服务（默认 `ws://127.0.0.1:8080/ws`）。

> 不会用就用[yunhubot-websdk](https://github.com/Soffd/yunhubot-websdk "yunhubot-websdk")，省时省力

### 2. 安装并配置插件

参考上方「安装」章节完成插件安装，然后在 AstrBot 管理面板填写 `ws_url` 等配置项，确保地址能够从 AstrBot 所在机器访问到 SDK。

### 3. 启动 AstrBot

```bash
python main.py
# 或使用 AstrBot 官方启动方式
```

AstrBot 加载插件后，适配器会自动连接 SDK 的 WS 端点，日志中出现以下内容表示连接成功：

```
[云湖] WS 连接成功，开始接收事件
```

### 4. 验证

在云湖平台向机器人发送一条消息，AstrBot 正常响应则说明配置完成。

---

## 消息类型支持

| 类型 | 接收 | 发送 |
|------|:----:|:----:|
| 文本 | ✅ | ✅ |
| 图片 | ✅ | ✅（本地文件 / 远程 URL / imageKey）|
| 文件 | ✅ | ✅（本地文件 / 远程 URL / fileKey）|
| 视频 | ✅ | ✅（本地文件 / 远程 URL / videoKey）|

---

## 常见问题

**Q：日志显示 `WS 连接异常` 后循环重连？**

检查 `ws_url` 地址是否正确，以及云湖 SDK 是否已正常启动并监听该端口。

**Q：消息发出去但云湖没收到？**

确认 `ws_token` 与 SDK 启动参数一致；若不需要鉴权，`ws_token` 应留空。

**Q：群聊消息没有回复？**

确认云湖 SDK 版本支持群聊事件（`message.receive.normal`），并检查机器人是否已被添加到群中。

**Q：图片下载成功，但是其他插件无法处理图片？**

云湖官方的图片有鉴权（`Referer: https://myapp.jwznb.com`），其他插件没有请求头会被拦截，目前没想到解决办法，只能将图片提交给 LLM 。

---

## 协议

MIT License


---


