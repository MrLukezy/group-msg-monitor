# LLM 聊天内容总结与分析 — 设计方案

> 版本：v0.1  
> 日期：2026-07-27  
> 状态：设计（待实现）  
> 原则：**监控优先**；LLM 是分析层，不做成聊天客户端

---

## 1. 目标

在现有「NapCat → OneBot → 落库/告警」之上，增加 **LLM 分析层**，让运营/研发能快速回答：

1. **这段时间群里在聊什么？**（总结）
2. **有没有风险/异常/需跟进？**（分析）
3. **某人/某主题最近说了什么？**（检索增强问答）

### 成功标准

| 编号 | 标准 | 说明 |
|------|------|------|
| L1 | 可定时产出 | 按小时/天自动生成群摘要，可在桌面端查看 |
| L2 | 可手动触发 | 选定时间范围一键「现在总结」 |
| L3 | 结构化结果 | 输出固定字段（主题、要点、风险、待办），便于展示与告警 |
| L4 | 可控成本 | 支持开关、抽样、token 上限、本地模型 |
| L5 | 隐私默认本机 | API Key 仅本地；可选不上云（Ollama） |

### 非目标（本期不做）

- 不做完整聊天 UI / 代发消息机器人人设对话
- 不做全量历史向量化检索（可二期）
- 不做图片/语音多模态理解（文本为主；富媒体记类型摘要即可）
- 不把原始聊天默认同步到第三方 SaaS 以外的「训练用途」

---

## 2. 总体架构

```text
                    ┌──────────────────────────────────────┐
                    │           现有监控主链路               │
群消息 → NapCat → OneBot WS → filters → log/store/alert   │
                    └───────────────┬──────────────────────┘
                                    │ SQLite messages
                                    ▼
                    ┌──────────────────────────────────────┐
                    │           LLM 分析层（新增）            │
                    │  window_builder → llm_client →        │
                    │  report_store / alert_bridge          │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────┴──────────────────────┐
                    │  Tauri：报告列表 / 手动总结 / 风险卡片  │
                    └──────────────────────────────────────┘
```

**关键分层：**

| 层 | 职责 | 是否调用 LLM |
|----|------|--------------|
| 采集层 | 收消息、过滤、落库 | 否 |
| 分析调度层 | 按窗口切消息、触发任务、限流 | 否 |
| LLM 层 | 提示词 + 模型调用 + JSON 校验 | 是 |
| 呈现/告警层 | 桌面展示、Webhook 推送高风险摘要 | 否（只用结果） |

采集与分析解耦：LLM 挂了，监控仍可收消息；分析可事后补跑。

---

## 3. 分析能力矩阵（做什么）

### 3.1 三种触发方式

| 模式 | 触发 | 输入窗口 | 输出 | 优先级 |
|------|------|----------|------|--------|
| A. 定时总结 | Cron：每 N 分钟 / 每小时 / 每天 | 上一完整窗口 | 群摘要报告 | P0 |
| B. 手动总结 | 桌面端按钮 | 用户选时间范围或「最近 K 条」 | 同上 | P0 |
| C. 实时轻分析 | 每条或小批次（可选） | 单条/滑动短窗 | 风险标签、是否告警 | P1 |

建议一期先做 **A + B**；C 默认关闭（贵、吵），仅对「已开告警的群」或命中关键词后再调 LLM。

### 3.2 报告内容（统一结构化）

所有总结强制要求模型输出 JSON（失败则重试/降级为纯文本存档）：

```json
{
  "period": { "start": "...", "end": "...", "msg_count": 128 },
  "headline": "一句话概括",
  "topics": [
    { "title": "主题", "summary": "简述", "heat": "high|mid|low", "speakers": ["张三"] }
  ],
  "key_points": ["要点1", "要点2"],
  "risks": [
    { "level": "high|mid|low", "type": "投诉|泄密|对立|求助|其他", "detail": "...", "evidence": ["原句摘录"] }
  ],
  "action_items": [
    { "owner_hint": "可能负责人/角色", "task": "建议跟进事项", "priority": "high|mid|low" }
  ],
  "sentiment": "positive|neutral|negative|mixed",
  "notable_users": [
    { "name": "张三", "role_hint": "活跃/投诉方/答疑", "note": "..." }
  ]
}
```

桌面端按卡片展示：标题 → 主题 → 风险（标红）→ 待办。

### 3.3 分析维度（提示词里固定问）

1. **主题聚类**：聊了哪几件事  
2. **信息密度**：决策/结论/链接/时间点  
3. **风险与情绪**：冲突、辱骂、泄露、紧急求助  
4. **待办抽取**：谁需要跟进什么（不确定就标「待确认」）  
5. **异常**：刷屏、广告、明显跑题（可配置）

---

## 4. 数据设计

### 4.1 复用现有表

`messages` 已有：`group_id / user_id / sender_name / content / event_time / message_id`。

分析输入直接 `SELECT ... WHERE group_id=? AND event_time BETWEEN ? AND ? ORDER BY event_time`。

### 4.2 新增表

```sql
-- 分析任务（可重试）
CREATE TABLE IF NOT EXISTS llm_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_type TEXT NOT NULL,          -- schedule | manual | realtime
  group_id TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  status TEXT NOT NULL,            -- pending|running|ok|failed|skipped
  error TEXT,
  model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  finished_at TEXT
);

-- 分析报告
CREATE TABLE IF NOT EXISTS llm_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER,
  group_id TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  headline TEXT,
  sentiment TEXT,
  report_json TEXT NOT NULL,       -- 完整结构化结果
  report_md TEXT,                  -- 可读 Markdown 缓存
  risk_max TEXT,                   -- high|mid|low|none
  msg_count INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(group_id, window_start, window_end, job_id)
);

CREATE INDEX IF NOT EXISTS idx_llm_reports_group_time
ON llm_reports(group_id, window_start DESC);
```

可选二期：`llm_embeddings`（向量检索）。

---

## 5. 模块划分（在现有工程上扩展）

```text
app/
├── llm/
│   ├── __init__.py
│   ├── config.py          # 模型、温度、开关、窗口参数
│   ├── client.py          # OpenAI 兼容 API / Ollama 抽象
│   ├── prompts.py         # 系统提示 + 用户模板
│   ├── window.py          # 从 SQLite 切窗口、截断、脱敏
│   ├── parser.py          # JSON 提取与校验（pydantic）
│   ├── scheduler.py       # 定时任务（asyncio）
│   ├── service.py         # summarize(group, start, end) 主流程
│   └── risk_bridge.py     # 高风险 → 复用 alert webhook
├── handlers/              # 保持不变；realtime 可选挂一个钩子
└── main.py                # 启动时按配置拉起 scheduler
```

桌面端（Tauri）新增：

- 「分析报告」面板：按群、按时间看历史报告  
- 「立即总结」：最近 1h / 今日 / 自定义  
- 配置项：API Base、Model、API Key、定时开关  

---

## 6. LLM 接入设计

### 6.1 Provider 抽象（OpenAI 兼容）

统一走 Chat Completions 兼容接口，一套代码覆盖：

| Provider | `LLM_BASE_URL` 示例 | 说明 |
|----------|---------------------|------|
| OpenAI | `https://api.openai.com/v1` | 质量好，数据出域 |
| 国内兼容网关 | 各厂商文档 | DeepSeek / 通义 / 智谱等 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | 隐私优先，质量看模型 |

配置草案：

```env
LLM_ENABLED=false
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4.1-mini
LLM_TIMEOUT_SEC=60
LLM_MAX_INPUT_CHARS=24000
LLM_TEMPERATURE=0.2

# 调度
LLM_SCHEDULE_ENABLED=false
LLM_SCHEDULE_EVERY_MINUTES=60
LLM_MIN_MESSAGES=8          # 窗口内消息太少则 skip
LLM_REALTIME_ENABLED=false  # 默认关
```

### 6.2 窗口构建与截断策略

1. 拉取窗口内消息，格式化为：  
   `[HH:MM] 昵称(user_id): 文本`  
2. 过滤空消息、纯表情可压缩为 `[表情]`  
3. 超长时：  
   - 优先保留：**含关键词**、**@**、**链接**、**问号/求助语气**  
   - 其余均匀抽样 + 头尾保留  
4. 在 prompt 中声明：`以下为抽样后的聊天记录，可能不完整`

### 6.3 提示词原则

- System：你是「群聊监控分析助手」，只基于给定记录，禁止编造；不确定写「记录不足」  
- 强制 JSON Schema；`temperature` 偏低（0.1–0.3）  
- 输出语言：中文  
- `evidence` 必须来自原文摘录（便于人工核对，降低幻觉伤害）

### 6.4 失败与降级

| 情况 | 策略 |
|------|------|
| 超时/5xx | 指数退避重试 2 次，记 `llm_jobs.failed` |
| JSON 解析失败 | 再请求一次「只修 JSON」；仍失败则存原始文本 |
| 消息过少 | `skipped`，不浪费 token |
| Key 未配置 | 桌面提示，调度空转 |

---

## 7. 与现有告警的关系

```text
关键词告警（现有，便宜、确定） ──┐
                                 ├──► Webhook / 桌面红点
LLM 风险项 level=high（新增） ───┘
```

建议：

- 关键词命中：**立即告警**（保持现状）  
- LLM 高风险：写入报告 + **可选**推送「摘要告警」（带 headline + risk.detail）  
- 避免每条消息都 LLM，防止刷屏与费用爆炸  

---

## 8. 桌面端交互（示意）

```text
┌─ 分析报告 ─────────────────────────────┐
│ 群：[游戏开发干货分享群 ▼]  最近报告     │
│ [最近1小时] [今日] [自定义…] [立即总结]   │
│                                         │
│ ● 10:00–11:00  一句话标题…   风险:高     │
│   主题：发版延期 / 资源下载失败           │
│   待办：跟进构建机磁盘…                  │
└─────────────────────────────────────────┘
```

报告详情抽屉：完整 JSON 卡片化 + Markdown。

---

## 9. 隐私与合规

1. API Key 仅存 `.env`，不进 Git  
2. 默认提示文案标明：内容将发送至所配置的 LLM 服务商  
3. 提供 **本地模型** 路径（Ollama）作为隐私优先选项  
4. 可选脱敏：手机号、身份证号正则替换后再送模  
5. 用途限定：本人可见范围内的运营/工作辅助  

---

## 10. 实施阶段

### 阶段 1 — MVP（建议 1～2 天）

- [ ] `llm` 模块 + OpenAI 兼容 client  
- [ ] 手动 `summarize(group_id, start, end)` CLI / 桌面按钮  
- [ ] `llm_jobs` / `llm_reports` 落库  
- [ ] 桌面「最近报告」只读列表  

### 阶段 2 — 自动化

- [ ] 每小时调度  
- [ ] 高风险 → Webhook  
- [ ] 配置项进 Tauri 表单  

### 阶段 3 — 增强

- [ ] 实时轻分析（关键词后再 LLM）  
- [ ] 多群并行限流队列  
- [ ] 简单 RAG（按群 embedding）  
- [ ] 对比报告：「与上一窗口有何变化」  

---

## 11. 推荐默认决策

| 决策点 | 推荐 | 理由 |
|--------|------|------|
| 一期触发 | 手动 + 每小时 | 价值高、成本可控 |
| 模型 | 小模型 / mini 档或国内性价比模型 | 总结任务足够 |
| 输出 | 强制 JSON | 桌面好展示、好告警 |
| 实时 LLM | 默认关 | 避免费用与噪声 |
| 本地优先选项 | 支持 Ollama | 满足内网/隐私场景 |
| 与采集关系 | 异步、可补跑 | 不影响收消息稳定性 |

---

## 12. 下一步（待你确认后实现）

1. LLM 供应商偏好：**云 API** / **本地 Ollama** / 两者都要  
2. 一期范围：仅手动，还是手动 + 每小时定时  
3. 输出侧：只要桌面看，还是同时 Webhook 推高风险摘要  
4. 确认后按阶段 1 开工实现  

---

*文档结束*
