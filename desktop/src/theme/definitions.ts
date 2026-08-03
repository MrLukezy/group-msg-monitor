/** 皮肤定义：图片皮肤 / 纯色皮肤 / 自定义（运行时）。 */

export type ThemeMode = "dark" | "light";

/** image=壁纸皮肤；palette=纯色板；custom=用户自定义 */
export type ThemeCategory = "image" | "palette" | "custom";

export type ThemeVars = {
  bg0: string;
  bg1: string;
  bg2: string;
  bg3: string;
  bgMid: string;
  bgEnd: string;
  border: string;
  borderStrong: string;
  text: string;
  textDim: string;
  textMuted: string;
  onAccent: string;
  accent: string;
  accent2: string;
  accent3: string;
  accentSoft: string;
  green: string;
  blue: string;
  orange: string;
  red: string;
  shadow: string;
};

export type ThemeDef = {
  id: string;
  name: string;
  tagline: string;
  mode: ThemeMode;
  category: ThemeCategory;
  /** 氛围壁纸 public 路径 */
  atmosphere?: string;
  wallOpacity?: number;
  preview: { bg0: string; accent: string; accent2: string };
  vars: ThemeVars;
};

export function themeCategoryOf(theme: ThemeDef): ThemeCategory {
  if (theme.category) return theme.category;
  return theme.atmosphere ? "image" : "palette";
}

function pack(
  partial: Omit<ThemeVars, "accentSoft" | "shadow" | "border" | "onAccent"> &
    Partial<Pick<ThemeVars, "accentSoft" | "shadow" | "border" | "onAccent">>,
  mode: ThemeMode,
): ThemeVars {
  const accent = partial.accent;
  return {
    border:
      partial.border ??
      (mode === "light" ? "rgba(90, 100, 110, 0.12)" : "rgba(148, 163, 184, 0.10)"),
    accentSoft:
      partial.accentSoft ?? `color-mix(in srgb, ${accent} 14%, transparent)`,
    shadow:
      partial.shadow ??
      (mode === "light"
        ? "0 16px 40px rgba(60, 70, 90, 0.12)"
        : "0 20px 56px rgba(0, 0, 0, 0.42)"),
    onAccent: partial.onAccent ?? (mode === "light" ? "#ffffff" : "#15110c"),
    ...partial,
  } as ThemeVars;
}

/** 图片皮肤：壁纸 + 配套色板 */
function imageSkin(seed: {
  id: string;
  name: string;
  tagline: string;
  atmosphere: string;
  mode?: ThemeMode;
  wallOpacity?: number;
  bg: string;
  surface: string;
  accent: string;
  accent2: string;
  accent3: string;
  text?: string;
  onAccent?: string;
}): ThemeDef {
  const mode = seed.mode ?? "dark";
  const light = mode === "light";
  const text = seed.text ?? (light ? "#2a2e2c" : "#f2edf4");
  const textDim = light
    ? `color-mix(in srgb, ${text} 62%, ${seed.accent2})`
    : `color-mix(in srgb, ${text} 68%, ${seed.accent2})`;
  const textMuted = light
    ? `color-mix(in srgb, ${text} 42%, ${seed.bg})`
    : `color-mix(in srgb, ${text} 42%, ${seed.bg})`;
  const vars = pack(
    {
      bg0: seed.bg,
      bg1: light
        ? `color-mix(in srgb, ${seed.surface} 88%, transparent)`
        : `color-mix(in srgb, ${seed.surface} 82%, transparent)`,
      bg2: light
        ? `color-mix(in srgb, ${seed.surface} 92%, transparent)`
        : `color-mix(in srgb, ${seed.surface} 70%, transparent)`,
      bg3: light
        ? `color-mix(in srgb, ${seed.surface} 96%, ${seed.bg})`
        : `color-mix(in srgb, ${seed.surface} 88%, ${seed.bg})`,
      bgMid: seed.surface,
      bgEnd: seed.bg,
      borderStrong: `color-mix(in srgb, ${seed.accent} 42%, transparent)`,
      text,
      textDim,
      textMuted,
      accent: seed.accent,
      accent2: seed.accent2,
      accent3: seed.accent3,
      green: light ? "#5a9a6e" : "#70b090",
      blue: light ? "#5a8498" : "#72a0cc",
      orange: light ? "#b08958" : "#d49a62",
      red: light ? "#b07070" : "#d4777c",
      onAccent: seed.onAccent,
    },
    mode,
  );
  return {
    id: seed.id,
    name: seed.name,
    tagline: seed.tagline,
    mode,
    category: "image",
    atmosphere: seed.atmosphere,
    wallOpacity:
      seed.wallOpacity ?? (light ? 0.14 : 0.42),
    preview: { bg0: seed.bg, accent: seed.accent, accent2: seed.accent2 },
    vars,
  };
}

/** 纯色皮肤：无壁纸 */
function paletteSkin(seed: {
  id: string;
  name: string;
  tagline: string;
  mode: ThemeMode;
  bg: string;
  surface: string;
  accent: string;
  accent2: string;
  accent3: string;
  text: string;
  textDim: string;
  textMuted: string;
  onAccent?: string;
}): ThemeDef {
  const vars = pack(
    {
      bg0: seed.bg,
      bg1:
        seed.mode === "light"
          ? `color-mix(in srgb, #ffffff 72%, ${seed.surface})`
          : `color-mix(in srgb, ${seed.surface} 82%, transparent)`,
      bg2:
        seed.mode === "light"
          ? `color-mix(in srgb, ${seed.surface} 90%, transparent)`
          : `color-mix(in srgb, ${seed.surface} 70%, transparent)`,
      bg3: `color-mix(in srgb, ${seed.surface} 96%, ${seed.bg})`,
      bgMid: seed.surface,
      bgEnd: seed.bg,
      borderStrong: `color-mix(in srgb, ${seed.accent} 36%, transparent)`,
      text: seed.text,
      textDim: seed.textDim,
      textMuted: seed.textMuted,
      accent: seed.accent,
      accent2: seed.accent2,
      accent3: seed.accent3,
      green: seed.mode === "light" ? "#5a9a6e" : "#6aab8e",
      blue: seed.mode === "light" ? "#5a8498" : "#6a9fc4",
      orange: seed.mode === "light" ? "#b08958" : "#c9956a",
      red: seed.mode === "light" ? "#b07070" : "#c97878",
      onAccent: seed.onAccent,
    },
    seed.mode,
  );
  return {
    id: seed.id,
    name: seed.name,
    tagline: seed.tagline,
    mode: seed.mode,
    category: "palette",
    preview: { bg0: seed.bg, accent: seed.accent, accent2: seed.accent2 },
    vars,
  };
}

export const THEMES: ThemeDef[] = [
  imageSkin({
    id: "midnight",
    name: "星渚",
    tagline: "星沉沧海，灯火孤洲",
    atmosphere: "/theme-atmospheres/midnight.webp",
    mode: "dark",
    wallOpacity: 0.34,
    bg: "#0a0d12",
    surface: "#10151c",
    accent: "#d4a574",
    accent2: "#6b9e9a",
    accent3: "#8b7ec8",
    text: "#e8ecef",
    onAccent: "#15110c",
  }),
  imageSkin({
    id: "daylight",
    name: "素笺",
    tagline: "素纸落墨，清光入卷",
    atmosphere: "/theme-atmospheres/dawn.webp",
    mode: "light",
    wallOpacity: 0.12,
    bg: "#f6f3ec",
    surface: "#efebe2",
    accent: "#5a7a72",
    accent2: "#b08958",
    accent3: "#7a8e9a",
    text: "#2a2e2c",
    onAccent: "#ffffff",
  }),
  imageSkin({
    id: "ocean",
    name: "秩序工坊",
    tagline: "旧线拆解，结构重新归位",
    atmosphere: "/theme-atmospheres/refactor.webp",
    mode: "dark",
    wallOpacity: 0.46,
    bg: "#081015",
    surface: "#12232a",
    accent: "#63a5a0",
    accent2: "#728fa8",
    accent3: "#b7a36b",
    text: "#f2edf4",
    onAccent: "#071313",
  }),
  imageSkin({
    id: "graphite",
    name: "无限月读",
    tagline: "虚空生白，万象皆寂",
    atmosphere: "/theme-atmospheres/void.webp",
    mode: "dark",
    wallOpacity: 0.5,
    bg: "#040408",
    surface: "#080812",
    accent: "#9a7ac8",
    accent2: "#6a98c4",
    accent3: "#7a86c0",
    text: "#ece8f4",
    onAccent: "#ffffff",
  }),
  imageSkin({
    id: "bamboo",
    name: "竹影",
    tagline: "翠叶筛光，静影沉璧",
    atmosphere: "/theme-atmospheres/bamboo.webp",
    mode: "light",
    wallOpacity: 0.16,
    bg: "#f0f5ef",
    surface: "#e4eee2",
    accent: "#5a8a68",
    accent2: "#8aaa7a",
    accent3: "#7a9e8e",
    text: "#243028",
    onAccent: "#ffffff",
  }),
  imageSkin({
    id: "apricot",
    name: "杏雨",
    tagline: "细雨湿春，杏花微照",
    atmosphere: "/theme-atmospheres/apricot.webp",
    mode: "light",
    wallOpacity: 0.14,
    bg: "#f8f1ec",
    surface: "#f0e6de",
    accent: "#c4886a",
    accent2: "#8a9e8a",
    accent3: "#b898a0",
    text: "#3a2e28",
    onAccent: "#ffffff",
  }),
  imageSkin({
    id: "harbor",
    name: "听潮",
    tagline: "港湾灯火，消息随潮",
    atmosphere: "/theme-atmospheres/harbor.webp",
    mode: "dark",
    wallOpacity: 0.44,
    bg: "#071018",
    surface: "#102029",
    accent: "#5eb7b0",
    accent2: "#d4a574",
    accent3: "#7a9ec8",
    text: "#e8f0f2",
    onAccent: "#071313",
  }),
  imageSkin({
    id: "ember",
    name: "余烬",
    tagline: "炭火微明，信号未息",
    atmosphere: "/theme-atmospheres/ember.webp",
    mode: "dark",
    wallOpacity: 0.4,
    bg: "#120c0a",
    surface: "#1c1410",
    accent: "#d4895a",
    accent2: "#a87858",
    accent3: "#8a6e5a",
    text: "#f2ebe4",
    onAccent: "#1a100c",
  }),

  paletteSkin({
    id: "forest",
    name: "青岚",
    tagline: "翠色铺陈，清爽可读",
    mode: "light",
    bg: "#f0f5ef",
    surface: "#e4eee2",
    accent: "#5a8a68",
    accent2: "#8aaa7a",
    accent3: "#7a9e8e",
    text: "#243028",
    textDim: "#5a6e5e",
    textMuted: "#889888",
    onAccent: "#ffffff",
  }),
  paletteSkin({
    id: "rose",
    name: "暖陶",
    tagline: "陶色温润，纸感留白",
    mode: "light",
    bg: "#f8f1ec",
    surface: "#f0e6de",
    accent: "#c4886a",
    accent2: "#8a9e8a",
    accent3: "#b898a0",
    text: "#3a2e28",
    textDim: "#7a6458",
    textMuted: "#a09086",
    onAccent: "#ffffff",
  }),
  paletteSkin({
    id: "mist",
    name: "雾汀",
    tagline: "灰蓝轻雾，冷静克制",
    mode: "light",
    bg: "#f2f4f6",
    surface: "#e8ecf0",
    accent: "#5a7a8a",
    accent2: "#8a9aaa",
    accent3: "#7a8e9a",
    text: "#243038",
    textDim: "#5a6a74",
    textMuted: "#889098",
    onAccent: "#ffffff",
  }),
  paletteSkin({
    id: "ink",
    name: "墨夜",
    tagline: "深墨为底，琥珀为点",
    mode: "dark",
    bg: "#0c0e12",
    surface: "#161a22",
    accent: "#c9a06a",
    accent2: "#6a8e9a",
    accent3: "#8a7a9a",
    text: "#e8ecef",
    textDim: "#9aa7b5",
    textMuted: "#5c6b7a",
    onAccent: "#15110c",
  }),
  paletteSkin({
    id: "celadon",
    name: "青瓷",
    tagline: "釉色温润，浅青如瓷",
    mode: "light",
    bg: "#f3f7f5",
    surface: "#e6efeb",
    accent: "#5a8e82",
    accent2: "#8aada0",
    accent3: "#7a9e9a",
    text: "#243530",
    textDim: "#5a6e68",
    textMuted: "#889890",
    onAccent: "#ffffff",
  }),
  paletteSkin({
    id: "slate",
    name: "岩灰",
    tagline: "冷灰沉静，适合长读",
    mode: "dark",
    bg: "#121418",
    surface: "#1a1e24",
    accent: "#8aa0b0",
    accent2: "#6a8a9a",
    accent3: "#9a8a7a",
    text: "#e6eaee",
    textDim: "#9aa4ae",
    textMuted: "#606870",
    onAccent: "#121418",
  }),
];

export function getThemeById(id: string | null | undefined): ThemeDef | undefined {
  if (!id || id === "custom") return undefined;
  return THEMES.find((t) => t.id === id);
}

export function themesByCategory(category: ThemeCategory): ThemeDef[] {
  return THEMES.filter((t) => themeCategoryOf(t) === category);
}

export const DEFAULT_THEME_ID = "midnight";
