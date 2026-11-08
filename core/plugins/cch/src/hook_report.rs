//! `cch hook report`: tells the local `operations` service a session's plan, todos and prompts.
//! A verb result may carry `blocked`, mirrored into a marker file, so the prompt hook calls the
//! service only while the card waits for Tony.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::hooks::{result_of, Hub, PostError};

const DEADLINE: Duration = Duration::from_secs(8);
const RETRY_GAP: Duration = Duration::from_secs(1);

fn plans_dir(home: &Path) -> PathBuf {
    home.join("plans")
}

fn marker_dir(home: &Path) -> PathBuf {
    home.join("awm-operations")
}

/// The marker that says a session's card waits in Needs you.
pub fn marker(home: &Path, session_id: &str) -> PathBuf {
    marker_dir(home).join(format!("{}.blocked", session_id.replace('/', "_")))
}

/// One operations verb. Only a refused connection retries: the gateway restarting. Anything
/// else, a node with no operations included, answers `{}` and the hook stays quiet.
fn call(hub: &dyn Hub, verb: &str, args: Value) -> Value {
    let deadline = Instant::now() + DEADLINE;
    loop {
        let left = deadline.saturating_duration_since(Instant::now()).max(RETRY_GAP);
        match hub.invoke(&format!("operations_{verb}"), &args, std::process::id(), left) {
            Ok(body) => return result_of(&body).filter(Value::is_object).unwrap_or_else(|| json!({})),
            Err(PostError::Failed(why)) if why.contains("refused") && Instant::now() + RETRY_GAP < deadline => {
                std::thread::sleep(RETRY_GAP);
            }
            Err(_) => return json!({}),
        }
    }
}

fn report(hub: &dyn Hub, home: &Path, sid: &str, verb: &str, mut args: Value) -> Value {
    args["session_id"] = json!(sid);
    let result = call(hub, verb, args);
    if let Some(blocked) = result.get("blocked").and_then(Value::as_bool) {
        let m = marker(home, sid);
        if blocked {
            let _ = std::fs::create_dir_all(marker_dir(home));
            let _ = std::fs::write(&m, "");
        } else {
            let _ = std::fs::remove_file(&m);
        }
    }
    result
}

/// The hook output that hands a card's notice to the agent.
fn notice(result: &Value, event: &str) -> Option<String> {
    let n = result.get("notice").and_then(Value::as_str).filter(|n| !n.is_empty())?;
    Some(json!({"hookSpecificOutput": {"hookEventName": event, "additionalContext": n}}).to_string())
}

fn item_id(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    }
}

/// The full task list on disk: TaskCreate and TaskUpdate payloads are deltas.
fn task_items(home: &Path, session_id: &str) -> Vec<Value> {
    let list = std::env::var("CLAUDE_CODE_TASK_LIST_ID").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| session_id.to_string());
    let Ok(dir) = std::fs::read_dir(home.join("tasks").join(list)) else { return vec![] };
    let mut items: Vec<(String, Value)> = dir
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .filter_map(|e| serde_json::from_str::<Value>(&std::fs::read_to_string(e.path()).ok()?).ok())
        .filter_map(|t| {
            let id = item_id(t.get("id"))?;
            let row = json!({"id": id, "subject": t["subject"].as_str()?, "status": t["status"].as_str()?});
            Some((id, row))
        })
        .collect();
    items.sort_by(|a, b| (a.0.len(), &a.0).cmp(&(b.0.len(), &b.0)));
    items.into_iter().map(|(_, v)| v).collect()
}

fn todowrite_items(todos: Option<&Value>) -> Vec<Value> {
    let Some(todos) = todos.and_then(Value::as_array) else { return vec![] };
    todos
        .iter()
        .enumerate()
        .filter(|(_, t)| t.is_object())
        .map(|(n, t)| {
            let id = item_id(t.get("id")).unwrap_or_else(|| (n + 1).to_string());
            json!({"id": id, "subject": t["content"].as_str().unwrap_or_default(), "status": t["status"].as_str().unwrap_or_default()})
        })
        .collect()
}

/// One hook payload, against the gateway `hub` and the Claude config root `home`. Answers what
/// to print.
pub fn handle(hub: &dyn Hub, home: &Path, cwd: &Path, p: &Value) -> Option<String> {
    let sid = p.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty())?;
    let tool = p.get("tool_name").and_then(Value::as_str).unwrap_or_default();
    let input = p.get("tool_input").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({}));
    let subagent = p.get("agent_id").is_some_and(|v| !v.is_null() && v != "");
    match p.get("hook_event_name").and_then(Value::as_str) {
        Some("SessionStart") => {
            if p.get("source").and_then(Value::as_str) == Some("clear") {
                call(hub, "hook_session_clear", json!({"new_session_id": sid}));
            }
            return None;
        }
        Some("UserPromptSubmit") => {
            if marker(home, sid).exists() {
                report(hub, home, sid, "hook_prompt", json!({"prompt": p.get("prompt").and_then(Value::as_str).unwrap_or_default()}));
            }
            return None;
        }
        _ => {}
    }
    match tool {
        "Write" | "Edit" if !subagent => {
            let path = cwd.join(input.get("file_path").and_then(Value::as_str).unwrap_or_default());
            if path.parent() != Some(plans_dir(home).as_path()) || path.extension().is_none_or(|e| e != "md") {
                return None;
            }
            let text = std::fs::read_to_string(&path).unwrap_or_default();
            notice(&report(hub, home, sid, "hook_plan", json!({"plan_path": path.display().to_string(), "plan_text": text})), "PostToolUse")
        }
        "ExitPlanMode" if !subagent => {
            let plan = p.get("tool_response")?.get("plan")?.as_str().filter(|s| !s.is_empty())?;
            notice(&report(hub, home, sid, "hook_plan_approved", json!({"plan_text": plan})), "PostToolUse")
        }
        "TaskCreate" | "TaskUpdate" | "TodoWrite" => {
            let items = if tool == "TodoWrite" { todowrite_items(input.get("todos")) } else { task_items(home, sid) };
            if !items.is_empty() || input.get("status").and_then(Value::as_str) == Some("deleted") {
                report(hub, home, sid, "hook_todos", json!({"items": items}));
            }
            None
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct Stub {
        calls: RefCell<Vec<(String, Value)>>,
        result: Value,
    }

    impl Hub for Stub {
        fn invoke(&self, name: &str, args: &Value, _pid: u32, _t: Duration) -> Result<Value, PostError> {
            self.calls.borrow_mut().push((name.to_string(), args.clone()));
            Ok(json!({"result": self.result}))
        }
    }

    fn stub(result: Value) -> Stub {
        Stub { calls: RefCell::default(), result }
    }

    fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let t = tempfile::tempdir().unwrap();
        let home = t.path().to_path_buf();
        std::fs::create_dir_all(plans_dir(&home)).unwrap();
        let cwd = home.join("work");
        (t, home, cwd)
    }

    #[test]
    fn a_plan_write_reports_and_prints_the_notice_but_a_subagent_or_other_file_does_not() {
        let (_t, home, cwd) = fixture();
        let plan = plans_dir(&home).join("p.md");
        std::fs::write(&plan, "the plan").unwrap();
        let hub = stub(json!({"notice": "see the card"}));
        let payload = json!({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": plan}});
        let out: Value = serde_json::from_str(&handle(&hub, &home, &cwd, &payload).unwrap()).unwrap();
        assert_eq!(out["hookSpecificOutput"], json!({"hookEventName": "PostToolUse", "additionalContext": "see the card"}));
        assert_eq!(hub.calls.borrow()[0], ("operations_hook_plan".to_string(), json!({"session_id": "s1", "plan_path": plan, "plan_text": "the plan"})));
        let mut sub = payload.clone();
        sub["agent_id"] = json!("a1");
        let mut other = payload;
        other["tool_input"]["file_path"] = json!("/tmp/x.md");
        assert!(handle(&hub, &home, &cwd, &sub).is_none() && handle(&hub, &home, &cwd, &other).is_none());
        assert_eq!(hub.calls.borrow().len(), 1);
    }

    #[test]
    fn the_prompt_hook_calls_only_while_the_marker_says_blocked() {
        let (_t, home, cwd) = fixture();
        let prompt = json!({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"});
        let free = stub(json!({"blocked": false}));
        handle(&free, &home, &cwd, &prompt);
        assert!(free.calls.borrow().is_empty());
        let todos = json!({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "TodoWrite",
                           "tool_input": {"todos": [{"content": "a", "status": "pending"}]}});
        handle(&stub(json!({"blocked": true})), &home, &cwd, &todos);
        assert!(marker(&home, "s1").exists());
        handle(&free, &home, &cwd, &prompt);
        assert_eq!(free.calls.borrow()[0].0, "operations_hook_prompt");
        assert!(!marker(&home, "s1").exists(), "blocked: false clears it");
    }

    #[test]
    fn only_a_clear_reports_a_session_start() {
        let (_t, home, cwd) = fixture();
        let hub = stub(json!({}));
        handle(&hub, &home, &cwd, &json!({"session_id": "s2", "hook_event_name": "SessionStart", "source": "startup"}));
        assert!(hub.calls.borrow().is_empty());
        handle(&hub, &home, &cwd, &json!({"session_id": "s2", "hook_event_name": "SessionStart", "source": "clear"}));
        assert_eq!(hub.calls.borrow()[0], ("operations_hook_session_clear".to_string(), json!({"new_session_id": "s2"})));
    }

    #[test]
    fn a_node_with_no_operations_stays_quiet() {
        struct Absent;
        impl Hub for Absent {
            fn invoke(&self, _n: &str, _a: &Value, _p: u32, _t: Duration) -> Result<Value, PostError> {
                Err(PostError::Status(404))
            }
        }
        let (_t, home, cwd) = fixture();
        let todos = json!({"session_id": "s1", "tool_name": "TodoWrite", "tool_input": {"todos": [{"content": "a", "status": "pending"}]}});
        assert!(handle(&Absent, &home, &cwd, &todos).is_none());
    }
}
