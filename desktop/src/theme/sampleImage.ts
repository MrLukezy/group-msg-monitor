/**
 * 从图片采样主色，供自定义皮肤自动配色。
 * 算法：缩小画布 → 加权平均（跳过近白/近黑/低饱和）→ 可选第二强调色。
 */
import { hexToHsl, hslToHex } from "./customPalette";

export type SampledPalette = {
  /** 主色 #rrggbb */
  baseColor: string;
  /** 建议的第二强调色 */
  accent2: string;
  /** 建议明暗模式 */
  suggestedMode: "dark" | "light";
};

function clampByte(n: number) {
  return Math.max(0, Math.min(255, Math.round(n)));
}

function rgbToHex(r: number, g: number, b: number): string {
  const to = (v: number) => clampByte(v).toString(16).padStart(2, "0");
  return `#${to(r)}${to(g)}${to(b)}`;
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("无法读取图片"));
    img.src = src;
  });
}

/**
 * 从 File / dataURL / blobURL 采样主色。
 */
export async function samplePaletteFromImage(
  source: File | string,
): Promise<SampledPalette> {
  const src =
    typeof source === "string" ? source : URL.createObjectURL(source);
  const revoke = typeof source !== "string";
  try {
    const img = await loadImage(src);
    const maxSide = 64;
    const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
    const w = Math.max(8, Math.round(img.width * scale));
    const h = Math.max(8, Math.round(img.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) throw new Error("Canvas 不可用");
    ctx.drawImage(img, 0, 0, w, h);
    const { data } = ctx.getImageData(0, 0, w, h);

    let sumR = 0;
    let sumG = 0;
    let sumB = 0;
    let weight = 0;
    let lumSum = 0;
    let lumN = 0;

    // 第二桶：偏暖/偏冷的次要色
    let sumR2 = 0;
    let sumG2 = 0;
    let sumB2 = 0;
    let weight2 = 0;

    for (let i = 0; i < data.length; i += 4) {
      const a = data[i + 3];
      if (a < 200) continue;
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const lum = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 255;
      lumSum += lum;
      lumN += 1;
      // 跳过近白 / 近黑
      if (lum > 0.92 || lum < 0.08) continue;
      const sat = max === 0 ? 0 : (max - min) / max;
      if (sat < 0.08) continue;
      // 饱和度与中间亮度加权，让主色更「有色」
      const wgt = sat * (1 - Math.abs(lum - 0.45) * 1.4);
      if (wgt <= 0.02) continue;
      sumR += r * wgt;
      sumG += g * wgt;
      sumB += b * wgt;
      weight += wgt;

      // 次要色：色相偏离主色较多的像素稍后二次平均；先按暖冷粗分
      const warm = r > b;
      if (warm) {
        sumR2 += r * wgt;
        sumG2 += g * wgt;
        sumB2 += b * wgt;
        weight2 += wgt;
      }
    }

    let baseColor: string;
    if (weight > 0) {
      baseColor = rgbToHex(sumR / weight, sumG / weight, sumB / weight);
    } else {
      // 回退：全图平均（含灰）
      let ar = 0;
      let ag = 0;
      let ab = 0;
      let n = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] < 200) continue;
        ar += data[i];
        ag += data[i + 1];
        ab += data[i + 2];
        n += 1;
      }
      baseColor = n ? rgbToHex(ar / n, ag / n, ab / n) : "#3d8b8b";
    }

    let accent2: string;
    if (weight2 > 0) {
      accent2 = rgbToHex(sumR2 / weight2, sumG2 / weight2, sumB2 / weight2);
    } else {
      const hsl = hexToHsl(baseColor);
      accent2 = hslToHex(hsl.h + 36, Math.min(0.62, hsl.s * 0.9), hsl.l);
    }

    const avgLum = lumN ? lumSum / lumN : 0.4;
    const suggestedMode: "dark" | "light" = avgLum > 0.55 ? "light" : "dark";

    // 校正过灰主色
    const hsl = hexToHsl(baseColor);
    if (hsl.s < 0.12) {
      baseColor = hslToHex(hsl.h || 180, 0.36, suggestedMode === "light" ? 0.42 : 0.55);
    }

    return { baseColor, accent2, suggestedMode };
  } finally {
    if (revoke) URL.revokeObjectURL(src);
  }
}

/** 把 File 压成适合 localStorage 的 dataURL（最长边 1280） */
export async function fileToWallpaperDataUrl(file: File): Promise<string> {
  const objUrl = URL.createObjectURL(file);
  try {
    const img = await loadImage(objUrl);
    const maxSide = 1280;
    const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas 不可用");
    ctx.drawImage(img, 0, 0, w, h);
    return canvas.toDataURL("image/jpeg", 0.82);
  } finally {
    URL.revokeObjectURL(objUrl);
  }
}
