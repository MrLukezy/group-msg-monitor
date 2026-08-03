import {
  DEFAULT_THEME_ID,
  getThemeById,
  themeCategoryOf,
  type ThemeCategory,
  type ThemeDef,
  type ThemeMode,
  type ThemeVars,
} from "./definitions";
import {
  buildCustomThemeDef,
  loadCustomWall,
  onAccentForColor,
  type CustomThemePrefs,
} from "./customPalette";

const VAR_MAP: Array<[keyof ThemeVars, string]> = [
  ["bg0", "--bg-0"],
  ["bg1", "--bg-1"],
  ["bg2", "--bg-2"],
  ["bg3", "--bg-3"],
  ["bgMid", "--bg-mid"],
  ["bgEnd", "--bg-end"],
  ["border", "--border"],
  ["borderStrong", "--border-strong"],
  ["text", "--text"],
  ["textDim", "--text-dim"],
  ["textMuted", "--text-muted"],
  ["onAccent", "--on-accent"],
  ["accent", "--accent"],
  ["accent2", "--accent-2"],
  ["accent3", "--accent-3"],
  ["accentSoft", "--accent-soft"],
  ["green", "--green"],
  ["blue", "--blue"],
  ["orange", "--orange"],
  ["red", "--red"],
  ["shadow", "--shadow"],
];

export type ThemePalette = {
  id: string;
  mode: ThemeMode;
  category: ThemeCategory;
  accent: string;
  accent2: string;
  accent3: string;
  bg0: string;
  atmosphere?: string;
};

function paletteFromDef(theme: ThemeDef): ThemePalette {
  return {
    id: theme.id,
    mode: theme.mode,
    category: themeCategoryOf(theme),
    accent: theme.vars.accent,
    accent2: theme.vars.accent2,
    accent3: theme.vars.accent3,
    bg0: theme.vars.bg0,
    atmosphere: theme.atmosphere,
  };
}

export function resolveThemeDef(
  themeId: string,
  custom?: CustomThemePrefs | null,
): ThemeDef {
  if (themeId === "custom") {
    return buildCustomThemeDef(custom ?? undefined, {
      atmosphere: custom?.source === "image" ? loadCustomWall() : undefined,
    });
  }
  return getThemeById(themeId) ?? getThemeById(DEFAULT_THEME_ID)!;
}

/** 将完整 ThemeDef 应用到 document */
export function applyThemeDef(
  theme: ThemeDef,
  opts?: { wallOpacity?: number | null; panelOpacity?: number | null },
): ThemePalette {
  const root = document.documentElement;
  const body = document.body;
  const mode = theme.mode ?? "dark";
  const category = themeCategoryOf(theme);

  root.setAttribute("data-theme", theme.id);
  root.setAttribute("data-theme-mode", mode);
  root.setAttribute("data-theme-category", category);
  if (body) {
    body.setAttribute("data-theme", theme.id);
  }

  for (const [key, cssVar] of VAR_MAP) {
    root.style.setProperty(cssVar, theme.vars[key]);
  }

  const atmosphere = theme.atmosphere ? `url("${theme.atmosphere}")` : "none";
  root.style.setProperty("--theme-atmosphere", atmosphere);
  const themeDefault =
    theme.wallOpacity ?? (mode === "light" ? 0.16 : 0.4);
  const userWall = opts?.wallOpacity;
  const wall =
    theme.atmosphere && (category === "image" || category === "custom")
      ? typeof userWall === "number"
        ? Math.max(0, Math.min(1, userWall))
        : themeDefault
      : 0;
  root.style.setProperty("--theme-wall-opacity", String(wall));

  const panel =
    typeof opts?.panelOpacity === "number"
      ? Math.max(0.35, Math.min(1, opts.panelOpacity))
      : 0.82;
  root.style.setProperty("--ui-panel-opacity", String(panel));
  root.style.setProperty("--ui-panel-mix", `${Math.round(panel * 100)}%`);

  root.style.setProperty(
    "--on-accent",
    theme.vars.onAccent || onAccentForColor(theme.vars.accent),
  );

  // 兼容旧样式里依赖的别名
  root.style.setProperty("--panel", "var(--bg-1)");
  root.style.setProperty("--bg", "var(--bg-0)");

  return paletteFromDef(theme);
}

export function applyTheme(
  themeId: string,
  custom?: CustomThemePrefs | null,
  opts?: { wallOpacity?: number | null; panelOpacity?: number | null },
): ThemePalette {
  return applyThemeDef(resolveThemeDef(themeId, custom), opts);
}

/** 仅更新透明度相关 CSS 变量，避免整页换肤与 DOM 重建。 */
export function applyOpacityVars(opts: {
  wallOpacity: number;
  panelOpacity: number;
  hasAtmosphere: boolean;
}): void {
  const root = document.documentElement;
  const wall = opts.hasAtmosphere
    ? Math.max(0, Math.min(1, opts.wallOpacity))
    : 0;
  const panel = Math.max(0.35, Math.min(1, opts.panelOpacity));
  root.style.setProperty("--theme-wall-opacity", String(wall));
  // 用百分比 token，避免 color-mix(calc(var*100%)) 的高额样式重算
  root.style.setProperty("--ui-panel-mix", `${Math.round(panel * 100)}%`);
  root.style.setProperty("--ui-panel-opacity", String(panel));
}

/** 当前皮肤默认壁纸透明度（无壁纸时返回 0） */
export function themeDefaultWallOpacity(
  themeId: string,
  custom?: CustomThemePrefs | null,
): number {
  const theme = resolveThemeDef(themeId, custom);
  if (!theme.atmosphere) return 0;
  return theme.wallOpacity ?? (theme.mode === "light" ? 0.16 : 0.4);
}
