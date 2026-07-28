use std::fs;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use base64::Engine;
use serde::Serialize;
use serde_json::Value;

struct AppState {
    monitor_child: Mutex<Option<Child>>,
}

fn project_root() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // desktop
    p.pop(); // project root
    p
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
}

#[tauri::command]
fn get_status(state: tauri::State<'_, AppState>) -> StatusInfo {
    StatusInfo {
        napcat_installed: napcat_dir().join("launcher-user.bat").exists()
            || napcat_dir().join("launcher.bat").exists(),
        napcat_webui_up: port_open("127.0.0.1", read_webui_port()),
        onebot_ws_up: port_open("127.0.0.1", 3001),
        monitor_running: monitor_service_running(&state),
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
    Command::new("cmd")
        .args(["/C", "start", "", &path.display().to_string()])
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok("已请求启动 NapCat".into())
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
    let mut guard = state.monitor_child.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
        Ok("监听服务已停止".into())
    } else if let Some(pid) = read_monitor_lock_pid().filter(|p| pid_alive(*p)) {
        let status = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/F"])
            .status()
            .map_err(|e| format!("结束监听进程失败: {e}"))?;
        if status.success() {
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
    recent_messages_from_db(group_id, limit)
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

fn recent_messages_from_db(group_id: Option<String>, limit: i64) -> Result<Value, String> {
    use rusqlite::Connection;

    let path = messages_db_path();
    if !path.exists() {
        return Ok(Value::Array(vec![]));
    }
    let lim = limit.clamp(1, 200);
    let blocked = blocked_group_ids_from_disk();
    let conn = Connection::open(&path).map_err(|e| format!("打开 messages.db 失败: {e}"))?;

    let mut rows_out: Vec<Value> = Vec::new();

    let mut push_row = |id: i64,
                        group_id: String,
                        user_id: String,
                        sender_name: String,
                        content: String,
                        event_time: Option<i64>,
                        created_at: String| {
        rows_out.push(serde_json::json!({
            "id": id,
            "groupId": group_id,
            "groupName": "",
            "userId": user_id,
            "senderName": sender_name,
            "content": content,
            "eventTime": event_time,
            "createdAt": created_at,
        }));
    };

    if let Some(gid) = group_id.filter(|s| !s.is_empty()) {
        if blocked.contains(&gid) {
            return Ok(Value::Array(vec![]));
        }
        let mut stmt = conn
            .prepare(
                "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                        COALESCE(content,''), event_time, COALESCE(created_at,'')
                 FROM messages WHERE group_id=? ORDER BY id DESC LIMIT ?",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(rusqlite::params![gid, lim], |r| {
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
        for row in rows {
            let (id, group_id, user_id, sender_name, content, event_time, created_at) =
                row.map_err(|e| e.to_string())?;
            push_row(
                id,
                group_id,
                user_id,
                sender_name,
                content,
                event_time,
                created_at,
            );
        }
    } else {
        let fetch_lim = (lim.saturating_mul(5)).clamp(lim, 500);
        let mut stmt = conn
            .prepare(
                "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                        COALESCE(content,''), event_time, COALESCE(created_at,'')
                 FROM messages ORDER BY id DESC LIMIT ?",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(rusqlite::params![fetch_lim], |r| {
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
        for row in rows {
            let (id, group_id, user_id, sender_name, content, event_time, created_at) =
                row.map_err(|e| e.to_string())?;
            if blocked.contains(&group_id) {
                continue;
            }
            push_row(
                id,
                group_id,
                user_id,
                sender_name,
                content,
                event_time,
                created_at,
            );
        }
        if rows_out.len() as i64 > lim {
            rows_out.truncate(lim as usize);
        }
    }
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
fn api_run_llm(group_id: String) -> Result<Value, String> {
    let v = py_api_json(&["run-llm", "--group-id", &group_id])?;
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
fn api_list_reports(group_id: Option<String>, limit: i64) -> Result<Value, String> {
    let lim = limit.to_string();
    let v = if let Some(gid) = group_id.filter(|s| !s.is_empty()) {
        py_api_json(&["list-reports", "--group-id", &gid, "--limit", &lim])?
    } else {
        py_api_json(&["list-reports", "--limit", &lim])?
    };
    Ok(v)
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
            "providers": providers,
        }),
    );
    let ui = obj.remove("ui").unwrap_or(Value::Object(Default::default()));
    let mut ui_obj = ui.as_object().cloned().unwrap_or_default();
    out.insert(
        "ui".into(),
        serde_json::json!({
            "compactModeEnabled": ui_obj
                .remove("compact_mode_enabled")
                .unwrap_or(Value::Bool(false)),
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
    if !port_open("127.0.0.1", 3001) {
        return Err("OneBot WS 未启动".into());
    }

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
    let cache = serde_json::json!({
        "groups": parsed.get("groups").cloned().unwrap_or(Value::Array(vec![])),
        "login": parsed.get("login").cloned().unwrap_or(Value::Null),
    });
    let raw = cache.to_string();
    py_api_json(&["cache-groups", "--json", &raw])?;
    let n = cache
        .get("groups")
        .and_then(|x| x.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    let _ = build_onebot_ws_url(ws, token);
    Ok(format!("已从 OneBot 拉取 {n} 个群"))
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
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            start_napcat,
            start_monitor,
            stop_monitor,
            api_list_groups,
            api_recent_messages,
            api_messages_in_window,
            api_get_settings,
            api_save_settings,
            api_get_group,
            api_save_group,
            api_run_llm,
            api_pull_history,
            api_list_reports,
            api_token_stats,
            api_fetch_models,
            api_test_provider,
            api_test_onebot,
            api_bind_qq,
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
