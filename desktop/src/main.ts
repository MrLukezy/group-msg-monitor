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
  channel?: string;
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

const WECHAT_CHANNEL_ENABLED = false;

type ChannelBindQQ = { bound: boolean; label?: string; lastError?: string };
type ChannelBindWechat = {
  bound: boolean;
  label?: string;
  lastError?: string;
  dataDir?: string;
  decryptedDir?: string;
  keysPath?: string;
  pollSeconds?: number;
};
type ChannelBindTelegram = {
  bound: boolean;
  label?: string;
  lastError?: string;
  apiId?: number;
  apiHash?: string;
  botToken?: string;
  pollTimeout?: number;
};

type AppSettings = {
  onebotWsUrl: string;
  onebotAccessToken: string;
  channels?: {
    qq?: ChannelBindQQ;
    wechat?: ChannelBindWechat;
    telegram?: ChannelBindTelegram;
  };
  llm: {
    providers: LlmProvider[];
    activeProviderId: string;
    reportKeepLimit?: number;
  };
  ui?: {
    compactModeEnabled?: boolean;
    theme?: string;
  };
};

type GroupConfig = {
  groupId: string;
  groupName: string;
  channel?: string;
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
    textEnabled?: boolean;
    providerId: string;
    model: string;
    prompt: string;
    imageEnabled?: boolean;
    imageSameAsText?: boolean;
    imageProviderId?: string;
    imageModel?: string;
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
  windowExtended?: boolean;
  lookbackMessages?: number;
  llmContextRounds?: number;
  earlierMessages?: number;
  contextUsage?: {
    used_earlier_context?: boolean;
    earlier_rounds?: number;
    earlier_messages?: number;
    summary?: string;
  };
  lookbackReasons?: string[];
  source?: string;
  skipped?: boolean;
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
  tokenUsage?: {
    promptTokens?: number;
    completionTokens?: number;
    totalTokens?: number;
  };
  favorited?: boolean;
  favoritedAt?: string;
  hasFavoriteMessages?: boolean;
};

const FAVORITES_GROUP_ID = "__favorites__";
let monitoredDetailReportId: number | null = null;

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
const HIDE_SKIPPED_KEY = "gmm_hide_skipped_reports";
const NORMAL_SIZE = { width: 1280, height: 820 };
const COMPACT_SIZE = { width: 420, height: 168 };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

let currentGroupId: string | null = null;
let settingsCache: AppSettings | null = null;
let editingProviderId: string | null = null;
let settingsModelOptions: { id: string }[] = [];
let groupModelOptions: { id: string }[] = [];
let imageModelOptions: { id: string }[] = [];
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
let unreadByGroup = new Map<string, number>();
let unreadReportIds = new Set<number>();

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
  return String(s ?? "")
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
  if (r.skipped) return true;
  const h = r.headline || "";
  return h.includes("[定时跳过]") || h.includes("[跳过]");
}

function hideSkippedReportsEnabled(): boolean {
  const el = document.getElementById("monitored-hide-skipped") as HTMLInputElement | null;
  if (el) return !!el.checked;
  const raw = localStorage.getItem(HIDE_SKIPPED_KEY);
  return raw !== "0";
}

function loadHideSkippedToggle() {
  const el = document.getElementById("monitored-hide-skipped") as HTMLInputElement | null;
  if (!el) return;
  const raw = localStorage.getItem(HIDE_SKIPPED_KEY);
  el.checked = raw !== "0";
}

function saveHideSkippedToggle(on: boolean) {
  localStorage.setItem(HIDE_SKIPPED_KEY, on ? "1" : "0");
}

function visibleReports(reports: ReportRow[]): ReportRow[] {
  if (!hideSkippedReportsEnabled()) return reports;
  return reports.filter((r) => !isSkippedReport(r));
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
  const byGroup = new Map<string, number>();
  const unreadIds = new Set<number>();
  for (const r of unread) {
    if (r.id) unreadIds.add(r.id);
    if (!r.groupId) continue;
    byGroup.set(r.groupId, (byGroup.get(r.groupId) || 0) + 1);
  }
  unreadByGroup = byGroup;
  unreadReportIds = unreadIds;
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
  updateMonitoredGroupUnreadBadges();
  updateReportTitleUnreadBadges();
  return unread;
}

function isReportUnread(reportId: number): boolean {
  return !!reportId && unreadReportIds.has(reportId);
}

function reportUnreadDotHtml(reportId: number): string {
  if (!isReportUnread(reportId)) return "";
  return `<span class="report-unread-dot" title="未读"></span>`;
}

function updateReportTitleUnreadBadges() {
  document.querySelectorAll<HTMLButtonElement>(".report-title-item").forEach((btn) => {
    const id = Number(btn.dataset.id || 0);
    const unread = isReportUnread(id);
    btn.classList.toggle("is-unread", unread);
    let dot = btn.querySelector<HTMLElement>(".report-unread-dot");
    if (!unread) {
      dot?.remove();
      return;
    }
    if (!dot) {
      dot = document.createElement("span");
      dot.className = "report-unread-dot";
      dot.title = "未读";
      btn.appendChild(dot);
    }
  });
}

function groupUnreadCount(groupId: string): number {
  return unreadByGroup.get(groupId) || 0;
}

function unreadBadgeHtml(count: number, extraClass = ""): string {
  if (count <= 0) return "";
  const text = count > 99 ? "99+" : String(count);
  return `<span class="group-unread-badge ${extraClass}" title="未读 LLM ${text} 条">${text}</span>`;
}

function updateMonitoredGroupUnreadBadges() {
  document
    .querySelectorAll<HTMLButtonElement>("#monitored-group-list .monitored-group-item")
    .forEach((btn) => {
      const gid = btn.dataset.id || "";
      const n = groupUnreadCount(gid);
      let el = btn.querySelector<HTMLElement>(".group-unread-badge");
      if (n <= 0) {
        el?.remove();
        btn.classList.toggle("has-unread", false);
        return;
      }
      btn.classList.toggle("has-unread", true);
      const text = n > 99 ? "99+" : String(n);
      if (!el) {
        el = document.createElement("span");
        el.className = "group-unread-badge";
        btn.appendChild(el);
      }
      el.textContent = text;
      el.title = `未读 LLM ${text} 条`;
    });
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

function channelLabel(ch?: string) {
  const c = (ch || "qq").toLowerCase();
  if (c === "wechat" || c === "wx") return "微信";
  if (c === "telegram" || c === "tg") return "TG";
  return "QQ";
}

function guessChannel(groupId: string, explicit?: string) {
  if (explicit) return explicit;
  if (groupId.startsWith("wx:") || groupId.startsWith("wechat:")) return "wechat";
  if (groupId.startsWith("tg:") || groupId.startsWith("telegram:")) return "telegram";
  return "qq";
}

function switchSettingsTab(name: string) {
  document.querySelectorAll(".settings-tab").forEach((t) => {
    t.classList.toggle("active", (t as HTMLElement).dataset.stab === name);
  });
  const channelsEl = document.getElementById("stab-channels");
  if (channelsEl) channelsEl.classList.toggle("hidden", name !== "channels");
  const onebotEl = document.getElementById("stab-onebot");
  if (onebotEl) onebotEl.classList.toggle("hidden", true);
  $("stab-llm").classList.toggle("hidden", name !== "llm");
  $("stab-appearance").classList.toggle("hidden", name !== "appearance");
}

function renderChannelBindings(settings?: AppSettings | null) {
  const s = settings || settingsCache;
  const qq = s?.channels?.qq;
  const wx = s?.channels?.wechat;
  const tg = s?.channels?.telegram;

  const wxCard = document.querySelector<HTMLElement>('.channel-card[data-channel="wechat"]');
  if (wxCard) wxCard.hidden = !WECHAT_CHANNEL_ENABLED;

  const setBadge = (id: string, bound: boolean) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = bound ? "已绑定" : "未绑定";
    el.classList.toggle("on", bound);
  };
  setBadge("ch-qq-badge", !!qq?.bound);
  if (WECHAT_CHANNEL_ENABLED) {
    setBadge("ch-wx-badge", !!wx?.bound);
  } else {
    const badge = document.getElementById("ch-wx-badge");
    if (badge) {
      badge.textContent = "已屏蔽";
      badge.classList.remove("on");
    }
  }
  setBadge("ch-tg-badge", !!tg?.bound);

  const qqStatus = document.getElementById("ch-qq-status");
  if (qqStatus) {
    qqStatus.textContent = qq?.bound
      ? `已绑定${qq.label ? ` · ${qq.label}` : ""}`
      : "未绑定 · 需 NapCat OneBot WS";
  }
  const wxStatus = document.getElementById("ch-wx-status");
  if (wxStatus) {
    if (!WECHAT_CHANNEL_ENABLED) {
      wxStatus.textContent = "已屏蔽 · Windows ≥4.1.10 暂无稳定取 key 方案";
    } else if (!wx?.bound) {
      wxStatus.textContent = "未绑定 · 需本机 PC 微信保持登录";
    } else if (wx.lastError) {
      wxStatus.textContent = `已绑定${wx.label ? ` · ${wx.label}` : ""} · 尚未就绪：${wx.lastError}`;
    } else {
      wxStatus.textContent = `已绑定${wx.label ? ` · ${wx.label}` : ""} · 需保持 PC 微信登录`;
    }
  }
  const tgStatus = document.getElementById("ch-tg-status");
  if (tgStatus) {
    tgStatus.textContent = tg?.bound
      ? `已绑定${tg.label ? ` · ${tg.label}` : ""} · 用户账号`
      : "未绑定 · 扫码登录个人号（非 Bot）";
  }
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

const GROUPS_ACTIVE_FILTER_KEY = "gmm_groups_active_filter_v2";
const GROUPS_KEEP_ENABLED_KEY = "gmm_groups_keep_enabled";

/** 活跃过滤：0=全部, any=有落库消息, 7/30/90=近 N 天 */
type GroupsActiveFilter = "0" | "any" | "7" | "30" | "90";

function loadGroupsActiveFilter(): GroupsActiveFilter {
  const raw = (localStorage.getItem(GROUPS_ACTIVE_FILTER_KEY) || "").trim();
  if (raw === "0" || raw === "any" || raw === "7" || raw === "30" || raw === "90") {
    return raw;
  }
  // 不再沿用旧的默认「近30天」，避免把仅拉取、尚未落库的群全滤掉
  return "0";
}

function saveGroupsActiveFilter(v: GroupsActiveFilter) {
  localStorage.setItem(GROUPS_ACTIVE_FILTER_KEY, v);
}

function loadGroupsKeepEnabled(): boolean {
  const raw = localStorage.getItem(GROUPS_KEEP_ENABLED_KEY);
  if (raw === null) return true;
  return raw === "1" || raw === "true";
}

function saveGroupsKeepEnabled(on: boolean) {
  localStorage.setItem(GROUPS_KEEP_ENABLED_KEY, on ? "1" : "0");
}

function normalizeGroupLastTs(g: GroupItem): number {
  const raw = Number(g.lastTime || 0);
  if (!raw) return 0;
  // 兼容误存毫秒时间戳
  return raw > 1e12 ? Math.floor(raw / 1000) : raw;
}

function groupMatchesActiveFilter(g: GroupItem, filter: GroupsActiveFilter): boolean {
  if (filter === "0") return true;
  const last = normalizeGroupLastTs(g);
  const hasMsg = last > 0 || Number(g.msgCount || 0) > 0;
  if (filter === "any") return hasMsg;
  if (!hasMsg || !last) return false;
  const days = Number(filter);
  const cutoff = Math.floor(Date.now() / 1000) - days * 24 * 60 * 60;
  return last >= cutoff;
}

function activeFilterLabel(filter: GroupsActiveFilter): string {
  if (filter === "0") return "全部群";
  if (filter === "any") return "有落库消息";
  return `近 ${filter} 天内有消息`;
}

function updateGroupsFilterSummary(opts: {
  filter: GroupsActiveFilter;
  keepEnabled: boolean;
  showBlocked: boolean;
  shown: number;
  total: number;
  hiddenInactive: number;
  hiddenBlocked: number;
}) {
  const el = document.getElementById("groups-filter-summary");
  if (!el) return;
  const bits = [
    `<strong>过滤</strong>：${activeFilterLabel(opts.filter)}`,
    opts.keepEnabled ? "始终显示监听中" : "监听中也按活跃过滤",
    opts.showBlocked ? "含已屏蔽" : "隐藏已屏蔽",
    `显示 <strong>${opts.shown}</strong> / ${opts.total}`,
  ];
  if (opts.hiddenInactive > 0) {
    bits.push(`已隐藏不活跃/无落库 ${opts.hiddenInactive}`);
  }
  if (opts.hiddenBlocked > 0) bits.push(`已隐藏屏蔽 ${opts.hiddenBlocked}`);
  if (opts.filter !== "0") {
    bits.push("活跃度按本地落库消息判断");
  }
  el.innerHTML = bits.join(" · ");
}

async function refreshGroups() {
  const box = $("groups-list");
  try {
    const sort = $<HTMLSelectElement>("groups-sort").value;
    const q = $<HTMLInputElement>("groups-q").value.trim();
    const showBlocked = $<HTMLInputElement>("groups-show-blocked").checked;
    const activeSel = document.getElementById(
      "groups-active-filter",
    ) as HTMLSelectElement | null;
    const keepEnabledEl = document.getElementById(
      "groups-keep-enabled",
    ) as HTMLInputElement | null;
    const filter = (activeSel?.value || loadGroupsActiveFilter()) as GroupsActiveFilter;
    const keepEnabled = keepEnabledEl ? keepEnabledEl.checked : loadGroupsKeepEnabled();
    if (activeSel) activeSel.value = filter;
    if (keepEnabledEl) keepEnabledEl.checked = keepEnabled;

    const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", { sort, q });
    groupsCache = res.groups || [];
    rememberGroupNames(groupsCache);
    await refreshUnreadBadge().catch(() => undefined);

    let hiddenBlocked = 0;
    let hiddenInactive = 0;
    const visible = groupsCache.filter((g) => {
      if (!showBlocked && g.blocked) {
        hiddenBlocked += 1;
        return false;
      }
      if (!groupMatchesActiveFilter(g, filter)) {
        if (keepEnabled && g.enabled && !g.blocked) return true;
        hiddenInactive += 1;
        return false;
      }
      return true;
    });

    updateGroupsFilterSummary({
      filter,
      keepEnabled,
      showBlocked,
      shown: visible.length,
      total: groupsCache.length,
      hiddenInactive,
      hiddenBlocked,
    });

    if (!visible.length) {
      box.innerHTML = `<div class="empty">${
        groupsCache.length
          ? "当前过滤条件下没有群。可将活跃过滤改为「全部群」，或勾选「始终显示监听中」。"
          : "暂无群。可先绑定通道后点「拉取群列表」，或启动监听收消息。"
      }</div>`;
      return;
    }
    box.innerHTML = visible
      .map((g) => {
        const lastTs = normalizeGroupLastTs(g);
        const last = lastTs
          ? new Date(lastTs * 1000).toLocaleString()
          : "暂无落库消息";
        const name = groupDisplayName(g.groupId, g.groupName);
        const ch = guessChannel(g.groupId, g.channel);
        const unread = g.enabled && !g.blocked ? groupUnreadCount(g.groupId) : 0;
        const statusBadge = g.blocked
          ? `<span class="badge blocked">已屏蔽</span>`
          : `<span class="badge ${g.enabled ? "on" : ""}">${g.enabled ? "监听中" : "未启用"}</span>`;
        return `<button class="group-item ${g.blocked ? "is-blocked" : ""} ${
          unread > 0 ? "has-unread" : ""
        }" type="button" data-id="${escapeHtml(g.groupId)}">
        ${unreadBadgeHtml(unread)}
        <div>
          <div class="name">${escapeHtml(name)}</div>
          <div class="meta"><span class="channel-tag">${escapeHtml(channelLabel(ch))}</span> · ${escapeHtml(
            g.groupId,
          )} · 最近 ${escapeHtml(last)} · ${Number(g.msgCount) || 0} 条</div>
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
  } catch (e) {
    console.error("refreshGroups failed", e);
    box.innerHTML = `<div class="empty">加载群列表失败：${escapeHtml(String(e))}</div>`;
    const summary = document.getElementById("groups-filter-summary");
    if (summary) summary.textContent = "过滤条件不可用（列表加载失败）";
    throw e;
  }
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

function reportTotalTokens(r: ReportRow): number {
  const n =
    r.totalTokens ??
    r.tokenUsage?.totalTokens ??
    (r.promptTokens || 0) + (r.completionTokens || 0);
  return Number.isFinite(n) ? Math.max(0, Number(n)) : 0;
}

/** 展示 token 数：千级用 k。 */
function formatTokenCount(n?: number | null): string {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "0";
  if (v < 1000) return String(Math.round(v));
  if (v < 10000) return `${(v / 1000).toFixed(1)}k`;
  return `${Math.round(v / 1000)}k`;
}

function clampReportKeepLimit(n: number): number {
  if (!Number.isFinite(n)) return 100;
  const stepped = Math.round(n / 10) * 10;
  return Math.max(20, Math.min(500, stepped));
}

function syncReportKeepSlider(value?: number) {
  const el = document.getElementById("s-llm-report-keep") as HTMLInputElement | null;
  const label = document.getElementById("s-llm-report-keep-val");
  if (!el) return;
  const v = clampReportKeepLimit(Number(value ?? (el.value || 100)));
  el.value = String(v);
  if (label) label.textContent = String(v);
}

function reportTokenMeta(r: ReportRow): string {
  const total = reportTotalTokens(r);
  if (!total) return "";
  const prompt = r.promptTokens ?? r.tokenUsage?.promptTokens ?? 0;
  const completion = r.completionTokens ?? r.tokenUsage?.completionTokens ?? 0;
  if (prompt || completion) {
    return ` · Token ${formatTokenCount(total)}（入 ${formatTokenCount(prompt)} / 出 ${formatTokenCount(completion)}）`;
  }
  return ` · Token ${formatTokenCount(total)}`;
}

/** 渲染分析结果：`**用户名**`（非「标题：」形式）与已知发言者显示为黄色。 */
function renderReportMdHtml(md: string, extraUserNames: string[] = []): string {
  const raw = (md || "").trim() || "（无详细内容）";
  type Tok = { kind: "user" | "bold" | "text"; text: string };
  const toks: Tok[] = [];
  const re = /\*\*([^*\n]+)\*\*(：|:)?/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw))) {
    if (m.index > last) toks.push({ kind: "text", text: raw.slice(last, m.index) });
    if (m[2]) {
      toks.push({ kind: "bold", text: m[1] });
      toks.push({ kind: "text", text: m[2] });
    } else {
      toks.push({ kind: "user", text: m[1] });
    }
    last = m.index + m[0].length;
  }
  if (last < raw.length) toks.push({ kind: "text", text: raw.slice(last) });

  const known = [
    ...new Set(
      extraUserNames
        .map((n) => (n || "").trim())
        .filter((n) => n.length >= 2 && n.length <= 32),
    ),
  ].sort((a, b) => b.length - a.length);

  const paintText = (text: string): string => {
    if (!known.length || !text) return escapeHtml(text);
    const hits: string[] = [];
    let out = text;
    for (const name of known) {
      if (!out.includes(name)) continue;
      const parts = out.split(name);
      out = parts.join(`\u0000N${hits.length}\u0000`);
      hits.push(name);
    }
    return escapeHtml(out).replace(/\u0000N(\d+)\u0000/g, (_, i) => {
      const name = hits[Number(i)] || "";
      return `<span class="report-user-name">${escapeHtml(name)}</span>`;
    });
  };

  return toks
    .map((t) => {
      if (t.kind === "user") {
        return `<span class="report-user-name">${escapeHtml(t.text)}</span>`;
      }
      if (t.kind === "bold") {
        return `<strong class="report-md-strong">${escapeHtml(t.text)}</strong>`;
      }
      return paintText(t.text)
        .replace(/^## (.+)$/gm, `<span class="report-md-h2">$1</span>`)
        .replace(/^### (.+)$/gm, `<span class="report-md-h3">$1</span>`);
    })
    .join("");
}

function setReportDetailBody(md: string, extraUserNames: string[] = []) {
  $("monitored-report-detail-body").innerHTML = renderReportMdHtml(md, extraUserNames);
}

function renderMonitoredReportList(reports: ReportRow[]) {
  const box = $("monitored-report-list");
  if (!monitoredSelectedGroupId) {
    box.innerHTML = `<div class="empty">请选择左侧群查看分析主题</div>`;
    return;
  }
  const shown = visibleReports(reports);
  if (!reports.length) {
    const isFav = monitoredSelectedGroupId === FAVORITES_GROUP_ID;
    const g = groupsCache.find((x) => x.groupId === monitoredSelectedGroupId);
    box.innerHTML = `<div class="empty">${
      isFav
        ? "暂无收藏。打开某次分析详情后点「收藏」，可永久保留分析与聊天记录。"
        : g?.llmEnabled
          ? "暂无 LLM 分析主题，可在群配置中「立即 LLM 分析」"
          : "该群未开启 LLM 检测"
    }</div>`;
    return;
  }
  if (!shown.length) {
    box.innerHTML = `<div class="empty">当前仅有定时跳过记录；可关闭「忽略定时跳过」查看</div>`;
    return;
  }
  box.innerHTML = shown
    .map((r) => {
      const risk = (r.riskMax || "none").toLowerCase();
      const skipped = isSkippedReport(r);
      const unread = !skipped && isReportUnread(r.id);
      return `<button class="report-title-item ${risk === "high" ? "high" : ""} ${
        skipped ? "skipped" : ""
      } ${unread ? "is-unread" : ""} ${r.favorited ? "is-favorited" : ""}" type="button" data-id="${r.id}">
        ${reportUnreadDotHtml(unread ? r.id : 0)}
        <div class="report-title-text">${r.favorited ? "★ " : ""}${escapeHtml(r.headline || "(无标题)")}</div>
        <div class="report-title-meta">${escapeHtml(r.createdAt || "")} · 风险 ${escapeHtml(
          r.riskMax || "-",
        )} · ${r.msgCount ?? "-"} 条${escapeHtml(reportTokenMeta(r))}${
          r.favorited ? " · 已收藏" : ""
        }</div>
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
  document.querySelectorAll<HTMLButtonElement>("#monitored-group-list .monitored-group-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.id === groupId);
  });

  if (groupId === FAVORITES_GROUP_ID) {
    $("monitored-reports-title").textContent = "收藏夹 · LLM 主题";
    $("monitored-reports-hint").textContent = "已收藏的分析永久保留，不受清理影响";
    const reports = await invoke<ReportRow[]>("api_list_reports", {
      groupId: null,
      limit: 500,
      favoritesOnly: true,
    }).catch(() => [] as ReportRow[]);
    monitoredReportsCache = reports || [];
    renderMonitoredReportList(monitoredReportsCache);
    return;
  }

  const g = groupsCache.find((x) => x.groupId === groupId);
  const name = groupDisplayName(groupId, g?.groupName);
  $("monitored-reports-title").textContent = `${name} · LLM 主题`;
  $("monitored-reports-hint").textContent = `群号 ${groupId}`;

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
  monitoredDetailReportId = reportId;
  $("monitored-master").classList.add("hidden");
  $("monitored-report-detail").classList.remove("hidden");
  animateViewEnter($("monitored-report-detail"));
  $("monitored-report-detail-title").textContent = report.headline || "(无标题)";
  const lookbackNote =
    report.windowExtended && report.lookbackMessages
      ? report.llmContextRounds
        ? ` · 多轮向前补文 ${report.llmContextRounds} 轮 / ${report.lookbackMessages} 条`
        : ` · 含回溯前文 ${report.lookbackMessages} 条`
      : "";
  $("monitored-report-detail-meta").textContent =
    `${formatReportWindow(report)} · 风险 ${report.riskMax || "-"} · ${report.msgCount ?? "-"} 条消息${lookbackNote}${reportTokenMeta(report)}${
      report.favorited ? " · 已收藏" : ""
    }`;
  syncFavoriteButton(!!report.favorited);
  const reportMd = (report.reportMd || "").trim() || "（无详细内容）";
  setReportDetailBody(reportMd);

  const msgsBox = $("monitored-report-detail-msgs");
  msgsBox.dataset.fp = "";
  msgsBox.innerHTML = `<div class="empty">加载相关对话…</div>`;
  $("monitored-report-msgs-hint").textContent = report.favorited
    ? "收藏快照中的聊天记录（永久保留）"
    : report.windowExtended
      ? report.llmContextRounds
        ? `分析实际使用的消息（LLM 审查 ${report.llmContextRounds} 轮后补入前文）`
        : "分析实际使用的消息（含向前回溯的前文）"
      : "分析窗口内的原始消息";

  const start = Number(report.windowStart || 0);
  const end = Number(report.windowEnd || 0);

  try {
    let rows: MessageRow[] = [];
    if (report.favorited || report.hasFavoriteMessages) {
      rows = await invoke<MessageRow[]>("api_report_favorite_messages", {
        reportId,
      }).catch(() => [] as MessageRow[]);
    }
    if (!rows.length && report.groupId && start && end) {
      rows = await invoke<MessageRow[]>("api_messages_in_window", {
        groupId: report.groupId,
        startTs: start,
        endTs: end,
        limit: 800,
      });
    }
    const reasons = (report.lookbackReasons || []).filter(Boolean).join("；");
    const roundBit = report.llmContextRounds
      ? `LLM 审查 ${report.llmContextRounds} 轮 · `
      : "";
    $("monitored-report-msgs-hint").textContent = report.favorited
      ? `收藏快照 · 共 ${rows.length} 条 · ${formatReportWindow(report)}`
      : report.windowExtended
        ? `共 ${rows.length} 条 · ${roundBit}已回溯前文${
            report.lookbackMessages ? ` ${report.lookbackMessages} 条` : ""
          }${reasons ? `（${reasons}）` : ""} · ${formatReportWindow(report)}`
        : `共 ${rows.length} 条 · ${formatReportWindow(report)}`;
    if (!rows.length) {
      msgsBox.innerHTML = `<div class="empty">${
        report.favorited
          ? "收藏时未能快照到聊天记录（可能当时窗内无消息）"
          : "该时间窗内无落库消息（可能已被清理或分析时回退了其它窗口）"
      }</div>`;
      return;
    }
    const senderNames = [
      ...new Set(rows.map((m) => (m.senderName || "").trim()).filter(Boolean)),
    ];
    setReportDetailBody(reportMd, senderNames);
    msgsBox.innerHTML = rows.map((m) => messageArticleHtml(m, { hideGroup: true })).join("");
    msgsBox.dataset.fp = idsFingerprint(rows);
    msgsBox.scrollTop = 0;
  } catch (e) {
    msgsBox.innerHTML = `<div class="empty">加载对话失败：${escapeHtml(String(e))}</div>`;
  }
}

function syncFavoriteButton(favorited: boolean) {
  const btn = document.getElementById("btn-favorite-report") as HTMLButtonElement | null;
  if (!btn) return;
  btn.classList.toggle("is-favorited", favorited);
  btn.textContent = favorited ? "★ 已收藏" : "☆ 收藏";
  btn.title = favorited
    ? "取消收藏后，该报告将重新纳入自动清理"
    : "收藏后永久保留分析与当时的聊天记录快照";
}

async function toggleFavoriteCurrentReport() {
  if (!monitoredDetailReportId) {
    toast("未打开分析报告", true);
    return;
  }
  const report = monitoredReportsCache.find((r) => r.id === monitoredDetailReportId);
  const next = !report?.favorited;
  try {
    const res = await invoke<{ ok?: boolean; favorited?: boolean; messageCount?: number }>(
      "api_set_report_favorite",
      {
        reportId: monitoredDetailReportId,
        favorited: next,
      },
    );
    if (report) {
      report.favorited = !!res.favorited;
      report.hasFavoriteMessages = !!res.favorited;
    }
    syncFavoriteButton(!!res.favorited);
    const meta = $("monitored-report-detail-meta");
    if (report && meta) {
      const base = meta.textContent || "";
      meta.textContent = res.favorited
        ? base.includes("已收藏")
          ? base
          : `${base} · 已收藏`
        : base.replace(/\s*·\s*已收藏/g, "");
    }
    renderMonitoredReportList(monitoredReportsCache);
    toast(
      res.favorited
        ? `已收藏（快照 ${res.messageCount ?? 0} 条聊天，清理时不删除）`
        : "已取消收藏",
    );
  } catch (e) {
    toast(String(e), true);
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
  let totalTokens = 0;
  if (enabled.length) {
    const allReports = await invoke<ReportRow[]>("api_list_reports", {
      groupId: null,
      limit: 500,
    }).catch(() => [] as ReportRow[]);
    const seen = new Set<string>();
    for (const r of allReports || []) {
      if (r.groupId) seen.add(r.groupId);
      totalTokens += reportTotalTokens(r);
    }
    withReport = enabled.filter((g) => seen.has(g.groupId)).length;
  }
  try {
    const tok = await invoke<{
      totalTokens?: number;
      promptTokens?: number;
      completionTokens?: number;
    }>("api_token_stats", { groupId: null });
    const fromDb = Number(tok?.totalTokens || 0) || 0;
    // DB 汇总更完整；列表仅最近 N 条，作兜底
    if (fromDb > 0) totalTokens = fromDb;
  } catch {
    // 旧桌面端可能没有 api_token_stats，沿用列表累加
  }

  $("monitored-stats").innerHTML = `
    <div class="stat-card"><div class="stat-num">${enabled.length}</div><div class="stat-label">监听中</div></div>
    <div class="stat-card"><div class="stat-num">${totalMsg}</div><div class="stat-label">累计消息</div></div>
    <div class="stat-card"><div class="stat-num">${llm}</div><div class="stat-label">LLM 开启</div></div>
    <div class="stat-card"><div class="stat-num">${withReport}</div><div class="stat-label">已有分析</div></div>
    <div class="stat-card" title="${
      totalTokens > 0
        ? "含官方 usage 与缺失时的粗估"
        : "需重启监听服务后重新分析才会累计"
    }"><div class="stat-num">${formatTokenCount(totalTokens)}</div><div class="stat-label">总 Token</div></div>
  `;

  const box = $("monitored-group-list");
  const favCount = await invoke<ReportRow[]>("api_list_reports", {
    groupId: null,
    limit: 500,
    favoritesOnly: true,
  })
    .then((rows) => (rows || []).length)
    .catch(() => 0);

  if (!enabled.length) {
    monitoredSelectedGroupId = FAVORITES_GROUP_ID;
    monitoredReportsCache = [];
    box.innerHTML = `
      <button class="monitored-group-item favorites-item active" type="button" data-id="${FAVORITES_GROUP_ID}">
        <div class="name">★ 收藏夹</div>
        <div class="meta">${favCount} 条永久保留</div>
      </button>
      <div class="empty">暂无启用监听的群。到「群列表」打开群配置并勾选「启用监听此群」。</div>`;
    box.querySelectorAll<HTMLButtonElement>(".monitored-group-item").forEach((btn) => {
      btn.onclick = () => {
        selectMonitoredGroup(btn.dataset.id || "").catch((e) => toast(String(e), true));
      };
    });
    await selectMonitoredGroup(FAVORITES_GROUP_ID);
    return;
  }

  if (
    !monitoredSelectedGroupId ||
    (monitoredSelectedGroupId !== FAVORITES_GROUP_ID &&
      !enabled.some((g) => g.groupId === monitoredSelectedGroupId))
  ) {
    monitoredSelectedGroupId = enabled[0].groupId;
  }

  const favActive = monitoredSelectedGroupId === FAVORITES_GROUP_ID ? "active" : "";
  box.innerHTML =
    `<button class="monitored-group-item favorites-item ${favActive}" type="button" data-id="${FAVORITES_GROUP_ID}">
        <div class="name">★ 收藏夹</div>
        <div class="meta">${favCount} 条永久保留</div>
      </button>` +
    enabled
      .map((g) => {
        const last = g.lastTime
          ? new Date(g.lastTime * 1000).toLocaleString()
          : "暂无消息";
        const name = groupDisplayName(g.groupId, g.groupName);
        const active = g.groupId === monitoredSelectedGroupId ? "active" : "";
        const unread = groupUnreadCount(g.groupId);
        const unreadCls = unread > 0 ? "has-unread" : "";
        return `<button class="monitored-group-item ${active} ${unreadCls}" type="button" data-id="${escapeHtml(
          g.groupId,
        )}">
        ${unreadBadgeHtml(unread)}
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

  await refreshUnreadBadge().catch(() => undefined);

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

function fillProvidersSelect(selected?: string, selectId = "g-llm-provider") {
  const sel = $<HTMLSelectElement>(selectId);
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

function syncLlmAnalysisPanels() {
  const textEl = document.getElementById("g-llm-text-enabled") as HTMLInputElement | null;
  const imageEl = document.getElementById("g-llm-image-enabled") as HTMLInputElement | null;
  const sameEl = document.getElementById("g-llm-image-same") as HTMLInputElement | null;
  const textCfg = document.getElementById("g-llm-text-config");
  const imageWrap = document.getElementById("g-llm-image-wrap");
  const imageCfg = document.getElementById("g-llm-image-config");
  if (!textEl || !imageEl || !sameEl || !textCfg || !imageWrap || !imageCfg) return;
  const textOn = textEl.checked;
  const imageOn = imageEl.checked;
  const sameAsText = sameEl.checked;
  textCfg.classList.toggle("hidden", !textOn);
  imageWrap.classList.toggle("hidden", !imageOn);
  imageCfg.classList.toggle("hidden", !imageOn || sameAsText);
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

async function refreshImageModels() {
  const providerId = $<HTMLSelectElement>("g-llm-image-provider").value;
  if (!providerId) {
    toast("请先选择图片 Provider", true);
    return;
  }
  const btn = $<HTMLButtonElement>("btn-refresh-image-models");
  btn.disabled = true;
  try {
    toast("正在拉取图片模型列表…");
    imageModelOptions = await fetchModelsByProviderId(providerId);
    const keep = $<HTMLSelectElement>("g-llm-image-model").value;
    const provider = settingsCache?.llm.providers.find((p) => p.id === providerId);
    if (provider?.defaultModel && !imageModelOptions.find((m) => m.id === provider.defaultModel)) {
      imageModelOptions.unshift({ id: provider.defaultModel });
    }
    fillModelSelect(
      "g-llm-image-model",
      imageModelOptions,
      keep || provider?.defaultModel || "",
    );
    toast(`已获取 ${imageModelOptions.length} 个模型`);
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
  const prev = settingsCache.channels || {};
  const next: AppSettings = {
    onebotWsUrl: $<HTMLInputElement>("s-ws").value.trim(),
    onebotAccessToken: $<HTMLInputElement>("s-token").value.trim(),
    channels: {
      qq: {
        bound: !!prev.qq?.bound,
        label: prev.qq?.label || "",
        lastError: prev.qq?.lastError || "",
      },
      wechat: {
        bound: WECHAT_CHANNEL_ENABLED ? !!prev.wechat?.bound : false,
        label: WECHAT_CHANNEL_ENABLED ? prev.wechat?.label || "" : "",
        lastError: WECHAT_CHANNEL_ENABLED
          ? prev.wechat?.lastError || ""
          : "微信通道已暂时屏蔽",
        dataDir: $<HTMLInputElement>("s-wx-dir")?.value.trim() || prev.wechat?.dataDir || "",
        decryptedDir:
          $<HTMLInputElement>("s-wx-decrypted")?.value.trim() || prev.wechat?.decryptedDir || "",
        // keysPath 由后端扫 key / 导入成功后写入，不把「导入输入框」当成存储路径
        keysPath: prev.wechat?.keysPath || "",
        pollSeconds: prev.wechat?.pollSeconds ?? 1,
      },
      telegram: {
        bound: !!prev.telegram?.bound,
        label: prev.telegram?.label || "",
        lastError: prev.telegram?.lastError || "",
        apiId: Number($<HTMLInputElement>("s-tg-api-id").value.trim() || 0),
        apiHash: $<HTMLInputElement>("s-tg-api-hash").value.trim(),
        botToken: "",
        pollTimeout: prev.telegram?.pollTimeout ?? 25,
      },
    },
    llm: {
      activeProviderId: settingsCache.llm.activeProviderId,
      providers: settingsCache.llm.providers,
      reportKeepLimit: clampReportKeepLimit(
        Number($<HTMLInputElement>("s-llm-report-keep")?.value || settingsCache.llm.reportKeepLimit || 100),
      ),
    },
    ui: {
      compactModeEnabled: $<HTMLInputElement>("s-compact-mode").checked,
      theme: selectedTheme,
    },
  };
  await invoke("api_save_settings", { settings: next });
  settingsCache = next;
  applyTheme(selectedTheme);
  renderChannelBindings(next);
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
  $("detail-sub").textContent = `${channelLabel(guessChannel(groupId, cfg.channel))} · ${groupId}`;

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
  $<HTMLInputElement>("g-llm-text-enabled").checked = cfg.llmMonitor?.textEnabled !== false;
  $<HTMLInputElement>("g-llm-image-enabled").checked = cfg.llmMonitor?.imageEnabled !== false;
  $<HTMLInputElement>("g-llm-image-same").checked = cfg.llmMonitor?.imageSameAsText !== false;
  fillProvidersSelect(
    cfg.llmMonitor?.providerId || settingsCache.llm.activeProviderId,
    "g-llm-provider",
  );
  fillProvidersSelect(
    cfg.llmMonitor?.imageProviderId ||
      cfg.llmMonitor?.providerId ||
      settingsCache.llm.activeProviderId,
    "g-llm-image-provider",
  );
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
  const imageModel = cfg.llmMonitor?.imageModel || "";
  const imageProviderId = $<HTMLSelectElement>("g-llm-image-provider").value;
  const imageProvider = settingsCache.llm.providers.find((p) => p.id === imageProviderId);
  imageModelOptions = [];
  if (imageModel) imageModelOptions.push({ id: imageModel });
  else if (imageProvider?.defaultModel) imageModelOptions.push({ id: imageProvider.defaultModel });
  fillModelSelect(
    "g-llm-image-model",
    imageModelOptions,
    imageModel || imageProvider?.defaultModel || "",
  );
  $<HTMLInputElement>("g-llm-every").value = String(cfg.llmMonitor?.everyMinutes ?? 60);
  $<HTMLInputElement>("g-llm-window").value = String(cfg.llmMonitor?.windowMinutes ?? 60);
  $<HTMLInputElement>("g-llm-min").value = String(cfg.llmMonitor?.minMessages ?? 8);
  $<HTMLTextAreaElement>("g-llm-prompt").value = cfg.llmMonitor?.prompt || "";
  syncLlmAnalysisPanels();

  const msgs = await invoke<MessageRow[]>("api_recent_messages", { groupId, limit: 40 });
  const detailBox = $("detail-messages");
  detailBox.dataset.fp = "";
  renderMessages("detail-messages", msgs, { hideGroup: true });
  const reports = await invoke<ReportRow[]>("api_list_reports", { groupId, limit: 30 });
  const box = $("detail-reports");
  const shown = visibleReports(reports || []);
  if (!reports.length) {
    box.innerHTML = `<div class="empty">暂无 LLM 报告，可点「立即执行」</div>`;
  } else if (!shown.length) {
    box.innerHTML = `<div class="empty">当前仅有定时跳过记录；可在「监听中」关闭「忽略定时跳过」查看</div>`;
  } else {
    box.innerHTML = shown
      .map((r) => {
        const risk = (r.riskMax || "none").toLowerCase();
        const skipped = isSkippedReport(r);
        const unread = !skipped && isReportUnread(r.id);
        return `<button class="report-title-item ${risk === "high" ? "high" : ""} ${
          skipped ? "skipped" : ""
        } ${unread ? "is-unread" : ""}" type="button" data-id="${r.id}">
          ${reportUnreadDotHtml(unread ? r.id : 0)}
          <div class="report-title-text">${escapeHtml(r.headline || "(无标题)")}</div>
          <div class="report-title-meta">${escapeHtml(r.createdAt || "")} · 风险 ${escapeHtml(
            r.riskMax || "-",
          )} · ${r.msgCount ?? "-"} 条${escapeHtml(reportTokenMeta(r))}</div>
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
  const cached = groupsCache.find((g) => g.groupId === groupId);
  return {
    groupId,
    groupName: $<HTMLInputElement>("g-name").value.trim(),
    channel: guessChannel(groupId, cached?.channel),
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
      textEnabled: $<HTMLInputElement>("g-llm-text-enabled").checked,
      providerId: $<HTMLSelectElement>("g-llm-provider").value,
      model: $<HTMLSelectElement>("g-llm-model").value.trim(),
      prompt: $<HTMLTextAreaElement>("g-llm-prompt").value,
      imageEnabled: $<HTMLInputElement>("g-llm-image-enabled").checked,
      imageSameAsText: $<HTMLInputElement>("g-llm-image-same").checked,
      imageProviderId: $<HTMLSelectElement>("g-llm-image-provider").value,
      imageModel: $<HTMLSelectElement>("g-llm-image-model").value.trim(),
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
  $<HTMLInputElement>("s-wx-dir").value = settingsCache.channels?.wechat?.dataDir || "";
  $<HTMLInputElement>("s-wx-decrypted").value =
    settingsCache.channels?.wechat?.decryptedDir || "";
  // 导入框留给用户填「已有密钥文件」；不预填后端默认输出路径（文件往往还不存在）
  $<HTMLInputElement>("s-wx-keys").value = "";
  $<HTMLInputElement>("s-tg-api-id").value = String(settingsCache.channels?.telegram?.apiId || "");
  $<HTMLInputElement>("s-tg-api-hash").value = settingsCache.channels?.telegram?.apiHash || "";
  $<HTMLInputElement>("s-compact-mode").checked = !!settingsCache.ui?.compactModeEnabled;
  applyTheme(settingsCache.ui?.theme || "midnight");
  renderChannelBindings(settingsCache);
  syncReportKeepSlider(settingsCache.llm?.reportKeepLimit ?? 100);
  const active = activeProvider();
  settingsModelOptions = active?.defaultModel ? [{ id: active.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, active?.defaultModel || "");
  renderProviderList();
  switchSettingsTab("channels");
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

type LightboxState = {
  url: string;
  dataUrl: string;
  mime: string;
};

let lightboxState: LightboxState | null = null;

function setLightboxStatus(text: string) {
  const el = document.getElementById("image-lightbox-status");
  if (el) el.textContent = text;
}

function closeImageLightbox() {
  const root = document.getElementById("image-lightbox");
  if (!root) return;
  root.classList.add("hidden");
  root.hidden = true;
  const img = document.getElementById("image-lightbox-img") as HTMLImageElement | null;
  if (img) {
    img.removeAttribute("src");
  }
  lightboxState = null;
  setLightboxStatus("");
}

async function openImageLightbox(url: string) {
  const root = document.getElementById("image-lightbox");
  const img = document.getElementById("image-lightbox-img") as HTMLImageElement | null;
  if (!root || !img) {
    toast("图片预览组件不可用", true);
    return;
  }
  lightboxState = null;
  root.hidden = false;
  root.classList.remove("hidden");
  img.removeAttribute("src");
  setLightboxStatus("正在加载图片…");
  try {
    const res = await invoke<{ dataUrl: string; mime: string; bytesLen: number }>(
      "fetch_image_data_url",
      { url },
    );
    lightboxState = { url, dataUrl: res.dataUrl, mime: res.mime || "image/jpeg" };
    img.src = res.dataUrl;
    const kb = Math.max(1, Math.round((res.bytesLen || 0) / 1024));
    setLightboxStatus(`${kb} KB · ${res.mime || "image"}`);
  } catch (e) {
    setLightboxStatus(String(e));
    toast(String(e), true);
  }
}

function dataUrlToBlob(dataUrl: string): Blob {
  const m = /^data:([^;,]+)?(;base64)?,(.*)$/i.exec(dataUrl);
  if (!m) throw new Error("无效的图片数据");
  const mime = m[1] || "image/png";
  const isB64 = !!m[2];
  const data = m[3] || "";
  if (isB64) {
    const bin = atob(data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }
  return new Blob([decodeURIComponent(data)], { type: mime });
}

function extFromMime(mime: string): string {
  if (mime.includes("png")) return "png";
  if (mime.includes("webp")) return "webp";
  if (mime.includes("gif")) return "gif";
  if (mime.includes("bmp")) return "bmp";
  return "jpg";
}

async function copyLightboxImage() {
  if (!lightboxState?.dataUrl) {
    toast("图片尚未加载完成", true);
    return;
  }
  try {
    const blob = dataUrlToBlob(lightboxState.dataUrl);
    const mime = blob.type || lightboxState.mime || "image/png";
    if (!("clipboard" in navigator) || typeof ClipboardItem === "undefined") {
      throw new Error("当前环境不支持复制图片到剪贴板");
    }
    await navigator.clipboard.write([new ClipboardItem({ [mime]: blob })]);
    toast("图片已复制到剪贴板");
  } catch (e) {
    toast(`复制失败：${e}`, true);
  }
}

function saveLightboxImage() {
  if (!lightboxState?.dataUrl) {
    toast("图片尚未加载完成", true);
    return;
  }
  try {
    const a = document.createElement("a");
    a.href = lightboxState.dataUrl;
    a.download = `group-msg-${Date.now()}.${extFromMime(lightboxState.mime)}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("已开始保存图片");
  } catch (e) {
    toast(`保存失败：${e}`, true);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  loadHideSkippedToggle();

  const hideSkippedEl = document.getElementById(
    "monitored-hide-skipped",
  ) as HTMLInputElement | null;
  if (hideSkippedEl) {
    hideSkippedEl.onchange = () => {
      saveHideSkippedToggle(!!hideSkippedEl.checked);
      renderMonitoredReportList(monitoredReportsCache);
    };
  }

  document.getElementById("btn-lightbox-close")?.addEventListener("click", () => {
    closeImageLightbox();
  });
  document.getElementById("btn-lightbox-copy")?.addEventListener("click", () => {
    copyLightboxImage().catch((e) => toast(String(e), true));
  });
  document.getElementById("btn-lightbox-save")?.addEventListener("click", () => {
    saveLightboxImage();
  });
  document.getElementById("image-lightbox")?.addEventListener("click", (ev) => {
    const t = ev.target as HTMLElement | null;
    if (t?.closest?.("[data-lightbox-close]")) closeImageLightbox();
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeImageLightbox();
  });

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
    const imgA = t?.closest?.("a[data-image-url]") as HTMLAnchorElement | null;
    if (imgA) {
      ev.preventDefault();
      const url = imgA.dataset.imageUrl || imgA.href;
      if (url) openImageLightbox(url).catch((e) => toast(String(e), true));
      return;
    }
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
      switchSettingsTab((btn as HTMLElement).dataset.stab || "channels");
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
    monitoredDetailReportId = null;
    animateViewEnter($("monitored-master"));
    if (monitoredSelectedGroupId) {
      selectMonitoredGroup(monitoredSelectedGroupId).catch(() => undefined);
    }
  };
  const favBtn = document.getElementById("btn-favorite-report");
  if (favBtn) {
    favBtn.onclick = () => {
      toggleFavoriteCurrentReport().catch((e) => toast(String(e), true));
    };
  }
  $("btn-goto-groups").onclick = () => {
    switchTab("groups");
    refreshGroups().catch((e) => toast(String(e), true));
  };
  $("groups-sort").onchange = () => refreshGroups().catch((e) => toast(String(e), true));
  $("groups-show-blocked").onchange = () =>
    refreshGroups().catch((e) => toast(String(e), true));
  const activeFilterEl = document.getElementById(
    "groups-active-filter",
  ) as HTMLSelectElement | null;
  if (activeFilterEl) {
    activeFilterEl.value = loadGroupsActiveFilter();
    activeFilterEl.onchange = () => {
      saveGroupsActiveFilter(activeFilterEl.value as GroupsActiveFilter);
      refreshGroups().catch((e) => toast(String(e), true));
    };
  }
  const keepEnabledEl = document.getElementById(
    "groups-keep-enabled",
  ) as HTMLInputElement | null;
  if (keepEnabledEl) {
    keepEnabledEl.checked = loadGroupsKeepEnabled();
    keepEnabledEl.onchange = () => {
      saveGroupsKeepEnabled(!!keepEnabledEl.checked);
      refreshGroups().catch((e) => toast(String(e), true));
    };
  }
  $("groups-q").onkeydown = (ev) => {
    if (ev.key === "Enter") refreshGroups().catch((e) => toast(String(e), true));
  };
  $("btn-pull-channel-groups").onclick = async () => {
    try {
      const parts: string[] = [];
      if (settingsCache?.channels?.qq?.bound || !settingsCache?.channels) {
        try {
          parts.push(await invoke<string>("pull_onebot_groups"));
        } catch (e) {
          parts.push(`QQ: ${e}`);
        }
      }
      if (WECHAT_CHANNEL_ENABLED && settingsCache?.channels?.wechat?.bound) {
        const res = await invoke<{ ok?: boolean; message?: string }>("api_pull_wechat_groups");
        parts.push(res.message || (res.ok ? "微信群已拉取" : "微信拉取失败"));
      }
      if (settingsCache?.channels?.telegram?.bound) {
        const res = await invoke<{ ok?: boolean; message?: string }>("api_pull_telegram_groups");
        parts.push(res.message || (res.ok ? "TG 群已拉取" : "TG 拉取失败"));
      }
      toast(parts.filter(Boolean).join("；") || "请先在总配置绑定通道");
      await refreshGroups();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-bind-qq").onclick = async () => {
    try {
      await persistSettingsFromForm();
      const res = await invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
        "api_bind_qq",
        {
          payload: {
            onebotWsUrl: $<HTMLInputElement>("s-ws").value.trim(),
            onebotAccessToken: $<HTMLInputElement>("s-token").value.trim(),
          },
        },
      );
      if (res.channels && settingsCache) settingsCache.channels = res.channels;
      renderChannelBindings(settingsCache);
      showTestResult("onebot-test-result", !!res.ok, res.message || "");
      toast(res.message || "QQ 已绑定", !res.ok);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-unbind-qq").onclick = async () => {
    try {
      const res = await invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
        "api_unbind_channel",
        { channel: "qq" },
      );
      if (res.channels && settingsCache) settingsCache.channels = res.channels;
      renderChannelBindings(settingsCache);
      toast(res.message || "已解绑 QQ", !res.ok);
    } catch (e) {
      toast(String(e), true);
    }
  };
  if (WECHAT_CHANNEL_ENABLED) {
    $("btn-bind-wechat").onclick = async () => {
      try {
        await persistSettingsFromForm();
        const keysFile = $<HTMLInputElement>("s-wx-keys").value.trim();
        showTestResult(
          "wechat-bind-result",
          true,
          keysFile ? "正在用密钥文件解密…" : "正在绑定微信（扫 key / 解密）…",
        );
        const res = await invoke<{
          ok?: boolean;
          ready?: boolean;
          message?: string;
          channels?: AppSettings["channels"];
        }>("api_bind_wechat", {
          payload: {
            dataDir: $<HTMLInputElement>("s-wx-dir").value.trim(),
            keysFile,
            scanKeys: !keysFile,
          },
        });
        if (res.channels && settingsCache) {
          settingsCache.channels = res.channels;
          $<HTMLInputElement>("s-wx-dir").value = res.channels.wechat?.dataDir || "";
          $<HTMLInputElement>("s-wx-decrypted").value = res.channels.wechat?.decryptedDir || "";
        }
        renderChannelBindings(settingsCache);
        const ready = res.ready ?? res.ok;
        showTestResult("wechat-bind-result", !!ready, res.message || "");
        toast(res.message || "微信绑定完成", !ready);
        await refreshGroups();
      } catch (e) {
        showTestResult("wechat-bind-result", false, String(e));
        toast(String(e), true);
      }
    };
    $("btn-unbind-wechat").onclick = async () => {
      try {
        const res = await invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
          "api_unbind_channel",
          { channel: "wechat" },
        );
        if (res.channels && settingsCache) settingsCache.channels = res.channels;
        renderChannelBindings(settingsCache);
        toast(res.message || "已解绑微信", !res.ok);
      } catch (e) {
        toast(String(e), true);
      }
    };
    $("btn-wx-detect").onclick = async () => {
      try {
        const res = await invoke<{
          ok?: boolean;
          message?: string;
          accounts?: { data_dir?: string; account?: string; root?: string }[];
          roots?: string[];
        }>("api_wechat_detect");
        const first = res.accounts?.[0];
        if (first?.data_dir) $<HTMLInputElement>("s-wx-dir").value = first.data_dir;
        const lines = [
          res.message || "",
          ...(res.accounts || []).map(
            (a) => `账号 ${a.account || ""} → ${a.data_dir || ""}`,
          ),
          res.roots?.length ? `扫描根目录：\n${res.roots.join("\n")}` : "",
        ].filter(Boolean);
        showTestResult("wechat-bind-result", !!(res.accounts && res.accounts.length), lines.join("\n"));
        toast(res.message || "检测完成", !(res.accounts && res.accounts.length));
      } catch (e) {
        toast(String(e), true);
      }
    };
    $("btn-wx-scan-keys").onclick = async () => {
      try {
        showTestResult("wechat-bind-result", true, "正在扫描微信进程密钥…");
        const res = await invoke<{
          ok?: boolean;
          message?: string;
          channels?: AppSettings["channels"];
        }>("api_wechat_scan_keys");
        if (res.channels && settingsCache) {
          settingsCache.channels = res.channels;
          renderChannelBindings(settingsCache);
        }
        showTestResult("wechat-bind-result", !!res.ok, res.message || "");
        toast(res.message || "扫 key 完成", !res.ok);
      } catch (e) {
        showTestResult("wechat-bind-result", false, String(e));
        toast(String(e), true);
      }
    };
    $("btn-wx-import-keys").onclick = async () => {
      try {
        await persistSettingsFromForm();
        const keysFile = $<HTMLInputElement>("s-wx-keys").value.trim();
        if (!keysFile) {
          toast("请填写已有密钥文件的完整路径（如 all_keys.json），不要用尚未生成的默认路径", true);
          showTestResult(
            "wechat-bind-result",
            false,
            "当前本机还没有可用密钥。微信 4.1.10+ 无法自动扫描，请先用 ≤4.1.9 提取密钥文件，再把文件路径填到「导入密钥文件」后重试。",
          );
          return;
        }
        showTestResult("wechat-bind-result", true, "正在导入密钥并解密…");
        const res = await invoke<{
          ok?: boolean;
          ready?: boolean;
          message?: string;
          count?: number;
          channels?: AppSettings["channels"];
        }>("api_wechat_import_keys", {
          payload: {
            keysFile,
            dataDir: $<HTMLInputElement>("s-wx-dir").value.trim(),
          },
        });
        if (res.channels && settingsCache) {
          settingsCache.channels = res.channels;
          $<HTMLInputElement>("s-wx-dir").value = res.channels.wechat?.dataDir || "";
          $<HTMLInputElement>("s-wx-decrypted").value = res.channels.wechat?.decryptedDir || "";
        }
        renderChannelBindings(settingsCache);
        showTestResult("wechat-bind-result", !!res.ok, res.message || "");
        toast(res.message || "导入完成", !res.ok);
        await refreshGroups();
      } catch (e) {
        showTestResult("wechat-bind-result", false, String(e));
        toast(String(e), true);
      }
    };
    $("btn-pull-wechat-groups").onclick = async () => {
      try {
        const res = await invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
          "api_pull_wechat_groups",
        );
        if (res.channels && settingsCache) {
          settingsCache.channels = res.channels;
          renderChannelBindings(settingsCache);
        }
        showTestResult("wechat-bind-result", !!res.ok, res.message || "");
        toast(res.message || "微信群已拉取", !res.ok);
        await refreshGroups();
      } catch (e) {
        toast(String(e), true);
      }
    };
  }
  $("btn-bind-telegram").onclick = async () => {
    try {
      await persistSettingsFromForm();
      const res = await invoke<{
        ok?: boolean;
        message?: string;
        need_qr?: boolean;
        channels?: AppSettings["channels"];
      }>("api_bind_telegram", {
        payload: {
          apiId: Number($<HTMLInputElement>("s-tg-api-id").value.trim() || 0),
          apiHash: $<HTMLInputElement>("s-tg-api-hash").value.trim(),
        },
      });
      if (res.channels && settingsCache) settingsCache.channels = res.channels;
      renderChannelBindings(settingsCache);
      showTestResult("telegram-test-result", !!res.ok, res.message || "");
      toast(res.message || "Telegram 已绑定", !res.ok);
      if (res.ok) await refreshGroups();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-unbind-telegram").onclick = async () => {
    try {
      const res = await invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
        "api_unbind_channel",
        { channel: "telegram" },
      );
      if (res.channels && settingsCache) settingsCache.channels = res.channels;
      renderChannelBindings(settingsCache);
      toast(res.message || "已解绑 Telegram", !res.ok);
    } catch (e) {
      toast(String(e), true);
    }
  };
  let tgQrTimer = 0;
  const stopTgQrPoll = () => {
    if (tgQrTimer) {
      window.clearInterval(tgQrTimer);
      tgQrTimer = 0;
    }
  };
  const applyTgQrStatus = (st: {
    status?: string;
    message?: string;
    qr_png_base64?: string;
    url?: string;
  }) => {
    const box = $("tg-qr-box");
    const img = $<HTMLImageElement>("tg-qr-img");
    const hint = $("tg-qr-hint");
    const row2fa = $("tg-2fa-row");
    const status = st.status || "idle";
    if (status === "waiting_scan" || status === "starting" || status === "need_password") {
      box.classList.remove("hidden");
    }
    if (st.qr_png_base64) {
      img.src = `data:image/png;base64,${st.qr_png_base64}`;
      img.hidden = false;
    } else if (status === "starting") {
      img.hidden = true;
    }
    hint.textContent = st.message || status;
    row2fa.classList.toggle("hidden", status !== "need_password");
    if (status === "authorized") {
      showTestResult("telegram-test-result", true, st.message || "扫码成功");
      stopTgQrPoll();
      // 扫码成功后自动绑定
      invoke<{ ok?: boolean; message?: string; channels?: AppSettings["channels"] }>(
        "api_bind_telegram",
        {
          payload: {
            apiId: Number($<HTMLInputElement>("s-tg-api-id").value.trim() || 0),
            apiHash: $<HTMLInputElement>("s-tg-api-hash").value.trim(),
          },
        },
      )
        .then((res) => {
          if (res.channels && settingsCache) settingsCache.channels = res.channels;
          renderChannelBindings(settingsCache);
          showTestResult(
            "telegram-test-result",
            !!res.ok,
            res.message || st.message || "已绑定",
          );
          toast(res.message || "Telegram 已绑定", !res.ok);
          if (res.ok) refreshGroups().catch(() => undefined);
        })
        .catch((e) => toast(String(e), true));
    } else if (status === "error") {
      showTestResult("telegram-test-result", false, st.message || "扫码失败");
      stopTgQrPoll();
    }
  };
  $("btn-tg-qr-start").onclick = async () => {
    try {
      await persistSettingsFromForm();
      stopTgQrPoll();
      $("tg-qr-box").classList.remove("hidden");
      $("tg-qr-hint").textContent = "正在启动扫码…";
      const res = await invoke<{ ok?: boolean; message?: string }>("api_telegram_qr_start", {
        payload: {
          apiId: Number($<HTMLInputElement>("s-tg-api-id").value.trim() || 0),
          apiHash: $<HTMLInputElement>("s-tg-api-hash").value.trim(),
        },
      });
      if (!res.ok) {
        showTestResult("telegram-test-result", false, res.message || "启动失败");
        toast(res.message || "启动扫码失败", true);
        return;
      }
      toast("请用手机 Telegram 扫码");
      tgQrTimer = window.setInterval(() => {
        invoke<{
          status?: string;
          message?: string;
          qr_png_base64?: string;
          url?: string;
        }>("api_telegram_qr_status")
          .then(applyTgQrStatus)
          .catch(() => undefined);
      }, 1200);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-tg-qr-cancel").onclick = async () => {
    try {
      stopTgQrPoll();
      await invoke("api_telegram_qr_cancel");
      $("tg-qr-box").classList.add("hidden");
      toast("已取消扫码");
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-tg-2fa").onclick = async () => {
    try {
      const password = $<HTMLInputElement>("s-tg-2fa").value.trim();
      const res = await invoke<{ ok?: boolean; message?: string }>("api_telegram_qr_2fa", {
        payload: { password },
      });
      toast(res.message || "已提交密码", !res.ok);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-tg-detect").onclick = async () => {
    try {
      const res = await invoke<{
        ok?: boolean;
        message?: string;
        local?: { path?: string; note?: string }[];
        session?: { authorized?: boolean; message?: string };
      }>("api_telegram_detect");
      const lines = [
        res.message || "",
        ...(res.local || []).map((x) => `${x.path || ""} — ${x.note || ""}`),
        res.session?.message || "",
      ].filter(Boolean);
      showTestResult("telegram-test-result", !!res.ok, lines.join("\n"));
      toast(res.message || "检测完成", !res.ok);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-test-telegram").onclick = async () => {
    try {
      await persistSettingsFromForm();
      const res = await invoke<{ ok?: boolean; message?: string; latencyMs?: number }>(
        "api_test_telegram",
      );
      const latency = res.latencyMs != null ? `（${res.latencyMs} ms）` : "";
      showTestResult(
        "telegram-test-result",
        !!res.ok,
        `${res.ok ? "session 有效" : "未登录"} ${latency}\n${res.message || ""}`,
      );
      toast(res.ok ? "Telegram session 正常" : res.message || "请先扫码登录", !res.ok);
    } catch (e) {
      showTestResult("telegram-test-result", false, String(e));
      toast(String(e), true);
    }
  };
  $("btn-pull-telegram-groups").onclick = async () => {
    try {
      const res = await invoke<{ ok?: boolean; message?: string }>("api_pull_telegram_groups");
      toast(res.message || "TG 群已拉取", !res.ok);
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
      const wasEnabled = !!groupsCache.find((g) => g.groupId === form.groupId)?.enabled;
      if (form.enabled && !wasEnabled && !form.blocked) {
        toast("正在保存并补拉历史消息…");
      }
      const res = await invoke<{
        ok?: boolean;
        newlyEnabled?: boolean;
        history?: {
          ok?: boolean;
          fetched?: number;
          inserted?: number;
          skipped?: number;
          error?: string;
          message?: string;
        } | null;
      }>("api_save_group", { config: form });
      if (form.blocked) {
        toast("已屏蔽此群");
        $("group-detail").classList.add("hidden");
        $("groups-master").classList.remove("hidden");
        currentGroupId = null;
        await refreshGroups();
        return;
      }
      const hist = res.history;
      if (res.newlyEnabled && hist) {
        if (hist.ok === false) {
          toast(
            `已启用监听，但补拉历史失败：${hist.error || hist.message || "未知错误"}`,
            true,
          );
        } else {
          toast(
            `已启用监听，并补拉历史：获取 ${hist.fetched ?? 0} 条，新增 ${hist.inserted ?? 0} 条`,
          );
        }
      } else {
        toast("本群配置已保存");
      }
      if (currentGroupId) await openGroup(currentGroupId);
      await refreshGroups().catch(() => undefined);
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
      toast("正在从 OneBot 拉取历史消息（最多约 500 条）…");
      const res = await invoke<{
        ok?: boolean;
        fetched?: number;
        inserted?: number;
        skipped?: number;
        pages?: number;
        message?: string;
      }>("api_pull_history", { groupId: currentGroupId, count: 300 });
      if (res.ok === false) {
        toast(res.message || "拉取历史失败", true);
      } else {
        toast(
          `历史拉取完成：获取 ${res.fetched ?? 0} 条，新增 ${res.inserted ?? 0}，已有 ${
            res.skipped ?? 0
          }${res.pages ? `（${res.pages} 页）` : ""}`,
        );
      }
      await openGroup(currentGroupId);
      await refreshGroups().catch(() => undefined);
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
        totalTokens?: number;
        promptTokens?: number;
        completionTokens?: number;
      }>("api_run_llm", {
        groupId: currentGroupId,
      });
      if (result.status === "skipped") {
        toast(result.reason || "已跳过（消息不足）", true);
      } else {
        const extra = result.source ? ` · ${result.source}` : "";
        const tok =
          result.totalTokens && result.totalTokens > 0
            ? ` · Token ${formatTokenCount(result.totalTokens)}`
            : "";
        toast(
          `LLM 完成：${result.status}${result.riskMax ? " / 风险 " + result.riskMax : ""} · ${
            result.msgCount ?? "?"
          } 条${tok}${extra}`,
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
  const btnRefreshImageModels = document.getElementById("btn-refresh-image-models");
  if (btnRefreshImageModels) {
    btnRefreshImageModels.onclick = () => refreshImageModels();
  }
  $("btn-test-provider").onclick = () => testActiveProvider();
  $("btn-test-onebot").onclick = () => testOnebotConnectivity();
  const bindToggle = (id: string) => {
    const el = document.getElementById(id) as HTMLInputElement | null;
    if (el) el.onchange = () => syncLlmAnalysisPanels();
  };
  bindToggle("g-llm-text-enabled");
  bindToggle("g-llm-image-enabled");
  bindToggle("g-llm-image-same");
  $("g-llm-provider").onchange = () => {
    const providerId = $<HTMLSelectElement>("g-llm-provider").value;
    const provider = settingsCache?.llm.providers.find((p) => p.id === providerId);
    groupModelOptions = provider?.defaultModel ? [{ id: provider.defaultModel }] : [];
    fillModelSelect("g-llm-model", groupModelOptions, provider?.defaultModel || "");
  };
  const imageProviderSel = document.getElementById("g-llm-image-provider");
  if (imageProviderSel) {
    imageProviderSel.onchange = () => {
      const providerId = $<HTMLSelectElement>("g-llm-image-provider").value;
      const provider = settingsCache?.llm.providers.find((p) => p.id === providerId);
      imageModelOptions = provider?.defaultModel ? [{ id: provider.defaultModel }] : [];
      fillModelSelect("g-llm-image-model", imageModelOptions, provider?.defaultModel || "");
    };
  }
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

  const reportKeepEl = document.getElementById("s-llm-report-keep") as HTMLInputElement | null;
  if (reportKeepEl) {
    reportKeepEl.oninput = () => syncReportKeepSlider(Number(reportKeepEl.value));
    reportKeepEl.onchange = () => syncReportKeepSlider(Number(reportKeepEl.value));
  }

  try {
    settingsCache = await invoke<AppSettings>("api_get_settings");
    applyTheme(settingsCache.ui?.theme || "midnight");
    $<HTMLInputElement>("s-compact-mode").checked = !!settingsCache.ui?.compactModeEnabled;
    syncReportKeepSlider(settingsCache.llm?.reportKeepLimit ?? 100);
  } catch {
    applyTheme("midnight");
    syncReportKeepSlider(100);
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
