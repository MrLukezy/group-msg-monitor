# QQ 群消息实时监控

本机小型服务：通过 NapCat（OneBot 11）WebSocket 实时接收指定 QQ 群消息，支持日志、SQLite 落库、关键词 Webhook 告警。

> 约束：不向目标群拉入机器人号；使用已在群内的个人号（NapCat 登录）收消息。

## 架构

```text
QQ 群 → NapCat(个人号) → OneBot WS(127.0.0.1) → 本监控服务 → 日志 / SQLite / Webhook
```

## 快速开始

### 推荐：桌面界面（Tauri）

命令行二维码易乱码时，用图形界面扫码与启停服务：

```powershell
cd D:\project\group-msg-monitor
# 或双击 start-desktop.bat
cd desktop
npm install
npm run tauri dev
```

界面分页签：

1. **实时监控**：全群消息滚动；启停监控服务 / NapCat  
2. **群列表**：手动刷新；最近消息序 / 名称序 / 搜索；点选进入该群配置（基础 + 关键词 + LLM）  
3. **总配置**：OneBot 与 LLM Provider（OpenAI 兼容 / OpenCode / Cursor SDK）

群详情支持：自定义 LLM 提示词、执行间隔、模型覆盖、**立即执行 LLM 分析**。

### 1. 安装依赖（命令行模式）

```powershell
cd D:\project\group-msg-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置

```powershell
copy .env.example .env
# 或
copy config.yaml.example config.yaml
```

至少修改：

| 项 | 说明 |
|----|------|
| `ONEBOT_WS_URL` / `onebot.ws_url` | NapCat 正向 WS，默认 `ws://127.0.0.1:3001` |
| `ONEBOT_ACCESS_TOKEN` | 与 NapCat 中 Token 一致的强随机串 |
| `MONITOR_GROUP_IDS` | 目标群号，逗号分隔 |

优先级：**环境变量 > `.env` > `config.yaml` > 默认值**。

### 3. 配置 NapCat（协议端）

1. 安装并启动 [NapCatQQ](https://napneko.github.io)
2. 用**已在目标群内**的 QQ 号登录
3. 网络配置建议：

```json
{
  "network": {
    "websocketServers": [
      {
        "name": "ws-monitor",
        "enable": true,
        "host": "127.0.0.1",
        "port": 3001,
        "token": "与 .env 中 ONEBOT_ACCESS_TOKEN 相同",
        "messagePostFormat": "string",
        "reportSelfMessage": false,
        "heartInterval": 30000
      }
    ]
  }
}
```

务必绑定 `127.0.0.1`，不要公网暴露。

### 4. 启动监控服务

```powershell
python -m app.main
```

群内有人发言后，控制台与 `logs/monitor.log` 应在数秒内出现记录；若开启落库，数据写入 `data/messages.db`。

## 功能说明

| 功能 | 配置 | 说明 |
|------|------|------|
| 群白名单 | `MONITOR_GROUP_IDS` | 仅处理列出的群 |
| 全量日志 | `MONITOR_LOG_ALL=true` | 目标群消息写入控制台与文件 |
| SQLite | `STORAGE_ENABLED=true` | 按 `message_id` 去重落库 |
| 关键词告警 | `ALERT_ENABLED` + `MONITOR_KEYWORDS` + `ALERT_WEBHOOK_URL` | 命中后 POST Webhook |

Webhook payload 同时包含飞书/钉钉常见字段，多数机器人可直接接收。

## 目录结构

```text
group-msg-monitor/
├── QQ群消息实时监控方案大纲.md
├── README.md
├── .env.example
├── config.yaml.example
├── requirements.txt
├── app/
│   ├── main.py
│   ├── config.py
│   ├── onebot_client.py
│   ├── filters.py
│   ├── models.py
│   └── handlers/
│       ├── log_handler.py
│       ├── store_handler.py
│       └── alert_handler.py
├── data/          # SQLite（gitignore）
└── logs/          # 滚动日志（gitignore）
```

## 验收对照

| 场景 | 预期 |
|------|------|
| 目标群发文本 | 数秒内出现日志 |
| 其他群发言 | 忽略（debug 级别可见） |
| Token 错误 | 连不上 / 鉴权失败 |
| NapCat 重启 | 自动指数退避重连 |
| 图片等富媒体 | 记录为 `[图片]` 等摘要 |

## 风险提示

- NapCat 属社区协议端，存在账号风控可能；建议专用小号、只收不发
- 同号可能与日常 QQ 客户端抢登录，需自行实测
- 切勿将 OneBot 端口映射到公网

更完整的方案说明见 `QQ群消息实时监控方案大纲.md`。
