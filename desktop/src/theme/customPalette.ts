/**
 * 由单一主色派生完整主题色板。
 * 背景走低饱和同色相中性色；语义色保持可辨识。
 */
import type { ThemeDef, ThemeMode, ThemeVars } from "./definitions";

export type CustomThemePrefs = {
  /** #rrggbb */
  baseColor: string;
  mode: ThemeMode;
  /** color=纯色自动；image=图片采样自动 */
  source?: "color" | "image";
};

export const DEFAULT_CUSTOM_THEME: CustomThemePrefs = {
  baseColor: "#3d8b8b",
  mode: "dark",
  source: "color",
};

/** 快捷色（避开默认紫系） */
export const CUSTOM_QUICK_COLORS = [
  "#3d8b8b",
  "#5a7a72",
  "#b08958",
  "#3d6e8a",
  "#8b5a4a",
  "#4a7c59",
  "#c45c4a",
  "#6b5b4b",
] as const;

const CUSTOM_WALL_KEY = "gmm_custom_theme_wall";

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n));
}

function normalizeHex(input: string | undefined | null): string | null {
  if (!input) return null;
  let s = input.trim();
  if (!s.startsWith("#")) s = `#${s}`;
  if (/^#[0-9a-fA-F]{3}$/.test(s)) {
    const r = s[1];
    const g = s[2];
    const b = s[3];
    s = `#${r}${r}${g}${g}${b}${b}`;
  }
  if (!/^#[0-9a-fA-F]{6}$/.test(s)) return null;
  return s.toLowerCase();
}

export function isValidCustomHex(input: string): boolean {
  return normalizeHex(input) !== null;
}

export function normalizeCustomTheme(
  raw: Partial<CustomThemePrefs> | null | undefined,
): CustomThemePrefs | undefined {
  if (!raw) return undefined;
  const baseColor = normalizeHex(raw.baseColor);
  if (!baseColor) return undefined;
  const mode: ThemeMode = raw.mode === "light" ? "light" : "dark";
  const source = raw.source === "image" ? "image" : "color";
  return { baseColor, mode, source };
}

type Hsl = { h: number; s: number; l: number };

export function hexToHsl(hex: string): Hsl {
  const n = normalizeHex(hex) ?? DEFAULT_CUSTOM_THEME.baseColor;
  const r = parseInt(n.slice(1, 3), 16) / 255;
  const g = parseInt(n.slice(3, 5), 16) / 255;
  const b = parseInt(n.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  const l = (max + min) / 2;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  if (d !== 0) {
    switch (max) {
      case r:
        h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
        break;
      case g:
        h = ((b - r) / d + 2) / 6;
        break;
      default:
        h = ((r - g) / d + 4) / 6;
        break;
    }
  }
  return { h: h * 360, s, l };
}

export function hslToHex(h: number, s: number, l: number): string {
  const hh = ((h % 360) + 360) % 360;
  const ss = clamp(s, 0, 1);
  const ll = clamp(l, 0, 1);
  const c = (1 - Math.abs(2 * ll - 1)) * ss;
  const x = c * (1 - Math.abs(((hh / 60) % 2) - 1));
  const m = ll - c / 2;
  let rp = 0;
  let gp = 0;
  let bp = 0;
  if (hh < 60) [rp, gp, bp] = [c, x, 0];
  else if (hh < 120) [rp, gp, bp] = [x, c, 0];
  else if (hh < 180) [rp, gp, bp] = [0, c, x];
  else if (hh < 240) [rp, gp, bp] = [0, x, c];
  else if (hh < 300) [rp, gp, bp] = [x, 0, c];
  else [rp, gp, bp] = [c, 0, x];
  const to = (v: number) =>
    Math.round((v + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${to(rp)}${to(gp)}${to(bp)}`;
}

function hslRgba(h: number, s: number, l: number, a: number): string {
  const hex = hslToHex(h, s, l);
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${clamp(a, 0, 1)})`;
}

/** 强调色上的前景字色 */
export function onAccentForColor(hex: string): string {
  const { l } = hexToHsl(hex);
  return l > 0.62 ? "#1a1c1e" : "#ffffff";
}

export function loadCustomWall(): string {
  try {
    return localStorage.getItem(CUSTOM_WALL_KEY) || "";
  } catch {
    return "";
  }
}

export function saveCustomWall(dataUrl: string) {
  try {
    if (!dataUrl) {
      localStorage.removeItem(CUSTOM_WALL_KEY);
      return;
    }
    // 限制约 1.2MB，避免撑爆 localStorage
    if (dataUrl.length > 1_200_000) {
      throw new Error("图片过大，请选用更小的壁纸（建议 1280×720 以内）");
    }
    localStorage.setItem(CUSTOM_WALL_KEY, dataUrl);
  } catch (e) {
    throw e instanceof Error ? e : new Error(String(e));
  }
}

export function clearCustomWall() {
  saveCustomWall("");
}

/** 根据主色生成完整 ThemeDef（id 固定为 custom） */
export function buildCustomThemeDef(
  prefs: CustomThemePrefs | Partial<CustomThemePrefs> | null | undefined,
  opts?: { atmosphere?: string },
): ThemeDef {
  const normalized = normalizeCustomTheme(prefs) ?? DEFAULT_CUSTOM_THEME;
  const { baseColor, mode, source } = normalized;
  const src = hexToHsl(baseColor);
  const h = src.h;
  const accentS = clamp(src.s < 0.08 ? 0.42 : src.s, 0.32, 0.7);
  const accentL =
    mode === "light"
      ? clamp(src.l, 0.3, 0.46)
      : clamp(src.l < 0.35 ? 0.55 : src.l, 0.48, 0.68);

  const accent = hslToHex(h, accentS, accentL);
  const accent2 = hslToHex(
    h + 36,
    clamp(accentS * 0.88, 0.28, 0.62),
    clamp(accentL + (mode === "light" ? 0.04 : -0.04), 0.28, 0.7),
  );
  const accent3 = hslToHex(
    h - 42,
    clamp(accentS * 0.72, 0.22, 0.55),
    clamp(accentL + 0.02, 0.3, 0.68),
  );

  const green = hslToHex(148, 0.38, mode === "light" ? 0.4 : 0.55);
  const blue = hslToHex(208, 0.4, mode === "light" ? 0.42 : 0.58);
  const orange = hslToHex(28, 0.55, mode === "light" ? 0.48 : 0.58);
  const red = hslToHex(4, 0.52, mode === "light" ? 0.48 : 0.58);

  let vars: ThemeVars;
  if (mode === "light") {
    vars = {
      bg0: hslToHex(h, 0.14, 0.965),
      bg1: hslRgba(h, 0.16, 0.98, 0.9),
      bg2: hslRgba(h, 0.12, 0.94, 0.92),
      bg3: hslRgba(h, 0.1, 0.9, 0.96),
      bgMid: hslToHex(h, 0.1, 0.93),
      bgEnd: hslToHex(h, 0.12, 0.9),
      border: hslRgba(h, 0.18, 0.35, 0.12),
      borderStrong: hslRgba(h, accentS, accentL, 0.36),
      text: hslToHex(h, 0.14, 0.18),
      textDim: hslToHex(h, 0.1, 0.38),
      textMuted: hslToHex(h, 0.08, 0.55),
      onAccent: onAccentForColor(accent),
      accent,
      accent2,
      accent3,
      accentSoft: `color-mix(in srgb, ${accent} 14%, transparent)`,
      green,
      blue,
      orange,
      red,
      shadow: "0 16px 40px rgba(60, 70, 90, 0.12)",
    };
  } else {
    vars = {
      bg0: hslToHex(h, 0.22, 0.065),
      bg1: hslRgba(h, 0.2, 0.12, 0.88),
      bg2: hslRgba(h, 0.18, 0.1, 0.92),
      bg3: hslRgba(h, 0.16, 0.08, 0.96),
      bgMid: hslToHex(h, 0.2, 0.09),
      bgEnd: hslToHex(h, 0.24, 0.05),
      border: hslRgba(h, 0.2, 0.7, 0.12),
      borderStrong: hslRgba(h, accentS, accentL, 0.38),
      text: hslToHex(h, 0.1, 0.93),
      textDim: hslToHex(h, 0.12, 0.68),
      textMuted: hslToHex(h, 0.1, 0.42),
      onAccent: onAccentForColor(accent),
      accent,
      accent2,
      accent3,
      accentSoft: `color-mix(in srgb, ${accent} 16%, transparent)`,
      green,
      blue,
      orange,
      red,
      shadow: "0 20px 56px rgba(0, 0, 0, 0.42)",
    };
  }

  const atmosphere = opts?.atmosphere || (source === "image" ? loadCustomWall() : "");
  return {
    id: "custom",
    name: "自定义",
    tagline:
      source === "image"
        ? mode === "light"
          ? "从图片采样铺就纸感界面"
          : "从图片采样铺就夜色界面"
        : mode === "light"
          ? "以主色铺就纸感界面"
          : "以主色铺就夜色界面",
    mode,
    category: "custom",
    atmosphere: atmosphere || undefined,
    wallOpacity: atmosphere ? (mode === "light" ? 0.18 : 0.4) : 0,
    preview: { bg0: vars.bg0, accent, accent2 },
    vars,
  };
}
