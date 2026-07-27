use std::fs;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

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
        napcat_installed: napcat_dir().join("launcher-user.bat").exists()
            || napcat_dir().join("launcher.bat").exists(),
        napcat_webui_up: port_open("127.0.0.1", read_webui_port()),
        onebot_ws_up: port_open("127.0.0.1", 3001),
        monitor_running,
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
            return Err("监控服务已在运行".into());
        }
    }
    let root = project_root();
    let mut child = Command::new(python_exe())
        .arg("-m")
        .arg("app.main")
        .current_dir(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("启动监控失败: {e}"))?;
    std::thread::sleep(Duration::from_millis(400));
    match child.try_wait() {
        Ok(Some(code)) => {
            return Err(format!("监控进程立即退出 code={code:?}"));
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

fn recent_messages_from_db(group_id: Option<String>, limit: i64) -> Result<Value, String> {
    use rusqlite::Connection;

    let path = messages_db_path();
    if !path.exists() {
        return Ok(Value::Array(vec![]));
    }
    let lim = limit.clamp(1, 200);
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
        let mut stmt = conn
            .prepare(
                "SELECT id, group_id, COALESCE(user_id,''), COALESCE(sender_name,''),
                        COALESCE(content,''), event_time, COALESCE(created_at,'')
                 FROM messages ORDER BY id DESC LIMIT ?",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(rusqlite::params![lim], |r| {
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
        "enabled": o.get("enabled").cloned().unwrap_or(Value::Bool(true)),
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
            "providerId": lo.get("provider_id").cloned().unwrap_or(Value::String(String::new())),
            "model": lo.get("model").cloned().unwrap_or(Value::String(String::new())),
            "prompt": lo.get("prompt").cloned().unwrap_or(Value::String(String::new())),
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
            api_get_settings,
            api_save_settings,
            api_get_group,
            api_save_group,
            api_run_llm,
            api_pull_history,
            api_list_reports,
            api_fetch_models,
            api_test_provider,
            api_test_onebot,
            pull_onebot_groups
        ])
        .setup(|_| {
            let _ = fs::create_dir_all(project_root().join("data"));
            let _ = fs::create_dir_all(project_root().join("data").join("group_configs"));
            let _ = fs::create_dir_all(project_root().join("logs"));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
