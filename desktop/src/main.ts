import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalSize } from "@tauri-apps/api/window";
import { openUrl } from "@tauri-apps/plugin-opener";
import gsap from "gsap";
import QRCode from "qrcode";
import { formatTime, renderMsgHtml } from "./cq";
import { setupUiSounds } from "./ui-sounds";

type StatusInfo = {
  napcatInstalled: boolean;
  napcatWebuiUp: boolean;
  onebotWsUp: boolean;
  monitorRunning: boolean;
  qqMode?: string;
  officialQqRunning?: boolean;
  napcatProcessRunning?: boolean;
  notificationAccess?: string;
  uiaReady?: boolean;
};

type GroupItem = {
  groupId: string;
  groupName: string;
  channel?: string;
  enabled: boolean;
  lastTime?: number | null;
  msgCount: number;
  activityCount?: number;
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
  liveCursor?: number | null;
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
const TELEGRAM_CHANNEL_ENABLED = false;

type ChannelBindQQ = {
  bound: boolean;
  label?: string;
  lastError?: string;
  mode?: "onebot" | "passive" | string;
  notificationAccess?: string;
  uiaReady?: boolean;
  pollSeconds?: number;
  groupNameMap?: Record<string, string>;
};
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
    defaultImageModel?: string;
    defaultPrompt?: string;
    defaultEveryMinutes?: number;
    defaultWindowMinutes?: number;
    defaultMinMessages?: number;
    reportKeepLimit?: number;
  };
  ui?: {
    theme?: string;
  };
};

type GroupConfig = {
  groupId: string;
  groupName: string;
  channel?: string;
  enabled: boolean;
  basic: { logAll: boolean; storageEnabled: boolean };
  keywordMonitor: {
    enabled: boolean;
    keywords: string[];
    alertEnabled: boolean;
    webhookUrl: string;
  };
  llmMonitor: {
    enabled: boolean;
    useGlobalDefaults?: boolean;
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
  jobId?: number | null;
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
  failed?: boolean;
  error?: {
    stage?: string;
    type?: string;
    summary?: string;
    detail?: string;
    log_excerpt?: string;
  };
  githubIssueUrl?: string;
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
  tagline: string;
  mode: "dark" | "light";
  atmosphere?: string;
  swatches: [string, string, string];
}[] = [
  {
    id: "midnight",
    name: "星渚",
    tagline: "星沉沧海，灯火孤洲",
    mode: "dark",
    atmosphere: "/theme-atmospheres/midnight.webp",
    swatches: ["#0a0d12", "#d4a574", "#6b9e9a"],
  },
  {
    id: "daylight",
    name: "素笺",
    tagline: "素纸落墨，清光入卷",
    mode: "light",
    atmosphere: "/theme-atmospheres/dawn.webp",
    swatches: ["#f6f3ec", "#5a7a72", "#b08958"],
  },
  {
    id: "ocean",
    name: "秩序工坊",
    tagline: "旧线拆解，结构重新归位",
    mode: "dark",
    atmosphere: "/theme-atmospheres/refactor.webp",
    swatches: ["#081015", "#63a5a0", "#728fa8"],
  },
  {
    id: "forest",
    name: "竹影",
    tagline: "翠叶筛光，静影沉璧",
    mode: "light",
    swatches: ["#f0f5ef", "#5a8a68", "#8aaa7a"],
  },
  {
    id: "rose",
    name: "杏雨",
    tagline: "细雨湿春，杏花微照",
    mode: "light",
    swatches: ["#f8f1ec", "#c4886a", "#8a9e8a"],
  },
  {
    id: "graphite",
    name: "无限月读",
    tagline: "虚空生白，万象皆寂",
    mode: "dark",
    atmosphere: "/theme-atmospheres/void.webp",
    swatches: ["#040408", "#9a7ac8", "#6a98c4"],
  },
];

const READ_IDS_KEY = "gmm_read_report_ids";
const HIDE_SKIPPED_KEY = "gmm_hide_skipped_reports";
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

let currentGroupId: string | null = null;
let settingsCache: AppSettings | null = null;
let editingProviderId: string | null = null;
let settingsModelOptions: { id: string }[] = [];
let groupModelOptions: { id: string }[] = [];
let imageModelOptions: { id: string }[] = [];
let toastTimer = 0;
let settingsAutoSaveTimer = 0;
let settingsAutoSaveReady = false;
let settingsSaveGeneration = 0;
let settingsSaveChain: Promise<unknown> = Promise.resolve();
let reduceMotion = false;
let groupNameMap = new Map<string, string>();
let groupsCache: GroupItem[] = [];
const groupQuickSaveInFlight = new Set<string>();
let liveScrollQuietUntil = 0;
let liveSelectedGroupId: string | null = null;
let liveGroupsRefreshAt = 0;
const liveMessageCache = new Map<string, MessageRow[]>();
const liveMessageCursors = new Map<string, number>();
let liveRenderFrame = 0;
let liveActivityInitialized = false;
const liveActivityWatermarks = new Map<string, number>();
const liveUnreadGroups = new Set<string>();
let monitoredSelectedGroupId: string | null = null;
let monitoredReportsCache: ReportRow[] = [];
let monitoredReportTab: "success" | "errors" = "success";
let selectedTheme = "midnight";
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
    groupsCache.filter((g) => g.enabled).map((g) => g.groupId),
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

function isFailedReport(r: ReportRow): boolean {
  // list-reports 会为正常报告返回 error: {}；空对象在 JS 中也是真值，
  // 不能据此判断失败。失败以服务端明确标记为准，标题判断仅兼容旧记录。
  return r.failed === true || (r.headline || "").includes("[分析失败]");
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

function syncMonitoredReportTabs(reports: ReportRow[]) {
  const visible = visibleReports(reports);
  const successCount = visible.filter((r) => !isFailedReport(r)).length;
  const errorCount = reports.filter(isFailedReport).length;
  $("monitored-success-count").textContent = String(successCount);
  $("monitored-error-count").textContent = String(errorCount);

  const successTab = $<HTMLButtonElement>("monitored-tab-success");
  const errorTab = $<HTMLButtonElement>("monitored-tab-errors");
  const showingErrors = monitoredReportTab === "errors";
  successTab.classList.toggle("active", !showingErrors);
  errorTab.classList.toggle("active", showingErrors);
  successTab.setAttribute("aria-selected", String(!showingErrors));
  errorTab.setAttribute("aria-selected", String(showingErrors));

  document
    .querySelector<HTMLElement>(".monitored-reports-head .toolbar-check")
    ?.classList.toggle("hidden", showingErrors);
}

function applyTheme(themeId: string) {
  const theme = THEMES.find((t) => t.id === themeId) || THEMES[0];
  selectedTheme = theme.id;
  document.body.setAttribute("data-theme", theme.id);
  document.documentElement.setAttribute("data-theme", theme.id);
  document.documentElement.setAttribute("data-theme-mode", theme.mode);
  document.documentElement.setAttribute(
    "data-theme-category",
    theme.atmosphere ? "achievement" : "palette",
  );
  renderThemePicker();
}

function renderThemePicker() {
  const box = document.getElementById("theme-picker");
  if (!box) return;
  box.innerHTML = THEMES.map((t) => {
    const active = t.id === selectedTheme ? "active" : "";
    return `<button class="theme-card ${active}" type="button" data-theme-id="${t.id}">
      <div class="theme-swatches ${t.atmosphere ? "has-wall" : ""}" ${
        t.atmosphere ? `style="background-image:url(${t.atmosphere})"` : ""
      }>
        <span style="background:${t.swatches[0]}"></span>
        <span style="background:${t.swatches[1]}"></span>
        <span style="background:${t.swatches[2]}"></span>
      </div>
      <div class="theme-card-name">${escapeHtml(t.name)}</div>
      <div class="theme-card-tagline">${escapeHtml(t.tagline)}</div>
    </button>`;
  }).join("");
  box.querySelectorAll<HTMLButtonElement>(".theme-card").forEach((btn) => {
    btn.onclick = () => {
      applyTheme(btn.dataset.themeId || "midnight");
      scheduleSettingsAutoSave(150);
    };
  });
}

async function refreshUnreadBadge() {
  const read = loadReadIds();
  const reports = await invoke<ReportRow[]>("api_list_reports", {
    groupId: null,
    limit: 200,
  }).catch(() => [] as ReportRow[]);
  const enabledIds = new Set(
    groupsCache.filter((g) => g.enabled).map((g) => g.groupId),
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
  await refreshUnreadBadge();
}

function setPill(key: string, on: boolean, label: string) {
  const el = document.querySelector(`.pill[data-key="${key}"]`) as HTMLButtonElement;
  if (!el) return;
  if (on) el.dataset.repairing = "false";
  const repairing = el.dataset.repairing === "true";
  const next = `${label}${on ? " · 在线" : repairing ? " · 重连中…" : " · 离线 · 点击重连"}`;
  const wasOn = el.classList.contains("on");
  el.dataset.online = String(on);
  el.disabled = repairing && !on;
  el.title = on
    ? `${label} 当前在线`
    : repairing
      ? `正在修复并重连 ${label}`
      : `${label} 当前离线，点击尝试修复并重连`;
  el.setAttribute("aria-label", el.title);
  if (el.textContent === next && wasOn === on) return;
  el.classList.toggle("on", on);
  el.classList.toggle("off", !on);
  el.textContent = next;
}

let lastStatus: StatusInfo | null = null;
let reconnectAllInFlight = false;
let reconnectAllStep = "";

function currentQqMode(settings?: AppSettings | null): "onebot" | "passive" {
  void settings;
  return "onebot";
}

function syncQqModeFields(settings?: AppSettings | null) {
  const mode = currentQqMode(settings);
  const select = document.getElementById("s-qq-mode") as HTMLSelectElement | null;
  if (select && select.value !== mode) select.value = mode;
  const onebotFields = document.getElementById("qq-onebot-fields");
  const passiveFields = document.getElementById("qq-passive-fields");
  const hint = document.getElementById("qq-mode-hint");
  const btnTest = document.getElementById("btn-test-onebot") as HTMLButtonElement | null;
  const btnPull = document.getElementById("btn-pull-onebot-groups") as HTMLButtonElement | null;
  const btnDetect = document.getElementById("btn-detect-qq-passive") as HTMLButtonElement | null;
  const passive = mode === "passive";
  onebotFields?.classList.toggle("hidden", passive);
  passiveFields?.classList.toggle("hidden", !passive);
  btnTest?.classList.toggle("hidden", passive);
  btnPull?.classList.toggle("hidden", passive);
  btnDetect?.classList.toggle("hidden", !passive);
  if (hint) {
    hint.textContent = passive
      ? "被动模式：主号只跑官方 QQ。用系统通知采集后台消息，用当前聊天窗口补采；静音群/历史/原图可能缺失。"
      : "当前仅支持 NapCat / OneBot 完整监听。";
  }
  const qq = (settings || settingsCache)?.channels?.qq;
  const poll = document.getElementById("s-qq-poll") as HTMLInputElement | null;
  if (poll && qq?.pollSeconds) poll.value = String(qq.pollSeconds);
  const passiveStatus = document.getElementById("qq-passive-status");
  if (passiveStatus && passive) {
    const access = qq?.notificationAccess || "unknown";
    const mapCount = Object.keys(qq?.groupNameMap || {}).length;
    passiveStatus.textContent =
      `通知权限: ${access} · UIA: ${qq?.uiaReady ? "可用" : "待探测"} · 已映射群名 ${mapCount} 个。` +
      " 静音群/关闭通知/未打开历史可能漏消息；请勿同时运行 NapCat。";
  }
}

function napcatOnline(status: StatusInfo): boolean {
  return status.napcatInstalled && (status.napcatWebuiUp || status.onebotWsUp);
}

function syncReconnectAllButton(status?: StatusInfo | null, step = "") {
  const btn = document.getElementById("btn-reconnect-all") as HTMLButtonElement | null;
  if (!btn) return;
  const current = status || lastStatus;
  const mode = current?.qqMode || currentQqMode();
  const needsRepair =
    !!current &&
    (mode === "passive"
      ? !current.officialQqRunning || !current.monitorRunning || !!current.napcatProcessRunning
      : !napcatOnline(current) || !current.onebotWsUp || !current.monitorRunning);
  if (step) reconnectAllStep = step;
  btn.classList.toggle("hidden", !reconnectAllInFlight && !needsRepair);
  btn.disabled = reconnectAllInFlight;
  btn.textContent = reconnectAllInFlight ? step || reconnectAllStep || "正在重连…" : "一键重连";
}

async function waitForStatus(
  predicate: (status: StatusInfo) => boolean,
  timeoutMs: number,
  step: string,
): Promise<StatusInfo> {
  const deadline = Date.now() + timeoutMs;
  let status = await refreshStatus();
  while (!predicate(status) && Date.now() < deadline) {
    syncReconnectAllButton(status, step);
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
    status = await refreshStatus();
  }
  if (!predicate(status)) throw new Error(`${step}超时，请检查登录授权或端口占用`);
  return status;
}

async function reconnectAll() {
  if (reconnectAllInFlight) return;
  reconnectAllInFlight = true;
  syncReconnectAllButton(lastStatus, "正在检测…");
  try {
    let status = await refreshStatus();
    const mode = status.qqMode || currentQqMode();

    if (mode === "passive") {
      if (status.napcatProcessRunning) {
        throw new Error("被动模式下检测到 NapCat 仍在运行，请先关闭 NapCat 再重连官方 QQ");
      }
      if (!status.officialQqRunning) {
        syncReconnectAllButton(status, "1/2 打开官方 QQ…");
        await invoke<string>("show_qq_window");
        status = await waitForStatus((s) => !!s.officialQqRunning, 60_000, "1/2 等待官方 QQ…");
      }
      if (!status.monitorRunning) {
        syncReconnectAllButton(status, "2/2 启动监听…");
        try {
          await invoke<string>("start_monitor");
        } catch (e) {
          status = await refreshStatus();
          if (!status.monitorRunning) throw e;
        }
        status = await waitForStatus((s) => s.monitorRunning, 15_000, "2/2 等待监听服务…");
      }
      toast("重连完成：官方 QQ 与被动监听服务已在线");
    } else {
      if (!napcatOnline(status) || !status.onebotWsUp) {
        syncReconnectAllButton(status, "1/3 启动 NapCat…");
        try {
          await invoke<string>("start_napcat");
        } catch (e) {
          status = await refreshStatus();
          if (!napcatOnline(status)) throw e;
        }
        status = await refreshStatus();
        if (!status.onebotWsUp) {
          openNapcatLogin();
          toast("NapCat 已启动，请完成 QQ 扫码授权，随后会自动继续重连");
        }
        status = await waitForStatus((s) => s.onebotWsUp, 120_000, "2/3 等待 OneBot…");
      }

      if (!status.monitorRunning) {
        syncReconnectAllButton(status, "3/3 启动监听…");
        try {
          await invoke<string>("start_monitor");
        } catch (e) {
          status = await refreshStatus();
          if (!status.monitorRunning) throw e;
        }
        status = await waitForStatus((s) => s.monitorRunning, 15_000, "3/3 等待监听服务…");
      }

      toast("重连完成：NapCat、OneBot、监听服务均已在线");
    }
    await refreshStatus();
  } catch (e) {
    toast(`一键重连失败：${e}`, true);
  } finally {
    reconnectAllInFlight = false;
    reconnectAllStep = "";
    syncReconnectAllButton(lastStatus);
  }
}

type NapcatLoginStatus = {
  status: "starting" | "waiting_scan" | "authorized" | "error";
  message: string;
  qrCodeUrl: string;
};

let napcatQrTimer = 0;
let napcatQrPolling = false;
let lastNapcatQrUrl = "";

function stopNapcatQrPoll() {
  if (napcatQrTimer) {
    window.clearInterval(napcatQrTimer);
    napcatQrTimer = 0;
  }
}

function closeNapcatLogin() {
  stopNapcatQrPoll();
  $("napcat-login-modal").classList.add("hidden");
}

async function pollNapcatLoginStatus() {
  if (napcatQrPolling) return;
  napcatQrPolling = true;
  try {
    const status = await invoke<NapcatLoginStatus>("get_napcat_login_status");
    const hint = $("napcat-qr-hint");
    const image = $<HTMLImageElement>("napcat-qr-img");
    const placeholder = $("napcat-qr-placeholder");
    hint.textContent = status.message;
    hint.classList.toggle("success", status.status === "authorized");
    hint.classList.toggle("error", status.status === "error");

    if (status.qrCodeUrl && status.qrCodeUrl !== lastNapcatQrUrl) {
      image.src = await QRCode.toDataURL(status.qrCodeUrl, {
        width: 220,
        margin: 1,
        errorCorrectionLevel: "M",
      });
      image.hidden = false;
      placeholder.classList.add("hidden");
      lastNapcatQrUrl = status.qrCodeUrl;
    } else if (!status.qrCodeUrl && status.status !== "authorized") {
      placeholder.textContent = status.message;
      placeholder.classList.remove("hidden");
      image.hidden = true;
    }

    if (status.status === "authorized") {
      stopNapcatQrPoll();
      placeholder.textContent = "✓ QQ 登录授权成功";
      placeholder.classList.remove("hidden");
      image.hidden = true;
      await refreshStatus();
      window.setTimeout(closeNapcatLogin, 1400);
    }
  } catch (e) {
    $("napcat-qr-hint").textContent = String(e);
    $("napcat-qr-hint").classList.add("error");
  } finally {
    napcatQrPolling = false;
  }
}

function openNapcatLogin() {
  stopNapcatQrPoll();
  lastNapcatQrUrl = "";
  const modal = $("napcat-login-modal");
  const image = $<HTMLImageElement>("napcat-qr-img");
  const placeholder = $("napcat-qr-placeholder");
  modal.classList.remove("hidden");
  image.hidden = true;
  placeholder.textContent = "正在启动 NapCat…";
  placeholder.classList.remove("hidden");
  $("napcat-qr-hint").textContent = "请稍候，正在获取二维码…";
  $("napcat-qr-hint").classList.remove("success", "error");
  pollNapcatLoginStatus().catch((e) => console.error(e));
  napcatQrTimer = window.setInterval(() => {
    pollNapcatLoginStatus().catch((e) => console.error(e));
  }, 1200);
}

async function repairStatus(key: string) {
  const el = document.querySelector(`.pill[data-key="${key}"]`) as HTMLButtonElement | null;
  if (!el) return;
  const passive = currentQqMode() === "passive";
  const label =
    key === "monitor"
      ? "监听服务"
      : key === "onebot"
        ? passive
          ? "官方QQ"
          : "OneBot"
        : passive
          ? "无NapCat"
          : "NapCat";
  if (el.dataset.online === "true") {
    toast(`${label} 当前正常，无需重连`);
    return;
  }
  if (el.dataset.repairing === "true") return;

  el.dataset.repairing = "true";
  setPill(key, false, label);
  try {
    if (passive) {
      if (key === "napcat") {
        toast("被动模式下不应启动 NapCat；请关闭 NapCat 后使用官方 QQ", true);
        el.dataset.repairing = "false";
        await refreshStatus();
        return;
      }
      if (key === "onebot") {
        const message = await invoke<string>("show_qq_window");
        toast(`${message}，正在等待官方 QQ…`);
      } else {
        const message = await invoke<string>("start_monitor");
        toast(`${message}，正在等待监听服务…`);
      }
    } else {
      const command = key === "monitor" ? "start_monitor" : "start_napcat";
      const message = await invoke<string>(command);
      toast(`${message}，正在等待 ${label} 恢复连接…`);
      if (key !== "monitor") openNapcatLogin();
    }
  } catch (e) {
    el.dataset.repairing = "false";
    setPill(key, false, label);
    toast(String(e), true);
    return;
  }

  window.setTimeout(async () => {
    el.dataset.repairing = "false";
    try {
      await refreshStatus();
    } catch (e) {
      console.error(e);
    }
  }, key === "monitor" ? 1800 : 5000);
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
  const tgCard = document.querySelector<HTMLElement>('.channel-card[data-channel="telegram"]');
  if (tgCard) tgCard.hidden = !TELEGRAM_CHANNEL_ENABLED;

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
  setBadge("ch-tg-badge", TELEGRAM_CHANNEL_ENABLED && !!tg?.bound);

  const qqStatus = document.getElementById("ch-qq-status");
  if (qqStatus) {
    qqStatus.textContent = qq?.bound
      ? `已绑定 · OneBot${qq.label ? ` · ${qq.label}` : ""}${qq.lastError ? ` · ${qq.lastError}` : ""}`
      : "未绑定 · 当前仅支持 NapCat / OneBot";
  }
  syncQqModeFields(s);
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
    tgStatus.textContent = TELEGRAM_CHANNEL_ENABLED && tg?.bound
      ? `已绑定${tg.label ? ` · ${tg.label}` : ""} · 用户账号`
      : "暂不支持";
  }
}

async function refreshStatus(): Promise<StatusInfo> {
  const s = await invoke<StatusInfo>("get_status");
  lastStatus = s;
  const passive = (s.qqMode || currentQqMode()) === "passive";
  if (passive) {
    setPill("napcat", !s.napcatProcessRunning, s.napcatProcessRunning ? "NapCat占用" : "无NapCat");
    setPill("onebot", !!s.officialQqRunning, s.officialQqRunning ? "官方QQ" : "官方QQ");
  } else {
    setPill("napcat", s.napcatInstalled && (s.napcatWebuiUp || s.onebotWsUp), "NapCat");
    setPill("onebot", s.onebotWsUp, "OneBot");
  }
  setPill("monitor", s.monitorRunning, "监听服务");
  syncReconnectAllButton(s);
  return s;
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
    const retained = cut > 0 ? newIds.length - cut : 0;
    if (
      cut > 0 &&
      retained > 0 &&
      newIds.slice(cut).join(",") === prevIds.slice(0, retained).join(",")
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

function scheduleLiveRender(rows: MessageRow[]) {
  if (liveRenderFrame) cancelAnimationFrame(liveRenderFrame);
  liveRenderFrame = requestAnimationFrame(() => {
    liveRenderFrame = 0;
    renderMessages("live-messages", rows, { hideGroup: true });
  });
}

function liveActivityValue(group: GroupItem): number {
  return Math.max(
    Number(group.activityCount) || 0,
    Number(group.msgCount) || 0,
  );
}

function updateLiveUnreadGroups(groups: GroupItem[]) {
  for (const group of groups) {
    const current = liveActivityValue(group);
    const previous = liveActivityWatermarks.get(group.groupId);
    if (
      liveActivityInitialized &&
      previous !== undefined &&
      current > previous &&
      group.groupId !== liveSelectedGroupId
    ) {
      liveUnreadGroups.add(group.groupId);
    }
    liveActivityWatermarks.set(group.groupId, current);
  }
  if (liveSelectedGroupId) liveUnreadGroups.delete(liveSelectedGroupId);
  liveActivityInitialized = true;
}

function renderLiveGroupList(groups: GroupItem[]) {
  const box = $("live-group-list");
  const fp = groups
    .map((g) => `${g.groupId}:${g.lastTime || 0}:${g.activityCount || 0}:${g.msgCount || 0}`)
    .join("|");
  const unreadFp = Array.from(liveUnreadGroups).sort().join(",");
  const fullFp = `${liveSelectedGroupId || ""}::${unreadFp}::${fp}`;
  if (box.dataset.fp === fullFp) return;
  box.dataset.fp = fullFp;
  $("live-groups-title").textContent = `监听群 · ${groups.length}`;

  if (!groups.length) {
    box.innerHTML = `<div class="empty">暂无已启用监听的群。请到「群列表」打开群配置，并勾选「启用监听此群」。</div>`;
    return;
  }

  box.innerHTML = groups
    .map((g) => {
      const active = g.groupId === liveSelectedGroupId;
      const unread = liveUnreadGroups.has(g.groupId);
      const name = groupDisplayName(g.groupId, g.groupName);
      const lastTs = normalizeGroupLastTs(g);
      const last = lastTs ? formatTime("", lastTs) : "暂无消息";
      return `<button class="monitored-group-item ${active ? "active" : ""} ${
        unread ? "has-live-unread" : ""
      }" type="button" data-id="${escapeHtml(g.groupId)}">
        ${unread ? `<span class="live-unread-dot" title="有新群消息"></span>` : ""}
        <span class="name">${escapeHtml(name)}</span>
        <span class="meta">${escapeHtml(g.groupId)} · ${escapeHtml(last)} · ${
          Number(g.activityCount) || Number(g.msgCount) || 0
        } 条</span>
      </button>`;
    })
    .join("");

  box.querySelectorAll<HTMLButtonElement>(".monitored-group-item").forEach((btn) => {
    btn.onclick = () => {
      const groupId = btn.dataset.id || "";
      if (!groupId || groupId === liveSelectedGroupId) return;
      liveSelectedGroupId = groupId;
      liveUnreadGroups.delete(groupId);
      liveScrollQuietUntil = 0;
      const messages = $("live-messages");
      messages.dataset.fp = "";
      messages.scrollTop = 0;
      messages.innerHTML = `<div class="empty">正在加载消息…</div>`;
      renderLiveGroupList(groups);
      refreshLive(false, true).catch((e) => toast(String(e), true));
    };
  });
}

async function refreshLive(forceGroups = false, reloadMessages = false) {
  const now = Date.now();
  if (forceGroups || !groupsCache.length || now >= liveGroupsRefreshAt) {
    const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", {
      sort: "recent",
      q: "",
    });
    groupsCache = res.groups || [];
    rememberGroupNames(groupsCache);
    liveGroupsRefreshAt = Date.now() + 30_000;
    liveGroupsRefreshAt = now + 30_000;
  }

  const liveGroups = groupsCache
    .filter((g) => g.enabled)
    .sort((a, b) => normalizeGroupLastTs(b) - normalizeGroupLastTs(a));
  updateLiveUnreadGroups(liveGroups);
  if (!liveGroups.some((g) => g.groupId === liveSelectedGroupId)) {
    liveSelectedGroupId = liveGroups[0]?.groupId || null;
    if (liveSelectedGroupId) liveUnreadGroups.delete(liveSelectedGroupId);
    $("live-messages").dataset.fp = "";
  }
  renderLiveGroupList(liveGroups);

  if (!liveSelectedGroupId) {
    $("live-messages-title").textContent = "实时消息";
    $("live-messages-hint").textContent = "暂无已勾选「启用监听此群」的群";
    renderMessages("live-messages", []);
    return;
  }

  const selected = liveGroups.find((g) => g.groupId === liveSelectedGroupId);
  $("live-messages-title").textContent = groupDisplayName(
    liveSelectedGroupId,
    selected?.groupName,
  );
  $("live-messages-hint").textContent = `群号 ${liveSelectedGroupId} · 仅显示该群消息`;
  const requestedGroupId = liveSelectedGroupId;
  const cached = liveMessageCache.get(requestedGroupId);
  if (cached && !reloadMessages && !forceGroups) {
    scheduleLiveRender(cached);
    return;
  }
  let rows: MessageRow[] = [];
  try {
    rows = await invoke<MessageRow[]>("api_recent_live_messages", {
      groupId: requestedGroupId,
      limit: 80,
    });
  } catch {
    // 兼容未重启的旧桌面端：退回 messages 表
    rows = await invoke<MessageRow[]>("api_recent_messages", {
      groupId: requestedGroupId,
      limit: 80,
    });
  }
  if (requestedGroupId !== liveSelectedGroupId) return;
  for (const m of rows) {
    if (m.groupName) groupNameMap.set(m.groupId, m.groupName);
  }
  liveMessageCache.set(requestedGroupId, rows.slice(0, 80));
  liveMessageCursors.set(
    requestedGroupId,
    rows.reduce((n, m) => Math.max(n, Number(m.liveCursor || 0)), 0),
  );
  if (!rows.length) {
    $("live-messages-hint").textContent = `群号 ${requestedGroupId} · 暂无消息（等待该群新消息）`;
  }
  scheduleLiveRender(rows);
}

async function refreshLiveMessagesIncremental() {
  if (
    !liveSelectedGroupId ||
    !$("view-live").classList.contains("active") ||
    document.hidden ||
    Date.now() < liveScrollQuietUntil
  ) {
    return;
  }
  const groupId = liveSelectedGroupId;
  if (!liveMessageCache.has(groupId)) {
    await refreshLive(false, true);
    return;
  }
  const afterId = liveMessageCursors.get(groupId) || 0;
  const result = await invoke<{ messages: MessageRow[]; cursor: number }>(
    "api_live_messages_since",
    { groupId, afterId, limit: 80 },
  );
  if (groupId !== liveSelectedGroupId) return;
  const incoming = result.messages || [];
  liveMessageCursors.set(groupId, Math.max(afterId, Number(result.cursor || 0)));
  if (!incoming.length) return;

  const previous = liveMessageCache.get(groupId) || [];
  const seen = new Set<number>();
  const merged = [...incoming, ...previous]
    .filter((m) => {
      if (seen.has(m.id)) return false;
      seen.add(m.id);
      return true;
    })
    .slice(0, 80);
  liveMessageCache.set(groupId, merged);
  scheduleLiveRender(merged);

  const newestEvent = Math.max(...incoming.map((m) => Number(m.eventTime || 0)));
  const displayDelayMs = newestEvent > 0 ? Date.now() - newestEvent * 1000 : 0;
  if (displayDelayMs > 2000) {
    console.warn("live message display delay", {
      groupId,
      displayDelayMs,
      count: incoming.length,
    });
  }
  const group = groupsCache.find((g) => g.groupId === groupId);
  if (group && newestEvent > normalizeGroupLastTs(group)) {
    group.lastTime = newestEvent;
    group.activityCount = Number(group.activityCount || 0) + incoming.length;
  }
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
  if (filter === "any") return "有接收活动";
  return `近 ${filter} 天内有消息`;
}

function updateGroupsFilterSummary(opts: {
  filter: GroupsActiveFilter;
  keepEnabled: boolean;
  shown: number;
  total: number;
  hiddenInactive: number;
}) {
  const el = document.getElementById("groups-filter-summary");
  if (!el) return;
  const bits = [
    `<strong>过滤</strong>：${activeFilterLabel(opts.filter)}`,
    opts.keepEnabled ? "始终显示监听中" : "监听中也按活跃过滤",
    `显示 <strong>${opts.shown}</strong> / ${opts.total}`,
  ];
  if (opts.hiddenInactive > 0) {
    bits.push(`已隐藏不活跃/无落库 ${opts.hiddenInactive}`);
  }
  if (opts.filter !== "0") {
    bits.push("活跃度按最近接收事件判断");
  }
  el.innerHTML = bits.join(" · ");
}

async function openGroupKeywordConfig(groupId: string) {
  if (!groupId) return;
  await openGroup(groupId);
  window.requestAnimationFrame(() => {
    const input = $<HTMLInputElement>("g-kw-enabled");
    input.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "center",
    });
    input.focus({ preventScroll: true });
  });
}

async function toggleGroupQuickSetting(
  groupId: string,
  action: "monitor" | "llm",
  sourceButton: HTMLButtonElement,
) {
  if (!groupId || groupQuickSaveInFlight.has(groupId)) return;
  groupQuickSaveInFlight.add(groupId);
  const item = sourceButton.closest<HTMLElement>(".group-item");
  const buttons = Array.from(
    item?.querySelectorAll<HTMLButtonElement>(".group-quick-btn") || [],
  );
  buttons.forEach((button) => {
    button.disabled = true;
  });

  const nextEnabled = sourceButton.getAttribute("aria-pressed") !== "true";
  try {
    const config = await invoke<GroupConfig>("api_get_group", { groupId });
    if (action === "monitor") {
      config.enabled = nextEnabled;
    } else {
      config.llmMonitor.enabled = nextEnabled;
    }
    await invoke("api_save_group", { config });
    toast(
      `${action === "monitor" ? "群监听" : "LLM 分析"}已${nextEnabled ? "开启" : "关闭"}`,
    );
    await refreshGroups();
  } finally {
    groupQuickSaveInFlight.delete(groupId);
    buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}

async function refreshGroups() {
  const box = $("groups-list");
  try {
    const sort = $<HTMLSelectElement>("groups-sort").value;
    const q = $<HTMLInputElement>("groups-q").value.trim();
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

    let hiddenInactive = 0;
    const visible = groupsCache.filter((g) => {
      if (!groupMatchesActiveFilter(g, filter)) {
        if (keepEnabled && g.enabled) return true;
        hiddenInactive += 1;
        return false;
      }
      return true;
    });

    updateGroupsFilterSummary({
      filter,
      keepEnabled,
      shown: visible.length,
      total: groupsCache.length,
      hiddenInactive,
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
          : "暂无接收记录";
        const name = groupDisplayName(g.groupId, g.groupName);
        const ch = guessChannel(g.groupId, g.channel);
        const unread = g.enabled ? groupUnreadCount(g.groupId) : 0;
        return `<article class="group-item ${
          unread > 0 ? "has-unread" : ""
        }" role="button" tabindex="0" data-id="${escapeHtml(g.groupId)}">
        ${unreadBadgeHtml(unread)}
        <div>
          <div class="name">${escapeHtml(name)}</div>
          <div class="meta"><span class="channel-tag">${escapeHtml(channelLabel(ch))}</span> · ${escapeHtml(
            g.groupId,
          )} · 最近接收 ${escapeHtml(last)} · 接收 ${Number(g.activityCount) || 0} / 入库 ${
            Number(g.msgCount) || 0
          } 条</div>
        </div>
        <div class="badges group-quick-actions">
          <button class="badge group-quick-btn ${g.enabled ? "on" : ""}" type="button"
            data-action="monitor" aria-pressed="${g.enabled}" title="点击${g.enabled ? "关闭" : "开启"}监听">
            ${g.enabled ? "监听中" : "未启用"}
          </button>
          <button class="badge group-quick-btn ${g.keywordEnabled ? "on" : ""}" type="button"
            data-action="keyword" title="打开关键词配置">关键词</button>
          <button class="badge group-quick-btn ${g.llmEnabled ? "on" : ""}" type="button"
            data-action="llm" aria-pressed="${g.llmEnabled}" title="点击${g.llmEnabled ? "关闭" : "开启"} LLM">
            LLM
          </button>
        </div>
      </article>`;
      })
      .join("");
    box.querySelectorAll<HTMLElement>(".group-item").forEach((item) => {
      item.onclick = () => openGroup(item.dataset.id || "");
      item.onkeydown = (event) => {
        if ((event.target as HTMLElement | null)?.closest(".group-quick-btn")) return;
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openGroup(item.dataset.id || "").catch((e) => toast(String(e), true));
      };
    });
    box.querySelectorAll<HTMLButtonElement>(".group-quick-btn").forEach((btn) => {
      btn.onclick = (event) => {
        event.stopPropagation();
        const item = btn.closest<HTMLElement>(".group-item");
        const groupId = item?.dataset.id || "";
        const action = btn.dataset.action;
        if (action === "keyword") {
          openGroupKeywordConfig(groupId).catch((e) => toast(String(e), true));
          return;
        }
        if (action === "monitor" || action === "llm") {
          toggleGroupQuickSetting(groupId, action, btn).catch((e) => toast(String(e), true));
        }
      };
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

  const paintPlainText = (text: string): string => {
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

  const paintText = (text: string): string => {
    if (!text) return "";
    const linkRe =
      /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"'\])}，。！？、；：]+)/gi;
    let out = "";
    let lastIndex = 0;
    let link: RegExpExecArray | null;
    while ((link = linkRe.exec(text))) {
      out += paintPlainText(text.slice(lastIndex, link.index));
      const url = link[2] || link[3] || "";
      const label = link[1] || url;
      const safeUrl = escapeHtml(url);
      out += `<a class="report-link" href="${safeUrl}" data-ext-url="${safeUrl}" title="${safeUrl}">${paintPlainText(label)}</a>`;
      lastIndex = link.index + link[0].length;
    }
    out += paintPlainText(text.slice(lastIndex));
    return out;
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
  syncMonitoredReportTabs(reports);
  if (!monitoredSelectedGroupId) {
    box.innerHTML = `<div class="empty">请选择左侧群查看分析主题</div>`;
    return;
  }
  const visible = visibleReports(reports);
  const shown =
    monitoredReportTab === "errors"
      ? reports.filter(isFailedReport)
      : visible.filter((r) => !isFailedReport(r));
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
    if (monitoredReportTab === "errors") {
      box.innerHTML = `<div class="empty">暂无 LLM 错误记录</div>`;
    } else if (reports.some((r) => !isFailedReport(r) && isSkippedReport(r))) {
      box.innerHTML = `<div class="empty">当前仅有定时跳过记录；可关闭「忽略定时跳过」查看</div>`;
    } else {
      box.innerHTML = `<div class="empty">暂无成功的 LLM 分析结果；错误记录请切换到「错误记录」分栏查看</div>`;
    }
    return;
  }
  box.innerHTML = shown
    .map((r) => {
      const risk = (r.riskMax || "none").toLowerCase();
      const skipped = isSkippedReport(r);
      const failed = isFailedReport(r);
      const unread = !skipped && isReportUnread(r.id);
      return `<button class="report-title-item ${risk === "high" ? "high" : ""} ${
        skipped ? "skipped" : ""
      } ${failed ? "failed" : ""} ${unread ? "is-unread" : ""} ${
        r.favorited ? "is-favorited" : ""
      }" type="button" data-id="${r.id}">
        ${reportUnreadDotHtml(unread ? r.id : 0)}
        <div class="report-title-text">${r.favorited ? "★ " : ""}${escapeHtml(r.headline || "(无标题)")}</div>
        <div class="report-title-meta">${escapeHtml(r.createdAt || "")} · 风险 ${escapeHtml(
          r.riskMax || "-",
        )} · ${failed ? "分析失败" : `${r.msgCount ?? "-"} 条`}${escapeHtml(reportTokenMeta(r))}${
          r.favorited ? " · 已收藏" : ""
        }${r.githubIssueUrl ? " · 已上报 Issue" : ""}</div>
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
  const configBtn = document.getElementById(
    "btn-configure-monitored-group",
  ) as HTMLButtonElement | null;
  if (configBtn) configBtn.hidden = !groupId || groupId === FAVORITES_GROUP_ID;
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

async function openMonitoredGroupConfig(groupId: string) {
  if (!groupId || groupId === FAVORITES_GROUP_ID) return;
  switchTab("groups");
  await openGroup(groupId);
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
  syncIssueButton(report);
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

function syncIssueButton(report?: ReportRow) {
  const btn = document.getElementById("btn-report-issue") as HTMLButtonElement | null;
  if (!btn) return;
  const visible = !!report?.failed;
  btn.classList.toggle("hidden", !visible);
  btn.textContent = report?.githubIssueUrl ? "查看 Issue" : "上报 Issue";
  btn.title = report?.githubIssueUrl || "预览脱敏错误与日志后创建 GitHub Issue";
}

function closeIssueReportModal() {
  $("issue-report-modal").classList.add("hidden");
}

async function openIssueReportModal() {
  if (!monitoredDetailReportId) {
    toast("未打开分析失败主题", true);
    return;
  }
  const report = monitoredReportsCache.find((r) => r.id === monitoredDetailReportId);
  if (!report?.failed) {
    toast("仅分析失败主题可以上报", true);
    return;
  }
  if (report.githubIssueUrl) {
    await openUrl(report.githubIssueUrl);
    return;
  }
  try {
    const preview = await invoke<{
      repo: string;
      title: string;
      body: string;
      issueUrl?: string;
    }>("api_github_issue_preview", { reportId: report.id });
    if (preview.issueUrl) {
      report.githubIssueUrl = preview.issueUrl;
      syncIssueButton(report);
      await openUrl(preview.issueUrl);
      return;
    }
    $("issue-report-repo").textContent = `目标仓库：${preview.repo}`;
    $<HTMLInputElement>("issue-report-preview-title").value = preview.title || "";
    $<HTMLTextAreaElement>("issue-report-preview-body").value = preview.body || "";
    $("issue-report-modal").classList.remove("hidden");
  } catch (e) {
    toast(String(e), true);
  }
}

async function confirmIssueReport() {
  if (!monitoredDetailReportId) return;
  const btn = $<HTMLButtonElement>("btn-confirm-issue-report");
  btn.disabled = true;
  btn.textContent = "正在创建…";
  try {
    const result = await invoke<{ ok: boolean; issueUrl: string }>("api_report_github_issue", {
      reportId: monitoredDetailReportId,
    });
    const report = monitoredReportsCache.find((r) => r.id === monitoredDetailReportId);
    if (report) {
      report.githubIssueUrl = result.issueUrl;
      syncIssueButton(report);
    }
    closeIssueReportModal();
    renderMonitoredReportList(monitoredReportsCache);
    toast("GitHub Issue 已创建");
    if (result.issueUrl) await openUrl(result.issueUrl);
  } catch (e) {
    toast(String(e), true);
  } finally {
    btn.disabled = false;
    btn.textContent = "确认创建 Issue";
  }
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
  const enabled = groupsCache.filter((g) => g.enabled);
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

function fillGlobalDefaultConfig() {
  if (!settingsCache) return;
  const provider = activeProvider();
  if (provider) settingsCache.llm.activeProviderId = provider.id;
  fillProvidersSelect(provider?.id || "", "s-global-provider");
  settingsModelOptions = provider?.defaultModel ? [{ id: provider.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, provider?.defaultModel || "");
  fillModelSelect(
    "s-default-image-model",
    settingsModelOptions,
    settingsCache.llm.defaultImageModel || "",
    "未选择时复用默认文本模型",
    true,
  );
  $<HTMLTextAreaElement>("s-global-prompt").value = settingsCache.llm.defaultPrompt || "";
  $<HTMLInputElement>("s-global-every").value = String(
    settingsCache.llm.defaultEveryMinutes ?? 60,
  );
  $<HTMLInputElement>("s-global-window").value = String(
    settingsCache.llm.defaultWindowMinutes ?? 60,
  );
  $<HTMLInputElement>("s-global-min").value = String(
    settingsCache.llm.defaultMinMessages ?? 8,
  );
}

function syncLlmAnalysisPanels() {
  const useGlobalEl = document.getElementById("g-llm-use-global") as HTMLInputElement | null;
  const customConfig = document.getElementById("g-llm-custom-config");
  const textEl = document.getElementById("g-llm-text-enabled") as HTMLInputElement | null;
  const imageEl = document.getElementById("g-llm-image-enabled") as HTMLInputElement | null;
  const sameEl = document.getElementById("g-llm-image-same") as HTMLInputElement | null;
  const textCfg = document.getElementById("g-llm-text-config");
  const imageWrap = document.getElementById("g-llm-image-wrap");
  const imageCfg = document.getElementById("g-llm-image-config");
  if (
    !useGlobalEl ||
    !customConfig ||
    !textEl ||
    !imageEl ||
    !sameEl ||
    !textCfg ||
    !imageWrap ||
    !imageCfg
  )
    return;
  customConfig.classList.toggle("hidden", useGlobalEl.checked);
  const globalHint = document.getElementById("g-llm-global-hint");
  if (globalHint) {
    globalHint.textContent =
      useGlobalEl.checked && settingsCache
        ? `当前全局参数：每 ${settingsCache.llm.defaultEveryMinutes ?? 60} 分钟执行，分析最近 ${
            settingsCache.llm.defaultWindowMinutes ?? 60
          } 分钟，至少 ${settingsCache.llm.defaultMinMessages ?? 8} 条消息。`
        : "当前使用本群自己的调度参数、代理、模型和提示词。";
  }
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
    item.addEventListener("click", () => {
      void switchActiveProvider(id);
    });
    item.querySelectorAll<HTMLElement>("[data-act]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        const act = el.dataset.act;
        // 主区域 data-act=select：不拦截，交给条目 click 切换活跃代理
        if (act === "select") return;
        ev.stopPropagation();
        if (act === "edit") openEditProvider(id);
        if (act === "del") void deleteProvider(id);
      });
    });
  });
}

async function switchActiveProvider(id: string) {
  if (!settingsCache) return;
  const p = settingsCache.llm.providers.find((x) => x.id === id);
  if (!p) return;
  if (settingsCache.llm.activeProviderId === id) return;
  settingsCache.llm.activeProviderId = id;
  fillGlobalDefaultConfig();
  renderProviderList();
  try {
    await persistSettingsFromForm();
  } catch (e) {
    toast(String(e), true);
  }
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

async function saveProviderForm() {
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
    // 新建后立即设为活跃，便于直接测通/拉模型
    settingsCache.llm.activeProviderId = id;
  }
  const active = activeProvider();
  if (active) {
    fillGlobalDefaultConfig();
  }
  renderProviderList();
  closeProviderForm();
  try {
    await persistSettingsFromForm();
    toast("代理已保存");
  } catch (e) {
    toast(String(e), true);
  }
}

async function deleteProvider(id: string) {
  if (!settingsCache) return;
  settingsCache.llm.providers = settingsCache.llm.providers.filter((p) => p.id !== id);
  if (settingsCache.llm.activeProviderId === id) {
    settingsCache.llm.activeProviderId = settingsCache.llm.providers[0]?.id || "";
  }
  renderProviderList();
  const active = activeProvider();
  if (active) fillGlobalDefaultConfig();
  try {
    await persistSettingsFromForm();
    toast("代理已删除");
  } catch (e) {
    toast(String(e), true);
  }
}

function fillModelSelect(
  selectId: string,
  models: { id: string }[],
  current?: string,
  emptyHint = "先点右侧刷新获取模型",
  allowEmpty = false,
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
  sel.innerHTML =
    (allowEmpty ? `<option value="">${escapeHtml(emptyHint)}</option>` : "") +
    ids.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
  sel.value = cur && ids.includes(cur) ? cur : allowEmpty ? "" : ids[0];
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
  const providerId = $<HTMLSelectElement>("s-global-provider").value;
  const active = settingsCache?.llm.providers.find((p) => p.id === providerId);
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
    const imageKeep = $<HTMLSelectElement>("s-default-image-model").value;
    fillModelSelect(
      "s-default-image-model",
      settingsModelOptions,
      imageKeep,
      "未选择时复用默认文本模型",
      true,
    );
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

function setSettingsAutoSaveStatus(
  state: "idle" | "saving" | "saved" | "error",
  text: string,
) {
  const el = document.getElementById("settings-auto-save-status");
  if (!el) return;
  el.classList.remove("saving", "saved", "error");
  if (state !== "idle") el.classList.add(state);
  el.textContent = text;
}

function persistSettingsFromForm(): Promise<AppSettings> {
  const generation = ++settingsSaveGeneration;
  setSettingsAutoSaveStatus("saving", "正在自动保存…");
  const task = settingsSaveChain
    .catch(() => undefined)
    .then(() => persistSettingsSnapshot());
  settingsSaveChain = task;
  task.then(
    () => {
      if (generation === settingsSaveGeneration) {
        setSettingsAutoSaveStatus("saved", "已自动保存");
      }
    },
    () => {
      if (generation === settingsSaveGeneration) {
        setSettingsAutoSaveStatus("error", "自动保存失败");
      }
    },
  );
  return task;
}

function scheduleSettingsAutoSave(delayMs = 700) {
  if (!settingsAutoSaveReady || !settingsCache) return;
  window.clearTimeout(settingsAutoSaveTimer);
  setSettingsAutoSaveStatus("saving", "等待自动保存…");
  settingsAutoSaveTimer = window.setTimeout(() => {
    settingsAutoSaveTimer = 0;
    persistSettingsFromForm().catch((e) => toast(`自动保存失败：${e}`, true));
  }, delayMs);
}

async function flushSettingsAutoSave() {
  if (settingsAutoSaveTimer) {
    window.clearTimeout(settingsAutoSaveTimer);
    settingsAutoSaveTimer = 0;
    await persistSettingsFromForm();
    return;
  }
  await settingsSaveChain.catch(() => undefined);
}

async function persistSettingsSnapshot(): Promise<AppSettings> {
  if (!settingsCache) settingsCache = await invoke<AppSettings>("api_get_settings");
  const globalProviderId =
    $<HTMLSelectElement>("s-global-provider").value || settingsCache.llm.activeProviderId;
  settingsCache.llm.activeProviderId = globalProviderId;
  const active = activeProvider();
  const model = $<HTMLSelectElement>("s-default-model").value.trim();
  if (active) {
    settingsCache.llm.providers = settingsCache.llm.providers.map((p) =>
      p.id === active.id ? { ...p, defaultModel: model } : p,
    );
  }
  const prev = settingsCache.channels || {};
  const qqMode = "onebot";
  const prevMode = "onebot";
  const next: AppSettings = {
    onebotWsUrl: $<HTMLInputElement>("s-ws").value.trim(),
    onebotAccessToken: $<HTMLInputElement>("s-token").value.trim(),
    channels: {
      qq: {
        bound: !!prev.qq?.bound,
        label: prev.qq?.label || "",
        lastError: prev.qq?.lastError || "",
        mode: qqMode,
        notificationAccess: prev.qq?.notificationAccess || "",
        uiaReady: !!prev.qq?.uiaReady,
        pollSeconds: Number($<HTMLInputElement>("s-qq-poll")?.value || prev.qq?.pollSeconds || 1.5),
        groupNameMap: prev.qq?.groupNameMap || {},
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
        bound: TELEGRAM_CHANNEL_ENABLED ? !!prev.telegram?.bound : false,
        label: TELEGRAM_CHANNEL_ENABLED ? prev.telegram?.label || "" : "",
        lastError: TELEGRAM_CHANNEL_ENABLED
          ? prev.telegram?.lastError || ""
          : "Telegram 通道暂不支持",
        apiId: Number($<HTMLInputElement>("s-tg-api-id").value.trim() || 0),
        apiHash: $<HTMLInputElement>("s-tg-api-hash").value.trim(),
        botToken: "",
        pollTimeout: prev.telegram?.pollTimeout ?? 25,
      },
    },
    llm: {
      activeProviderId: globalProviderId,
      providers: settingsCache.llm.providers,
      defaultImageModel: $<HTMLSelectElement>("s-default-image-model").value.trim(),
      defaultPrompt: $<HTMLTextAreaElement>("s-global-prompt").value,
      defaultEveryMinutes: Math.max(
        1,
        Number($<HTMLInputElement>("s-global-every").value || 60),
      ),
      defaultWindowMinutes: Math.max(
        1,
        Number($<HTMLInputElement>("s-global-window").value || 60),
      ),
      defaultMinMessages: Math.max(
        1,
        Number($<HTMLInputElement>("s-global-min").value || 8),
      ),
      reportKeepLimit: clampReportKeepLimit(
        Number($<HTMLInputElement>("s-llm-report-keep")?.value || settingsCache.llm.reportKeepLimit || 100),
      ),
    },
    ui: {
      theme: selectedTheme,
    },
  };
  await invoke("api_save_settings", { settings: next });
  settingsCache = next;
  applyTheme(selectedTheme);
  renderChannelBindings(next);
  if (prevMode !== qqMode && next.channels?.qq?.bound) {
    try {
      await invoke("stop_monitor");
      await invoke("start_monitor");
      toast(`已切换到 ${qqMode === "passive" ? "官方 QQ 被动" : "OneBot"} 模式并重启监听`);
      await refreshStatus();
    } catch (e) {
      toast(`模式已保存，但重启监听失败：${e}`, true);
    }
  }
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

  $<HTMLInputElement>("g-enabled").checked = !!cfg.enabled;
  $<HTMLInputElement>("g-log-all").checked = !!cfg.basic?.logAll;
  $<HTMLInputElement>("g-storage").checked = !!cfg.basic?.storageEnabled;
  $<HTMLInputElement>("g-name").value = cfg.groupName || "";
  $<HTMLInputElement>("g-kw-enabled").checked = !!cfg.keywordMonitor?.enabled;
  $<HTMLInputElement>("g-keywords").value = (cfg.keywordMonitor?.keywords || []).join(",");
  $<HTMLInputElement>("g-kw-alert").checked = !!cfg.keywordMonitor?.alertEnabled;
  $<HTMLInputElement>("g-kw-webhook").value = cfg.keywordMonitor?.webhookUrl || "";
  $<HTMLInputElement>("g-llm-enabled").checked = !!cfg.llmMonitor?.enabled;
  $<HTMLInputElement>("g-llm-use-global").checked =
    cfg.llmMonitor?.useGlobalDefaults !== false;
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
  const shown = visibleReports(reports || []).filter((r) => !isFailedReport(r));
  if (!reports.length) {
    box.innerHTML = `<div class="empty">暂无 LLM 报告，可点「立即执行」</div>`;
  } else if (!shown.length) {
    const hasErrors = reports.some(isFailedReport);
    box.innerHTML = `<div class="empty">${
      hasErrors
        ? "暂无成功的 LLM 分析结果；错误记录请到「监听中 → 错误记录」查看"
        : "当前仅有定时跳过记录；可在「监听中」关闭「忽略定时跳过」查看"
    }</div>`;
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

function readGroupForm(): GroupConfig {
  const groupId = currentGroupId || "";
  const cached = groupsCache.find((g) => g.groupId === groupId);
  return {
    groupId,
    groupName: $<HTMLInputElement>("g-name").value.trim(),
    channel: guessChannel(groupId, cached?.channel),
    enabled: $<HTMLInputElement>("g-enabled").checked,
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
      useGlobalDefaults: $<HTMLInputElement>("g-llm-use-global").checked,
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
  const qqModeEl = document.getElementById("s-qq-mode") as HTMLSelectElement | null;
  if (qqModeEl) qqModeEl.value = settingsCache.channels?.qq?.mode === "passive" ? "passive" : "onebot";
  const pollEl = document.getElementById("s-qq-poll") as HTMLInputElement | null;
  if (pollEl) pollEl.value = String(settingsCache.channels?.qq?.pollSeconds || 1.5);
  $<HTMLInputElement>("s-wx-dir").value = settingsCache.channels?.wechat?.dataDir || "";
  $<HTMLInputElement>("s-wx-decrypted").value =
    settingsCache.channels?.wechat?.decryptedDir || "";
  // 导入框留给用户填「已有密钥文件」；不预填后端默认输出路径（文件往往还不存在）
  $<HTMLInputElement>("s-wx-keys").value = "";
  $<HTMLInputElement>("s-tg-api-id").value = String(settingsCache.channels?.telegram?.apiId || "");
  $<HTMLInputElement>("s-tg-api-hash").value = settingsCache.channels?.telegram?.apiHash || "";
  applyTheme(settingsCache.ui?.theme || "midnight");
  renderChannelBindings(settingsCache);
  syncReportKeepSlider(settingsCache.llm?.reportKeepLimit ?? 100);
  fillGlobalDefaultConfig();
  renderProviderList();
  switchSettingsTab("channels");
}

function startIndependentRefreshLoops() {
  const repeat = (work: () => Promise<void>, delayMs: number) => {
    const run = async () => {
      try {
        await work();
      } catch (e) {
        console.error(e);
      } finally {
        window.setTimeout(run, document.hidden ? Math.max(delayMs, 5000) : delayMs);
      }
    };
    void run();
  };

  repeat(refreshLiveMessagesIncremental, 1000);
  repeat(async () => {
    if (!document.hidden) await refreshStatus();
  }, 15_000);
  repeat(async () => {
    if (!document.hidden) await pollLlmTipsAndUnread();
  }, 15_000);
  repeat(async () => {
    if (document.hidden) return;
    if ($("view-live").classList.contains("active")) {
      await refreshLive();
    } else if (
      $("view-groups").classList.contains("active") &&
      !$("groups-master").classList.contains("hidden")
    ) {
      await refreshGroups();
    }
  }, 15_000);
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
  setupUiSounds();

  const hideSkippedEl = document.getElementById(
    "monitored-hide-skipped",
  ) as HTMLInputElement | null;
  if (hideSkippedEl) {
    hideSkippedEl.onchange = () => {
      saveHideSkippedToggle(!!hideSkippedEl.checked);
      renderMonitoredReportList(monitoredReportsCache);
    };
  }
  $("btn-configure-monitored-group").onclick = () => {
    openMonitoredGroupConfig(monitoredSelectedGroupId || "").catch((e) =>
      toast(`打开群配置失败：${e}`, true),
    );
  };
  $("monitored-tab-success").onclick = () => {
    monitoredReportTab = "success";
    renderMonitoredReportList(monitoredReportsCache);
  };
  $("monitored-tab-errors").onclick = () => {
    monitoredReportTab = "errors";
    renderMonitoredReportList(monitoredReportsCache);
  };

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
      if (name === "live") refreshLive(true).catch((e) => toast(String(e), true));
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
      await getCurrentWindow().minimize();
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
      await flushSettingsAutoSave();
      await getCurrentWindow().close();
    } catch (e) {
      toast(String(e), true);
    }
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

  document.querySelectorAll<HTMLButtonElement>(".pill[data-key]").forEach((pill) => {
    pill.onclick = () => repairStatus(pill.dataset.key || "").catch((e) => toast(String(e), true));
  });
  $("btn-reconnect-all").onclick = () => {
    reconnectAll().catch((e) => toast(String(e), true));
  };
  $("btn-close-napcat-login").onclick = closeNapcatLogin;
  $("btn-done-napcat-login").onclick = closeNapcatLogin;
  document.querySelectorAll("[data-napcat-login-close]").forEach((el) => {
    (el as HTMLElement).onclick = closeNapcatLogin;
  });
  $("btn-refresh-napcat-qr").onclick = async () => {
    try {
      toast(await invoke<string>("refresh_napcat_login_qr"));
      lastNapcatQrUrl = "";
      await pollNapcatLoginStatus();
    } catch (e) {
      toast(String(e), true);
    }
  };

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
      openNapcatLogin();
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
  $("btn-report-issue").onclick = () => {
    openIssueReportModal().catch((e) => toast(String(e), true));
  };
  $("btn-confirm-issue-report").onclick = () => {
    confirmIssueReport().catch((e) => toast(String(e), true));
  };
  $("btn-close-issue-report").onclick = closeIssueReportModal;
  $("btn-cancel-issue-report").onclick = closeIssueReportModal;
  document.querySelectorAll("[data-issue-report-close]").forEach((el) => {
    (el as HTMLElement).onclick = closeIssueReportModal;
  });
  $("btn-goto-groups").onclick = () => {
    switchTab("groups");
    refreshGroups().catch((e) => toast(String(e), true));
  };
  $("groups-sort").onchange = () => refreshGroups().catch((e) => toast(String(e), true));
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
    const button = $<HTMLButtonElement>("btn-pull-channel-groups");
    button.disabled = true;
    toast("正在拉取 QQ 群列表，并同步每群最近 10 条消息…");
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
      if (TELEGRAM_CHANNEL_ENABLED && settingsCache?.channels?.telegram?.bound) {
        const res = await invoke<{ ok?: boolean; message?: string }>("api_pull_telegram_groups");
        parts.push(res.message || (res.ok ? "TG 群已拉取" : "TG 拉取失败"));
      }
      toast(parts.filter(Boolean).join("；") || "请先在总配置绑定通道");
      await refreshGroups();
    } catch (e) {
      toast(String(e), true);
    } finally {
      button.disabled = false;
    }
  };
  $("btn-bind-qq").onclick = async () => {
    try {
      await persistSettingsFromForm();
      const mode = currentQqMode();
      const res = await invoke<{
        ok?: boolean;
        message?: string;
        channels?: AppSettings["channels"];
        detect?: Record<string, unknown>;
      }>("api_bind_qq", {
        payload: {
          mode,
          onebotWsUrl: $<HTMLInputElement>("s-ws").value.trim(),
          onebotAccessToken: $<HTMLInputElement>("s-token").value.trim(),
          pollSeconds: Number($<HTMLInputElement>("s-qq-poll")?.value || 1.5),
        },
      });
      if (res.channels && settingsCache) settingsCache.channels = res.channels;
      renderChannelBindings(settingsCache);
      showTestResult("onebot-test-result", !!res.ok, res.message || "");
      toast(res.message || "QQ 已绑定", !res.ok);
      try {
        await invoke("stop_monitor");
        await invoke("start_monitor");
      } catch {
        /* 未运行时可忽略 stop */
        try {
          await invoke("start_monitor");
        } catch (e) {
          toast(`绑定成功，但启动监听失败：${e}`, true);
        }
      }
      await refreshStatus();
      await refreshGroups();
    } catch (e) {
      toast(String(e), true);
    }
  };
  const qqModeSelect = document.getElementById("s-qq-mode") as HTMLSelectElement | null;
  if (qqModeSelect) {
    qqModeSelect.onchange = () => {
      syncQqModeFields({
        ...(settingsCache as AppSettings),
        channels: {
          ...(settingsCache?.channels || {}),
          qq: {
            ...(settingsCache?.channels?.qq || { bound: false }),
            mode: qqModeSelect.value === "passive" ? "passive" : "onebot",
          },
        },
      });
    };
  }
  const btnDetectPassive = document.getElementById("btn-detect-qq-passive");
  if (btnDetectPassive) {
    btnDetectPassive.onclick = async () => {
      try {
        showTestResult("onebot-test-result", true, "正在探测官方 QQ / 通知 / UIA…");
        const res = await invoke<{
          ok?: boolean;
          message?: string;
          channels?: AppSettings["channels"];
          detect?: {
            officialQqRunning?: boolean;
            napcatRunning?: boolean;
            notificationAccess?: string;
            uiaOk?: boolean;
            uiaGroupName?: string;
            limitations?: string[];
          };
        }>("api_detect_qq_passive");
        if (res.channels && settingsCache) settingsCache.channels = res.channels;
        renderChannelBindings(settingsCache);
        const d = res.detect || {};
        const detail = [
          res.message || "探测完成",
          `官方QQ=${d.officialQqRunning ? "是" : "否"}`,
          `NapCat=${d.napcatRunning ? "仍在运行" : "未运行"}`,
          `通知=${d.notificationAccess || "unknown"}`,
          `UIA=${d.uiaOk ? "可用" : "不可用"}`,
          d.uiaGroupName ? `当前会话=${d.uiaGroupName}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        showTestResult("onebot-test-result", !!res.ok && !d.napcatRunning, detail);
        toast(detail, !res.ok || !!d.napcatRunning);
        await refreshStatus();
      } catch (e) {
        showTestResult("onebot-test-result", false, String(e));
        toast(String(e), true);
      }
    };
  }
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
      if (form.enabled && !wasEnabled) {
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
        error?: string;
      }>("api_run_llm", {
        groupId: currentGroupId,
      });
      if (result.status === "skipped") {
        toast(result.reason || "已跳过（消息不足）", true);
      } else if (result.status === "failed") {
        toast(`LLM 分析失败，已归入「错误记录」分栏：${result.error || "请查看详情"}`, true);
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
  $("btn-save-provider").onclick = () => {
    void saveProviderForm();
  };
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
  bindToggle("g-llm-use-global");
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
  $("s-global-provider").onchange = () => {
    if (!settingsCache) return;
    settingsCache.llm.defaultPrompt = $<HTMLTextAreaElement>("s-global-prompt").value;
    settingsCache.llm.defaultEveryMinutes = Math.max(
      1,
      Number($<HTMLInputElement>("s-global-every").value || 60),
    );
    settingsCache.llm.defaultWindowMinutes = Math.max(
      1,
      Number($<HTMLInputElement>("s-global-window").value || 60),
    );
    settingsCache.llm.defaultMinMessages = Math.max(
      1,
      Number($<HTMLInputElement>("s-global-min").value || 8),
    );
    settingsCache.llm.defaultImageModel = "";
    settingsCache.llm.activeProviderId = $<HTMLSelectElement>("s-global-provider").value;
    fillGlobalDefaultConfig();
    renderProviderList();
  };

  const settingsView = document.getElementById("view-settings");
  const onSettingsFormChanged = (event: Event) => {
    const target = event.target as HTMLElement | null;
    if (!target?.matches("input, select, textarea")) return;
    if (target.closest("#provider-form")) return;
    if (target.id === "s-wx-keys" || target.id === "s-tg-2fa") return;
    scheduleSettingsAutoSave(event.type === "change" ? 250 : 700);
  };
  settingsView?.addEventListener("input", onSettingsFormChanged);
  settingsView?.addEventListener("change", onSettingsFormChanged);

  const reportKeepEl = document.getElementById("s-llm-report-keep") as HTMLInputElement | null;
  if (reportKeepEl) {
    reportKeepEl.oninput = () => syncReportKeepSlider(Number(reportKeepEl.value));
    reportKeepEl.onchange = () => syncReportKeepSlider(Number(reportKeepEl.value));
  }

  try {
    settingsCache = await invoke<AppSettings>("api_get_settings");
    applyTheme(settingsCache.ui?.theme || "midnight");
    syncReportKeepSlider(settingsCache.llm?.reportKeepLimit ?? 100);
  } catch {
    applyTheme("midnight");
    syncReportKeepSlider(100);
  }
  settingsAutoSaveReady = true;
  setSettingsAutoSaveStatus("idle", "修改后自动保存");

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

  await refreshStatus().catch(() => undefined);
  await refreshLive(false, true).catch(() => undefined);
  await refreshUnreadBadge().catch(() => undefined);
  startIndependentRefreshLoops();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    refreshStatus().catch(() => undefined);
    refreshLiveMessagesIncremental().catch(() => undefined);
  });

  try {
    await getCurrentWindow().show();
    await getCurrentWindow().setFocus();
  } catch {
    /* browser preview */
  }
});
