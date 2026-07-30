use std::fs;
use std::io::Write;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use base64::Engine;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

struct AppState {
    monitor_child: Mutex<Option<Child>>,
    napcat_webui_credential: Mutex<Option<String>>,
}

fn project_root() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // desktop
    p.pop(); // project root
    p
}

fn current_github_repo() -> Result<String, String> {
    let output = Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(project_root())
        .output()
        .map_err(|e| format!("无法读取 Git origin: {e}"))?;
    if !output.status.success() {
        return Err("当前项目未配置 Git origin".into());
    }
    let remote = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let path = if let Some(rest) = remote.strip_prefix("git@github.com:") {
        rest
    } else if let Some(rest) = remote.strip_prefix("https://github.com/") {
        rest
    } else if let Some(rest) = remote.strip_prefix("http://github.com/") {
        rest
    } else {
        return Err("当前 origin 不是 GitHub 仓库".into());
    };
    let repo = path.trim_end_matches(".git").trim_matches('/');
    let mut parts = repo.split('/');
    let owner = parts.next().unwrap_or("");
    let name = parts.next().unwrap_or("");
    if owner.is_empty() || name.is_empty() || parts.next().is_some() {
        return Err("无法从 origin 解析 GitHub owner/repo".into());
    }
    Ok(format!("{owner}/{name}"))
}

fn napcat_dir() -> PathBuf {
    project_root().join("third_party").join("NapCatShell")
}

fn python_exe() -> PathBuf {
    let venv = project_root().join(".venv").join("Scripts").join("python.exe");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python")
    }
}

fn port_open(host: &str, port: u16) -> bool {
    TcpStream::connect_timeout(
        &format!("{host}:{port}").parse().unwrap(),
        Duration::from_millis(80),
    )
    .is_ok()
}

fn read_webui_port() -> u16 {
    let path = napcat_dir().join("config").join("webui.json");
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| v.get("port").and_then(|p| p.as_u64()))
        .unwrap_or(6099) as u16
}

fn read_onebot_endpoint() -> (String, u16) {
    let url = fs::read_to_string(project_root().join("data").join("app_settings.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| {
            v.get("onebot_ws_url")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .unwrap_or_else(|| "ws://127.0.0.1:3001".into());
    let authority = url
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(url.as_str())
        .split('/')
        .next()
        .unwrap_or("127.0.0.1:3001");
    let mut parts = authority.rsplitn(2, ':');
    let port = parts.next().and_then(|p| p.parse().ok()).unwrap_or(3001);
    let host = parts.next().unwrap_or("127.0.0.1").trim_matches(['[', ']']);
    (host.to_string(), port)
}

fn read_webui_config() -> Result<(u16, String), String> {
    let path = napcat_dir().join("config").join("webui.json");
    let raw = fs::read_to_string(&path)
        .map_err(|e| format!("读取 NapCat WebUI 配置失败（{}）: {e}", path.display()))?;
    let config: Value =
        serde_json::from_str(&raw).map_err(|e| format!("解析 NapCat WebUI 配置失败: {e}"))?;
    let port = config.get("port").and_then(Value::as_u64).unwrap_or(6099) as u16;
    let token = config
        .get("token")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if token.is_empty() {
        return Err("NapCat WebUI token 为空".into());
    }
    Ok((port, token))
}

fn napcat_webui_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(4))
        .build()
        .map_err(|e| format!("创建 NapCat WebUI 客户端失败: {e}"))
}

fn napcat_webui_credential(
    state: &tauri::State<'_, AppState>,
    client: &reqwest::blocking::Client,
    port: u16,
    token: &str,
) -> Result<String, String> {
    if let Some(credential) = state
        .napcat_webui_credential
        .lock()
        .map_err(|e| e.to_string())?
        .clone()
    {
        return Ok(credential);
    }

    let digest = Sha256::digest(format!("{token}.napcat").as_bytes());
    let hash = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let response = client
        .post(format!("http://127.0.0.1:{port}/api/auth/login"))
        .json(&serde_json::json!({ "hash": hash }))
        .send()
        .map_err(|e| format!("连接 NapCat WebUI 登录接口失败: {e}"))?;
    let payload: Value = response
        .json()
        .map_err(|e| format!("解析 NapCat WebUI 登录响应失败: {e}"))?;
    let credential = payload
        .pointer("/data/Credential")
        .or_else(|| payload.pointer("/data/credential"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            payload
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("NapCat WebUI 授权失败")
                .to_string()
        })?
        .to_string();
    *state
        .napcat_webui_credential
        .lock()
        .map_err(|e| e.to_string())? = Some(credential.clone());
    Ok(credential)
}

fn napcat_webui_post(
    state: &tauri::State<'_, AppState>,
    path: &str,
) -> Result<Value, String> {
    let (port, token) = read_webui_config()?;
    if !port_open("127.0.0.1", port) {
        return Err("NapCat WebUI 正在启动".into());
    }
    let client = napcat_webui_client()?;
    let credential = napcat_webui_credential(state, &client, port, &token)?;
    let response = client
        .post(format!("http://127.0.0.1:{port}/api{path}"))
        .bearer_auth(&credential)
        .json(&serde_json::json!({}))
        .send()
        .map_err(|e| format!("请求 NapCat WebUI 失败: {e}"))?;
    let payload: Value = response
        .json()
        .map_err(|e| format!("解析 NapCat WebUI 响应失败: {e}"))?;
    let message = payload
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("");
    if message.eq_ignore_ascii_case("Unauthorized") {
        *state
            .napcat_webui_credential
            .lock()
            .map_err(|e| e.to_string())? = None;
        return Err("NapCat WebUI 凭据已过期，正在重新授权".into());
    }
    Ok(payload)
}

fn py_api(args: &[&str]) -> Result<String, String> {
    let output = Command::new(python_exe())
        .arg(project_root().join("scripts").join("desktop_api.py"))
        .args(args)
        .current_dir(project_root())
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .output()
        .map_err(|e| format!("调用 desktop_api 失败: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "desktop_api 失败: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn py_api_json(args: &[&str]) -> Result<Value, String> {
    let stdout = py_api(args)?;
    let line = stdout
        .lines()
        .rev()
        .find(|l| {
            let t = l.trim();
            t.starts_with('{') || t.starts_with('[')
        })
        .ok_or_else(|| format!("desktop_api 未返回 JSON: {stdout}"))?;
    serde_json::from_str(line).map_err(|e| format!("解析 JSON 失败: {e}; line={line}"))
}

fn monitor_lock_path() -> PathBuf {
    project_root().join("data").join("monitor.lock")
}

fn monitor_stop_path() -> PathBuf {
    project_root().join("data").join("monitor.stop")
}

fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    {
        let output = Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH"])
            .output();
        match output {
            Ok(o) => String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()),
            Err(_) => false,
        }
    }
    #[cfg(not(windows))]
    {
        PathBuf::from(format!("/proc/{pid}")).exists()
    }
}

fn read_monitor_lock_pid() -> Option<u32> {
    let path = monitor_lock_path();
    if !path.exists() {
        return None;
    }
    #[cfg(windows)]
    {
        use std::io::Read;
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_SHARE_READ: u32 = 0x1;
        const FILE_SHARE_WRITE: u32 = 0x2;
        let mut f = fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .open(&path)
            .ok()?;
        let mut raw = String::new();
        f.read_to_string(&mut raw).ok()?;
        raw.trim().parse::<u32>().ok()
    }
    #[cfg(not(windows))]
    {
        fs::read_to_string(&path)
            .ok()
            .and_then(|raw| raw.trim().parse::<u32>().ok())
    }
}

fn owned_monitor_child_running(state: &AppState) -> bool {
    state
        .monitor_child
        .lock()
        .ok()
        .map(|mut g| {
            if let Some(child) = g.as_mut() {
                match child.try_wait() {
                    Ok(Some(_)) => {
                        *g = None;
                        false
                    }
                    Ok(None) => true,
                    Err(_) => false,
                }
            } else {
                false
            }
        })
        .unwrap_or(false)
}

/// 监听服务是否在跑：本窗口拉起的子进程，或本机 lock 中的 PID 仍存活。
fn monitor_service_running(state: &AppState) -> bool {
    if owned_monitor_child_running(state) {
        return true;
    }
    match read_monitor_lock_pid() {
        Some(pid) => pid_alive(pid),
        // lock 存在但读不到（被占用）→ 通常表示服务仍在持锁
        None => monitor_lock_path().exists(),
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct StatusInfo {
    napcat_installed: bool,
    napcat_webui_up: bool,
    onebot_ws_up: bool,
    monitor_running: bool,
    qq_mode: String,
    official_qq_running: bool,
    napcat_process_running: bool,
    notification_access: String,
    uia_ready: bool,
}

fn qq_mode_from_settings() -> String {
    "onebot".into()
}

fn process_running(image_name: &str) -> bool {
    let output = Command::new("tasklist")
        .args(["/FI", &format!("IMAGENAME eq {image_name}")])
        .output();
    match output {
        Ok(out) => String::from_utf8_lossy(&out.stdout).contains(image_name),
        Err(_) => false,
    }
}

#[tauri::command]
fn get_status(state: tauri::State<'_, AppState>) -> StatusInfo {
    let (onebot_host, onebot_port) = read_onebot_endpoint();
    StatusInfo {
        napcat_installed: napcat_dir().join("launcher-user.bat").exists()
            || napcat_dir().join("launcher.bat").exists(),
        napcat_webui_up: port_open("127.0.0.1", read_webui_port()),
        onebot_ws_up: port_open(&onebot_host, onebot_port),
        monitor_running: monitor_service_running(&state),
        qq_mode: "onebot".into(),
        official_qq_running: false,
        napcat_process_running: false,
        notification_access: String::new(),
        uia_ready: false,
    }
}

#[tauri::command]
fn start_napcat() -> Result<String, String> {
    let launcher = project_root().join("restart-napcat.bat");
    let fallback = project_root().join("start-napcat.bat");
    let path = if launcher.exists() { launcher } else { fallback };
    if !path.exists() {
        return Err("未找到 start-napcat.bat / restart-napcat.bat".into());
    }
    let mut command = Command::new("cmd");
    command
        .args(["/C", &path.display().to_string()])
        .current_dir(project_root())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    command
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok("已请求启动 NapCat".into())
}

/// 按当前 QQ 模式打开窗口：
/// - onebot：尝试唤起已有 QQ（多为 NapCat Shell 无界面进程，会给出明确提示）
/// - passive：激活或启动系统安装的官方 QQ
#[tauri::command]
fn show_qq_window() -> Result<String, String> {
    #[cfg(not(windows))]
    {
        return Err("当前平台不支持唤起 QQ 窗口".into());
    }
    #[cfg(windows)]
    {
        let mode = qq_mode_from_settings();
        if mode == "passive" {
            return start_or_show_official_qq();
        }
        let script = project_root().join("scripts").join("show_qq_window.ps1");
        if !script.exists() {
            return Err("未找到 scripts/show_qq_window.ps1".into());
        }
        let mut command = Command::new("powershell");
        command
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                &script.display().to_string(),
            ])
            .current_dir(project_root());
        command.creation_flags(0x08000000);
        let output = command
            .output()
            .map_err(|e| format!("唤起 QQ 窗口失败: {e}"))?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        if stdout.contains("activated") {
            Ok("已找到 QQ 进程窗口。注意：NapCat Shell 模式没有可聊天界面，日常聊天请使用手机 QQ".into())
        } else if stdout.contains("tray-only") {
            Err("QQ 进程在跑但无可见主面板。NapCat Shell 本身不提供聊天界面，日常聊天请使用手机 QQ".into())
        } else {
            Err("未找到 QQ 窗口。请先启动 NapCat".into())
        }
    }
}

#[cfg(windows)]
fn start_or_show_official_qq() -> Result<String, String> {
    if process_running("NapCatWinBootMain.exe") {
        return Err("检测到 NapCat 仍在运行。请先关闭 NapCat，再启动官方 QQ，避免重复登录".into());
    }
    let script = project_root().join("scripts").join("show_qq_window.ps1");
    if script.exists() {
        let mut command = Command::new("powershell");
        command
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                &script.display().to_string(),
            ])
            .current_dir(project_root());
        command.creation_flags(0x08000000);
        if let Ok(output) = command.output() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            if stdout.contains("activated") {
                return Ok("已唤起官方 QQ 窗口".into());
            }
            if stdout.contains("tray-only") {
                return Err("官方 QQ 在托盘中，请点击系统托盘的 QQ 图标恢复主面板".into());
            }
        }
    }
    // 启动系统安装的官方 QQ
    let mut command = Command::new("powershell");
    command.args([
        "-NoProfile",
        "-Command",
        r#"
$p = $null
$u = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ' -ErrorAction SilentlyContinue).UninstallString
if ($u) { $p = Join-Path (Split-Path $u) 'QQ.exe' }
if (-not $p -or -not (Test-Path $p)) {
  $cands = @(
    "$env:ProgramFiles\Tencent\QQNT\QQ.exe",
    "${env:ProgramFiles(x86)}\Tencent\QQNT\QQ.exe",
    "$env:LOCALAPPDATA\Programs\Tencent\QQNT\QQ.exe"
  )
  $p = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $p) { Write-Output 'missing'; exit 1 }
Start-Process $p
Write-Output "started:$p"
"#,
    ]);
    command.creation_flags(0x08000000);
    let output = command
        .output()
        .map_err(|e| format!("启动官方 QQ 失败: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if stdout.contains("started:") {
        Ok("已启动官方 QQ".into())
    } else {
        Err("未找到系统安装的官方 QQ，请先安装腾讯 QQ".into())
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct NapcatLoginStatus {
    status: String,
    message: String,
    qr_code_url: String,
}

#[tauri::command]
fn get_napcat_login_status(state: tauri::State<'_, AppState>) -> NapcatLoginStatus {
    let payload = match napcat_webui_post(&state, "/QQLogin/CheckLoginStatus") {
        Ok(payload) => payload,
        Err(message) => {
            return NapcatLoginStatus {
                status: "starting".into(),
                message,
                qr_code_url: String::new(),
            };
        }
    };
    let data = payload.get("data").unwrap_or(&Value::Null);
    if data
        .get("isLogin")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return NapcatLoginStatus {
            status: "authorized".into(),
            message: "QQ 登录授权成功".into(),
            qr_code_url: String::new(),
        };
    }
    let qr_code_url = data
        .get("qrcodeurl")
        .or_else(|| data.get("qrcodeUrl"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let login_error = data
        .get("loginError")
        .and_then(Value::as_str)
        .unwrap_or("");
    NapcatLoginStatus {
        status: if login_error.is_empty() {
            "waiting_scan".into()
        } else {
            "error".into()
        },
        message: if login_error.is_empty() {
            if qr_code_url.is_empty() {
                "正在等待 NapCat 生成登录二维码…".into()
            } else {
                "请使用手机 QQ 扫描二维码授权登录".into()
            }
        } else {
            login_error.to_string()
        },
        qr_code_url,
    }
}

#[tauri::command]
fn refresh_napcat_login_qr(state: tauri::State<'_, AppState>) -> Result<String, String> {
    napcat_webui_post(&state, "/QQLogin/RefreshQRcode")?;
    Ok("已刷新 QQ 登录二维码".into())
}

#[tauri::command]
fn start_monitor(state: tauri::State<'_, AppState>) -> Result<String, String> {
    {
        let guard = state.monitor_child.lock().map_err(|e| e.to_string())?;
        if guard.is_some() {
            return Err("监听服务已在本窗口运行".into());
        }
    }
    if monitor_service_running(&state) {
        return Err("检测到本机已有监听服务在运行（请先点「停止」，或结束多余的 python -m app.main）".into());
    }
    let _ = fs::remove_file(monitor_stop_path());
    let root = project_root();
    let mut child = Command::new(python_exe())
        .arg("-m")
        .arg("app.main")
        .current_dir(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("启动监听失败: {e}"))?;
    std::thread::sleep(Duration::from_millis(400));
    match child.try_wait() {
        Ok(Some(code)) => {
            return Err(format!("监听进程立即退出 code={code:?}（常见原因：已有另一实例占用）"));
        }
        Ok(None) => {}
        Err(e) => return Err(e.to_string()),
    }
    *state.monitor_child.lock().map_err(|e| e.to_string())? = Some(child);
    Ok("监听服务已启动".into())
}

#[tauri::command]
fn stop_monitor(state: tauri::State<'_, AppState>) -> Result<String, String> {
    fs::write(monitor_stop_path(), b"stop")
        .map_err(|e| format!("写入停止请求失败: {e}"))?;
    let mut guard = state.monitor_child.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = guard.take() {
        for _ in 0..200 {
            match child.try_wait() {
                Ok(Some(_)) => {
                    let _ = child.wait();
                    let _ = fs::remove_file(monitor_stop_path());
                    return Ok("监听服务已优雅停止，消息队列已排空".into());
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => break,
            }
        }
        let _ = child.kill();
        let _ = child.wait();
        let _ = fs::remove_file(monitor_stop_path());
        Ok("监听服务停止超时，已强制结束".into())
    } else if let Some(pid) = read_monitor_lock_pid().filter(|p| pid_alive(*p)) {
        for _ in 0..80 {
            std::thread::sleep(Duration::from_millis(250));
            if !pid_alive(pid) {
                let _ = fs::remove_file(monitor_stop_path());
                return Ok(format!("外部监听服务已优雅停止 PID={pid}"));
            }
        }
        let status = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/F"])
            .status()
            .map_err(|e| format!("结束监听进程失败: {e}"))?;
        if status.success() {
            let _ = fs::remove_file(monitor_stop_path());
            Ok(format!("已结束外部监听进程 PID={pid}"))
        } else {
            Err(format!("结束外部监听进程失败 PID={pid}"))
        }
    } else {
        Err("监听服务未在运行".into())
    }
}

#[tauri::command]
fn api_list_groups(sort: String, q: String) -> Result<Value, String> {
    py_api_json(&["list-groups", "--sort", &sort, "--q", &q])
}

#[tauri::command]
fn api_recent_messages(group_id: Option<String>, limit: i64) -> Result<Value, String> {
    // 热路径：Rust 直读 SQLite，避免每次轮询拉起 Python
    recent_messages_from_db(group_id, limit, false)
}

#[tauri::command]
fn api_recent_live_messages(group_id: Option<String>, limit: i64) -> Result<Value, String> {
    // 实时面板使用独立滚动表，可覆盖未单独启用分析/持久化的群。
    recent_messages_from_db(group_id, limit, true)
}

#[tauri::command]
fn api_live_messages_since(group_id: String, after_id: i64, limit: i64) -> Result<Value, String> {
    use rusqlite::Connection;

    let path = messages_db_path();
    if !path.exists() || group_id.is_empty() {
        return Ok(serde_json::json!({"messages": [], "cursor": after_id.max(0)}));
    }
    let conn = Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
            | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| format!("打开 messages.db 失败: {e}"))?;
    conn.busy_timeout(std::time::Duration::from_millis(250))
        .map_err(|e| e.to_string())?;
    if !table_exists(&conn, "live_messages")? {
        return Ok(serde_json::json!({"messages": [], "cursor": after_id.max(0)}));
    }
    let lim = limit.clamp(1, 200);
    let mut stmt = conn
        .prepare(
            "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                    COALESCE(content,''), event_time, COALESCE(created_at,'')
             FROM live_messages
             WHERE group_id=? AND id>?
             ORDER BY id ASC
             LIMIT ?",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params![group_id, after_id.max(0), lim], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, Option<i64>>(5)?,
                r.get::<_, String>(6)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    let cursor = rows
        .iter()
        .map(|r| r.0)
        .max()
        .unwrap_or_else(|| after_id.max(0));
    let messages = rows
        .into_iter()
        .rev()
        .map(|r| {
            serde_json::json!({
                "id": -r.0.abs(),
                "groupId": r.1,
                "groupName": "",
                "userId": r.2,
                "senderName": r.3,
                "content": r.4,
                "eventTime": r.5,
                "createdAt": r.6,
                "liveCursor": r.0,
            })
        })
        .collect::<Vec<_>>();
    Ok(serde_json::json!({"messages": messages, "cursor": cursor}))
}

fn messages_db_path() -> PathBuf {
    project_root().join("data").join("messages.db")
}

fn blocked_group_ids_from_disk() -> std::collections::HashSet<String> {
    let dir = project_root().join("data").join("group_configs");
    let mut out = std::collections::HashSet::new();
    let entries = match fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return out,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(v) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        if v.get("blocked").and_then(|b| b.as_bool()).unwrap_or(false) {
            if let Some(gid) = v.get("group_id").and_then(|x| x.as_str()) {
                out.insert(gid.to_string());
            }
        }
    }
    out
}

fn table_exists(conn: &rusqlite::Connection, table: &str) -> Result<bool, String> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        [table],
        |r| r.get(0),
    )
    .map_err(|e| e.to_string())
}

#[derive(Clone)]
struct DbMessageRow {
    id: i64,
    group_id: String,
    user_id: String,
    sender_name: String,
    content: String,
    message_id: String,
    event_time: Option<i64>,
    created_at: String,
    from_live: bool,
}

fn query_message_table(
    conn: &rusqlite::Connection,
    table: &str,
    group_id: Option<&str>,
    limit: i64,
    from_live: bool,
) -> Result<Vec<DbMessageRow>, String> {
    if !table_exists(conn, table)? {
        return Ok(vec![]);
    }
    let sql = if group_id.is_some() {
        format!(
            "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                    COALESCE(content,''), COALESCE(message_id,''), event_time,
                    COALESCE(created_at,'')
             FROM {table} WHERE group_id=? ORDER BY id DESC LIMIT ?"
        )
    } else {
        format!(
            "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                    COALESCE(content,''), COALESCE(message_id,''), event_time,
                    COALESCE(created_at,'')
             FROM {table} ORDER BY id DESC LIMIT ?"
        )
    };
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let map_row = |r: &rusqlite::Row<'_>| -> rusqlite::Result<DbMessageRow> {
        Ok(DbMessageRow {
            id: r.get(0)?,
            group_id: r.get(1)?,
            user_id: r.get(2)?,
            sender_name: r.get(3)?,
            content: r.get(4)?,
            message_id: r.get(5)?,
            event_time: r.get(6)?,
            created_at: r.get(7)?,
            from_live,
        })
    };
    let rows = if let Some(gid) = group_id {
        stmt.query_map(rusqlite::params![gid, limit], map_row)
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?
    } else {
        stmt.query_map(rusqlite::params![limit], map_row)
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?
    };
    Ok(rows)
}

fn merge_message_rows(mut rows: Vec<DbMessageRow>, limit: i64) -> Vec<DbMessageRow> {
    // 先按时间/id 新到旧，再按 message_id（或内容指纹）去重
    rows.sort_by(|a, b| {
        let ta = a.event_time.unwrap_or(0);
        let tb = b.event_time.unwrap_or(0);
        tb.cmp(&ta)
            .then_with(|| b.created_at.cmp(&a.created_at))
            .then_with(|| b.id.cmp(&a.id))
    });
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for row in rows {
        let key = if !row.message_id.is_empty() {
            format!("mid:{}:{}", row.group_id, row.message_id)
        } else {
            format!(
                "fp:{}:{}:{}:{}",
                row.group_id,
                row.sender_name,
                row.event_time.unwrap_or(0),
                row.content
            )
        };
        if !seen.insert(key) {
            continue;
        }
        out.push(row);
        if out.len() as i64 >= limit {
            break;
        }
    }
    out
}

fn recent_messages_from_db(
    group_id: Option<String>,
    limit: i64,
    live: bool,
) -> Result<Value, String> {
    use rusqlite::Connection;

    let path = messages_db_path();
    if !path.exists() {
        return Ok(Value::Array(vec![]));
    }
    let lim = limit.clamp(1, 200);
    let blocked = blocked_group_ids_from_disk();
    let conn = Connection::open(&path).map_err(|e| format!("打开 messages.db 失败: {e}"))?;

    let gid = group_id.filter(|s| !s.is_empty());
    if let Some(ref id) = gid {
        if blocked.contains(id) {
            return Ok(Value::Array(vec![]));
        }
    }

    let fetch_lim = if live {
        (lim.saturating_mul(3)).clamp(lim, 500)
    } else if gid.is_none() {
        (lim.saturating_mul(5)).clamp(lim, 500)
    } else {
        lim
    };

    let mut rows = if live {
        // 实时面板：合并滚动表 + 持久化消息，避免历史/补拉只落 messages 时右侧空白
        let mut merged = query_message_table(
            &conn,
            "live_messages",
            gid.as_deref(),
            fetch_lim,
            true,
        )?;
        merged.extend(query_message_table(
            &conn,
            "messages",
            gid.as_deref(),
            fetch_lim,
            false,
        )?);
        merge_message_rows(merged, lim)
    } else {
        query_message_table(&conn, "messages", gid.as_deref(), fetch_lim, false)?
    };

    if gid.is_none() {
        rows.retain(|r| !blocked.contains(&r.group_id));
        if rows.len() as i64 > lim {
            rows.truncate(lim as usize);
        }
    }

    let rows_out: Vec<Value> = rows
        .into_iter()
        .map(|r| {
            // live 表与 messages 表 id 可能冲突；给 live 行加偏移，保证前端指纹稳定
            let live_cursor = if r.from_live {
                Value::from(r.id)
            } else {
                Value::Null
            };
            let id = if r.from_live {
                -(r.id.abs())
            } else {
                r.id
            };
            serde_json::json!({
                "id": id,
                "groupId": r.group_id,
                "groupName": "",
                "userId": r.user_id,
                "senderName": r.sender_name,
                "content": r.content,
                "eventTime": r.event_time,
                "createdAt": r.created_at,
                "liveCursor": live_cursor,
            })
        })
        .collect();
    Ok(Value::Array(rows_out))
}

#[tauri::command]
fn api_messages_in_window(
    group_id: String,
    start_ts: i64,
    end_ts: i64,
    limit: i64,
) -> Result<Value, String> {
    messages_in_window_from_db(group_id, start_ts, end_ts, limit)
}

fn messages_in_window_from_db(
    group_id: String,
    start_ts: i64,
    end_ts: i64,
    limit: i64,
) -> Result<Value, String> {
    use rusqlite::Connection;

    let path = messages_db_path();
    if !path.exists() || group_id.is_empty() {
        return Ok(Value::Array(vec![]));
    }
    if blocked_group_ids_from_disk().contains(&group_id) {
        return Ok(Value::Array(vec![]));
    }
    let lim = limit.clamp(1, 800);
    let start = start_ts.min(end_ts);
    let end = start_ts.max(end_ts);
    let conn = Connection::open(&path).map_err(|e| format!("打开 messages.db 失败: {e}"))?;

    let mut stmt = conn
        .prepare(
            "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                    COALESCE(content,''), event_time, COALESCE(created_at,'')
             FROM messages
             WHERE group_id=?
               AND COALESCE(event_time, 0) >= ?
               AND COALESCE(event_time, 0) <= ?
             ORDER BY COALESCE(event_time, 0) ASC, id ASC
             LIMIT ?",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params![group_id, start, end, lim], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, Option<i64>>(5)?,
                r.get::<_, String>(6)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    let mut rows_out: Vec<Value> = Vec::new();
    for row in rows {
        let (id, gid, user_id, sender_name, content, event_time, created_at) =
            row.map_err(|e| e.to_string())?;
        rows_out.push(serde_json::json!({
            "id": id,
            "groupId": gid,
            "groupName": "",
            "userId": user_id,
            "senderName": sender_name,
            "content": content,
            "eventTime": event_time,
            "createdAt": created_at,
        }));
    }
    Ok(Value::Array(rows_out))
}

#[tauri::command]
fn api_get_settings() -> Result<Value, String> {
    let v = py_api_json(&["get-settings"])?;
    // 转成前端 camelCase
    Ok(settings_to_camel(v))
}

#[tauri::command]
fn api_save_settings(settings: Value) -> Result<Value, String> {
    let raw = settings.to_string();
    py_api_json(&["save-settings", "--json", &raw])
}

#[tauri::command]
fn api_get_group(group_id: String) -> Result<Value, String> {
    let v = py_api_json(&["get-group", "--group-id", &group_id])?;
    Ok(group_to_camel(v))
}

#[tauri::command]
fn api_save_group(config: Value) -> Result<Value, String> {
    let raw = config.to_string();
    py_api_json(&["save-group", "--json", &raw])
}

#[tauri::command]
async fn api_run_llm(group_id: String) -> Result<Value, String> {
    // Python 分析可能持续数分钟，必须放到阻塞线程，避免卡住 Tauri 主线程。
    let v = tauri::async_runtime::spawn_blocking(move || {
        py_api_json(&["run-llm", "--group-id", &group_id])
    })
    .await
    .map_err(|e| format!("LLM 分析后台任务异常: {e}"))??;
    // normalize
    let mut out = v.clone();
    if let Some(obj) = out.as_object_mut() {
        if let Some(risk) = obj.remove("risk_max") {
            obj.insert("riskMax".into(), risk);
        }
        if let Some(mc) = obj.remove("msg_count") {
            obj.insert("msgCount".into(), mc);
        }
        if let Some(job) = obj.remove("job_id") {
            obj.insert("jobId".into(), job);
        }
        if let Some(tt) = obj.remove("total_tokens") {
            obj.insert("totalTokens".into(), tt);
        }
        if let Some(pt) = obj.remove("prompt_tokens") {
            obj.insert("promptTokens".into(), pt);
        }
        if let Some(ct) = obj.remove("completion_tokens") {
            obj.insert("completionTokens".into(), ct);
        }
        if let Some(tu) = obj.remove("token_usage") {
            // keep nested snake or map to camel briefly
            if let Some(tu_obj) = tu.as_object() {
                let mut mapped = serde_json::Map::new();
                mapped.insert(
                    "promptTokens".into(),
                    tu_obj
                        .get("prompt_tokens")
                        .cloned()
                        .unwrap_or(Value::Number(0.into())),
                );
                mapped.insert(
                    "completionTokens".into(),
                    tu_obj
                        .get("completion_tokens")
                        .cloned()
                        .unwrap_or(Value::Number(0.into())),
                );
                mapped.insert(
                    "totalTokens".into(),
                    tu_obj
                        .get("total_tokens")
                        .cloned()
                        .unwrap_or(Value::Number(0.into())),
                );
                obj.insert("tokenUsage".into(), Value::Object(mapped));
            } else {
                obj.insert("tokenUsage".into(), tu);
            }
        }
    }
    Ok(out)
}

#[tauri::command]
fn api_pull_history(group_id: String, count: i64) -> Result<Value, String> {
    let cnt = count.to_string();
    py_api_json(&["pull-history", "--group-id", &group_id, "--count", &cnt])
}

#[tauri::command]
fn api_list_reports(
    group_id: Option<String>,
    limit: i64,
    favorites_only: Option<bool>,
) -> Result<Value, String> {
    let lim = limit.to_string();
    if favorites_only.unwrap_or(false) {
        return py_api_json(&["list-reports", "--limit", &lim, "--favorites-only"]);
    }
    if let Some(gid) = group_id.filter(|s| !s.is_empty()) {
        py_api_json(&["list-reports", "--group-id", &gid, "--limit", &lim])
    } else {
        py_api_json(&["list-reports", "--limit", &lim])
    }
}

#[tauri::command]
fn api_set_report_favorite(report_id: i64, favorited: bool) -> Result<Value, String> {
    let rid = report_id.to_string();
    let flag = if favorited { "1" } else { "0" };
    py_api_json(&[
        "set-report-favorite",
        "--report-id",
        &rid,
        "--favorited",
        flag,
    ])
}

#[tauri::command]
fn api_report_favorite_messages(report_id: i64) -> Result<Value, String> {
    let rid = report_id.to_string();
    py_api_json(&["report-favorite-messages", "--report-id", &rid])
}

#[tauri::command]
fn api_github_issue_preview(report_id: i64) -> Result<Value, String> {
    let rid = report_id.to_string();
    let mut preview = py_api_json(&["github-issue-preview", "--report-id", &rid])?;
    let repo = current_github_repo()?;
    if let Some(obj) = preview.as_object_mut() {
        obj.insert("repo".into(), Value::String(repo));
    }
    Ok(preview)
}

#[tauri::command]
fn api_report_github_issue(report_id: i64) -> Result<Value, String> {
    let rid = report_id.to_string();
    let preview = py_api_json(&["github-issue-preview", "--report-id", &rid])?;
    if let Some(url) = preview
        .get("issueUrl")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Ok(serde_json::json!({"ok": true, "issueUrl": url, "existing": true}));
    }
    let repo = current_github_repo()?;
    let title = preview
        .get("title")
        .and_then(Value::as_str)
        .ok_or_else(|| "Issue 标题缺失".to_string())?;
    let body = preview
        .get("body")
        .and_then(Value::as_str)
        .ok_or_else(|| "Issue 正文缺失".to_string())?;

    let mut child = Command::new("gh")
        .args([
            "issue",
            "create",
            "--repo",
            &repo,
            "--title",
            title,
            "--body-file",
            "-",
        ])
        .current_dir(project_root())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("无法启动 gh，请确认 GitHub CLI 已安装: {e}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(body.as_bytes())
            .map_err(|e| format!("写入 Issue 正文失败: {e}"))?;
    }
    let output = child
        .wait_with_output()
        .map_err(|e| format!("等待 gh 失败: {e}"))?;
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!("创建 GitHub Issue 失败: {}", err.trim()));
    }
    let issue_url = String::from_utf8_lossy(&output.stdout)
        .lines()
        .find(|line| line.trim().starts_with("https://github.com/"))
        .map(str::trim)
        .ok_or_else(|| "gh 未返回 Issue URL".to_string())?
        .to_string();
    py_api_json(&[
        "set-report-issue",
        "--report-id",
        &rid,
        "--issue-url",
        &issue_url,
    ])?;
    Ok(serde_json::json!({
        "ok": true,
        "repo": repo,
        "issueUrl": issue_url,
        "existing": false
    }))
}

#[tauri::command]
fn api_token_stats(group_id: Option<String>) -> Result<Value, String> {
    let v = if let Some(gid) = group_id.filter(|s| !s.is_empty()) {
        py_api_json(&["token-stats", "--group-id", &gid])?
    } else {
        py_api_json(&["token-stats"])?
    };
    Ok(v)
}

fn settings_to_camel(v: Value) -> Value {
    let mut obj = v.as_object().cloned().unwrap_or_default();
    let mut out = serde_json::Map::new();
    out.insert(
        "onebotWsUrl".into(),
        obj.remove("onebot_ws_url").unwrap_or(Value::String(String::new())),
    );
    out.insert(
        "onebotAccessToken".into(),
        obj.remove("onebot_access_token")
            .unwrap_or(Value::String(String::new())),
    );
    let channels = obj
        .remove("channels")
        .unwrap_or(Value::Object(Default::default()));
    let mut ch = channels.as_object().cloned().unwrap_or_default();
    let qq = ch.remove("qq").unwrap_or(Value::Object(Default::default()));
    let mut qq_o = qq.as_object().cloned().unwrap_or_default();
    let wx = ch
        .remove("wechat")
        .unwrap_or(Value::Object(Default::default()));
    let mut wx_o = wx.as_object().cloned().unwrap_or_default();
    let tg = ch
        .remove("telegram")
        .unwrap_or(Value::Object(Default::default()));
    let mut tg_o = tg.as_object().cloned().unwrap_or_default();
    out.insert(
        "channels".into(),
        serde_json::json!({
            "qq": {
                "bound": qq_o.remove("bound").unwrap_or(Value::Bool(false)),
                "label": qq_o.remove("label").unwrap_or(Value::String(String::new())),
                "lastError": qq_o.remove("last_error").unwrap_or(Value::String(String::new())),
                "mode": qq_o.remove("mode").unwrap_or(Value::String("onebot".into())),
                "notificationAccess": qq_o
                    .remove("notification_access")
                    .unwrap_or(Value::String(String::new())),
                "uiaReady": qq_o.remove("uia_ready").unwrap_or(Value::Bool(false)),
                "pollSeconds": qq_o.remove("poll_seconds").unwrap_or(Value::from(1.5)),
                "groupNameMap": qq_o
                    .remove("group_name_map")
                    .unwrap_or(Value::Object(Default::default())),
            },
            "wechat": {
                "bound": wx_o.remove("bound").unwrap_or(Value::Bool(false)),
                "label": wx_o.remove("label").unwrap_or(Value::String(String::new())),
                "lastError": wx_o.remove("last_error").unwrap_or(Value::String(String::new())),
                "dataDir": wx_o.remove("data_dir").unwrap_or(Value::String(String::new())),
                "decryptedDir": wx_o.remove("decrypted_dir").unwrap_or(Value::String(String::new())),
                "keysPath": wx_o.remove("keys_path").unwrap_or(Value::String(String::new())),
                "pollSeconds": wx_o.remove("poll_seconds").unwrap_or(Value::from(1.0)),
            },
            "telegram": {
                "bound": tg_o.remove("bound").unwrap_or(Value::Bool(false)),
                "label": tg_o.remove("label").unwrap_or(Value::String(String::new())),
                "lastError": tg_o.remove("last_error").unwrap_or(Value::String(String::new())),
                "apiId": tg_o.remove("api_id").unwrap_or(Value::from(0)),
                "apiHash": tg_o.remove("api_hash").unwrap_or(Value::String(String::new())),
                "botToken": tg_o.remove("bot_token").unwrap_or(Value::String(String::new())),
                "pollTimeout": tg_o.remove("poll_timeout").unwrap_or(Value::from(25)),
            },
        }),
    );
    let llm = obj.remove("llm").unwrap_or(Value::Object(Default::default()));
    let mut llm_obj = llm.as_object().cloned().unwrap_or_default();
    let providers = llm_obj
        .remove("providers")
        .and_then(|x| x.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .map(|p| {
            let mut po = p.as_object().cloned().unwrap_or_default();
            serde_json::json!({
                "id": po.remove("id").unwrap_or(Value::Null),
                "name": po.remove("name").unwrap_or(Value::Null),
                "type": po.remove("type").unwrap_or(Value::Null),
                "baseUrl": po.remove("base_url").unwrap_or(Value::String(String::new())),
                "apiKey": po.remove("api_key").unwrap_or(Value::String(String::new())),
                "defaultModel": po.remove("default_model").unwrap_or(Value::String(String::new())),
            })
        })
        .collect::<Vec<_>>();
    out.insert(
        "llm".into(),
        serde_json::json!({
            "activeProviderId": llm_obj.remove("active_provider_id").unwrap_or(Value::String(String::new())),
            "defaultImageModel": llm_obj.remove("default_image_model").unwrap_or(Value::String(String::new())),
            "defaultPrompt": llm_obj.remove("default_prompt").unwrap_or(Value::String(String::new())),
            "defaultEveryMinutes": llm_obj.remove("default_every_minutes").unwrap_or(Value::Number(60.into())),
            "defaultWindowMinutes": llm_obj.remove("default_window_minutes").unwrap_or(Value::Number(60.into())),
            "defaultMinMessages": llm_obj.remove("default_min_messages").unwrap_or(Value::Number(8.into())),
            "reportKeepLimit": llm_obj.remove("report_keep_limit").unwrap_or(Value::Number(100.into())),
            "providers": providers,
        }),
    );
    let ui = obj.remove("ui").unwrap_or(Value::Object(Default::default()));
    let mut ui_obj = ui.as_object().cloned().unwrap_or_default();
    out.insert(
        "ui".into(),
        serde_json::json!({
            "theme": ui_obj
                .remove("theme")
                .unwrap_or(Value::String("midnight".into())),
        }),
    );
    Value::Object(out)
}

fn group_to_camel(v: Value) -> Value {
    let mut o = v.as_object().cloned().unwrap_or_default();
    let basic = o.remove("basic").unwrap_or(Value::Object(Default::default()));
    let bo = basic.as_object().cloned().unwrap_or_default();
    let kw = o
        .remove("keyword_monitor")
        .unwrap_or(Value::Object(Default::default()));
    let ko = kw.as_object().cloned().unwrap_or_default();
    let llm = o
        .remove("llm_monitor")
        .unwrap_or(Value::Object(Default::default()));
    let lo = llm.as_object().cloned().unwrap_or_default();
    serde_json::json!({
        "groupId": o.get("group_id").cloned().unwrap_or(Value::Null),
        "groupName": o.get("group_name").cloned().unwrap_or(Value::String(String::new())),
        "channel": o.get("channel").cloned().unwrap_or(Value::String("qq".into())),
        "enabled": o.get("enabled").cloned().unwrap_or(Value::Bool(false)),
        "blocked": o.get("blocked").cloned().unwrap_or(Value::Bool(false)),
        "basic": {
            "logAll": bo.get("log_all").cloned().unwrap_or(Value::Bool(true)),
            "storageEnabled": bo.get("storage_enabled").cloned().unwrap_or(Value::Bool(true)),
        },
        "keywordMonitor": {
            "enabled": ko.get("enabled").cloned().unwrap_or(Value::Bool(true)),
            "keywords": ko.get("keywords").cloned().unwrap_or(Value::Array(vec![])),
            "alertEnabled": ko.get("alert_enabled").cloned().unwrap_or(Value::Bool(false)),
            "webhookUrl": ko.get("webhook_url").cloned().unwrap_or(Value::String(String::new())),
        },
        "llmMonitor": {
            "enabled": lo.get("enabled").cloned().unwrap_or(Value::Bool(false)),
            "useGlobalDefaults": lo
                .get("use_global_defaults")
                .cloned()
                .unwrap_or(Value::Bool(true)),
            "textEnabled": lo.get("text_enabled").cloned().unwrap_or(Value::Bool(true)),
            "providerId": lo.get("provider_id").cloned().unwrap_or(Value::String(String::new())),
            "model": lo.get("model").cloned().unwrap_or(Value::String(String::new())),
            "prompt": lo.get("prompt").cloned().unwrap_or(Value::String(String::new())),
            "imageEnabled": lo.get("image_enabled").cloned().unwrap_or(Value::Bool(true)),
            "imageSameAsText": lo
                .get("image_same_as_text")
                .cloned()
                .unwrap_or(Value::Bool(true)),
            "imageProviderId": lo
                .get("image_provider_id")
                .cloned()
                .unwrap_or(Value::String(String::new())),
            "imageModel": lo
                .get("image_model")
                .cloned()
                .unwrap_or(Value::String(String::new())),
            "everyMinutes": lo.get("every_minutes").cloned().unwrap_or(Value::from(60)),
            "windowMinutes": lo.get("window_minutes").cloned().unwrap_or(Value::from(60)),
            "minMessages": lo.get("min_messages").cloned().unwrap_or(Value::from(8)),
        }
    })
}

fn build_onebot_ws_url(ws_url: &str, token: &str) -> String {
    let mut url = ws_url.trim().to_string();
    if url.is_empty() {
        url = "ws://127.0.0.1:3001".into();
    }
    while url.ends_with('/') {
        url.pop();
    }
    if !token.is_empty() && !url.contains("access_token=") {
        let sep = if url.contains('?') { '&' } else { '?' };
        url = format!("{url}{sep}access_token={token}");
    }
    url
}

#[tauri::command]
fn api_fetch_models(provider_id: String) -> Result<Value, String> {
    if provider_id.is_empty() {
        py_api_json(&["fetch-models"])
    } else {
        py_api_json(&["fetch-models", "--provider-id", &provider_id])
    }
}

#[tauri::command]
fn api_test_provider(provider_id: String, model: String) -> Result<Value, String> {
    let mut args: Vec<String> = vec!["test-provider".into()];
    if !provider_id.is_empty() {
        args.push("--provider-id".into());
        args.push(provider_id);
    }
    if !model.is_empty() {
        args.push("--model".into());
        args.push(model);
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    py_api_json(&refs)
}

#[tauri::command]
fn api_test_onebot() -> Result<Value, String> {
    py_api_json(&["test-onebot"])
}

#[tauri::command]
fn api_bind_qq(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    let v = py_api_json(&["bind-qq", "--json", &raw])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_detect_qq_passive() -> Result<Value, String> {
    let v = py_api_json(&["detect-qq-passive"])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_set_qq_group_map(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    let v = py_api_json(&["set-qq-group-map", "--json", &raw])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_bind_telegram(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    let v = py_api_json(&["bind-telegram", "--json", &raw])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_bind_wechat(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    let v = py_api_json(&["bind-wechat", "--json", &raw])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_unbind_channel(channel: String) -> Result<Value, String> {
    let v = py_api_json(&["unbind-channel", "--channel", &channel])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_test_telegram() -> Result<Value, String> {
    py_api_json(&["test-telegram"])
}

#[tauri::command]
fn api_telegram_qr_start(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    py_api_json(&["telegram-qr-start", "--json", &raw])
}

#[tauri::command]
fn api_telegram_qr_status() -> Result<Value, String> {
    py_api_json(&["telegram-qr-status"])
}

#[tauri::command]
fn api_telegram_qr_cancel() -> Result<Value, String> {
    py_api_json(&["telegram-qr-cancel"])
}

#[tauri::command]
fn api_telegram_qr_2fa(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    py_api_json(&["telegram-qr-2fa", "--json", &raw])
}

#[tauri::command]
fn api_telegram_detect() -> Result<Value, String> {
    py_api_json(&["telegram-detect"])
}

#[tauri::command]
fn api_wechat_detect() -> Result<Value, String> {
    py_api_json(&["wechat-detect"])
}

#[tauri::command]
fn api_wechat_scan_keys() -> Result<Value, String> {
    let v = py_api_json(&["wechat-scan-keys"])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_wechat_import_keys(payload: Value) -> Result<Value, String> {
    let raw = payload.to_string();
    let v = py_api_json(&["wechat-import-keys", "--json", &raw])?;
    Ok(channels_result_to_camel(v))
}

#[tauri::command]
fn api_pull_telegram_groups() -> Result<Value, String> {
    py_api_json(&["pull-telegram-groups"])
}

#[tauri::command]
fn api_pull_wechat_groups() -> Result<Value, String> {
    let v = py_api_json(&["pull-wechat-groups"])?;
    Ok(channels_result_to_camel(v))
}

fn channels_result_to_camel(v: Value) -> Value {
    let mut obj = v.as_object().cloned().unwrap_or_default();
    if let Some(ch) = obj.remove("channels") {
        let wrapped = serde_json::json!({ "channels": ch });
        if let Value::Object(mut camel) = settings_to_camel(wrapped) {
            if let Some(channels) = camel.remove("channels") {
                obj.insert("channels".into(), channels);
            }
        }
    }
    Value::Object(obj)
}

#[tauri::command]
fn pull_onebot_groups() -> Result<String, String> {
    // 用 Python list_groups script style via websockets is already proven; call a tiny helper
    let settings = py_api_json(&["get-settings"])?;
    let ws = settings
        .get("onebot_ws_url")
        .and_then(|x| x.as_str())
        .unwrap_or("ws://127.0.0.1:3001");
    let token = settings
        .get("onebot_access_token")
        .and_then(|x| x.as_str())
        .unwrap_or("");
    // Prefer Python pull via list_groups.py --json then cache
    let output = Command::new(python_exe())
        .arg(project_root().join("scripts").join("list_groups.py"))
        .arg("--json")
        .current_dir(project_root())
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("ONEBOT_WS_URL", ws)
        .env("ONEBOT_ACCESS_TOKEN", token)
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "拉取失败: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout
        .lines()
        .rev()
        .find(|l| l.trim().starts_with('{'))
        .ok_or_else(|| "未返回群列表 JSON".to_string())?;
    // list_groups --json returns login+groups; wrap for cache
    let parsed: Value = serde_json::from_str(line).map_err(|e| e.to_string())?;
    let status = parsed.get("status").and_then(Value::as_str).unwrap_or("");
    let retcode = parsed.get("retcode").and_then(Value::as_i64).unwrap_or(-1);
    if status != "ok" || retcode != 0 {
        return Err(format!(
            "OneBot get_group_list 失败: status={status}, retcode={retcode}"
        ));
    }
    let groups = parsed
        .get("groups")
        .and_then(Value::as_array)
        .ok_or_else(|| "OneBot 群列表格式错误".to_string())?;
    let cache = serde_json::json!({
        "groups": groups,
        "login": parsed.get("login").cloned().unwrap_or(Value::Null),
    });
    let raw = cache.to_string();
    py_api_json(&["cache-groups", "--json", &raw])?;
    let n = cache
        .get("groups")
        .and_then(|x| x.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    let group_ids = groups
        .iter()
        .filter_map(|g| {
            g.get("group_id").and_then(|v| {
                v.as_str()
                    .map(ToString::to_string)
                    .or_else(|| v.as_i64().map(|n| n.to_string()))
                    .or_else(|| v.as_u64().map(|n| n.to_string()))
            })
        })
        .collect::<Vec<_>>();
    let recent_raw = serde_json::json!({"groupIds": group_ids}).to_string();
    let count = "10".to_string();
    let recent = py_api_json(&[
        "sync-group-recents",
        "--json",
        &recent_raw,
        "--count",
        &count,
    ]);
    let _ = build_onebot_ws_url(ws, token);
    match recent {
        Ok(stats) => {
            let succeeded = stats.get("succeeded").and_then(Value::as_u64).unwrap_or(0);
            let failed = stats.get("failed").and_then(Value::as_u64).unwrap_or(0);
            let fetched = stats.get("fetched").and_then(Value::as_u64).unwrap_or(0);
            Ok(format!(
                "已从 OneBot 拉取 {n} 个群；近期消息同步成功 {succeeded} 群、失败 {failed} 群，共 {fetched} 条"
            ))
        }
        Err(err) => Ok(format!(
            "已从 OneBot 拉取 {n} 个群；近期消息时间同步失败：{err}"
        )),
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct FetchedImage {
    data_url: String,
    mime: String,
    bytes_len: usize,
}

#[tauri::command]
fn fetch_image_data_url(url: String) -> Result<FetchedImage, String> {
    let url = url.trim().to_string();

    // 本地媒体：gmm-media:media/... 或 media/...
    let local_rel = if let Some(rest) = url.strip_prefix("gmm-media:") {
        Some(rest.trim_start_matches('/').to_string())
    } else if url.starts_with("media/") {
        Some(url.clone())
    } else {
        None
    };

    if let Some(rel) = local_rel {
        let path = project_root().join("data").join(&rel);
        // 防路径穿越：必须落在 data/media 下
        let media_root = project_root().join("data").join("media");
        let canon = path
            .canonicalize()
            .map_err(|e| format!("本地图片不存在: {rel} ({e})"))?;
        let media_canon = media_root
            .canonicalize()
            .unwrap_or(media_root.clone());
        if !canon.starts_with(&media_canon) {
            return Err("非法本地图片路径".into());
        }
        let bytes = fs::read(&canon).map_err(|e| format!("读取本地图片失败: {e}"))?;
        if bytes.is_empty() {
            return Err("图片内容为空".into());
        }
        if bytes.len() > 25 * 1024 * 1024 {
            return Err("图片过大（超过 25MB）".into());
        }
        let mime = match canon
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_lowercase()
            .as_str()
        {
            "png" => "image/png",
            "gif" => "image/gif",
            "webp" => "image/webp",
            "bmp" => "image/bmp",
            _ => "image/jpeg",
        }
        .to_string();
        let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        return Ok(FetchedImage {
            data_url: format!("data:{mime};base64,{b64}"),
            mime,
            bytes_len: bytes.len(),
        });
    }

    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("仅支持本地媒体或 http/https 图片地址".into());
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(45))
        .user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
             (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("创建 HTTP 客户端失败: {e}"))?;
    let mut req = client.get(&url);
    let lower = url.to_lowercase();
    if lower.contains("qpic.cn") || lower.contains("qq.com") || lower.contains("gtimg.cn") {
        req = req.header("Referer", "https://web.qphoto.qq.com/");
    }
    let resp = req.send().map_err(|e| format!("下载图片失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "下载图片失败: HTTP {}（远程图床可能已过期；新消息会在落库时保存到本地）",
            resp.status()
        ));
    }
    let mime = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("image/jpeg")
        .split(';')
        .next()
        .unwrap_or("image/jpeg")
        .trim()
        .to_string();
    if !mime.starts_with("image/") && mime != "application/octet-stream" {
        return Err(format!("不是图片类型: {mime}"));
    }
    let bytes = resp
        .bytes()
        .map_err(|e| format!("读取图片内容失败: {e}"))?;
    if bytes.is_empty() {
        return Err("图片内容为空".into());
    }
    if bytes.len() > 25 * 1024 * 1024 {
        return Err("图片过大（超过 25MB）".into());
    }
    let mime = if mime == "application/octet-stream" {
        "image/jpeg".to_string()
    } else {
        mime
    };
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    Ok(FetchedImage {
        data_url: format!("data:{mime};base64,{b64}"),
        mime,
        bytes_len: bytes.len(),
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            monitor_child: Mutex::new(None),
            napcat_webui_credential: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            start_napcat,
            show_qq_window,
            get_napcat_login_status,
            refresh_napcat_login_qr,
            start_monitor,
            stop_monitor,
            api_list_groups,
            api_recent_messages,
            api_recent_live_messages,
            api_live_messages_since,
            api_messages_in_window,
            api_get_settings,
            api_save_settings,
            api_get_group,
            api_save_group,
            api_run_llm,
            api_pull_history,
            api_list_reports,
            api_set_report_favorite,
            api_report_favorite_messages,
            api_github_issue_preview,
            api_report_github_issue,
            api_token_stats,
            api_fetch_models,
            api_test_provider,
            api_test_onebot,
            api_bind_qq,
            api_detect_qq_passive,
            api_set_qq_group_map,
            api_bind_telegram,
            api_bind_wechat,
            api_unbind_channel,
            api_test_telegram,
            api_telegram_qr_start,
            api_telegram_qr_status,
            api_telegram_qr_cancel,
            api_telegram_qr_2fa,
            api_telegram_detect,
            api_wechat_detect,
            api_wechat_scan_keys,
            api_wechat_import_keys,
            api_pull_telegram_groups,
            api_pull_wechat_groups,
            pull_onebot_groups,
            fetch_image_data_url
        ])
        .setup(|_| {
            let _ = fs::create_dir_all(project_root().join("data"));
            let _ = fs::create_dir_all(project_root().join("data").join("group_configs"));
            let _ = fs::create_dir_all(project_root().join("data").join("media"));
            let _ = fs::create_dir_all(project_root().join("logs"));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
