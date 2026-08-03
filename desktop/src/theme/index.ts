export {
  THEMES,
  DEFAULT_THEME_ID,
  getThemeById,
  themesByCategory,
  themeCategoryOf,
  type ThemeCategory,
  type ThemeDef,
  type ThemeMode,
  type ThemeVars,
} from "./definitions";

export {
  applyTheme,
  applyThemeDef,
  applyOpacityVars,
  resolveThemeDef,
  themeDefaultWallOpacity,
  type ThemePalette,
} from "./applyTheme";

export {
  CUSTOM_QUICK_COLORS,
  DEFAULT_CUSTOM_THEME,
  buildCustomThemeDef,
  clearCustomWall,
  isValidCustomHex,
  loadCustomWall,
  normalizeCustomTheme,
  onAccentForColor,
  saveCustomWall,
  type CustomThemePrefs,
} from "./customPalette";

export {
  fileToWallpaperDataUrl,
  samplePaletteFromImage,
  type SampledPalette,
} from "./sampleImage";
