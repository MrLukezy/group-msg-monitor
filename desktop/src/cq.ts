/** OneBot / NapCat CQ 码解析与消息展示 HTML 渲染 */

export type MsgSeg =
  | { kind: "text"; text: string }
  | { kind: "link"; url: string }
  | { kind: "image"; url?: string; file?: string }
  | { kind: "face"; id?: string; label?: string }
  | { kind: "reply"; id?: string; text?: string }
  | { kind: "at"; qq?: string; name?: string }
  | { kind: "file"; name?: string }
  | { kind: "record" }
  | { kind: "video" }
  | { kind: "json"; summary?: string }
  | { kind: "chip"; label: string };

function escapeHtml(s: string) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function unescapeCq(value: string) {
  return value
    .replace(/&#91;/g, "[")
    .replace(/&#93;/g, "]")
    .replace(/&amp;/g, "&")
    .replace(/&#44;/g, ",");
}

/** 解析单个 CQ 参数表（支持 raw={...} JSON） */
function parseCqParams(paramStr: string): Record<string, string> {
  const params: Record<string, string> = {};
  let i = 0;
  const s = paramStr;
  while (i < s.length) {
    if (s[i] === ",") {
      i += 1;
      continue;
    }
    const eq = s.indexOf("=", i);
    if (eq === -1) break;
    const key = s.slice(i, eq).trim();
    i = eq + 1;
    if (!key) break;

    if (s[i] === "{" || s[i] === "[") {
      const open = s[i];
      const close = open === "{" ? "}" : "]";
      let depth = 0;
      const start = i;
      while (i < s.length) {
        const ch = s[i];
        if (ch === open) depth += 1;
        else if (ch === close) {
          depth -= 1;
          if (depth === 0) {
            i += 1;
            break;
          }
        }
        i += 1;
      }
      params[key] = unescapeCq(s.slice(start, i));
      continue;
    }

    let j = i;
    while (j < s.length) {
      if (s[j] === "]") break;
      if (s[j] === ",") {
        const peek = s.slice(j + 1).match(/^([a-zA-Z0-9_]+)=/);
        if (peek) break;
      }
      j += 1;
    }
    params[key] = unescapeCq(s.slice(i, j).trim());
    i = j;
  }
  return params;
}

function findCqEnd(s: string, start: number): number {
  // start 指向 '[' of [CQ:
  let i = start + 4;
  while (i < s.length) {
    if (s[i] === "{") {
      let depth = 0;
      while (i < s.length) {
        if (s[i] === "{") depth += 1;
        else if (s[i] === "}") {
          depth -= 1;
          if (depth === 0) {
            i += 1;
            break;
          }
        }
        i += 1;
      }
      continue;
    }
    if (s[i] === "]") return i;
    i += 1;
  }
  return -1;
}

function faceLabel(params: Record<string, string>): string | undefined {
  const raw = params.raw;
  if (raw) {
    try {
      const obj = JSON.parse(raw) as { faceText?: string; text?: string };
      const t = (obj.faceText || obj.text || "").trim();
      if (t) return t;
    } catch {
      /* ignore */
    }
  }
  return undefined;
}

function cqToSeg(type: string, params: Record<string, string>): MsgSeg {
  switch (type) {
    case "text":
      return { kind: "text", text: params.text || "" };
    case "image":
    case "mface":
      return {
        kind: "image",
        url: params.url || params.file || undefined,
        file: params.file,
      };
    case "face":
      return {
        kind: "face",
        id: params.id,
        label: faceLabel(params),
      };
    case "reply":
      return {
        kind: "reply",
        id: params.id || params.seq,
        text: params.text || params.content,
      };
    case "at":
      return {
        kind: "at",
        qq: params.qq,
        name: params.name,
      };
    case "file":
      return { kind: "file", name: params.name || params.file };
    case "record":
      return { kind: "record" };
    case "video":
      return { kind: "video" };
    case "json":
    case "xml":
    case "forward":
      return { kind: "json", summary: type === "forward" ? "合并转发" : "卡片消息" };
    case "share":
      return {
        kind: "link",
        url: params.url || "",
      };
    default:
      return { kind: "chip", label: type };
  }
}

function splitTextWithLinks(text: string): MsgSeg[] {
  if (!text) return [];
  const out: MsgSeg[] = [];
  let last = 0;
  const re = /https?:\/\/[^\s<>"'）】\]]+/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) {
      out.push({ kind: "text", text: text.slice(last, m.index) });
    }
    const rawUrl = m[0];
    const url = rawUrl.replace(/[.,;:!?，。；：！？]+$/u, "");
    out.push({ kind: "link", url });
    const trailing = rawUrl.slice(url.length);
    if (trailing) out.push({ kind: "text", text: trailing });
    last = m.index + rawUrl.length;
  }
  if (last < text.length) {
    out.push({ kind: "text", text: text.slice(last) });
  }
  return out;
}

export function parseCqContent(raw: string): MsgSeg[] {
  const input = (raw || "").replace(/\r\n/g, "\n");
  if (!input.trim()) return [{ kind: "text", text: "" }];

  const segs: MsgSeg[] = [];
  let i = 0;
  while (i < input.length) {
    const cqAt = input.indexOf("[CQ:", i);
    if (cqAt === -1) {
      segs.push(...splitTextWithLinks(input.slice(i)));
      break;
    }
    if (cqAt > i) {
      segs.push(...splitTextWithLinks(input.slice(i, cqAt)));
    }
    const end = findCqEnd(input, cqAt);
    if (end === -1) {
      segs.push(...splitTextWithLinks(input.slice(cqAt)));
      break;
    }
    const body = input.slice(cqAt + 4, end); // type,params...
    const comma = body.indexOf(",");
    const type = (comma === -1 ? body : body.slice(0, comma)).trim();
    const paramStr = comma === -1 ? "" : body.slice(comma);
    if (type) {
      segs.push(cqToSeg(type, parseCqParams(paramStr)));
    }
    i = end + 1;
  }

  // 合并相邻 text
  const merged: MsgSeg[] = [];
  for (const seg of segs) {
    const prev = merged[merged.length - 1];
    if (seg.kind === "text" && prev?.kind === "text") {
      prev.text += seg.text;
    } else {
      merged.push(seg);
    }
  }
  return merged.filter((s) => !(s.kind === "text" && !s.text));
}

function shortenUrl(url: string, max = 56): string {
  try {
    const u = new URL(url);
    const path = u.pathname.length > 18 ? u.pathname.slice(0, 16) + "…" : u.pathname;
    const host = u.host;
    const s = host + path;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  } catch {
    return url.length > max ? url.slice(0, max - 1) + "…" : url;
  }
}

function isHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

export function renderMsgHtml(raw: string): string {
  const segs = parseCqContent(raw);
  if (!segs.length) {
    return `<span class="msg-empty">（空消息）</span>`;
  }

  const parts: string[] = [];
  for (const seg of segs) {
    switch (seg.kind) {
      case "text":
        parts.push(`<span class="msg-text">${escapeHtml(seg.text)}</span>`);
        break;
      case "link":
        if (!seg.url) break;
        parts.push(
          `<a class="msg-link" href="${escapeHtml(seg.url)}" data-ext-url="${escapeHtml(
            seg.url,
          )}" title="${escapeHtml(seg.url)}">${escapeHtml(shortenUrl(seg.url))}</a>`,
        );
        break;
      case "image": {
        const url = seg.url || "";
        if (url && isHttpUrl(url)) {
          // 不内嵌 <img>：避免轮询重绘时反复拉图导致卡死
          parts.push(
            `<a class="msg-chip image-link" href="${escapeHtml(url)}" data-ext-url="${escapeHtml(
              url,
            )}" title="${escapeHtml(url)}">🖼 查看图片</a>`,
          );
        } else {
          parts.push(`<span class="msg-chip">🖼 图片</span>`);
        }
        break;
      }
      case "face": {
        const label = seg.label || (seg.id ? `表情 ${seg.id}` : "表情");
        parts.push(`<span class="msg-chip face">😊 ${escapeHtml(label)}</span>`);
        break;
      }
      case "reply": {
        const idPart = seg.id ? `#${escapeHtml(seg.id)}` : "";
        const textPart = seg.text
          ? `<span class="msg-reply-text">${escapeHtml(seg.text)}</span>`
          : `<span class="msg-reply-text muted">原消息 ${idPart || ""}</span>`;
        parts.push(
          `<div class="msg-reply"><span class="msg-reply-label">回复</span>${textPart}</div>`,
        );
        break;
      }
      case "at": {
        const who = seg.name || seg.qq || "";
        parts.push(`<span class="msg-at">@${escapeHtml(who)}</span>`);
        break;
      }
      case "file":
        parts.push(`<span class="msg-chip">📄 ${escapeHtml(seg.name || "文件")}</span>`);
        break;
      case "record":
        parts.push(`<span class="msg-chip">🎤 语音</span>`);
        break;
      case "video":
        parts.push(`<span class="msg-chip">🎬 视频</span>`);
        break;
      case "json":
        parts.push(`<span class="msg-chip">🃏 ${escapeHtml(seg.summary || "卡片")}</span>`);
        break;
      case "chip":
        parts.push(`<span class="msg-chip">${escapeHtml(seg.label)}</span>`);
        break;
    }
  }
  return `<div class="msg-rich">${parts.join("")}</div>`;
}

export function formatTime(createdAt?: string, eventTime?: number | null): string {
  if (createdAt) {
    // 可能是 "2026-07-27 15:26:54" 或 ISO
    const s = createdAt.replace("T", " ").replace(/\.\d+.*/, "");
    if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 19);
  }
  if (eventTime && eventTime > 0) {
    const d = new Date(eventTime * (eventTime < 1e12 ? 1000 : 1));
    if (!Number.isNaN(d.getTime())) {
      const p = (n: number) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
  }
  return "-";
}
