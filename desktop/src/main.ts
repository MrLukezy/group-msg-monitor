import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalSize } from "@tauri-apps/api/window";
import { openUrl } from "@tauri-apps/plugin-opener";
import gsap from "gsap";
import { formatTime, renderMsgHtml } from "./cq";

type StatusInfo = {
  napcatInstalled: boolean;
  napcatWebuiUp: boolean;
  onebotWsUp: boolean;
  monitorRunning: boolean;
};

type GroupItem = {
  groupId: string;
  groupName: string;
  enabled: boolean;
  blocked?: boolean;
  lastTime?: number | null;
  msgCount: number;
  keywordEnabled: boolean;
  llmEnabled: boolean;
  memberCount?: number | null;
  maxMemberCount?: number | null;
};

type MessageRow = {
  id: number;
  groupId: string;
  groupName?: string;
  userId: string;
  senderName: string;
  content: string;
  eventTime?: number | null;
  createdAt: string;
};

type LlmProvider = {
  id: string;
  name: string;
  type: string;
  baseUrl: string;
  apiKey: string;
  defaultModel: string;
};

type AppSettings = {
  onebotWsUrl: string;
  onebotAccessToken: string;
  llm: {
    providers: LlmProvider[];
    activeProviderId: string;
  };
  ui?: {
    compactModeEnabled?: boolean;
    theme?: string;
  };
};

type GroupConfig = {
  groupId: string;
  groupName: string;
  enabled: boolean;
  blocked?: boolean;
  basic: { logAll: boolean; storageEnabled: boolean };
  keywordMonitor: {
    enabled: boolean;
    keywords: string[];
    alertEnabled: boolean;
    webhookUrl: string;
  };
  llmMonitor: {
    enabled: boolean;
    providerId: string;
    model: string;
    prompt: string;
    everyMinutes: number;
    windowMinutes: number;
    minMessages: number;
  };
};

type ReportRow = {
  id: number;
  groupId: string;
  headline?: string | null;
  riskMax?: string | null;
  msgCount?: number | null;
  createdAt: string;
  reportMd?: string | null;
  windowStart?: number | null;
  windowEnd?: number | null;
};

const PROVIDER_PRESETS: Record<
  string,
  { name: string; type: string; baseUrl: string; defaultModel: string }
> = {
  openai: {
    name: "OpenAI",
    type: "openai_compatible",
    baseUrl: "https://api.openai.com/v1",
    defaultModel: "gpt-4.1-mini",
  },
  deepseek: {
    name: "DeepSeek",
    type: "openai_compatible",
    baseUrl: "https://api.deepseek.com/v1",
    defaultModel: "deepseek-chat",
  },
  siliconflow: {
    name: "SiliconFlow",
    type: "openai_compatible",
    baseUrl: "https://api.siliconflow.cn/v1",
    defaultModel: "deepseek-ai/DeepSeek-V3",
  },
  opencode: {
    name: "OpenCode 本地",
    type: "opencode",
    baseUrl: "http://127.0.0.1:4096",
    defaultModel: "",
  },
  cursor: {
    name: "Cursor SDK",
    type: "cursor",
    baseUrl: "",
    defaultModel: "composer-2.5",
  },
  custom: {
    name: "自定义",
    type: "openai_compatible",
    baseUrl: "",
    defaultModel: "",
  },
};

const THEMES: {
  id: string;
  name: string;
  swatches: [string, string, string];
}[] = [
  { id: "midnight", name: "午夜青金", swatches: ["#080b12", "#2dd4bf", "#f0b429"] },
  { id: "daylight", name: "日光纸感", swatches: ["#f4f1ea", "#c45c26", "#2a6f6a"] },
  { id: "ocean", name: "深海青蓝", swatches: ["#07151f", "#38bdf8", "#22d3ee"] },
  { id: "forest", name: "林间翠绿", swatches: ["#0c1410", "#4ade80", "#86efac"] },
  { id: "rose", name: "蔷薇暗粉", swatches: ["#160b12", "#f472b6", "#fb7185"] },
  { id: "graphite", name: "石墨灰阶", swatches: ["#121212", "#e5e5e5", "#a3a3a3"] },
];

const READ_IDS_KEY = "gmm_read_report_ids";
const LAST_TIP_ID_KEY = "gmm_last_tip_report_id";
const NORMAL_SIZE = { width: 1280, height: 820 };
const COMPACT_SIZE = { width: 420, height: 168 };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

let currentGroupId: string | null = null;
let settingsCache: AppSettings | null = null;
let editingProviderId: string | null = null;
let settingsModelOptions: { id: string }[] = [];
let groupModelOptions: { id: string }[] = [];
let toastTimer = 0;
let reduceMotion = false;
let groupNameMap = new Map<string, string>();
let groupsCache: GroupItem[] = [];
let liveScrollQuietUntil = 0;
let tickInFlight = false;
let statusTick = 0;
let monitoredSelectedGroupId: string | null = null;
let monitoredReportsCache: ReportRow[] = [];
let isCompactView = false;
let selectedTheme = "midnight";
let lastTipReportId = Number(localStorage.getItem(LAST_TIP_ID_KEY) || "0");
let unreadCount = 0;

function motionOk() {
  return !reduceMotion;
}

function rememberGroupNames(groups: GroupItem[]) {
  for (const g of groups) {
    if (g.groupName) groupNameMap.set(g.groupId, g.groupName);
  }
}

function groupDisplayName(groupId: string, fallbackName?: string | null) {
  return (fallbackName || groupNameMap.get(groupId) || "").trim() || "(未命名群)";
}

function toast(msg: string, err = false) {
  const el = $("toast");
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("err", err);
  window.clearTimeout(toastTimer);
  gsap.killTweensOf(el);
  if (motionOk()) {
    gsap.fromTo(
      el,
      { autoAlpha: 0, y: 16 },
      { autoAlpha: 1, y: 0, duration: 0.28, ease: "power2.out" },
    );
  } else {
    gsap.set(el, { autoAlpha: 1, y: 0 });
  }
  toastTimer = window.setTimeout(() => {
    if (motionOk()) {
      gsap.to(el, {
        autoAlpha: 0,
        y: 10,
        duration: 0.2,
        ease: "power2.in",
        onComplete: () => {
          el.hidden = true;
        },
      });
    } else {
      el.hidden = true;
    }
  }, 4200);
}

function escapeHtml(s: string) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadReadIds(): Set<number> {
  try {
    const raw = JSON.parse(localStorage.getItem(READ_IDS_KEY) || "[]");
    if (!Array.isArray(raw)) return new Set();
    return new Set(raw.map((x) => Number(x)).filter((n) => Number.isFinite(n)));
  } catch {
    return new Set();
  }
}

function saveReadIds(ids: Set<number>) {
  const arr = Array.from(ids).sort((a, b) => b - a).slice(0, 800);
  localStorage.setItem(READ_IDS_KEY, JSON.stringify(arr));
}

function markReportRead(id: number) {
  if (!id) return;
  const ids = loadReadIds();
  if (ids.has(id)) return;
  ids.add(id);
  saveReadIds(ids);
  refreshUnreadBadge().catch(() => undefined);
}

async function clearAllUnread() {
  const reports = await invoke<ReportRow[]>("api_list_reports", {
    groupId: null,
    limit: 200,
  }).catch(() => [] as ReportRow[]);
  const enabledIds = new Set(
    groupsCache.filter((g) => g.enabled && !g.blocked).map((g) => g.groupId),
  );
  const ids = loadReadIds();
  let added = 0;
  for (const r of reports || []) {
    if (!r.id || isSkippedReport(r)) continue;
    if (enabledIds.size && !enabledIds.has(r.groupId)) continue;
    if (!ids.has(r.id)) {
      ids.add(r.id);
      added += 1;
    }
  }
  saveReadIds(ids);
  await refreshUnreadBadge();
  if (added > 0) toast(`已清除 ${added} 条未读`);
  else toast("暂无未读");
}

function isSkippedReport(r: ReportRow): boolean {
  return (r.headline || "").includes("[定时跳过]") || (r.headline || "").includes("[跳过]");
}

function applyTheme(themeId: string) {
  const id = THEMES.some((t) => t.id === themeId) ? themeId : "midnight";
  selectedTheme = id;
  document.body.setAttribute("data-theme", id);
  document.documentElement.setAttribute("data-theme", id);
  renderThemePicker();
}

function renderThemePicker() {
  const box = document.getElementById("theme-picker");
  if (!box) return;
  box.innerHTML = THEMES.map((t) => {
    const active = t.id === selectedTheme ? "active" : "";
    return `<button class="theme-card ${active}" type="button" data-theme-id="${t.id}">
      <div class="theme-swatches">
        <span style="background:${t.swatches[0]}"></span>
        <span style="background:${t.swatches[1]}"></span>
        <span style="background:${t.swatches[2]}"></span>
      </div>
      <div class="theme-card-name">${escapeHtml(t.name)}</div>
    </button>`;
  }).join("");
  box.querySelectorAll<HTMLButtonElement>(".theme-card").forEach((btn) => {
    btn.onclick = () => applyTheme(btn.dataset.themeId || "midnight");
  });
}

function compactModeEnabled(): boolean {
  return !!settingsCache?.ui?.compactModeEnabled || $<HTMLInputElement>("s-compact-mode")?.checked;
}

function pushTipBubble(title: string, meta: string, target: "stack" | "compact" = "stack") {
  const host =
    target === "compact" ? $("compact-tips") : ($("tip-stack") as HTMLElement);
  if (!host) return;
  const el = document.createElement("div");
  el.className = "tip-bubble";
  el.innerHTML = `<div class="tip-title">${escapeHtml(title)}</div><div class="tip-meta">${escapeHtml(meta)}</div>`;
  host.prepend(el);
  while (host.children.length > 5) host.lastElementChild?.remove();
  window.setTimeout(() => el.remove(), 12000);
}

async function enterCompactView() {
  if (isCompactView) return;
  isCompactView = true;
  document.body.classList.add("is-compact");
  $("compact-view").classList.remove("hidden");
  $("compact-status").textContent = unreadCount
    ? `未读 LLM 总结 ${unreadCount} 条`
    : "缩略模式 · 等待新的 LLM 分析";
  try {
    const win = getCurrentWindow();
    await win.setAlwaysOnTop(true);
    await win.setMinSize(new LogicalSize(360, 120));
    await win.setSize(new LogicalSize(COMPACT_SIZE.width, COMPACT_SIZE.height));
  } catch {
    /* browser preview */
  }
}

async function exitCompactView() {
  if (!isCompactView) return;
  isCompactView = false;
  document.body.classList.remove("is-compact");
  $("compact-view").classList.add("hidden");
  try {
    const win = getCurrentWindow();
    await win.setAlwaysOnTop(false);
    await win.setMinSize(new LogicalSize(960, 680));
    await win.setSize(new LogicalSize(NORMAL_SIZE.width, NORMAL_SIZE.height));
    await win.center();
  } catch {
    /* ignore */
  }
}

async function refreshUnreadBadge() {
  const read = loadReadIds();
  const reports = await invoke<ReportRow[]>("api_list_reports", {
    groupId: null,
    limit: 200,
  }).catch(() => [] as ReportRow[]);
  const enabledIds = new Set(
    groupsCache.filter((g) => g.enabled && !g.blocked).map((g) => g.groupId),
  );
  const unread = (reports || []).filter(
    (r) =>
      r.id &&
      !read.has(r.id) &&
      !isSkippedReport(r) &&
      (!enabledIds.size || enabledIds.has(r.groupId)),
  );
  unreadCount = unread.length;
  const badge = $("monitored-unread-badge");
  const clearBtn = document.getElementById("btn-clear-unread") as HTMLButtonElement | null;
  if (badge) {
    if (unreadCount > 0) {
      badge.hidden = false;
      badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
      badge.title = "点击清除全部未读";
    } else {
      badge.hidden = true;
      badge.title = "";
    }
  }
  if (clearBtn) {
    clearBtn.hidden = unreadCount <= 0;
  }
  if (isCompactView) {
    $("compact-status").textContent = unreadCount
      ? `未读 LLM 总结 ${unreadCount} 条`
      : "缩略模式 · 等待新的 LLM 分析";
  }
  return unread;
}

async function pollLlmTipsAndUnread() {
  const unread = (await refreshUnreadBadge()) || [];
  if (!unread.length) return;
  const newest = unread[0];
  if (!newest?.id || newest.id <= lastTipReportId) return;
  // 初始化：只记录水位，不刷历史 tips
  if (lastTipReportId <= 0) {
    lastTipReportId = newest.id;
    localStorage.setItem(LAST_TIP_ID_KEY, String(lastTipReportId));
    return;
  }
  const fresh = unread.filter((r) => r.id > lastTipReportId).reverse();
  lastTipReportId = newest.id;
  localStorage.setItem(LAST_TIP_ID_KEY, String(lastTipReportId));
  for (const r of fresh.slice(-3)) {
    const gname = groupDisplayName(r.groupId);
    const title = r.headline || "新的 LLM 分析";
    const meta = `${gname} · ${r.createdAt || ""}`;
    if (isCompactView) {
      pushTipBubble(title, meta, "compact");
    }
  }
}

function setPill(key: string, on: boolean, label: string) {
  const el = document.querySelector(`.pill[data-key="${key}"]`) as HTMLElement;
  if (!el) return;
  const next = `${label}${on ? " · 在线" : " · 离线"}`;
  const wasOn = el.classList.contains("on");
  if (el.textContent === next && wasOn === on) return;
  el.classList.toggle("on", on);
  el.classList.toggle("off", !on);
  el.textContent = next;
}

function animateViewEnter(view: HTMLElement) {
  if (!motionOk()) return;
  gsap.fromTo(
    view,
    { autoAlpha: 0.35, y: 10 },
    { autoAlpha: 1, y: 0, duration: 0.32, ease: "power2.out" },
  );
}

function switchTab(name: string) {
  document.querySelectorAll(".nav-item").forEach((t) => {
    t.classList.toggle("active", (t as HTMLElement).dataset.tab === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    const active = v.id === `view-${name}`;
    v.classList.toggle("active", active);
    if (active) animateViewEnter(v as HTMLElement);
  });
  if (name === "groups") {
    $("groups-master").classList.remove("hidden");
    $("group-detail").classList.add("hidden");
    currentGroupId = null;
  }
  if (name === "monitored") {
    showMonitoredMaster();
  }
}

function switchSettingsTab(name: string) {
  document.querySelectorAll(".settings-tab").forEach((t) => {
    t.classList.toggle("active", (t as HTMLElement).dataset.stab === name);
  });
  $("stab-onebot").classList.toggle("hidden", name !== "onebot");
  $("stab-llm").classList.toggle("hidden", name !== "llm");
  $("stab-appearance").classList.toggle("hidden", name !== "appearance");
}

async function refreshStatus() {
  const s = await invoke<StatusInfo>("get_status");
  setPill("napcat", s.napcatInstalled && (s.napcatWebuiUp || s.onebotWsUp), "NapCat");
  setPill("onebot", s.onebotWsUp, "OneBot");
  setPill("monitor", s.monitorRunning, "监听服务");
}

function messageArticleHtml(
  m: MessageRow,
  opts?: { hideGroup?: boolean },
): string {
  const when = formatTime(m.createdAt, m.eventTime);
  const gName = groupDisplayName(m.groupId, m.groupName);
  const hideGroup = !!opts?.hideGroup;
  const groupBlock = hideGroup
    ? ""
    : `<div class="msg-group">
        <span class="msg-group-name">${escapeHtml(gName)}</span>
        <span class="msg-group-id">${escapeHtml(m.groupId)}</span>
      </div>`;
  return `<article class="msg" data-id="${m.id}">
    <div class="msg-head">
      ${groupBlock}
      <div class="msg-meta">
        <span class="msg-sender">${escapeHtml(m.senderName || m.userId || "未知")}</span>
        <span class="msg-time">${escapeHtml(when)}</span>
        <span class="msg-id">#${m.id}</span>
      </div>
    </div>
    <div class="body">${renderMsgHtml(m.content || "")}</div>
  </article>`;
}

function idsFingerprint(rows: MessageRow[]): string {
  return rows.map((r) => r.id).join(",");
}

function renderMessages(
  boxId: string,
  rows: MessageRow[],
  opts?: { hideGroup?: boolean },
) {
  const box = $(boxId);
  if (!rows.length) {
    if (box.dataset.fp === "__empty__") return;
    box.dataset.fp = "__empty__";
    box.innerHTML = `<div class="empty">暂无消息</div>`;
    return;
  }

  const fp = idsFingerprint(rows);
  const prevFp = box.dataset.fp || "";
  if (prevFp === fp) return;

  // 增量：仅在列表头部插入新消息（ORDER BY id DESC）
  if (prevFp && prevFp !== "__empty__") {
    const prevIds = prevFp.split(",").filter(Boolean);
    const newIds = rows.map((r) => String(r.id));
    const oldHead = prevIds[0];
    const cut = oldHead ? newIds.indexOf(oldHead) : -1;
    if (
      cut > 0 &&
      newIds.length >= prevIds.length &&
      newIds.slice(cut).join(",") === prevIds.join(",")
    ) {
      const atTop = box.scrollTop < 48;
      const prevHeight = box.scrollHeight;
      const html = rows
        .slice(0, cut)
        .map((m) => messageArticleHtml(m, opts))
        .join("");
      box.insertAdjacentHTML("afterbegin", html);
      // 裁掉尾部多余节点，保持与数据条数一致
      while (box.children.length > rows.length) {
        box.lastElementChild?.remove();
      }
      box.dataset.fp = fp;
      if (!atTop) {
        box.scrollTop += box.scrollHeight - prevHeight;
      }
      return;
    }
  }

  const scrollTop = box.scrollTop;
  box.innerHTML = rows.map((m) => messageArticleHtml(m, opts)).join("");
  box.dataset.fp = fp;
  box.scrollTop = scrollTop;
}

async function refreshLive() {
  if (Date.now() < liveScrollQuietUntil) return;
  const rows = await invoke<MessageRow[]>("api_recent_messages", {
    groupId: null,
    limit: 40,
  });
  for (const m of rows) {
    if (m.groupName) groupNameMap.set(m.groupId, m.groupName);
  }
  renderMessages("live-messages", rows);
}

async function refreshGroups() {
  const sort = $<HTMLSelectElement>("groups-sort").value;
  const q = $<HTMLInputElement>("groups-q").value.trim();
  const showBlocked = $<HTMLInputElement>("groups-show-blocked").checked;
  const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", { sort, q });
  groupsCache = res.groups || [];
  rememberGroupNames(groupsCache);
  const box = $("groups-list");
  const visible = showBlocked
    ? groupsCache
    : groupsCache.filter((g) => !g.blocked);
  if (!visible.length) {
    box.innerHTML = `<div class="empty">${
      groupsCache.some((g) => g.blocked) && !showBlocked
        ? "当前列表已隐藏屏蔽群。勾选「显示已屏蔽」可查看。"
        : "暂无群。可先启动监听收消息，或点「从 OneBot 拉取」。"
    }</div>`;
    return;
  }
  box.innerHTML = visible
    .map((g) => {
      const last = g.lastTime
        ? new Date(g.lastTime * 1000).toLocaleString()
        : "暂无消息";
      const name = groupDisplayName(g.groupId, g.groupName);
      const statusBadge = g.blocked
        ? `<span class="badge blocked">已屏蔽</span>`
        : `<span class="badge ${g.enabled ? "on" : ""}">${g.enabled ? "监听中" : "未启用"}</span>`;
      return `<button class="group-item ${g.blocked ? "is-blocked" : ""}" type="button" data-id="${escapeHtml(g.groupId)}">
        <div>
          <div class="name">${escapeHtml(name)}</div>
          <div class="meta">群号 ${escapeHtml(g.groupId)} · 最近 ${escapeHtml(last)} · ${g.msgCount} 条</div>
        </div>
        <div class="badges">
          ${statusBadge}
          <span class="badge ${g.keywordEnabled ? "on" : ""}">关键词</span>
          <span class="badge ${g.llmEnabled ? "on" : ""}">LLM</span>
        </div>
      </button>`;
    })
    .join("");
  box.querySelectorAll<HTMLButtonElement>(".group-item").forEach((btn) => {
    btn.onclick = () => openGroup(btn.dataset.id || "");
  });
}

function showMonitoredMaster() {
  $("monitored-master").classList.remove("hidden");
  $("monitored-report-detail").classList.add("hidden");
}

function formatReportWindow(r: ReportRow): string {
  const start = r.windowStart
    ? new Date(r.windowStart * 1000).toLocaleString()
    : "";
  const end = r.windowEnd ? new Date(r.windowEnd * 1000).toLocaleString() : "";
  if (start && end) return `${start} ~ ${end}`;
  return r.createdAt || "";
}

function renderMonitoredReportList(reports: ReportRow[]) {
  const box = $("monitored-report-list");
  if (!monitoredSelectedGroupId) {
    box.innerHTML = `<div class="empty">请选择左侧群查看分析主题</div>`;
    return;
  }
  if (!reports.length) {
    const g = groupsCache.find((x) => x.groupId === monitoredSelectedGroupId);
    box.innerHTML = `<div class="empty">${
      g?.llmEnabled
        ? "暂无 LLM 分析主题，可在群配置中「立即 LLM 分析」"
        : "该群未开启 LLM 检测"
    }</div>`;
    return;
  }
  box.innerHTML = reports
    .map((r) => {
      const risk = (r.riskMax || "none").toLowerCase();
      const skipped = (r.headline || "").includes("[定时跳过]");
      return `<button class="report-title-item ${risk === "high" ? "high" : ""} ${
        skipped ? "skipped" : ""
      }" type="button" data-id="${r.id}">
        <div class="report-title-text">${escapeHtml(r.headline || "(无标题)")}</div>
        <div class="report-title-meta">${escapeHtml(r.createdAt || "")} · 风险 ${escapeHtml(
          r.riskMax || "-",
        )} · ${r.msgCount ?? "-"} 条</div>
      </button>`;
    })
    .join("");
  box.querySelectorAll<HTMLButtonElement>(".report-title-item").forEach((btn) => {
    btn.onclick = () => {
      const id = Number(btn.dataset.id || 0);
      openMonitoredReportDetail(id).catch((e) => toast(String(e), true));
    };
  });
}

async function selectMonitoredGroup(groupId: string) {
  monitoredSelectedGroupId = groupId;
  const g = groupsCache.find((x) => x.groupId === groupId);
  const name = groupDisplayName(groupId, g?.groupName);
  $("monitored-reports-title").textContent = `${name} · LLM 主题`;
  $("monitored-reports-hint").textContent = `群号 ${groupId}`;

  document.querySelectorAll<HTMLButtonElement>("#monitored-group-list .monitored-group-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.id === groupId);
  });

  const reports = await invoke<ReportRow[]>("api_list_reports", {
    groupId,
    limit: 80,
  }).catch(() => [] as ReportRow[]);
  monitoredReportsCache = reports || [];
  renderMonitoredReportList(monitoredReportsCache);
}

async function openMonitoredReportDetail(reportId: number) {
  const report = monitoredReportsCache.find((r) => r.id === reportId);
  if (!report) {
    toast("未找到该分析报告", true);
    return;
  }
  markReportRead(reportId);
  $("monitored-master").classList.add("hidden");
  $("monitored-report-detail").classList.remove("hidden");
  animateViewEnter($("monitored-report-detail"));
  $("monitored-report-detail-title").textContent = report.headline || "(无标题)";
  $("monitored-report-detail-meta").textContent =
    `${formatReportWindow(report)} · 风险 ${report.riskMax || "-"} · ${report.msgCount ?? "-"} 条消息`;
  $("monitored-report-detail-body").textContent =
    (report.reportMd || "").trim() || "（无详细内容）";

  const msgsBox = $("monitored-report-detail-msgs");
  msgsBox.dataset.fp = "";
  msgsBox.innerHTML = `<div class="empty">加载相关对话…</div>`;
  $("monitored-report-msgs-hint").textContent = "分析窗口内的原始消息";

  const start = Number(report.windowStart || 0);
  const end = Number(report.windowEnd || 0);
  if (!report.groupId || !start || !end) {
    msgsBox.innerHTML = `<div class="empty">该报告缺少时间窗口，无法匹配对话</div>`;
    return;
  }

  try {
    const rows = await invoke<MessageRow[]>("api_messages_in_window", {
      groupId: report.groupId,
      startTs: start,
      endTs: end,
      limit: 500,
    });
    $("monitored-report-msgs-hint").textContent =
      `共 ${rows.length} 条 · ${formatReportWindow(report)}`;
    if (!rows.length) {
      msgsBox.innerHTML = `<div class="empty">该时间窗内无落库消息（可能已被清理或分析时回退了其它窗口）</div>`;
      return;
    }
    msgsBox.innerHTML = rows.map((m) => messageArticleHtml(m, { hideGroup: true })).join("");
    msgsBox.dataset.fp = idsFingerprint(rows);
    msgsBox.scrollTop = 0;
  } catch (e) {
    msgsBox.innerHTML = `<div class="empty">加载对话失败：${escapeHtml(String(e))}</div>`;
  }
}

async function refreshMonitored() {
  showMonitoredMaster();
  const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", {
    sort: "recent",
    q: "",
  });
  groupsCache = res.groups || [];
  rememberGroupNames(groupsCache);
  const enabled = groupsCache.filter((g) => g.enabled && !g.blocked);
  const llm = enabled.filter((g) => g.llmEnabled).length;
  const totalMsg = enabled.reduce((n, g) => n + (g.msgCount || 0), 0);

  let withReport = 0;
  if (enabled.length) {
    const allReports = await invoke<ReportRow[]>("api_list_reports", {
      groupId: null,
      limit: 200,
    }).catch(() => [] as ReportRow[]);
    const seen = new Set<string>();
    for (const r of allReports || []) {
      if (r.groupId) seen.add(r.groupId);
    }
    withReport = enabled.filter((g) => seen.has(g.groupId)).length;
  }

  $("monitored-stats").innerHTML = `
    <div class="stat-card"><div class="stat-num">${enabled.length}</div><div class="stat-label">监听中</div></div>
    <div class="stat-card"><div class="stat-num">${totalMsg}</div><div class="stat-label">累计消息</div></div>
    <div class="stat-card"><div class="stat-num">${llm}</div><div class="stat-label">LLM 开启</div></div>
    <div class="stat-card"><div class="stat-num">${withReport}</div><div class="stat-label">已有分析</div></div>
  `;

  const box = $("monitored-group-list");
  if (!enabled.length) {
    monitoredSelectedGroupId = null;
    monitoredReportsCache = [];
    box.innerHTML = `<div class="empty">暂无启用监听的群。到「群列表」打开群配置并勾选「启用监听此群」。</div>`;
    $("monitored-reports-title").textContent = "LLM 分析主题";
    $("monitored-reports-hint").textContent = "请选择左侧群";
    renderMonitoredReportList([]);
    return;
  }

  if (
    !monitoredSelectedGroupId ||
    !enabled.some((g) => g.groupId === monitoredSelectedGroupId)
  ) {
    monitoredSelectedGroupId = enabled[0].groupId;
  }

  box.innerHTML = enabled
    .map((g) => {
      const last = g.lastTime
        ? new Date(g.lastTime * 1000).toLocaleString()
        : "暂无消息";
      const name = groupDisplayName(g.groupId, g.groupName);
      const active = g.groupId === monitoredSelectedGroupId ? "active" : "";
      return `<button class="monitored-group-item ${active}" type="button" data-id="${escapeHtml(
        g.groupId,
      )}">
        <div class="name">${escapeHtml(name)}</div>
        <div class="meta">群号 ${escapeHtml(g.groupId)}</div>
        <div class="meta">${escapeHtml(last)} · ${g.msgCount} 条</div>
        <div class="badges">
          <span class="badge ${g.llmEnabled ? "on" : ""}">LLM</span>
          <span class="badge ${g.keywordEnabled ? "on" : ""}">关键词</span>
        </div>
      </button>`;
    })
    .join("");

  box.querySelectorAll<HTMLButtonElement>(".monitored-group-item").forEach((btn) => {
    btn.onclick = () => {
      selectMonitoredGroup(btn.dataset.id || "").catch((e) => toast(String(e), true));
    };
  });

  if (monitoredSelectedGroupId) {
    await selectMonitoredGroup(monitoredSelectedGroupId);
  }
}

function genProviderId() {
  return `p_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

function activeProvider(): LlmProvider | undefined {
  if (!settingsCache) return undefined;
  const id = settingsCache.llm.activeProviderId;
  return (
    settingsCache.llm.providers.find((p) => p.id === id) ||
    settingsCache.llm.providers[0]
  );
}

function fillProvidersSelect(selected?: string) {
  const sel = $<HTMLSelectElement>("g-llm-provider");
  const providers = settingsCache?.llm.providers || [];
  sel.innerHTML = providers
    .map(
      (p) =>
        `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} (${escapeHtml(
          p.type,
        )})</option>`,
    )
    .join("");
  if (selected) sel.value = selected;
}

function renderProviderList() {
  if (!settingsCache) return;
  const box = $("provider-list");
  const activeId = settingsCache.llm.activeProviderId;
  if (!settingsCache.llm.providers.length) {
    box.innerHTML = `<div class="empty">还没有代理，点击下方「添加代理」</div>`;
    return;
  }
  box.innerHTML = settingsCache.llm.providers
    .map((p) => {
      const url = p.baseUrl || (p.type === "cursor" ? "cursor-sdk" : "-");
      return `<div class="provider-item ${p.id === activeId ? "active" : ""}" data-id="${escapeHtml(p.id)}">
        <div class="provider-item-main" data-act="select">
          <span class="provider-item-name">${escapeHtml(p.name)}</span>
          <span class="provider-item-url">${escapeHtml(url)}</span>
        </div>
        <div class="provider-item-actions">
          <button class="provider-action-btn" type="button" data-act="edit" title="编辑">✎</button>
          <button class="provider-action-btn provider-delete-btn" type="button" data-act="del" title="删除">🗑</button>
        </div>
      </div>`;
    })
    .join("");

  box.querySelectorAll<HTMLElement>(".provider-item").forEach((item) => {
    const id = item.dataset.id || "";
    item.addEventListener("click", () => switchActiveProvider(id));
    item.querySelectorAll<HTMLElement>("[data-act]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const act = el.dataset.act;
        if (act === "edit") openEditProvider(id);
        if (act === "del") deleteProvider(id);
      });
    });
  });
}

function switchActiveProvider(id: string) {
  if (!settingsCache) return;
  const p = settingsCache.llm.providers.find((x) => x.id === id);
  if (!p) return;
  settingsCache.llm.activeProviderId = id;
  settingsModelOptions = p.defaultModel ? [{ id: p.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, p.defaultModel || "");
  renderProviderList();
}

function openAddProviderForm() {
  editingProviderId = null;
  $("provider-form-title").textContent = "添加代理";
  $<HTMLSelectElement>("pf-preset").value = "openai";
  applyPreset("openai");
  $<HTMLInputElement>("pf-key").value = "";
  $("provider-form").classList.remove("hidden");
}

function openEditProvider(id: string) {
  if (!settingsCache) return;
  const p = settingsCache.llm.providers.find((x) => x.id === id);
  if (!p) return;
  editingProviderId = id;
  $("provider-form-title").textContent = "编辑代理";
  let preset = "custom";
  if (p.type === "opencode") preset = "opencode";
  else if (p.type === "cursor") preset = "cursor";
  else if ((p.baseUrl || "").includes("deepseek")) preset = "deepseek";
  else if ((p.baseUrl || "").includes("siliconflow")) preset = "siliconflow";
  else if ((p.baseUrl || "").includes("openai.com")) preset = "openai";
  $<HTMLSelectElement>("pf-preset").value = preset;
  $<HTMLInputElement>("pf-name").value = p.name;
  $<HTMLInputElement>("pf-url").value = p.baseUrl || "";
  $<HTMLInputElement>("pf-key").value = p.apiKey || "";
  $<HTMLInputElement>("pf-model").value = p.defaultModel || "";
  $("provider-form").classList.remove("hidden");
}

function applyPreset(key: string) {
  const preset = PROVIDER_PRESETS[key] || PROVIDER_PRESETS.custom;
  $<HTMLInputElement>("pf-name").value = preset.name;
  $<HTMLInputElement>("pf-url").value = preset.baseUrl;
  $<HTMLInputElement>("pf-model").value = preset.defaultModel;
}

function closeProviderForm() {
  editingProviderId = null;
  $("provider-form").classList.add("hidden");
}

function saveProviderForm() {
  if (!settingsCache) return;
  const name = $<HTMLInputElement>("pf-name").value.trim();
  const baseUrl = $<HTMLInputElement>("pf-url").value.trim();
  const apiKey = $<HTMLInputElement>("pf-key").value.trim();
  const defaultModel = $<HTMLInputElement>("pf-model").value.trim();
  const presetKey = $<HTMLSelectElement>("pf-preset").value;
  const type = (PROVIDER_PRESETS[presetKey] || PROVIDER_PRESETS.custom).type;
  if (!name) {
    toast("请填写代理名称", true);
    return;
  }
  if (type === "openai_compatible" && !baseUrl) {
    toast("OpenAI Compatible 需要 Base URL", true);
    return;
  }
  if (editingProviderId) {
    settingsCache.llm.providers = settingsCache.llm.providers.map((p) =>
      p.id === editingProviderId
        ? { ...p, name, baseUrl, apiKey, defaultModel, type }
        : p,
    );
  } else {
    const id = genProviderId();
    settingsCache.llm.providers.push({
      id,
      name,
      type,
      baseUrl,
      apiKey,
      defaultModel,
    });
    if (!settingsCache.llm.activeProviderId) {
      settingsCache.llm.activeProviderId = id;
    }
  }
  const active = activeProvider();
  if (active) {
    settingsModelOptions = active.defaultModel ? [{ id: active.defaultModel }] : settingsModelOptions;
    fillModelSelect("s-default-model", settingsModelOptions, active.defaultModel || "");
  }
  renderProviderList();
  closeProviderForm();
  toast("代理已更新（记得点右上角保存总配置）");
}

function deleteProvider(id: string) {
  if (!settingsCache) return;
  settingsCache.llm.providers = settingsCache.llm.providers.filter((p) => p.id !== id);
  if (settingsCache.llm.activeProviderId === id) {
    settingsCache.llm.activeProviderId = settingsCache.llm.providers[0]?.id || "";
  }
  renderProviderList();
  const active = activeProvider();
  settingsModelOptions = active?.defaultModel ? [{ id: active.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, active?.defaultModel || "");
}

function fillModelSelect(
  selectId: string,
  models: { id: string }[],
  current?: string,
  emptyHint = "先点右侧刷新获取模型",
) {
  const sel = $<HTMLSelectElement>(selectId);
  const cur = (current ?? sel.value).trim();
  const ids = [...models.map((m) => m.id).filter(Boolean)];
  if (cur && !ids.includes(cur)) ids.unshift(cur);
  if (!ids.length) {
    sel.innerHTML = `<option value="">${escapeHtml(emptyHint)}</option>`;
    sel.value = "";
    return;
  }
  sel.innerHTML = ids
    .map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`)
    .join("");
  sel.value = cur && ids.includes(cur) ? cur : ids[0];
}

async function fetchModelsByProviderId(providerId: string): Promise<{ id: string }[]> {
  const res = await invoke<{ models: { id: string }[]; error?: string }>("api_fetch_models", {
    providerId,
  });
  if (res.error && (!res.models || !res.models.length)) {
    throw new Error(res.error);
  }
  return res.models || [];
}

async function refreshSettingsModels() {
  const active = activeProvider();
  if (!active) {
    toast("请先添加并选择代理", true);
    return;
  }
  const btn = $<HTMLButtonElement>("btn-refresh-models");
  btn.disabled = true;
  try {
    toast("正在拉取模型列表…");
    settingsModelOptions = await fetchModelsByProviderId(active.id);
    if (active.defaultModel && !settingsModelOptions.find((m) => m.id === active.defaultModel)) {
      settingsModelOptions.unshift({ id: active.defaultModel });
    }
    const keep = $<HTMLSelectElement>("s-default-model").value || active.defaultModel || "";
    fillModelSelect("s-default-model", settingsModelOptions, keep);
    const chosen = $<HTMLSelectElement>("s-default-model").value;
    if (chosen && settingsCache) {
      settingsCache.llm.providers = settingsCache.llm.providers.map((p) =>
        p.id === active.id ? { ...p, defaultModel: chosen } : p,
      );
    }
    toast(`已获取 ${settingsModelOptions.length} 个模型`);
  } catch (e) {
    toast(String(e), true);
  } finally {
    btn.disabled = false;
  }
}

async function refreshGroupModels() {
  const providerId = $<HTMLSelectElement>("g-llm-provider").value;
  if (!providerId) {
    toast("请先选择 Provider", true);
    return;
  }
  const btn = $<HTMLButtonElement>("btn-refresh-group-models");
  btn.disabled = true;
  try {
    toast("正在拉取模型列表…");
    groupModelOptions = await fetchModelsByProviderId(providerId);
    const keep = $<HTMLSelectElement>("g-llm-model").value;
    const provider = settingsCache?.llm.providers.find((p) => p.id === providerId);
    if (provider?.defaultModel && !groupModelOptions.find((m) => m.id === provider.defaultModel)) {
      groupModelOptions.unshift({ id: provider.defaultModel });
    }
    fillModelSelect("g-llm-model", groupModelOptions, keep || provider?.defaultModel || "");
    toast(`已获取 ${groupModelOptions.length} 个模型`);
  } catch (e) {
    toast(String(e), true);
  } finally {
    btn.disabled = false;
  }
}

function showTestResult(elId: string, ok: boolean, text: string) {
  const el = $(elId);
  el.hidden = false;
  el.classList.toggle("ok", ok);
  el.classList.toggle("err", !ok);
  el.textContent = text;
}

async function persistSettingsFromForm(): Promise<AppSettings> {
  if (!settingsCache) settingsCache = await invoke<AppSettings>("api_get_settings");
  const active = activeProvider();
  const model = $<HTMLSelectElement>("s-default-model").value.trim();
  if (active) {
    settingsCache.llm.providers = settingsCache.llm.providers.map((p) =>
      p.id === active.id ? { ...p, defaultModel: model } : p,
    );
  }
  const next: AppSettings = {
    onebotWsUrl: $<HTMLInputElement>("s-ws").value.trim(),
    onebotAccessToken: $<HTMLInputElement>("s-token").value.trim(),
    llm: {
      activeProviderId: settingsCache.llm.activeProviderId,
      providers: settingsCache.llm.providers,
    },
    ui: {
      compactModeEnabled: $<HTMLInputElement>("s-compact-mode").checked,
      theme: selectedTheme,
    },
  };
  await invoke("api_save_settings", { settings: next });
  settingsCache = next;
  applyTheme(selectedTheme);
  return next;
}

async function testActiveProvider() {
  const active = activeProvider();
  if (!active) {
    toast("请先添加并选择代理", true);
    return;
  }
  const btn = $<HTMLButtonElement>("btn-test-provider");
  const model = $<HTMLSelectElement>("s-default-model").value.trim();
  btn.disabled = true;
  showTestResult("llm-test-result", true, "测试中…");
  try {
    await persistSettingsFromForm();
    const res = await invoke<{
      ok: boolean;
      message?: string;
      latencyMs?: number;
      endpoint?: string;
      model?: string;
    }>("api_test_provider", { providerId: active.id, model });
    const latency = res.latencyMs != null ? `（${res.latencyMs} ms）` : "";
    const extra = res.endpoint ? `\nendpoint: ${res.endpoint}` : "";
    showTestResult(
      "llm-test-result",
      !!res.ok,
      `${res.ok ? "成功" : "失败"} ${latency}\n${res.message || ""}${extra}`,
    );
    toast(res.ok ? "LLM 连通性正常" : res.message || "连通性测试失败", !res.ok);
  } catch (e) {
    showTestResult("llm-test-result", false, String(e));
    toast(String(e), true);
  } finally {
    btn.disabled = false;
  }
}

async function testOnebotConnectivity() {
  const btn = $<HTMLButtonElement>("btn-test-onebot");
  btn.disabled = true;
  showTestResult("onebot-test-result", true, "测试中…");
  try {
    await persistSettingsFromForm();
    const res = await invoke<{ ok: boolean; message?: string; latencyMs?: number }>("api_test_onebot");
    const latency = res.latencyMs != null ? `（${res.latencyMs} ms）` : "";
    showTestResult(
      "onebot-test-result",
      !!res.ok,
      `${res.ok ? "成功" : "失败"} ${latency}\n${res.message || ""}`,
    );
    toast(res.ok ? "OneBot 连通性正常" : res.message || "OneBot 测试失败", !res.ok);
  } catch (e) {
    showTestResult("onebot-test-result", false, String(e));
    toast(String(e), true);
  } finally {
    btn.disabled = false;
  }
}
async function openGroup(groupId: string) {
  if (!groupId) return;
  currentGroupId = groupId;
  if (!settingsCache) settingsCache = await invoke<AppSettings>("api_get_settings");
  const cfg = await invoke<GroupConfig>("api_get_group", { groupId });
  $("groups-master").classList.add("hidden");
  $("group-detail").classList.remove("hidden");
  animateViewEnter($("group-detail"));
  const displayName = groupDisplayName(groupId, cfg.groupName);
  if (cfg.groupName) groupNameMap.set(groupId, cfg.groupName);
  $("detail-title").textContent = displayName;
  $("detail-sub").textContent = `群号 ${groupId}`;

  $<HTMLInputElement>("g-blocked").checked = !!cfg.blocked;
  $<HTMLInputElement>("g-enabled").checked = !!cfg.enabled && !cfg.blocked;
  $<HTMLInputElement>("g-log-all").checked = !!cfg.basic?.logAll;
  $<HTMLInputElement>("g-storage").checked = !!cfg.basic?.storageEnabled;
  $<HTMLInputElement>("g-name").value = cfg.groupName || "";
  syncBlockEnableUi();
  $<HTMLInputElement>("g-kw-enabled").checked = !!cfg.keywordMonitor?.enabled;
  $<HTMLInputElement>("g-keywords").value = (cfg.keywordMonitor?.keywords || []).join(",");
  $<HTMLInputElement>("g-kw-alert").checked = !!cfg.keywordMonitor?.alertEnabled;
  $<HTMLInputElement>("g-kw-webhook").value = cfg.keywordMonitor?.webhookUrl || "";
  $<HTMLInputElement>("g-llm-enabled").checked = !!cfg.llmMonitor?.enabled;
  fillProvidersSelect(cfg.llmMonitor?.providerId || settingsCache.llm.activeProviderId);
  const groupModel = cfg.llmMonitor?.model || "";
  const selectedProviderId = $<HTMLSelectElement>("g-llm-provider").value;
  const selectedProvider = settingsCache.llm.providers.find((p) => p.id === selectedProviderId);
  groupModelOptions = [];
  if (groupModel) groupModelOptions.push({ id: groupModel });
  else if (selectedProvider?.defaultModel) groupModelOptions.push({ id: selectedProvider.defaultModel });
  fillModelSelect(
    "g-llm-model",
    groupModelOptions,
    groupModel || selectedProvider?.defaultModel || "",
  );
  $<HTMLInputElement>("g-llm-every").value = String(cfg.llmMonitor?.everyMinutes ?? 60);
  $<HTMLInputElement>("g-llm-window").value = String(cfg.llmMonitor?.windowMinutes ?? 60);
  $<HTMLInputElement>("g-llm-min").value = String(cfg.llmMonitor?.minMessages ?? 8);
  $<HTMLTextAreaElement>("g-llm-prompt").value = cfg.llmMonitor?.prompt || "";

  const msgs = await invoke<MessageRow[]>("api_recent_messages", { groupId, limit: 40 });
  const detailBox = $("detail-messages");
  detailBox.dataset.fp = "";
  renderMessages("detail-messages", msgs, { hideGroup: true });
  const reports = await invoke<ReportRow[]>("api_list_reports", { groupId, limit: 30 });
  const box = $("detail-reports");
  if (!reports.length) {
    box.innerHTML = `<div class="empty">暂无 LLM 报告，可点「立即执行」</div>`;
  } else {
    box.innerHTML = reports
      .map((r) => {
        const risk = (r.riskMax || "none").toLowerCase();
        const skipped = (r.headline || "").includes("[定时跳过]");
        return `<button class="report-title-item ${risk === "high" ? "high" : ""} ${
          skipped ? "skipped" : ""
        }" type="button" data-id="${r.id}">
          <div class="report-title-text">${escapeHtml(r.headline || "(无标题)")}</div>
          <div class="report-title-meta">${escapeHtml(r.createdAt || "")} · 风险 ${escapeHtml(
            r.riskMax || "-",
          )} · ${r.msgCount ?? "-"} 条</div>
        </button>`;
      })
      .join("");
    box.querySelectorAll<HTMLButtonElement>(".report-title-item").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.id || 0);
        const report = reports.find((x) => x.id === id);
        if (!report) return;
        monitoredSelectedGroupId = groupId;
        monitoredReportsCache = reports;
        switchTab("monitored");
        refreshMonitored()
          .then(() => openMonitoredReportDetail(id))
          .catch((e) => toast(String(e), true));
      };
    });
  }
}

function syncBlockEnableUi() {
  const blocked = $<HTMLInputElement>("g-blocked").checked;
  const enabledEl = $<HTMLInputElement>("g-enabled");
  if (blocked) {
    enabledEl.checked = false;
    enabledEl.disabled = true;
  } else {
    enabledEl.disabled = false;
  }
}

function readGroupForm(): GroupConfig {
  const groupId = currentGroupId || "";
  const blocked = $<HTMLInputElement>("g-blocked").checked;
  const enabled = !blocked && $<HTMLInputElement>("g-enabled").checked;
  return {
    groupId,
    groupName: $<HTMLInputElement>("g-name").value.trim(),
    enabled,
    blocked,
    basic: {
      logAll: $<HTMLInputElement>("g-log-all").checked,
      storageEnabled: $<HTMLInputElement>("g-storage").checked,
    },
    keywordMonitor: {
      enabled: $<HTMLInputElement>("g-kw-enabled").checked,
      keywords: $<HTMLInputElement>("g-keywords")
        .value.split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      alertEnabled: $<HTMLInputElement>("g-kw-alert").checked,
      webhookUrl: $<HTMLInputElement>("g-kw-webhook").value.trim(),
    },
    llmMonitor: {
      enabled: $<HTMLInputElement>("g-llm-enabled").checked,
      providerId: $<HTMLSelectElement>("g-llm-provider").value,
      model: $<HTMLSelectElement>("g-llm-model").value.trim(),
      prompt: $<HTMLTextAreaElement>("g-llm-prompt").value,
      everyMinutes: Number($<HTMLInputElement>("g-llm-every").value || 60),
      windowMinutes: Number($<HTMLInputElement>("g-llm-window").value || 60),
      minMessages: Number($<HTMLInputElement>("g-llm-min").value || 8),
    },
  };
}

async function loadSettingsView() {
  settingsCache = await invoke<AppSettings>("api_get_settings");
  $<HTMLInputElement>("s-ws").value = settingsCache.onebotWsUrl || "";
  $<HTMLInputElement>("s-token").value = settingsCache.onebotAccessToken || "";
  $<HTMLInputElement>("s-compact-mode").checked = !!settingsCache.ui?.compactModeEnabled;
  applyTheme(settingsCache.ui?.theme || "midnight");
  const active = activeProvider();
  settingsModelOptions = active?.defaultModel ? [{ id: active.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, active?.defaultModel || "");
  renderProviderList();
  switchSettingsTab("llm");
}

async function tick() {
  if (tickInFlight) return;
  tickInFlight = true;
  try {
    // 状态检测含 TCP，降频到约每 2 次
    statusTick += 1;
    if (statusTick % 2 === 1) {
      await refreshStatus();
    }
    if (statusTick % 2 === 0) {
      await pollLlmTipsAndUnread();
    }
    if ($("view-live").classList.contains("active") && !isCompactView) {
      await refreshLive();
    }
  } catch (e) {
    console.error(e);
  } finally {
    tickInFlight = false;
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  document.querySelectorAll(".nav-item").forEach((btn) => {
    (btn as HTMLButtonElement).onclick = () => {
      const name = (btn as HTMLElement).dataset.tab || "live";
      switchTab(name);
      if (name === "groups") refreshGroups().catch((e) => toast(String(e), true));
      if (name === "monitored") {
        refreshMonitored()
          .then(() => refreshUnreadBadge())
          .catch((e) => toast(String(e), true));
      }
      if (name === "settings") loadSettingsView().catch((e) => toast(String(e), true));
    };
  });

  $("win-min").onclick = async () => {
    try {
      if (compactModeEnabled()) {
        await enterCompactView();
      } else {
        await getCurrentWindow().minimize();
      }
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("win-max").onclick = async () => {
    try {
      await getCurrentWindow().toggleMaximize();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("win-close").onclick = async () => {
    try {
      await getCurrentWindow().close();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("win-close-compact").onclick = async () => {
    try {
      await getCurrentWindow().close();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-exit-compact").onclick = () => {
    exitCompactView().catch((e) => toast(String(e), true));
  };

  document.addEventListener("click", (ev) => {
    const t = ev.target as HTMLElement | null;
    const a = t?.closest?.("a[data-ext-url]") as HTMLAnchorElement | null;
    if (!a) return;
    ev.preventDefault();
    const url = a.dataset.extUrl || a.href;
    if (!url) return;
    openUrl(url).catch((e) => toast(String(e), true));
  });

  const liveBox = $("live-messages");
  liveBox.addEventListener(
    "scroll",
    () => {
      liveScrollQuietUntil = Date.now() + 1800;
    },
    { passive: true },
  );

  document.querySelectorAll(".settings-tab").forEach((btn) => {
    (btn as HTMLButtonElement).onclick = () => {
      switchSettingsTab((btn as HTMLElement).dataset.stab || "onebot");
    };
  });

  $("btn-start-monitor").onclick = async () => {
    try {
      toast(await invoke<string>("start_monitor"));
      await refreshStatus();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-stop-monitor").onclick = async () => {
    try {
      toast(await invoke<string>("stop_monitor"));
      await refreshStatus();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-start-napcat").onclick = async () => {
    try {
      toast(await invoke<string>("start_napcat"));
    } catch (e) {
      toast(String(e), true);
    }
  };

  $("btn-refresh-groups").onclick = () =>
    refreshGroups().catch((e) => toast(String(e), true));
  $("btn-refresh-monitored").onclick = () =>
    refreshMonitored().catch((e) => toast(String(e), true));
  $("btn-clear-unread").onclick = (ev) => {
    ev.stopPropagation();
    clearAllUnread().catch((e) => toast(String(e), true));
  };
  $("monitored-unread-badge").onclick = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    clearAllUnread().catch((e) => toast(String(e), true));
  };
  $("btn-back-monitored-report").onclick = () => {
    showMonitoredMaster();
    animateViewEnter($("monitored-master"));
    if (monitoredSelectedGroupId) {
      selectMonitoredGroup(monitoredSelectedGroupId).catch(() => undefined);
    }
  };
  $("btn-goto-groups").onclick = () => {
    switchTab("groups");
    refreshGroups().catch((e) => toast(String(e), true));
  };
  $("groups-sort").onchange = () => refreshGroups().catch((e) => toast(String(e), true));
  $("groups-show-blocked").onchange = () =>
    refreshGroups().catch((e) => toast(String(e), true));
  $("groups-q").onkeydown = (ev) => {
    if (ev.key === "Enter") refreshGroups().catch((e) => toast(String(e), true));
  };
  $("btn-pull-onebot-groups").onclick = async () => {
    try {
      toast(await invoke<string>("pull_onebot_groups"));
      await refreshGroups();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-back-groups").onclick = () => {
    $("group-detail").classList.add("hidden");
    $("groups-master").classList.remove("hidden");
    animateViewEnter($("groups-master"));
    currentGroupId = null;
    refreshGroups().catch(() => undefined);
  };
  $("btn-save-group").onclick = async () => {
    try {
      const form = readGroupForm();
      await invoke("api_save_group", { config: form });
      toast(form.blocked ? "已屏蔽此群" : "本群配置已保存");
      if (form.blocked) {
        $("group-detail").classList.add("hidden");
        $("groups-master").classList.remove("hidden");
        currentGroupId = null;
        await refreshGroups();
      }
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("g-blocked").onchange = () => syncBlockEnableUi();
  $("g-enabled").onchange = () => {
    if ($<HTMLInputElement>("g-enabled").checked) {
      $<HTMLInputElement>("g-blocked").checked = false;
      syncBlockEnableUi();
    }
  };
  $("btn-pull-history").onclick = async () => {
    if (!currentGroupId) return;
    try {
      toast("正在从 OneBot 拉取历史消息…");
      const res = await invoke<{
        ok?: boolean;
        fetched?: number;
        inserted?: number;
        skipped?: number;
      }>("api_pull_history", { groupId: currentGroupId, count: 100 });
      toast(
        `历史拉取完成：获取 ${res.fetched ?? 0} 条，新增 ${res.inserted ?? 0}，已有 ${
          res.skipped ?? 0
        }`,
      );
      await openGroup(currentGroupId);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-run-llm").onclick = async () => {
    if (!currentGroupId) return;
    try {
      toast("正在执行 LLM 分析…");
      const result = await invoke<{
        status: string;
        riskMax?: string;
        msgCount?: number;
        reason?: string;
        source?: string;
      }>("api_run_llm", {
        groupId: currentGroupId,
      });
      if (result.status === "skipped") {
        toast(result.reason || "已跳过（消息不足）", true);
      } else {
        const extra = result.source ? ` · ${result.source}` : "";
        toast(
          `LLM 完成：${result.status}${result.riskMax ? " / 风险 " + result.riskMax : ""} · ${
            result.msgCount ?? "?"
          } 条${extra}`,
        );
      }
      await openGroup(currentGroupId);
    } catch (e) {
      toast(String(e), true);
    }
  };

  $("btn-add-provider").onclick = () => openAddProviderForm();
  $("btn-close-provider-form").onclick = () => closeProviderForm();
  $("btn-cancel-provider").onclick = () => closeProviderForm();
  $("btn-save-provider").onclick = () => saveProviderForm();
  $("pf-preset").onchange = () => applyPreset($<HTMLSelectElement>("pf-preset").value);
  $("btn-refresh-models").onclick = () => refreshSettingsModels();
  $("btn-refresh-group-models").onclick = () => refreshGroupModels();
  $("btn-test-provider").onclick = () => testActiveProvider();
  $("btn-test-onebot").onclick = () => testOnebotConnectivity();
  $("g-llm-provider").onchange = () => {
    const providerId = $<HTMLSelectElement>("g-llm-provider").value;
    const provider = settingsCache?.llm.providers.find((p) => p.id === providerId);
    groupModelOptions = provider?.defaultModel ? [{ id: provider.defaultModel }] : [];
    fillModelSelect("g-llm-model", groupModelOptions, provider?.defaultModel || "");
  };
  $("s-default-model").onchange = () => {
    const model = $<HTMLSelectElement>("s-default-model").value.trim();
    const active = activeProvider();
    if (!active || !settingsCache || !model) return;
    settingsCache.llm.providers = settingsCache.llm.providers.map((p) =>
      p.id === active.id ? { ...p, defaultModel: model } : p,
    );
  };

  $("btn-save-settings").onclick = async () => {
    try {
      await persistSettingsFromForm();
      toast("总配置已保存");
    } catch (e) {
      toast(String(e), true);
    }
  };

  try {
    settingsCache = await invoke<AppSettings>("api_get_settings");
    applyTheme(settingsCache.ui?.theme || "midnight");
    $<HTMLInputElement>("s-compact-mode").checked = !!settingsCache.ui?.compactModeEnabled;
  } catch {
    applyTheme("midnight");
  }

  try {
    const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", {
      sort: "recent",
      q: "",
    });
    groupsCache = res.groups || [];
    rememberGroupNames(groupsCache);
  } catch {
    /* ignore */
  }

  try {
    await getCurrentWindow().setMinSize(new LogicalSize(960, 680));
  } catch {
    /* ignore */
  }

  if (motionOk()) {
    gsap.from(".sidebar", { x: -18, autoAlpha: 0, duration: 0.4, ease: "power2.out" });
    gsap.from(".main", { y: 12, autoAlpha: 0, duration: 0.4, delay: 0.06, ease: "power2.out" });
  }

  await tick();
  await refreshUnreadBadge().catch(() => undefined);
  window.setInterval(tick, 4000);

  try {
    await getCurrentWindow().show();
    await getCurrentWindow().setFocus();
  } catch {
    /* browser preview */
  }
});
