//! `operations hook` against a stub gateway, run the way Claude Code runs it: this crate's
//! binary, JSON on stdin, HOME and AWM_HUB_URL in its environment. Ported from test_hook.py.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

const BIN: &str = env!("CARGO_BIN_EXE_operations");

/// One recorded call: the request path, the caller pid header and the JSON body.
type Call = (String, Option<String>, Value);

struct Gateway {
    url: String,
    calls: Arc<Mutex<Vec<Call>>>,
    reply: Arc<Mutex<Value>>,
}

impl Gateway {
    fn start() -> Gateway {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let calls = Arc::new(Mutex::new(vec![]));
        let reply = Arc::new(Mutex::new(json!({"ok": true, "blocked": false})));
        let (c, r) = (calls.clone(), reply.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut first = String::new();
                if reader.read_line(&mut first).is_err() {
                    continue;
                }
                let path = first.split_whitespace().nth(1).unwrap_or_default().to_string();
                let (mut length, mut pid) = (0usize, None);
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some((k, v)) = line.trim_end().split_once(':') {
                        match k.to_ascii_lowercase().as_str() {
                            "content-length" => length = v.trim().parse().unwrap_or(0),
                            "x-awm-caller-pid" => pid = Some(v.trim().to_string()),
                            _ => {}
                        }
                    }
                }
                let mut body = vec![0u8; length];
                if reader.read_exact(&mut body).is_err() {
                    continue;
                }
                c.lock().unwrap().push((path, pid, serde_json::from_slice(&body).unwrap_or(Value::Null)));
                let data = json!({"result": r.lock().unwrap().to_string()}).to_string();
                let _ = write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{data}", data.len());
            }
        });
        Gateway { url, calls, reply }
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }

    fn bodies(&self) -> Vec<Value> {
        self.calls().into_iter().map(|c| c.2).collect()
    }

    fn set_reply(&self, v: Value) {
        *self.reply.lock().unwrap() = v;
    }
}

fn home() -> tempfile::TempDir {
    let d = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(d.path().join(".claude/plans")).unwrap();
    d
}

fn raw(home: &Path, gw_url: &str, input: &str, env: &[(&str, &str)]) -> (i32, String) {
    let mut cmd = Command::new(BIN);
    cmd.arg("hook").env_clear().env("PATH", "/usr/bin:/bin").env("HOME", home).env("AWM_HUB_URL", gw_url).current_dir(home);
    for (k, v) in env {
        cmd.env(k, v);
    }
    let mut child = cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    child.stdin.take().unwrap().write_all(input.as_bytes()).unwrap();
    let out = child.wait_with_output().unwrap();
    (out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout).to_string())
}

fn run(home: &Path, gw_url: &str, payload: Value) -> String {
    run_env(home, gw_url, payload, &[], false)
}

fn run_env(home: &Path, gw_url: &str, payload: Value, env: &[(&str, &str)], prints: bool) -> String {
    let (code, stdout) = raw(home, gw_url, &payload.to_string(), env);
    assert_eq!(code, 0);
    if !prints {
        assert_eq!(stdout, "", "the hook printed when it should not");
    }
    stdout
}

fn post(tool: &str, extra: Value) -> Value {
    let mut p = json!({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": tool});
    p.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    p
}

fn prompt(text: &str) -> Value {
    json!({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": text})
}

fn start(source: &str) -> Value {
    json!({"session_id": "s2", "hook_event_name": "SessionStart", "source": source})
}

fn marker(home: &Path) -> PathBuf {
    home.join(".claude/awm-operations/s1.blocked")
}

fn plan(home: &Path, name: &str, text: &str) -> PathBuf {
    let p = home.join(".claude/plans").join(name);
    std::fs::write(&p, text).unwrap();
    p
}

#[test]
fn a_clear_files_the_task_away_and_other_session_starts_are_ignored() {
    let (h, gw) = (home(), Gateway::start());
    for source in ["startup", "resume", "compact"] {
        run(h.path(), &gw.url, start(source));
    }
    assert!(gw.calls().is_empty());
    run(h.path(), &gw.url, start("clear"));
    assert_eq!(gw.bodies(), [json!({"name": "operations_hook_session_clear", "args": {"new_session_id": "s2"}})]);
    let pid = gw.calls()[0].1.clone().unwrap();
    assert!(!pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()), "{pid}");
}

#[test]
fn the_settings_fragment_runs_the_hook_on_clear_only() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("hooks/awm-operations-settings.json");
    let fragment: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let starts = fragment["hooks"]["SessionStart"].as_array().unwrap();
    assert_eq!(starts.len(), 1);
    assert_eq!(starts[0]["matcher"], "clear");
    assert!(starts[0]["hooks"][0]["command"].as_str().unwrap().contains("awm-operations-hook.sh"));
    let events: Vec<&String> = fragment["hooks"].as_object().unwrap().keys().collect();
    assert_eq!(events, ["PostToolUse", "UserPromptSubmit", "SessionStart"]);
}

#[test]
fn a_plan_write_sends_the_file_text() {
    let (h, gw) = (home(), Gateway::start());
    let p = plan(h.path(), "a-plan.md", "# plan");
    for tool in ["Write", "Edit"] {
        run(h.path(), &gw.url, post(tool, json!({"tool_input": {"file_path": p}})));
    }
    let (path, pid, body) = gw.calls()[0].clone();
    assert_eq!(path, "/invoke");
    assert!(pid.unwrap().bytes().all(|b| b.is_ascii_digit()));
    assert_eq!(body, json!({"name": "operations_hook_plan", "args": {"session_id": "s1", "plan_path": p, "plan_text": "# plan"}}));
    assert_eq!(gw.calls().len(), 2);
}

#[test]
fn a_relative_plan_path_resolves_against_the_cwd() {
    let (h, gw) = (home(), Gateway::start());
    let p = plan(h.path(), "rel.md", "# rel");
    run(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": ".claude/plans/rel.md"}})));
    assert_eq!(gw.bodies()[0]["args"]["plan_path"], json!(p));
}

#[test]
fn a_plan_that_opened_a_card_tells_the_agent() {
    let (h, gw) = (home(), Gateway::start());
    let p = plan(h.path(), "a-plan.md", "# plan");
    gw.set_reply(json!({"ok": true, "blocked": false, "opened": true, "notice": "card X is yours"}));
    let out = run_env(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": p}})), &[], true);
    let want = json!({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "card X is yours"}});
    assert_eq!(serde_json::from_str::<Value>(&out).unwrap(), want);
    let out = run_env(h.path(), &gw.url, post("ExitPlanMode", json!({"tool_response": {"plan": "# plan"}})), &[], true);
    assert_eq!(serde_json::from_str::<Value>(&out).unwrap()["hookSpecificOutput"]["additionalContext"], "card X is yours");
}

#[test]
fn other_writes_and_subagent_plans_are_ignored() {
    let (h, gw) = (home(), Gateway::start());
    let p = plan(h.path(), "a-plan.md", "# plan");
    run(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": p}, "agent_id": "sub1"})));
    run(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": h.path().join("notes.md")}})));
    run(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": h.path().join(".claude/plans/x.txt")}})));
    assert!(gw.calls().is_empty());
}

#[test]
fn an_approved_plan_is_sent_and_a_bare_response_is_not() {
    let (h, gw) = (home(), Gateway::start());
    run(h.path(), &gw.url, post("ExitPlanMode", json!({"tool_response": {"plan": "# approved", "isAgent": false}})));
    run(h.path(), &gw.url, post("ExitPlanMode", json!({"tool_response": {"status": "denied"}})));
    run(h.path(), &gw.url, post("ExitPlanMode", json!({"tool_response": {"plan": "# sub"}, "agent_id": "sub1"})));
    assert_eq!(gw.bodies(), [json!({"name": "operations_hook_plan_approved", "args": {"session_id": "s1", "plan_text": "# approved"}})]);
}

#[test]
fn task_events_send_the_whole_list_from_disk() {
    let (h, gw) = (home(), Gateway::start());
    let d = h.path().join(".claude/tasks/s1");
    std::fs::create_dir_all(&d).unwrap();
    for i in 1..=12 {
        let (status, subject) = if i % 2 == 1 { ("completed", "T1") } else { ("pending", "T2") };
        std::fs::write(d.join(format!("{i}.json")), json!({"id": i.to_string(), "subject": format!("{subject}.{i}"), "status": status}).to_string()).unwrap();
    }
    std::fs::write(d.join(".lock"), "").unwrap();
    std::fs::write(d.join("bad.json"), "{").unwrap();
    run(h.path(), &gw.url, post("TaskUpdate", json!({"tool_input": {"taskId": "2", "status": "completed"}})));
    let body = &gw.bodies()[0];
    assert_eq!(body["name"], "operations_hook_todos");
    let ids: Vec<String> = body["args"]["items"].as_array().unwrap().iter().map(|i| i["id"].as_str().unwrap().to_string()).collect();
    assert_eq!(ids, (1..=12).map(|n| n.to_string()).collect::<Vec<_>>());
    assert_eq!(body["args"]["items"][0], json!({"id": "1", "subject": "T1.1", "status": "completed"}));
}

#[test]
fn the_task_list_id_override_is_honoured() {
    let (h, gw) = (home(), Gateway::start());
    let d = h.path().join(".claude/tasks/named-list");
    std::fs::create_dir_all(&d).unwrap();
    std::fs::write(d.join("1.json"), json!({"id": "1", "subject": "x", "status": "pending"}).to_string()).unwrap();
    run_env(h.path(), &gw.url, post("TaskCreate", json!({"tool_input": {}})), &[("CLAUDE_CODE_TASK_LIST_ID", "named-list")], false);
    assert_eq!(gw.calls().len(), 1);
}

#[test]
fn deleting_the_last_task_sends_an_empty_list() {
    let (h, gw) = (home(), Gateway::start());
    std::fs::create_dir_all(h.path().join(".claude/tasks/s1")).unwrap();
    run(h.path(), &gw.url, post("TaskUpdate", json!({"tool_input": {"taskId": "1", "status": "deleted"}})));
    assert_eq!(gw.bodies()[0], json!({"name": "operations_hook_todos", "args": {"session_id": "s1", "items": []}}));
}

#[test]
fn a_cleared_list_sends_nothing() {
    let (h, gw) = (home(), Gateway::start());
    run(h.path(), &gw.url, post("TaskUpdate", json!({"tool_input": {"taskId": "1", "status": "completed"}})));
    std::fs::create_dir_all(h.path().join(".claude/tasks/s1")).unwrap();
    run(h.path(), &gw.url, post("TaskUpdate", json!({"tool_input": {"taskId": "1", "status": "completed"}})));
    run(h.path(), &gw.url, post("TodoWrite", json!({"tool_input": {"todos": []}})));
    assert!(gw.calls().is_empty());
}

#[test]
fn todowrite_carries_the_list_in_its_input() {
    let (h, gw) = (home(), Gateway::start());
    let todos = json!([{"content": "a", "status": "pending", "activeForm": "A"}, {"content": "b", "status": "completed", "activeForm": "B"}]);
    run(h.path(), &gw.url, post("TodoWrite", json!({"tool_input": {"todos": todos}})));
    assert_eq!(gw.bodies()[0]["args"]["items"], json!([{"id": "1", "subject": "a", "status": "pending"}, {"id": "2", "subject": "b", "status": "completed"}]));
}

#[test]
fn a_prompt_makes_no_call_unless_the_card_waits_for_tony() {
    let (h, gw) = (home(), Gateway::start());
    run(h.path(), &gw.url, prompt("fix the build"));
    run_env(h.path(), &gw.url, prompt("claim task ABCD1234"), &[("CLAUDE_JOB_DIR", "/jobs/j1")], false);
    assert!(gw.calls().is_empty());
}

#[test]
fn the_blocked_marker_follows_the_result() {
    let (h, gw) = (home(), Gateway::start());
    let p = plan(h.path(), "p.md", "x");
    gw.set_reply(json!({"ok": true, "blocked": true}));
    run(h.path(), &gw.url, post("Write", json!({"tool_input": {"file_path": p}})));
    assert!(marker(h.path()).exists());
    gw.set_reply(json!({"ok": true}));
    run(h.path(), &gw.url, prompt("while blocked"));
    assert_eq!(gw.calls().len(), 2);
    assert!(marker(h.path()).exists(), "a result with no blocked flag leaves the marker");
    assert_eq!(gw.bodies()[1], json!({"name": "operations_hook_prompt", "args": {"session_id": "s1", "prompt": "while blocked"}}));
    gw.set_reply(json!({"ok": true, "blocked": false}));
    run(h.path(), &gw.url, prompt("answering the question"));
    assert!(!marker(h.path()).exists());
    run(h.path(), &gw.url, prompt("later"));
    assert_eq!(gw.calls().len(), 3);
}

#[test]
fn a_dead_gateway_still_exits_zero_and_keeps_the_marker() {
    let h = home();
    std::fs::create_dir_all(marker(h.path()).parent().unwrap()).unwrap();
    std::fs::write(marker(h.path()), "").unwrap();
    let dead = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://127.0.0.1:{}", dead.local_addr().unwrap().port());
    drop(dead);
    run(h.path(), &url, prompt("x"));
    assert!(marker(h.path()).exists());
}

#[test]
fn garbage_input_exits_zero() {
    let h = home();
    for input in ["", "not json", "[]", r#"{"hook_event_name": "UserPromptSubmit"}"#] {
        assert_eq!(raw(h.path(), "http://127.0.0.1:9", input, &[]), (0, String::new()), "{input:?}");
    }
}

#[test]
fn the_marker_names_a_session_safely() {
    let m = awm_plugin_operations::hook::marker("a/b");
    assert_eq!(m.file_name().unwrap(), "a_b.blocked");
}
