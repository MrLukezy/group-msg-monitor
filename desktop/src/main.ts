import { invoke } from "@tauri-apps/api/core";

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
  lastTime?: number | null;
  msgCount: number;
  keywordEnabled: boolean;
  llmEnabled: boolean;
};

type MessageRow = {
  id: number;
  groupId: string;
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
};

type GroupConfig = {
  groupId: string;
  groupName: string;
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

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

let currentGroupId: string | null = null;
let settingsCache: AppSettings | null = null;
let editingProviderId: string | null = null;
let settingsModelOptions: { id: string }[] = [];
let groupModelOptions: { id: string }[] = [];

function toast(msg: string, err = false) {
  const el = $("toast");
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("err", err);
  window.setTimeout(() => {
    el.hidden = true;
  }, 4200);
}

function escapeHtml(s: string) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setPill(key: string, on: boolean, label: string) {
  const el = document.querySelector(`.pill[data-key="${key}"]`) as HTMLElement;
  if (!el) return;
  el.classList.toggle("on", on);
  el.classList.toggle("off", !on);
  el.textContent = `${label}${on ? " · 在线" : " · 离线"}`;
}

function switchTab(name: string) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", (t as HTMLElement).dataset.tab === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${name}`);
  });
  if (name === "groups") {
    $("groups-master").classList.remove("hidden");
    $("group-detail").classList.add("hidden");
    currentGroupId = null;
  }
}

function switchSettingsTab(name: string) {
  document.querySelectorAll(".settings-tab").forEach((t) => {
    t.classList.toggle("active", (t as HTMLElement).dataset.stab === name);
  });
  $("stab-onebot").classList.toggle("hidden", name !== "onebot");
  $("stab-llm").classList.toggle("hidden", name !== "llm");
}

async function refreshStatus() {
  const s = await invoke<StatusInfo>("get_status");
  setPill("napcat", s.napcatInstalled && (s.napcatWebuiUp || s.onebotWsUp), "NapCat");
  setPill("onebot", s.onebotWsUp, "OneBot");
  setPill("monitor", s.monitorRunning, "监控");
}

function renderMessages(boxId: string, rows: MessageRow[]) {
  const box = $(boxId);
  if (!rows.length) {
    box.innerHTML = `<div class="empty">暂无消息</div>`;
    return;
  }
  box.innerHTML = rows
    .map((m) => {
      const when = m.createdAt || (m.eventTime ? String(m.eventTime) : "-");
      return `<article class="msg">
        <div class="who">#${m.id} · 群 ${escapeHtml(m.groupId)} · ${escapeHtml(
          m.senderName || m.userId,
        )} · ${escapeHtml(when)}</div>
        <div class="body">${escapeHtml(m.content)}</div>
      </article>`;
    })
    .join("");
}

async function refreshLive() {
  const rows = await invoke<MessageRow[]>("api_recent_messages", {
    groupId: null,
    limit: 80,
  });
  renderMessages("live-messages", rows);
}

async function refreshGroups() {
  const sort = $<HTMLSelectElement>("groups-sort").value;
  const q = $<HTMLInputElement>("groups-q").value.trim();
  const res = await invoke<{ groups: GroupItem[] }>("api_list_groups", { sort, q });
  const box = $("groups-list");
  if (!res.groups.length) {
    box.innerHTML = `<div class="empty">暂无群。可先启动监控收消息，或点「从 OneBot 拉取」。</div>`;
    return;
  }
  box.innerHTML = res.groups
    .map((g) => {
      const last = g.lastTime
        ? new Date(g.lastTime * 1000).toLocaleString()
        : "暂无消息";
      return `<button class="group-item" type="button" data-id="${escapeHtml(g.groupId)}">
        <div>
          <div class="name">${escapeHtml(g.groupName || "(未命名群)")}</div>
          <div class="meta">${escapeHtml(g.groupId)} · 最近 ${escapeHtml(last)} · ${g.msgCount} 条</div>
        </div>
        <div class="badges">
          <span class="badge ${g.enabled ? "on" : ""}">${g.enabled ? "监控中" : "未启用"}</span>
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
  };
  await invoke("api_save_settings", { settings: next });
  settingsCache = next;
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
  $("detail-title").textContent = cfg.groupName || groupId;
  $("detail-sub").textContent = `群号 ${groupId}`;

  $<HTMLInputElement>("g-enabled").checked = !!cfg.enabled;
  $<HTMLInputElement>("g-log-all").checked = !!cfg.basic?.logAll;
  $<HTMLInputElement>("g-storage").checked = !!cfg.basic?.storageEnabled;
  $<HTMLInputElement>("g-name").value = cfg.groupName || "";
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
  renderMessages("detail-messages", msgs);
  const reports = await invoke<ReportRow[]>("api_list_reports", { groupId, limit: 10 });
  const box = $("detail-reports");
  if (!reports.length) {
    box.innerHTML = `<div class="empty">暂无 LLM 报告，可点「立即执行」</div>`;
  } else {
    box.innerHTML = reports
      .map((r) => {
        const risk = (r.riskMax || "none").toLowerCase();
        const skipped = (r.headline || "").includes("[定时跳过]");
        return `<article class="report ${risk === "high" ? "high" : ""} ${skipped ? "skipped" : ""}">
          <div class="title">${escapeHtml(r.headline || "(无标题)")}</div>
          <div class="meta">${escapeHtml(r.createdAt)} · 风险 ${escapeHtml(
            r.riskMax || "-",
          )} · ${r.msgCount ?? "-"} 条</div>
          <pre style="white-space:pre-wrap;font-size:12px;margin:8px 0 0">${escapeHtml(
            r.reportMd || "",
          )}</pre>
        </article>`;
      })
      .join("");
  }
}

function readGroupForm(): GroupConfig {
  const groupId = currentGroupId || "";
  return {
    groupId,
    groupName: $<HTMLInputElement>("g-name").value.trim(),
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
  const active = activeProvider();
  settingsModelOptions = active?.defaultModel ? [{ id: active.defaultModel }] : [];
  fillModelSelect("s-default-model", settingsModelOptions, active?.defaultModel || "");
  renderProviderList();
  switchSettingsTab("llm");
}

async function tick() {
  try {
    await refreshStatus();
    if ($("view-live").classList.contains("active")) {
      await refreshLive();
    }
  } catch (e) {
    console.error(e);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".tab").forEach((btn) => {
    (btn as HTMLButtonElement).onclick = () => {
      const name = (btn as HTMLElement).dataset.tab || "live";
      switchTab(name);
      if (name === "groups") refreshGroups().catch((e) => toast(String(e), true));
      if (name === "settings") loadSettingsView().catch((e) => toast(String(e), true));
    };
  });

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
  $("groups-sort").onchange = () => refreshGroups().catch((e) => toast(String(e), true));
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
    currentGroupId = null;
    refreshGroups().catch(() => undefined);
  };
  $("btn-save-group").onclick = async () => {
    try {
      await invoke("api_save_group", { config: readGroupForm() });
      toast("本群配置已保存");
    } catch (e) {
      toast(String(e), true);
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
  } catch {
    /* ignore */
  }
  await tick();
  window.setInterval(tick, 2500);
});
