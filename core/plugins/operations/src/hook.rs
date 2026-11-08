//! `operations hook`: the Claude Code hook that reports a session's plan, todos and prompts
//! to the local `operations` service. It exits 0 on every path and writes to stdout only when
//! a card opened, as context the agent reads.
//!
//! The call is a synchronous `POST /invoke` with `X-Awm-Caller-Pid`: the gateway walks the
//! hook's ancestry to find the session, which a detached hook would break. A verb result may
//! carry `blocked`, mirrored into a marker file, so the prompt hook calls the service only
//! while the card waits for Tony.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const DEADLINE: Duration = Duration::from_secs(8);
const RETRY_GAP: Duration = Duration::from_secs(1);

fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/".into()))
}

fn plans_dir() -> PathBuf {
    home().join(".claude/plans")
}

fn marker_dir() -> PathBuf {
    home().join(".claude/awm-operations")
}

/// The marker that says a session's card waits in Needs you.
pub fn marker(session_id: &str) -> PathBuf {
    marker_dir().join(format!("{}.blocked", session_id.replace('/', "_")))
}

enum Post {
    Answer(u16, String),
    Refused,
}

fn post(url: &str, body: &str, timeout: Duration) -> Post {
    let Ok(u) = url::Url::parse(url) else { return Post::Refused };
    let (host, port) = (u.host_str().unwrap_or("127.0.0.1").to_string(), u.port_or_known_default().unwrap_or(80));
    let addr = format!("{host}:{port}");
    let Ok(addrs) = std::net::ToSocketAddrs::to_socket_addrs(&addr) else { return Post::Refused };
    let Some(sock) = addrs.into_iter().next() else { return Post::Refused };
    let Ok(mut s) = TcpStream::connect_timeout(&sock, timeout) else { return Post::Refused };
    let _ = s.set_read_timeout(Some(timeout));
    let _ = s.set_write_timeout(Some(timeout));
    let req = format!(
        "POST {} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nX-Awm-Caller-Pid: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        u.path().trim_end_matches('/').to_string() + "/invoke",
        std::process::id(),
        body.len()
    );
    if s.write_all(req.as_bytes()).is_err() {
        return Post::Answer(0, String::new());
    }
    let mut raw = Vec::new();
    let _ = s.read_to_end(&mut raw);
    let text = String::from_utf8_lossy(&raw).to_string();
    let Some((head, rest)) = text.split_once("\r\n\r\n") else { return Post::Answer(0, String::new()) };
    let status = head.split_whitespace().nth(1).and_then(|c| c.parse().ok()).unwrap_or(0);
    let body = if head.to_ascii_lowercase().contains("transfer-encoding: chunked") { dechunk(rest) } else { rest.to_string() };
    Post::Answer(status, body)
}

fn dechunk(mut s: &str) -> String {
    let mut out = String::new();
    while let Some((size, rest)) = s.split_once("\r\n") {
        let Ok(n) = usize::from_str_radix(size.trim(), 16) else { break };
        if n == 0 || rest.len() < n {
            break;
        }
        out.push_str(&rest[..n]);
        s = rest[n..].trim_start_matches("\r\n");
    }
    out
}

/// One verb on the local gateway. Only a refused connection retries: the gateway restarting.
pub fn call(verb: &str, args: Value) -> Value {
    let base = std::env::var("AWM_HUB_URL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "http://127.0.0.1:7819".into());
    let body = json!({"name": format!("operations_{verb}"), "args": args}).to_string();
    let deadline = Instant::now() + DEADLINE;
    loop {
        let left = deadline.saturating_duration_since(Instant::now()).max(Duration::from_secs(1));
        match post(&base, &body, left) {
            Post::Answer(status, text) => {
                if !(200..300).contains(&status) {
                    return json!({});
                }
                let result = serde_json::from_str::<Value>(&text).ok().and_then(|v| v.get("result").cloned()).unwrap_or(Value::Null);
                let result = match result {
                    Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::Null),
                    other => other,
                };
                return if result.is_object() { result } else { json!({}) };
            }
            Post::Refused => {
                if Instant::now() + RETRY_GAP >= deadline {
                    return json!({});
                }
                std::thread::sleep(RETRY_GAP);
            }
        }
    }
}

fn report(sid: &str, verb: &str, mut args: Value) -> Value {
    args["session_id"] = json!(sid);
    let result = call(verb, args);
    if let Some(b) = result.get("blocked").and_then(Value::as_bool) {
        let m = marker(sid);
        if b {
            let _ = std::fs::create_dir_all(marker_dir());
            let _ = std::fs::write(&m, "");
        } else {
            let _ = std::fs::remove_file(&m);
        }
    }
    result
}

fn tell(result: &Value, event: &str) {
    if let Some(n) = result.get("notice").and_then(Value::as_str).filter(|n| !n.is_empty()) {
        println!("{}", json!({"hookSpecificOutput": {"hookEventName": event, "additionalContext": n}}));
    }
}

fn read_text(p: &Path) -> String {
    std::fs::read_to_string(p).unwrap_or_default()
}

/// The full task list on disk: TaskCreate and TaskUpdate payloads are deltas.
fn task_items(session_id: &str) -> Vec<Value> {
    let list = std::env::var("CLAUDE_CODE_TASK_LIST_ID").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| session_id.to_string());
    let dir = home().join(".claude/tasks").join(list);
    let mut items: Vec<(String, Value)> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(Result::ok)
                .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
                .filter_map(|e| serde_json::from_str::<Value>(&read_text(&e.path())).ok())
                .filter_map(|t| {
                    let id = match &t["id"] { Value::String(s) => s.clone(), Value::Number(n) => n.to_string(), _ => return None };
                    let (subject, status) = (t["subject"].as_str()?.to_string(), t["status"].as_str()?.to_string());
                    Some((id.clone(), json!({"id": id, "subject": subject, "status": status})))
                })
                .collect()
        })
        .unwrap_or_default();
    items.sort_by(|a, b| (a.0.len(), &a.0).cmp(&(b.0.len(), &b.0)));
    items.into_iter().map(|(_, v)| v).collect()
}

fn todowrite_items(todos: Option<&Value>) -> Vec<Value> {
    todos.and_then(Value::as_array).map(|a| {
        a.iter().enumerate().filter_map(|(n, t)| {
            let t = t.as_object()?;
            let id = match t.get("id") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                Some(Value::Number(x)) => x.to_string(),
                _ => (n + 1).to_string(),
            };
            Some(json!({"id": id, "subject": t.get("content").and_then(Value::as_str).unwrap_or_default(),
                        "status": t.get("status").and_then(Value::as_str).unwrap_or_default()}))
        }).collect()
    }).unwrap_or_default()
}

/// Handle one hook payload. Never fails: a broken hook must not break a turn.
pub fn handle(p: &Value) {
    let Some(sid) = p.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { return };
    let tool = p.get("tool_name").and_then(Value::as_str).unwrap_or_default();
    let input = p.get("tool_input").filter(|v| v.is_object()).cloned().unwrap_or(json!({}));
    let subagent = p.get("agent_id").is_some_and(|v| !v.is_null() && v != "");
    match p.get("hook_event_name").and_then(Value::as_str) {
        Some("SessionStart") => {
            if p.get("source").and_then(Value::as_str) == Some("clear") {
                call("hook_session_clear", json!({"new_session_id": sid}));
            }
            return;
        }
        Some("UserPromptSubmit") => {
            if marker(sid).exists() {
                report(sid, "hook_prompt", json!({"prompt": p.get("prompt").and_then(Value::as_str).unwrap_or_default()}));
            }
            return;
        }
        _ => {}
    }
    match tool {
        "Write" | "Edit" => {
            let path = PathBuf::from(input.get("file_path").and_then(Value::as_str).unwrap_or_default());
            let path = if path.is_absolute() { path } else { std::env::current_dir().unwrap_or_default().join(path) };
            if path.parent() == Some(plans_dir().as_path()) && path.extension().is_some_and(|e| e == "md") && !subagent {
                let text = read_text(&path);
                tell(&report(sid, "hook_plan", json!({"plan_path": path.display().to_string(), "plan_text": text})), "PostToolUse");
            }
        }
        "ExitPlanMode" if !subagent => {
            if let Some(plan) = p.get("tool_response").and_then(|r| r.get("plan")).and_then(Value::as_str).filter(|s| !s.is_empty()) {
                tell(&report(sid, "hook_plan_approved", json!({"plan_text": plan})), "PostToolUse");
            }
        }
        "TaskCreate" | "TaskUpdate" | "TodoWrite" => {
            let items = if tool == "TodoWrite" { todowrite_items(input.get("todos")) } else { task_items(sid) };
            if !items.is_empty() || input.get("status").and_then(Value::as_str) == Some("deleted") {
                report(sid, "hook_todos", json!({"items": items}));
            }
        }
        _ => {}
    }
}

/// Read the payload from stdin and handle it.
pub fn run() -> i32 {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_ok() {
        if let Ok(p) = serde_json::from_str::<Value>(&raw) {
            handle(&p);
        }
    }
    0
}
