use std::fs;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use base64::{engine::general_purpose::STANDARD as B64, Engine};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

struct AppState {
    monitor_child: Mutex<Option<Child>>,
}

fn project_root() -> PathBuf {
    // desktop/src-tauri -> desktop -> project root
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // desktop
    p.pop(); // project root
    p
}

fn napcat_dir() -> PathBuf {
    project_root().join("third_party").join("NapCatShell")
}

fn env_path() -> PathBuf {
    project_root().join(".env")
}

fn qrcode_path() -> PathBuf {
    napcat_dir().join("cache").join("qrcode.png")
}

fn webui_json_path() -> PathBuf {
    napcat_dir().join("config").join("webui.json")
}

fn messages_db_path() -> PathBuf {
    project_root().join("data").join("messages.db")
}

fn monitor_log_path() -> PathBuf {
    project_root().join("logs").join("monitor.log")
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
        Duration::from_millis(400),
    )
    .is_ok()
}

fn read_webui_token() -> Option<String> {
    let raw = fs::read_to_string(webui_json_path()).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("token")?.as_str().map(|s| s.to_string())
}

fn read_webui_port() -> u16 {
    fs::read_to_string(webui_json_path())
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|v| v.get("port").and_then(|p| p.as_u64()))
        .unwrap_or(6099) as u16
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct AppConfig {
    onebot_ws_url: String,
    onebot_access_token: String,
    monitor_group_ids: String,
    monitor_keywords: String,
    monitor_log_all: bool,
    storage_enabled: bool,
    alert_enabled: bool,
    alert_webhook_url: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            onebot_ws_url: "ws://127.0.0.1:3001".into(),
            onebot_access_token: String::new(),
            monitor_group_ids: String::new(),
            monitor_keywords: String::new(),
            monitor_log_all: true,
            storage_enabled: true,
            alert_enabled: false,
            alert_webhook_url: String::new(),
        }
    }
}

fn parse_env_bool(v: &str) -> bool {
    matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

fn load_config_from_env() -> AppConfig {
    let mut cfg = AppConfig::default();
    let Ok(content) = fs::read_to_string(env_path()) else {
        return cfg;
    };
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        let k = k.trim();
        let v = v.trim().trim_matches('"');
        match k {
            "ONEBOT_WS_URL" => cfg.onebot_ws_url = v.to_string(),
            "ONEBOT_ACCESS_TOKEN" => cfg.onebot_access_token = v.to_string(),
            "MONITOR_GROUP_IDS" => cfg.monitor_group_ids = v.to_string(),
            "MONITOR_KEYWORDS" => cfg.monitor_keywords = v.to_string(),
            "MONITOR_LOG_ALL" => cfg.monitor_log_all = parse_env_bool(v),
            "STORAGE_ENABLED" => cfg.storage_enabled = parse_env_bool(v),
            "ALERT_ENABLED" => cfg.alert_enabled = parse_env_bool(v),
            "ALERT_WEBHOOK_URL" => cfg.alert_webhook_url = v.to_string(),
            _ => {}
        }
    }
    cfg
}

fn upsert_env_key(content: &str, key: &str, value: &str) -> String {
    let mut found = false;
    let mut out = Vec::new();
    for line in content.lines() {
        if let Some((k, _)) = line.split_once('=') {
            if k.trim() == key {
                out.push(format!("{key}={value}"));
                found = true;
                continue;
            }
        }
        out.push(line.to_string());
    }
    if !found {
        out.push(format!("{key}={value}"));
    }
    let mut s = out.join("\n");
    if !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

fn sync_onebot_token(token: &str) -> Result<(), String> {
    let path = napcat_dir().join("config").join("onebot11.json");
    if !path.exists() {
        return Ok(());
    }
    let raw = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let mut v: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    if let Some(servers) = v
        .pointer_mut("/network/websocketServers")
        .and_then(|x| x.as_array_mut())
    {
        for s in servers {
            if let Some(obj) = s.as_object_mut() {
                obj.insert("token".into(), serde_json::Value::String(token.to_string()));
                obj.insert("enable".into(), serde_json::Value::Bool(true));
                obj.insert("host".into(), serde_json::Value::String("127.0.0.1".into()));
                obj.insert("port".into(), serde_json::json!(3001));
            }
        }
    }
    let pretty = serde_json::to_string_pretty(&v).map_err(|e| e.to_string())?;
    fs::write(&path, pretty).map_err(|e| e.to_string())
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct StatusInfo {
    project_root: String,
    napcat_installed: bool,
    napcat_webui_up: bool,
    onebot_ws_up: bool,
    monitor_running: bool,
    python_ready: bool,
    qrcode_exists: bool,
    webui_url: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct QrInfo {
    available: bool,
    image_base64: Option<String>,
    path: String,
    updated_at: Option<String>,
    decode_url: Option<String>,
    hint: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct MessageRow {
    id: i64,
    group_id: String,
    user_id: String,
    sender_name: String,
    content: String,
    event_time: Option<i64>,
    created_at: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct GroupInfo {
    group_id: String,
    group_name: String,
    member_count: Option<i64>,
    max_member_count: Option<i64>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct GroupListResult {
    login_user_id: Option<String>,
    login_nickname: Option<String>,
    groups: Vec<GroupInfo>,
}

fn build_onebot_ws_url(ws_url: &str, token: &str) -> String {
    let mut url = ws_url.trim().to_string();
    if url.is_empty() {
        url = "ws://127.0.0.1:3001".into();
    }
    // 去掉末尾多余斜杠，避免部分实现握手 400
    while url.ends_with('/') && url.len() > "ws://x".len() {
        url.pop();
    }
    if !token.is_empty() && !url.contains("access_token=") {
        let sep = if url.contains('?') { '&' } else { '?' };
        // 仅用 query token；部分 NapCat 版本对 Authorization 握手较挑剔
        let enc: String = token
            .chars()
            .map(|c| match c {
                'A'..='Z' | 'a'..='z' | '0'..='9' | '-' | '_' | '.' | '~' => c.to_string(),
                _ => format!("%{:02X}", c as u8),
            })
            .collect();
        url = format!("{url}{sep}access_token={enc}");
    }
    url
}

fn onebot_ws_connect() -> Result<tungstenite::WebSocket<std::net::TcpStream>, String> {
    use std::net::TcpStream;
    use tungstenite::client::IntoClientRequest;

    let cfg = load_config_from_env();
    let ws_url = build_onebot_ws_url(&cfg.onebot_ws_url, &cfg.onebot_access_token);

    // 从 ws URL 解析 host:port，先建 TCP 再做握手（比 connect(url) 更稳）
    let httpish = ws_url.replacen("ws://", "http://", 1).replacen("wss://", "https://", 1);
    let parsed = url::Url::parse(&httpish).map_err(|e| format!("无效的 OneBot WS 地址: {e}"))?;
    let host = parsed.host_str().unwrap_or("127.0.0.1");
    let port = parsed.port().unwrap_or(if parsed.scheme() == "https" { 443 } else { 80 });
    let addr = format!("{host}:{port}");

    let stream = TcpStream::connect(&addr).map_err(|e| format!("TCP 连接 {addr} 失败: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(8)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;

    let mut req = ws_url
        .into_client_request()
        .map_err(|e| format!("构造 WebSocket 请求失败: {e}"))?;
    // 明确 Host，减少握手被拒
    let host_header = if parsed.port().is_some() {
        format!("{host}:{port}")
    } else {
        host.to_string()
    };
    req.headers_mut().insert(
        "Host",
        host_header
            .parse()
            .map_err(|e| format!("Host 头无效: {e}"))?,
    );

    let (socket, response) =
        tungstenite::client::client(req, stream).map_err(|e| format!("连接 OneBot 失败: {e}"))?;
    let status = response.status().as_u16();
    if status != 101 {
        return Err(format!("OneBot 握手失败 HTTP {status}"));
    }
    Ok(socket)
}

fn onebot_ws_call(action: &str, echo: &str) -> Result<serde_json::Value, String> {
    use tungstenite::Message;

    if !port_open("127.0.0.1", 3001) {
        return Err("OneBot WS 未启动（127.0.0.1:3001）。请先登录 NapCat。".into());
    }

    let mut socket = onebot_ws_connect()?;
    let payload = serde_json::json!({
        "action": action,
        "params": {},
        "echo": echo,
    });
    socket
        .send(Message::Text(payload.to_string()))
        .map_err(|e| format!("发送 OneBot 请求失败: {e}"))?;

    let deadline = std::time::Instant::now() + Duration::from_secs(8);
    while std::time::Instant::now() < deadline {
        let msg = socket.read().map_err(|e| format!("读取 OneBot 响应失败: {e}"))?;
        let text = match msg {
            Message::Text(t) => t,
            Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
            Message::Ping(p) => {
                let _ = socket.send(Message::Pong(p));
                continue;
            }
            Message::Pong(_) | Message::Frame(_) => continue,
            Message::Close(_) => return Err("OneBot 连接已关闭".into()),
        };
        let v: serde_json::Value =
            serde_json::from_str(&text).map_err(|e| format!("解析 OneBot JSON 失败: {e}"))?;
        if v.get("echo").and_then(|x| x.as_str()) == Some(echo) {
            let status = v.get("status").and_then(|x| x.as_str()).unwrap_or("");
            let retcode = v.get("retcode").and_then(|x| x.as_i64()).unwrap_or(-1);
            if status != "ok" && retcode != 0 {
                return Err(format!(
                    "OneBot 调用失败 action={action} status={status} retcode={retcode}"
                ));
            }
            return Ok(v.get("data").cloned().unwrap_or(serde_json::Value::Null));
        }
    }
    Err(format!("等待 OneBot 响应超时 action={action}"))
}

fn get_group_list_via_python() -> Result<GroupListResult, String> {
    let py = python_exe();
    let script = project_root().join("scripts").join("list_groups.py");
    if !script.exists() {
        return Err("缺少 scripts/list_groups.py".into());
    }
    let output = Command::new(&py)
        .arg(&script)
        .arg("--json")
        .current_dir(project_root())
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .output()
        .map_err(|e| format!("调用 Python 拉群失败: {e}"))?;
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!("Python 拉群失败: {err}"));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    // 只取最后一行 JSON，忽略其它日志
    let line = stdout
        .lines()
        .rev()
        .find(|l| l.trim().starts_with('{'))
        .ok_or_else(|| format!("Python 未返回 JSON: {stdout}"))?;
    let v: serde_json::Value =
        serde_json::from_str(line).map_err(|e| format!("解析 Python JSON 失败: {e}"))?;
    let login = v.get("login").cloned().unwrap_or(serde_json::Value::Null);
    let groups_v = v.get("groups").cloned().unwrap_or(serde_json::Value::Null);
    Ok(parse_group_list_result(login, groups_v))
}

fn parse_group_list_result(login: serde_json::Value, groups_v: serde_json::Value) -> GroupListResult {
    let login_user_id = login
        .get("user_id")
        .map(|x| x.to_string().trim_matches('"').to_string());
    let login_nickname = login
        .get("nickname")
        .and_then(|x| x.as_str())
        .map(|s| s.to_string());

    let mut groups = Vec::new();
    if let Some(arr) = groups_v.as_array() {
        for g in arr {
            let group_id = g
                .get("group_id")
                .map(|x| x.to_string().trim_matches('"').to_string())
                .filter(|s| !s.is_empty() && s != "null")
                .unwrap_or_default();
            if group_id.is_empty() {
                continue;
            }
            groups.push(GroupInfo {
                group_id,
                group_name: g
                    .get("group_name")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string(),
                member_count: g.get("member_count").and_then(|x| x.as_i64()),
                max_member_count: g.get("max_member_count").and_then(|x| x.as_i64()),
            });
        }
    }
    groups.sort_by(|a, b| {
        a.group_name
            .to_lowercase()
            .cmp(&b.group_name.to_lowercase())
            .then(a.group_id.cmp(&b.group_id))
    });
    GroupListResult {
        login_user_id,
        login_nickname,
        groups,
    }
}

#[tauri::command]
fn get_group_list() -> Result<GroupListResult, String> {
    // 优先走已验证可用的 Python OneBot 客户端；
    // Rust tungstenite 握手在部分 NapCat 版本上会返回 HTTP 400。
    match get_group_list_via_python() {
        Ok(v) => Ok(v),
        Err(py_err) => {
            let login = onebot_ws_call("get_login_info", "desktop_login")
                .map_err(|e| format!("{py_err}；Rust WS 也失败: {e}"))?;
            let groups_v = onebot_ws_call("get_group_list", "desktop_groups")
                .map_err(|e| format!("{py_err}；Rust WS 也失败: {e}"))?;
            Ok(parse_group_list_result(login, groups_v))
        }
    }
}

#[tauri::command]
fn get_status(state: tauri::State<'_, AppState>) -> StatusInfo {
    let webui_port = read_webui_port();
    let monitor_running = state
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
        .unwrap_or(false);

    StatusInfo {
        project_root: project_root().display().to_string(),
        napcat_installed: napcat_dir().join("launcher.bat").exists(),
        napcat_webui_up: port_open("127.0.0.1", webui_port),
        onebot_ws_up: port_open("127.0.0.1", 3001),
        monitor_running,
        python_ready: python_exe().exists() || which_python(),
        qrcode_exists: qrcode_path().exists(),
        webui_url: format!("http://127.0.0.1:{webui_port}/webui"),
    }
}

fn which_python() -> bool {
    Command::new("python").arg("--version").output().is_ok()
}

#[tauri::command]
fn get_config() -> AppConfig {
    load_config_from_env()
}

#[tauri::command]
fn save_config(config: AppConfig) -> Result<(), String> {
    let path = env_path();
    let current = fs::read_to_string(&path).unwrap_or_default();
    let mut next = current;
    next = upsert_env_key(&next, "ONEBOT_WS_URL", &config.onebot_ws_url);
    next = upsert_env_key(&next, "ONEBOT_ACCESS_TOKEN", &config.onebot_access_token);
    next = upsert_env_key(&next, "MONITOR_GROUP_IDS", &config.monitor_group_ids);
    next = upsert_env_key(&next, "MONITOR_KEYWORDS", &config.monitor_keywords);
    next = upsert_env_key(
        &next,
        "MONITOR_LOG_ALL",
        if config.monitor_log_all { "true" } else { "false" },
    );
    next = upsert_env_key(
        &next,
        "STORAGE_ENABLED",
        if config.storage_enabled { "true" } else { "false" },
    );
    next = upsert_env_key(
        &next,
        "ALERT_ENABLED",
        if config.alert_enabled { "true" } else { "false" },
    );
    next = upsert_env_key(&next, "ALERT_WEBHOOK_URL", &config.alert_webhook_url);
    fs::write(&path, next).map_err(|e| e.to_string())?;
    sync_onebot_token(&config.onebot_access_token)?;
    Ok(())
}

#[tauri::command]
fn start_napcat() -> Result<String, String> {
    let launcher = project_root().join("start-napcat.bat");
    if !launcher.exists() {
        return Err("未找到 start-napcat.bat，请确认 NapCat 已安装".into());
    }
    // 需要管理员权限：弹出 UAC
    let status = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process -FilePath '{}' -Verb RunAs",
                launcher.display()
            ),
        ])
        .status()
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok("已请求启动 NapCat（请在 UAC 中允许）。登录二维码会显示在本界面。".into())
    } else {
        Err("启动 NapCat 失败，请手动以管理员运行 start-napcat.bat".into())
    }
}

#[tauri::command]
fn open_webui() -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/webui", read_webui_port());
    Command::new("cmd")
        .args(["/C", "start", "", &url])
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn get_qrcode() -> QrInfo {
    let path = qrcode_path();
    let path_str = path.display().to_string();
    if !path.exists() {
        // 也尝试从近期 NapCat 日志里找解码 URL
        let decode_url = find_qr_decode_url();
        return QrInfo {
            available: false,
            image_base64: None,
            path: path_str,
            updated_at: None,
            decode_url,
            hint: "暂无二维码。请先启动 NapCat，等待生成 cache/qrcode.png".into(),
        };
    }

    let meta = fs::metadata(&path).ok();
    let updated_at = meta.and_then(|m| m.modified().ok()).map(|t| {
        let dt: chrono::DateTime<chrono::Local> = t.into();
        dt.format("%Y-%m-%d %H:%M:%S").to_string()
    });

    let bytes = match fs::read(&path) {
        Ok(b) => b,
        Err(e) => {
            return QrInfo {
                available: false,
                image_base64: None,
                path: path_str,
                updated_at,
                decode_url: find_qr_decode_url(),
                hint: format!("读取二维码失败: {e}"),
            };
        }
    };

    QrInfo {
        available: true,
        image_base64: Some(format!("data:image/png;base64,{}", B64.encode(bytes))),
        path: path_str,
        updated_at,
        decode_url: find_qr_decode_url(),
        hint: "请用手机 QQ 扫描下方二维码".into(),
    }
}

fn find_qr_decode_url() -> Option<String> {
    let logs = napcat_dir().join("logs");
    if !logs.exists() {
        return None;
    }
    let mut files: Vec<_> = fs::read_dir(&logs)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .and_then(|x| x.to_str())
                .map(|x| x.eq_ignore_ascii_case("log") || x.eq_ignore_ascii_case("txt"))
                .unwrap_or(true)
        })
        .collect();
    files.sort_by_key(|p| std::cmp::Reverse(p.metadata().and_then(|m| m.modified()).ok()));
    for file in files.into_iter().take(5) {
        if let Ok(content) = fs::read_to_string(&file) {
            for line in content.lines().rev() {
                if let Some(idx) = line.find("http") {
                    let url = line[idx..].trim();
                    if url.contains("qq.com") || url.contains("qrcode") || url.contains("txz") {
                        return Some(url.trim_matches(|c: char| c == '"' || c == '\'').to_string());
                    }
                }
                if let Some(rest) = line.strip_prefix("二维码解码URL:") {
                    return Some(rest.trim().to_string());
                }
                if let Some(rest) = line.split("二维码解码URL:").nth(1) {
                    return Some(rest.trim().to_string());
                }
            }
        }
    }
    None
}

#[tauri::command]
fn refresh_qrcode_hint() -> Result<String, String> {
    // 尝试走 WebUI 刷新接口（若已启动）
    let port = read_webui_port();
    if !port_open("127.0.0.1", port) {
        return Err("NapCat WebUI 未启动，无法通过接口刷新。可重启 NapCat。".into());
    }
    let token = read_webui_token().unwrap_or_default();
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    // 先尝试登录拿 Authorization（部分版本 token 直接作 Bearer）
    let url = format!("http://127.0.0.1:{port}/api/QQLogin/RefreshQRcode");
    let resp = client
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .send()
        .map_err(|e| e.to_string())?;
    if resp.status().is_success() {
        Ok("已请求刷新二维码，请稍候".into())
    } else {
        Err(format!(
            "刷新失败 HTTP {}。也可直接重启 NapCat。",
            resp.status()
        ))
    }
}

#[tauri::command]
fn start_monitor(state: tauri::State<'_, AppState>) -> Result<String, String> {
    {
        let guard = state.monitor_child.lock().map_err(|e| e.to_string())?;
        if guard.is_some() {
            return Err("监控服务已在运行".into());
        }
    }

    let root = project_root();
    let py = python_exe();
    let cfg = load_config_from_env();
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("app.main")
        .current_dir(&root)
        .env("ONEBOT_WS_URL", &cfg.onebot_ws_url)
        .env("ONEBOT_ACCESS_TOKEN", &cfg.onebot_access_token)
        .env("MONITOR_GROUP_IDS", &cfg.monitor_group_ids)
        .env("MONITOR_KEYWORDS", &cfg.monitor_keywords)
        .env(
            "MONITOR_LOG_ALL",
            if cfg.monitor_log_all { "true" } else { "false" },
        )
        .env(
            "STORAGE_ENABLED",
            if cfg.storage_enabled { "true" } else { "false" },
        )
        .env(
            "ALERT_ENABLED",
            if cfg.alert_enabled { "true" } else { "false" },
        )
        .env("ALERT_WEBHOOK_URL", &cfg.alert_webhook_url)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("启动监控失败: {e}"))?;

    // 短暂确认进程仍在
    std::thread::sleep(Duration::from_millis(400));
    match child.try_wait() {
        Ok(Some(code)) => {
            return Err(format!(
                "监控进程立即退出 (code={code:?})，请检查 .env 与 Python 依赖"
            ));
        }
        Ok(None) => {}
        Err(e) => return Err(e.to_string()),
    }

    *state.monitor_child.lock().map_err(|e| e.to_string())? = Some(child);
    Ok("监控服务已启动".into())
}

#[tauri::command]
fn stop_monitor(state: tauri::State<'_, AppState>) -> Result<String, String> {
    let mut guard = state.monitor_child.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
        Ok("监控服务已停止".into())
    } else {
        Err("监控服务未在运行".into())
    }
}

#[tauri::command]
fn get_recent_messages(limit: i64) -> Result<Vec<MessageRow>, String> {
    let db = messages_db_path();
    if !db.exists() {
        return Ok(vec![]);
    }
    let conn = Connection::open(&db).map_err(|e| e.to_string())?;
    let lim = if limit <= 0 { 50 } else { limit.min(200) };
    let mut stmt = conn
        .prepare(
            "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''), COALESCE(content,''), event_time, COALESCE(created_at,'')
             FROM messages ORDER BY id DESC LIMIT ?1",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([lim], |row| {
            Ok(MessageRow {
                id: row.get(0)?,
                group_id: row.get(1)?,
                user_id: row.get(2)?,
                sender_name: row.get(3)?,
                content: row.get(4)?,
                event_time: row.get(5)?,
                created_at: row.get(6)?,
            })
        })
        .map_err(|e| e.to_string())?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| e.to_string())?);
    }
    Ok(out)
}

#[tauri::command]
fn get_log_tail(max_lines: usize) -> Result<String, String> {
    let path = monitor_log_path();
    if !path.exists() {
        return Ok(String::new());
    }
    let content = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let lines: Vec<&str> = content.lines().collect();
    let n = if max_lines == 0 { 80 } else { max_lines.min(300) };
    let start = lines.len().saturating_sub(n);
    Ok(lines[start..].join("\n"))
}

#[tauri::command]
fn open_qrcode_folder() -> Result<(), String> {
    let dir = qrcode_path()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(napcat_dir);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Command::new("explorer")
        .arg(dir)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            monitor_child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_config,
            save_config,
            start_napcat,
            open_webui,
            get_qrcode,
            refresh_qrcode_hint,
            start_monitor,
            stop_monitor,
            get_recent_messages,
            get_log_tail,
            open_qrcode_folder,
            get_group_list
        ])
        .setup(|app| {
            // ensure data dirs exist
            let _ = fs::create_dir_all(project_root().join("data"));
            let _ = fs::create_dir_all(project_root().join("logs"));
            let _ = fs::create_dir_all(napcat_dir().join("cache"));
            let _ = app;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
