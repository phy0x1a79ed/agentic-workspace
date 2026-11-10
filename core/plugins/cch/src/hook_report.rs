//! `cch hook report`: tells the local `operations` service a session's plan and todos, and Tony's
//! `claim task <id>` prompt. The session's state moves its card, so no hook moves one.

use std::path::Path;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::hooks::{result_of, Hub, PostError};

const DEADLINE: Duration = Duration::from_secs(8);
const RETRY_GAP: Duration = Duration::from_secs(1);

/// One operations verb for session `sid`. Only a refused connection retries: the gateway
/// restarting. Anything else, a node with no operations included, answers `{}` and the hook
/// stays quiet.
fn report(hub: &dyn Hub, sid: &str, verb: &str, mut args: Value) -> Value {
    args["session_id"] = json!(sid);
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

/// The task id of a `claim task <id>` prompt.
fn claimed(prompt: &str) -> Option<&str> {
    let mut words = prompt.split_whitespace();
    let (claim, task, id) = (words.next()?, words.next()?, words.next()?);
    (claim.eq_ignore_ascii_case("claim") && task.eq_ignore_ascii_case("task") && words.next().is_none()).then_some(id)
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

/// One hook payload, against the gateway `hub` and the Claude config root `home`.
pub fn handle(hub: &dyn Hub, home: &Path, p: &Value) {
    let Some(sid) = p.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { return };
    let tool = p.get("tool_name").and_then(Value::as_str).unwrap_or_default();
    let input = p.get("tool_input").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({}));
    let subagent = p.get("agent_id").is_some_and(|v| !v.is_null() && v != "");
    match p.get("hook_event_name").and_then(Value::as_str) {
        Some("SessionStart") => {
            return;
        }
        Some("UserPromptSubmit") => {
            if let Some(id) = claimed(p.get("prompt").and_then(Value::as_str).unwrap_or_default()) {
                report(hub, sid, "claim_task", json!({"task_id": id}));
            }
            return;
        }
        _ => {}
    }
    match tool {
        "ExitPlanMode" if !subagent => {
            let plan = p.get("tool_response").and_then(|r| r.get("plan")).and_then(Value::as_str).unwrap_or_default();
            if !plan.is_empty() {
                report(hub, sid, "hook_plan_approved", json!({"plan_text": plan}));
            }
        }
        "TaskCreate" | "TaskUpdate" | "TodoWrite" => {
            let items = if tool == "TodoWrite" { todowrite_items(input.get("todos")) } else { task_items(home, sid) };
            if !items.is_empty() || input.get("status").and_then(Value::as_str) == Some("deleted") {
                report(hub, sid, "hook_todos", json!({"items": items}));
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::path::PathBuf;

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

    fn fixture() -> (tempfile::TempDir, PathBuf) {
        let t = tempfile::tempdir().unwrap();
        let home = t.path().to_path_buf();
        (t, home)
    }

    #[test]
    fn plan_approval_and_a_claim_prompt_report_but_a_plan_write_or_other_prompt_does_not() {
        let (_t, home) = fixture();
        let hub = stub(json!({"task_id": "T1"}));
        let write = json!({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Write",
                           "tool_input": {"file_path": home.join("plans/p.md")}});
        let prompt = |text: &str| json!({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": text});
        for p in [write, prompt("hi"), prompt("claim task T1 now")] {
            handle(&hub, &home, &p);
        }
        assert!(hub.calls.borrow().is_empty());
        let approved = json!({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "tool_response": {"plan": "# P"}});
        handle(&hub, &home, &approved);
        handle(&hub, &home, &prompt("  Claim task Ab12Cd34 "));
        let calls = hub.calls.borrow();
        assert_eq!(calls[0], ("operations_hook_plan_approved".to_string(), json!({"session_id": "s1", "plan_text": "# P"})));
        assert_eq!(calls[1], ("operations_claim_task".to_string(), json!({"session_id": "s1", "task_id": "Ab12Cd34"})));
    }

    #[test]
    fn a_session_start_reports_nothing_here() {
        let (_t, home) = fixture();
        let hub = stub(json!({}));
        handle(&hub, &home, &json!({"session_id": "s2", "hook_event_name": "SessionStart", "source": "startup"}));
        assert!(hub.calls.borrow().is_empty());
        handle(&hub, &home, &json!({"session_id": "s2", "hook_event_name": "SessionStart", "source": "clear"}));
        assert!(hub.calls.borrow().is_empty(), "the brief hook reports a clear, with its parent");
    }

    #[test]
    fn a_node_with_no_operations_stays_quiet() {
        struct Absent;
        impl Hub for Absent {
            fn invoke(&self, _n: &str, _a: &Value, _p: u32, _t: Duration) -> Result<Value, PostError> {
                Err(PostError::Status(404))
            }
        }
        let (_t, home) = fixture();
        let todos = json!({"session_id": "s1", "tool_name": "TodoWrite", "tool_input": {"todos": [{"content": "a", "status": "pending"}]}});
        handle(&Absent, &home, &todos);
    }
}
