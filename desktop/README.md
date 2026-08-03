# 桌面端（Tauri）

群消息监听的图形界面：实时消息、分群配置、LLM 报告、皮肤与收藏夹。

完整安装、原理与免责声明见仓库根目录 [README.md](../README.md)。

## 开发启动

```powershell
# 仓库根目录先准备好 Python venv 与依赖
cd desktop
npm install
npm run tauri dev
```

也可双击根目录 `start-desktop.bat`。

## 技术栈

- Tauri 2 + Vite + TypeScript
- 前端通过本地脚本 / API 启停 Python 监控服务并读写配置

## 构建安装包

```powershell
cd desktop
npm run tauri build
```

产出为 NSIS 安装包（见 `src-tauri/tauri.conf.json` 中 `bundle` 配置）。
