import { invoke } from "@tauri-apps/api/core";

type StatusInfo = {
  projectRoot: string;
  napcatInstalled: boolean;
  napcatWebuiUp: boolean;
  onebotWsUp: boolean;
  monitorRunning: boolean;
  pythonReady: boolean;
  qrcodeExists: boolean;
  webuiUrl: string;
};

type AppConfig = {
  onebotWsUrl: string;
  onebotAccessToken: string;
  monitorGroupIds: string;
  monitorKeywords: string;
  monitorLogAll: boolean;
  storageEnabled: boolean;
  alertEnabled: boolean;
  alertWebhookUrl: string;
};

type QrInfo = {
  available: boolean;
  imageBase64?: string | null;
  path: string;
  updatedAt?: string | null;
  decodeUrl?: string | null;
  hint: string;
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

type GroupInfo = {
  groupId: string;
  groupName: string;
  memberCount?: number | null;
  maxMemberCount?: number | null;
};

type GroupListResult = {
  loginUserId?: string | null;
  loginNickname?: string | null;
  groups: GroupInfo[];
};

let cachedGroups: GroupInfo[] = [];
let selectedGroupIds = new Set<string>();

const $ = <T extends HTMLElement>(id: string) =>
  document.getElementById(id) as T;

function toast(msg: string, err = false) {
  const el = $("action-toast");
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("err", err);
  window.setTimeout(() => {
    el.hidden = true;
  }, 4200);
}

function setPill(key: string, on: boolean, label: string) {
  const el = document.querySelector(`.pill[data-key="${key}"]`) as HTMLElement;
  if (!el) return;
  el.classList.toggle("on", on);
  el.classList.toggle("off", !on);
  el.textContent = `${label}${on ? " · 在线" : " · 离线"}`;
}

async function refreshStatus() {
  const s = await invoke<StatusInfo>("get_status");
  setPill("napcat", s.napcatInstalled && (s.napcatWebuiUp || s.onebotWsUp), "NapCat");
  setPill("webui", s.napcatWebuiUp, "WebUI");
  setPill("onebot", s.onebotWsUp, "OneBot WS");
  setPill("monitor", s.monitorRunning, "监控服务");
  return s;
}

async function refreshQr() {
  const qr = await invoke<QrInfo>("get_qrcode");
  const img = $<HTMLImageElement>("qr-image");
  const empty = $("qr-empty");
  const hint = $("qr-hint");
  const meta = $("qr-meta");
  const urlBox = $("qr-url-box");
  const url = $<HTMLTextAreaElement>("qr-url");

  hint.textContent = qr.hint;
  meta.textContent = [
    qr.updatedAt ? `更新于 ${qr.updatedAt}` : null,
    `路径: ${qr.path}`,
  ]
    .filter(Boolean)
    .join(" · ");

  if (qr.available && qr.imageBase64) {
    img.src = qr.imageBase64;
    img.hidden = false;
    empty.hidden = true;
  } else {
    img.hidden = true;
    empty.hidden = false;
    empty.textContent = "等待二维码…";
  }

  if (qr.decodeUrl) {
    urlBox.hidden = false;
    url.value = qr.decodeUrl;
  } else {
    urlBox.hidden = true;
  }
}

async function refreshConfig() {
  const cfg = await invoke<AppConfig>("get_config");
  $<HTMLInputElement>("cfg-groups").value = cfg.monitorGroupIds;
  $<HTMLInputElement>("cfg-token").value = cfg.onebotAccessToken;
  $<HTMLInputElement>("cfg-ws").value = cfg.onebotWsUrl;
  $<HTMLInputElement>("cfg-keywords").value = cfg.monitorKeywords;
  $<HTMLInputElement>("cfg-webhook").value = cfg.alertWebhookUrl;
  $<HTMLInputElement>("cfg-log-all").checked = cfg.monitorLogAll;
  $<HTMLInputElement>("cfg-storage").checked = cfg.storageEnabled;
  $<HTMLInputElement>("cfg-alert").checked = cfg.alertEnabled;
}

function readConfigForm(): AppConfig {
  return {
    onebotWsUrl: $<HTMLInputElement>("cfg-ws").value.trim(),
    onebotAccessToken: $<HTMLInputElement>("cfg-token").value.trim(),
    monitorGroupIds: $<HTMLInputElement>("cfg-groups").value.trim(),
    monitorKeywords: $<HTMLInputElement>("cfg-keywords").value.trim(),
    monitorLogAll: $<HTMLInputElement>("cfg-log-all").checked,
    storageEnabled: $<HTMLInputElement>("cfg-storage").checked,
    alertEnabled: $<HTMLInputElement>("cfg-alert").checked,
    alertWebhookUrl: $<HTMLInputElement>("cfg-webhook").value.trim(),
  };
}

async function refreshMessages() {
  const rows = await invoke<MessageRow[]>("get_recent_messages", { limit: 40 });
  const box = $("messages");
  if (!rows.length) {
    box.innerHTML = `<div class="empty">暂无消息。确认群号正确且监控已启动。</div>`;
    return;
  }
  box.innerHTML = rows
    .map((m) => {
      const when = m.createdAt || (m.eventTime ? String(m.eventTime) : "-");
      return `<article class="msg">
        <div class="who">#${m.id} · 群 ${m.groupId} · ${m.senderName || m.userId} · ${when}</div>
        <div class="body">${escapeHtml(m.content)}</div>
      </article>`;
    })
    .join("");
}

async function refreshLogs() {
  const text = await invoke<string>("get_log_tail", { maxLines: 100 });
  $("logs").textContent = text || "（暂无日志）";
}

function parseSelectedFromInput() {
  selectedGroupIds = new Set(
    $<HTMLInputElement>("cfg-groups")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

function renderGroups() {
  const box = $("groups-list");
  const q = $<HTMLInputElement>("groups-filter").value.trim().toLowerCase();
  const rows = cachedGroups.filter((g) => {
    if (!q) return true;
    return (
      g.groupName.toLowerCase().includes(q) ||
      g.groupId.toLowerCase().includes(q)
    );
  });
  if (!cachedGroups.length) {
    box.innerHTML = `<div class="empty">点击「拉取群列表」加载（需 NapCat 已登录且 OneBot WS 在线）</div>`;
    return;
  }
  if (!rows.length) {
    box.innerHTML = `<div class="empty">没有匹配「${escapeHtml(q)}」的群</div>`;
    return;
  }
  box.innerHTML = rows
    .map((g) => {
      const checked = selectedGroupIds.has(g.groupId);
      const members =
        g.memberCount != null
          ? `${g.memberCount}${g.maxMemberCount != null ? "/" + g.maxMemberCount : ""}`
          : "-";
      return `<label class="group-row${checked ? " selected" : ""}" data-id="${escapeHtml(g.groupId)}">
        <input type="checkbox" ${checked ? "checked" : ""} data-id="${escapeHtml(g.groupId)}" />
        <span>
          <div class="name">${escapeHtml(g.groupName || "(未命名群)")}</div>
          <div class="id">${escapeHtml(g.groupId)}</div>
        </span>
        <span class="members">${escapeHtml(members)}</span>
      </label>`;
    })
    .join("");

  box.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach((cb) => {
    cb.onchange = () => {
      const id = cb.dataset.id || "";
      if (!id) return;
      if (cb.checked) selectedGroupIds.add(id);
      else selectedGroupIds.delete(id);
      renderGroups();
    };
  });
}

async function loadGroups() {
  parseSelectedFromInput();
  const result = await invoke<GroupListResult>("get_group_list");
  cachedGroups = result.groups || [];
  const who = [result.loginNickname, result.loginUserId].filter(Boolean).join(" · ");
  $("groups-login").textContent = who
    ? `当前账号：${who} · 共 ${cachedGroups.length} 个群`
    : `共 ${cachedGroups.length} 个群`;
  renderGroups();
  toast(`已拉取 ${cachedGroups.length} 个群`);
}

function applySelectedGroups() {
  const ids = Array.from(selectedGroupIds);
  $<HTMLInputElement>("cfg-groups").value = ids.join(",");
  toast(ids.length ? `已写入 ${ids.length} 个监控群号，请保存配置` : "未勾选任何群");
}

function escapeHtml(s: string) {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function tick() {
  try {
    await Promise.all([refreshStatus(), refreshQr(), refreshMessages(), refreshLogs()]);
  } catch (e) {
    console.error(e);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  $("btn-refresh").onclick = () => tick();
  $("btn-start-napcat").onclick = async () => {
    try {
      const msg = await invoke<string>("start_napcat");
      toast(msg);
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-refresh-qr").onclick = async () => {
    try {
      const msg = await invoke<string>("refresh_qrcode_hint");
      toast(msg);
      await refreshQr();
    } catch (e) {
      toast(String(e), true);
      await refreshQr();
    }
  };
  $("btn-open-qr-folder").onclick = async () => {
    try {
      await invoke("open_qrcode_folder");
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-open-webui").onclick = async () => {
    try {
      await invoke("open_webui");
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-copy-url").onclick = async () => {
    const v = $<HTMLTextAreaElement>("qr-url").value;
    await navigator.clipboard.writeText(v);
    toast("已复制解码 URL");
  };
  $("btn-start-monitor").onclick = async () => {
    try {
      await invoke("save_config", { config: readConfigForm() });
      const msg = await invoke<string>("start_monitor");
      toast(msg);
      await refreshStatus();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-stop-monitor").onclick = async () => {
    try {
      const msg = await invoke<string>("stop_monitor");
      toast(msg);
      await refreshStatus();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("config-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await invoke("save_config", { config: readConfigForm() });
      toast("配置已保存");
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-load-groups").onclick = async () => {
    try {
      await loadGroups();
    } catch (e) {
      toast(String(e), true);
    }
  };
  $("btn-apply-groups").onclick = () => applySelectedGroups();
  $("groups-filter").oninput = () => renderGroups();
  $<HTMLInputElement>("cfg-groups").onchange = () => {
    parseSelectedFromInput();
    renderGroups();
  };

  await refreshConfig();
  parseSelectedFromInput();
  await tick();
  window.setInterval(tick, 2500);
});
