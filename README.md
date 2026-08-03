# 群消息监听（group-msg-monitor）

本机运行的群消息采集与分析工具：接收指定群聊消息，落库、关键词告警，并可选调用大模型生成结构化分析报告。提供 **Tauri 桌面端** 与 **Python 监控服务**。

> **核心约束**：不向目标群拉入机器人账号；使用你**已在群内的个人号**收消息（经协议桥或官方 QQ 被动采集）。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| QQ 实时采集 | **NapCat / OneBot 11** WebSocket（推荐）；或 **官方 QQ 被动**（系统通知 + UI Automation，易漏消息） |
| 消息落库 | SQLite 去重存储，支持图片等媒体本地缓存 |
| 关键词告警 | 命中后 POST Webhook（兼容飞书 / 钉钉常见字段） |
| LLM 分析 | 定时 / 手动总结：主题、要点、风险、待办、名词剖析、深入分析；支持追问与本地收藏 |
| 桌面端 | 实时监控、分群配置、报告浏览、皮肤主题、收藏夹 |
| 多通道（实验） | 微信 / Telegram 代码保留，**当前默认关闭** |

---

## 基本实现原理

```text
┌─────────────┐     ┌──────────────────┐     ┌────────────────────┐
│  QQ / 通道   │ ──► │  采集适配层        │ ──► │  过滤 / 落库 / 告警  │
│  个人号收消息 │     │  OneBot / 被动等   │     │  handlers + SQLite  │
└─────────────┘     └──────────────────┘     └─────────┬──────────┘
                                                       │
                                                       ▼
                                             ┌────────────────────┐
                                             │  LLM 分析层（可选）  │
                                             │  切窗 → 调模型 →    │
                                             │  JSON 报告落库      │
                                             └─────────┬──────────┘
                                                       │
                                                       ▼
                                             ┌────────────────────┐
                                             │  Tauri 桌面端       │
                                             │  启停 / 配置 / 报告 │
                                             └────────────────────┘
```

**分层说明：**

1. **采集层**：把「本机已登录账号看到的消息」变成统一的 `GroupMessageEvent`。QQ 推荐路径是 NapCat 将 NTQQ 消息转为 OneBot 事件，经本机 WebSocket 推送；不依赖官方开放平台机器人进群。
2. **业务层**：按群白名单过滤 → 日志 → SQLite（`message_id` 去重）→ 关键词 Webhook。采集与 LLM **解耦**：模型挂了仍可继续收消息。
3. **分析层**：按时间窗取消息，多轮补上下文后调用 OpenAI 兼容 / OpenCode / Cursor 等 Provider，强制 JSON 输出并生成 Markdown 报告。终稿请求带超时与指数退避重试。
4. **呈现层**：桌面端通过本地 API 启停监控、改配置、刷消息与报告；不把原始聊天默认上传到无关第三方。

**为何不用本机 `nt_msg.db`？**  
新版 QQ 本地库多为加密 SQLite，不适合作实时主路径。协议桥或被动采集更适合「秒级到达」的监控场景。

---

## 环境要求

| 组件 | 要求 |
|------|------|
| 系统 | Windows 10/11（桌面端与官方 QQ 被动以 Windows 为主） |
| Python | 3.11+（建议） |
| Node.js | 18+（桌面端） |
| Rust | 安装 [Tauri 前置依赖](https://v2.tauri.app/start/prerequisites/)（首次跑桌面端需要） |
| QQ 协议端 | [NapCatQQ](https://napneko.github.io)（OneBot 模式）或本机已登录的官方 QQ（被动模式） |

---

## 初始化教程

### 方式 A：桌面端（推荐）

适合扫码、启停服务、分群配置与看报告。

```powershell
cd D:\project\group-msg-monitor
# 或双击 start-desktop.bat

# 1) Python 依赖（监控服务会被桌面端拉起）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 桌面端
cd desktop
npm install
npm run tauri dev
```

首次进入建议顺序：

1. **总配置**：填写 OneBot `ws_url` / `access_token`（与 NapCat 一致），或切换 QQ 为「被动」模式。  
2. 配置至少一个 **LLM Provider**（OpenAI 兼容网关 / 本地 Ollama 等），并设为当前使用。  
3. **群列表**：刷新群，开启目标群的「监听」与可选「LLM」。  
4. **实时监控**：启动监控服务；确认目标群发言后列表有新消息。  
5. 在群配置中点 **立即执行 LLM 分析**，到报告页查看结果。

辅助脚本（仓库根目录）：

| 脚本 | 作用 |
|------|------|
| `start-desktop.bat` | 启动 Tauri 桌面端 |
| `start-monitor.bat` | 仅启动 Python 监控服务 |
| `start-napcat.bat` / `restart-napcat.bat` | 按本机路径启停 NapCat（需自行改路径） |

### 方式 B：仅命令行监控服务

```powershell
cd D:\project\group-msg-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# 编辑 .env：ONEBOT_* 、可选 MONITOR_GROUP_IDS

python -m app.main
# 或 start-monitor.bat
```

配置优先级：**环境变量 > `.env` > `config.yaml` > 默认值**。  
分群开关、LLM、主题等以桌面端写入的 `data/app_settings.json` 与 `data/group_configs/` 为准（本地文件，已 gitignore）。

### 配置 NapCat（OneBot 模式）

1. 安装并启动 [NapCatQQ](https://napneko.github.io)。  
2. 用**已在目标群内**的 QQ 号登录。  
3. 网络配置示例（务必绑定本机，勿公网暴露）：

```json
{
  "network": {
    "websocketServers": [
      {
        "name": "ws-monitor",
        "enable": true,
        "host": "127.0.0.1",
        "port": 3001,
        "token": "与 ONEBOT_ACCESS_TOKEN / 桌面总配置相同",
        "messagePostFormat": "string",
        "reportSelfMessage": false,
        "heartInterval": 30000
      }
    ]
  }
}
```

### 官方 QQ 被动模式（备选）

不注入、不 Hook、不走私有协议：依赖 **Windows 通知** + **当前会话 UI Automation**。

- 需本机官方 QQ 在线，并允许通知。  
- 静音群、关通知、未打开会话时**可能漏消息**。  
- 图片通常仅占位，不下载原图。  
- 适合协议端不可用时的兜底，不建议作为唯一生产路径。

### 最小 `.env` 说明

| 项 | 说明 |
|----|------|
| `ONEBOT_WS_URL` | 默认 `ws://127.0.0.1:3001` |
| `ONEBOT_ACCESS_TOKEN` | 与 NapCat Token 一致的强随机串 |
| `MONITOR_GROUP_IDS` | 可选；优先用桌面分群配置 |
| `STORAGE_SQLITE_PATH` | 默认 `./data/messages.db` |
| `ALERT_WEBHOOK_URL` | 关键词告警 Webhook（可选） |

YAML 示例见 `config.yaml.example`。

---

## 桌面端界面

| 区域 | 用途 |
|------|------|
| 实时监控 | 全群 / 当前群消息流；启停监控 |
| 群列表 | 搜索排序；开关监听与 LLM；进入分群配置 |
| 分析报告 | 结构化要点、深入分析、追问、本地收藏 |
| 收藏夹 | 收藏的报告与追问问答 |
| 总配置 | OneBot、QQ 模式、LLM Provider、皮肤主题 |

群详情可配：自定义提示词、执行间隔、窗口时长、最少消息数、模型覆盖、立即分析。

---

## LLM 分析要点

- **触发**：定时（按群间隔）或手动。  
- **输入**：时间窗内消息（手动会限制条数 / 字符，避免撑爆上下文）。  
- **输出**：JSON → 规范化 → Markdown 报告；失败写入失败记录（超时等）。  
- **Provider**：`openai_compatible`（通义 / DeepSeek / Ollama 等）、`opencode`、`cursor`。  
- **韧性**：终稿请求默认约 300s 超时；对超时、网络错误、429/5xx 指数退避重试。

设计细节见 `docs/LLM聊天总结与分析设计.md`。

---

## 目录结构

```text
group-msg-monitor/
├── README.md
├── requirements.txt
├── .env.example
├── config.yaml.example
├── start-desktop.bat / start-monitor.bat
├── app/                     # Python 监控与分析
│   ├── main.py              # 服务入口
│   ├── onebot_client.py     # OneBot WS
│   ├── channels/            # QQ 被动 / 微信 / Telegram
│   ├── handlers/            # 日志、落库、告警
│   ├── llm/                 # 总结、追问、报告
│   └── settings_store.py    # 全局与分群 JSON 配置
├── desktop/                 # Tauri + TypeScript UI
├── scripts/                 # 桌面 API、被动采集探针、诊断脚本
├── tests/                   # 单元测试
├── docs/                    # 设计文档
├── data/                    # 本地数据（gitignore）
└── logs/                    # 滚动日志（gitignore）
```

---

## 开发与测试

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

模拟 OneBot 可用 `scripts/mock_onebot_ws.py`（需自行对照参数）。

---

## 验收对照

| 场景 | 预期 |
|------|------|
| 目标群发文本 | 数秒内出现在日志 / 桌面消息流 |
| 未开启监听的群 | 忽略 |
| Token 错误 | OneBot 连不上或鉴权失败 |
| NapCat 重启 | 监控端指数退避重连 |
| 富媒体 | 多为 `[图片]` 等摘要；开启媒体时尝试本地下载 |
| 手动 LLM | 后台执行，完成后报告列表更新；超时会重试再记失败 |

---

## 免责声明

1. **非官方产品**  
   本项目与腾讯 QQ、各即时通讯厂商、各模型服务商均无隶属或授权关系。NapCat 等协议端属社区方案，存在**账号风控、冻结、功能失效**等风险，请自行评估。

2. **仅供个人 / 内网合规使用**  
   使用者须确保对所监听群聊具备合法权限，并遵守当地法律法规、群规及平台服务条款。禁止用于窃听、骚扰、窃取商业秘密或其他侵害他人权益的行为。

3. **数据与隐私**  
   消息默认存于本机 `data/`。启用云端 LLM 时，相关文本会发送至你配置的 API 地址；请勿在未脱敏场景将敏感信息送往不可信服务。作者不对数据泄露或第三方模型侧留存承担责任。

4. **可靠性**  
   被动采集、网络抖动、模型超时等均可导致漏消息或分析失败。本软件按「现状」提供，**不保证**实时性、完整性或分析结果正确性。

5. **账号与安全**  
   建议使用专用小号、只收不发；OneBot 仅绑定 `127.0.0.1`，切勿映射公网。同号可能与日常 QQ 客户端冲突，需自行实测。

6. **免责范围**  
   因使用本软件导致的账号损失、数据损失、业务中断或任何间接损害，开发维护者不承担责任。继续使用即视为已阅读并同意上述条款。

---

## 更多文档

| 文档 | 内容 |
|------|------|
| `QQ群消息实时监控方案大纲.md` | 选型、约束与 MVP 方案 |
| `docs/方案大纲.md` | 方案大纲副本 |
| `docs/LLM聊天总结与分析设计.md` | LLM 分层、触发与输出约定 |
| `desktop/README.md` | 桌面端开发简要说明 |

---

## License

以仓库内实际声明为准；若无单独 License 文件，默认仅供学习与内部自用，转载或二次分发请保留本免责声明并自行承担合规责任。
