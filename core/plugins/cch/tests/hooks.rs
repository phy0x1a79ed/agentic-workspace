//! `cch hook <name>` run as the built binary: the name hook's cases, and each hook end to end
//! against a fake gateway on a local port.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use awm_harness::claude::ClaudeHome;
use cch::paths::Paths;
use serde_json::{json, Value};

const CCH: &str = env!("CARGO_BIN_EXE_cch");

/// Runs `cch hook <args>` with only `env` set and `input` on stdin; answers its output and pid.
fn hook(args: &[&str], input: &str, env: &[(&str, &Path)]) -> (Output, u32) {
    let mut cmd = Command::new(CCH);
    cmd.arg("hook").args(args).env_clear().env("PATH", "/usr/bin:/bin");
    for (k, v) in env {
        cmd.env(k, v);
    }
    let mut child = cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    let pid = child.id();
    child.stdin.take().unwrap().write_all(input.as_bytes()).unwrap();
    (child.wait_with_output().unwrap(), pid)
}

/// A gateway on a local port answering every request with `body`; each request arrives on the receiver.
fn gateway(body: &'static str) -> (String, mpsc::Receiver<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut got = Vec::new();
            let mut buf = [0u8; 4096];
            while let Ok(n) = stream.read(&mut buf) {
                got.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&got).into_owned();
                let done = text.find("\r\n\r\n").is_some_and(|split| {
                    let length = text[..split]
                        .lines()
                        .find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap()))
                        .unwrap_or(0);
                    got.len() >= split + 4 + length
                });
                if done || n == 0 {
                    break;
                }
            }
            let reply = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
            let _ = stream.write_all(reply.as_bytes());
            let _ = tx.send(String::from_utf8_lossy(&got).into_owned());
        }
    });
    (url, rx)
}

fn header_pid(request: &str) -> u32 {
    request
        .lines()
        .find_map(|l| l.strip_prefix("X-Awm-Caller-Pid: "))
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or_else(|| panic!("no caller pid in {request}"))
}

fn body(request: &str) -> Value {
    serde_json::from_str(request.split("\r\n\r\n").nth(1).unwrap()).unwrap()
}

/// A job directory holding a freshly claimed session's state record.
fn job() -> (tempfile::TempDir, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("jobs").join("job0001");
    std::fs::create_dir_all(&dir).unwrap();
    let state = json!({"name": "claimed dunlin", "nameSource": "user", "intent": "", "cwd": "/home/user", "state": "working"});
    std::fs::write(dir.join("state.json"), state.to_string()).unwrap();
    (tmp, dir)
}

fn run(job: &Path, payload: Value) -> Value {
    run_with(job, payload, &[])
}

fn run_with(job: &Path, payload: Value, env: &[(&str, &Path)]) -> Value {
    let mut env = env.to_vec();
    env.push(("CLAUDE_JOB_DIR", job));
    let (out, _) = hook(&["name"], &payload.to_string(), &env);
    assert!(out.status.success());
    assert_eq!(out.stdout, b"");
    serde_json::from_slice(&std::fs::read(job.join("state.json")).unwrap()).unwrap()
}

#[test]
fn test_a_first_prompt_clears_the_name_and_records_the_intent() {
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "fix the flaky import test"}));
    assert!(state.get("name").is_none());
    assert!(state.get("nameSource").is_none());
    assert_eq!(state["intent"], "fix the flaky import test");
}

#[test]
fn test_the_rest_of_the_record_is_left_exactly_as_it_was() {
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "hello there"}));
    assert_eq!(state["cwd"], "/home/user");
    assert_eq!(state["state"], "working");
}

#[test]
fn test_a_slash_command_is_not_a_first_prompt() {
    // `/cd` is typed into every session the pool hands out.
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "/cd /home/user/project"}));
    assert_eq!(state["name"], "claimed dunlin");
}

#[test]
fn test_a_session_that_is_not_the_pool_s_is_untouched() {
    let (_tmp, job) = job();
    std::fs::write(job.join("state.json"), json!({"name": "remote shell"}).to_string()).unwrap();
    let state = run(&job, json!({"source": "user", "prompt": "carry on"}));
    assert_eq!(state, json!({"name": "remote shell"}));
}

#[test]
fn test_a_session_that_has_already_been_titled_is_untouched() {
    // The name the namer wrote must survive every prompt after the first.
    let (_tmp, job) = job();
    std::fs::write(job.join("state.json"), json!({"name": "flaky import test", "nameSource": "auto"}).to_string()).unwrap();
    let state = run(&job, json!({"source": "user", "prompt": "and now the other one"}));
    assert_eq!(state["name"], "flaky import test");
}

#[test]
fn test_a_wake_up_is_not_a_person_typing() {
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "schedule_wakeup", "prompt": "check the run"}));
    assert_eq!(state["name"], "claimed dunlin");
}

#[test]
fn test_injected_context_is_not_part_of_the_intent() {
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "<system-reminder>noise</system-reminder>  the real ask"}));
    assert_eq!(state["intent"], "the real ask");
}

#[test]
fn test_a_long_prompt_is_truncated_the_way_the_vendor_truncates_it() {
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "x".repeat(900)}));
    assert_eq!(state["intent"].as_str().unwrap().len(), 500);
}

#[test]
fn test_an_empty_intent_is_never_written() {
    // `??` merges this field forward, so an empty string would stick for the life of the session.
    let (_tmp, job) = job();
    let state = run(&job, json!({"source": "user", "prompt": "   "}));
    assert_eq!(state["name"], "claimed dunlin");
}

#[test]
fn test_a_session_with_no_job_directory_is_a_no_op() {
    let (_tmp, job) = job();
    let (out, _) = hook(&["name"], &json!({"source": "user", "prompt": "hello"}).to_string(), &[]);
    assert!(out.status.success());
    let state: Value = serde_json::from_slice(&std::fs::read(job.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["name"], "claimed dunlin");
}

#[test]
fn test_garbage_on_standard_input_costs_nobody_a_prompt() {
    let (_tmp, job) = job();
    let (out, _) = hook(&["name"], "not json at all", &[("CLAUDE_JOB_DIR", &job)]);
    assert!(out.status.success());
    assert_eq!(out.stdout, b"");
}

#[test]
fn test_an_unreadable_state_record_costs_nobody_a_prompt() {
    let (_tmp, job) = job();
    std::fs::write(job.join("state.json"), "{ truncated").unwrap();
    let (out, _) = hook(&["name"], &json!({"source": "user", "prompt": "hello"}).to_string(), &[("CLAUDE_JOB_DIR", &job)]);
    assert!(out.status.success());
    assert_eq!(std::fs::read_to_string(job.join("state.json")).unwrap(), "{ truncated");
}

#[test]
fn test_the_hook_and_the_service_agree_on_the_claimed_prefix() {
    // The hook and the pool read the one `Paths::claimed_prefix`, the default and the override alike.
    let tmp = tempfile::tempdir().unwrap();
    let env = Paths::from_vars(ClaudeHome::at(tmp.path(), tmp.path().join(".claude.json")), tmp.path().into(), |_| None);
    assert_eq!(env.claimed_prefix, "claimed ");
    assert_eq!(Paths::under(tmp.path()).claimed_prefix, env.claimed_prefix);
    let (_tmp, job) = job();
    std::fs::write(job.join("state.json"), json!({"name": "held dunlin"}).to_string()).unwrap();
    let prefix = Path::new("held ");
    let state = run_with(&job, json!({"source": "user", "prompt": "hello"}), &[("AWM_CX_CLAIMED_PREFIX", prefix)]);
    assert_eq!(state, json!({"intent": "hello"}));
}

#[test]
fn brief_reaches_the_gateway_with_the_parent_pid_and_prints_the_line() {
    let tmp = tempfile::tempdir().unwrap();
    let wt = tmp.path().join("ws/projects/demo/main");
    std::fs::create_dir_all(&wt).unwrap();
    std::fs::write(wt.join(".git"), "gitdir: x").unwrap();
    let (url, got) = gateway(r#"{"result": {"brief": {"line": "The brief of demo/main."}}}"#);
    let input = json!({"cwd": wt, "source": "startup", "session_id": "00000000-0000-4000-8000-000000000001"});
    let (state, claude) = (tmp.path().join("state"), tmp.path().join("claude"));
    let env = [("HOME", tmp.path()), ("XDG_STATE_HOME", &state), ("CLAUDE_CONFIG_DIR", &claude), ("AWM_HUB_URL", Path::new(&url))];
    let (out, _) = hook(&["brief"], &input.to_string(), &env);
    assert!(out.status.success());
    let printed: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(printed["hookSpecificOutput"]["additionalContext"], "The brief of demo/main.");
    let request = got.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(body(&request), json!({"name": "journal_brief_read", "args": {"project": "demo", "scope": "main"}}));
    assert_eq!(header_pid(&request), std::process::id());
}

#[test]
fn plan_mode_sends_its_own_pid_and_prints_nothing() {
    let tmp = tempfile::tempdir().unwrap();
    let (url, got) = gateway(r#"{"result": {"ok": true, "mode": "bypassPermissions"}}"#);
    let input = json!({"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
        "session_id": "00000000-0000-4000-8000-000000000002", "permission_mode": "auto",
        "tool_response": {"plan": "# a plan"}});
    let claude = tmp.path().join("claude");
    std::fs::create_dir_all(&claude).unwrap();
    let env = [("HOME", tmp.path()), ("CLAUDE_CONFIG_DIR", &claude), ("AWM_HUB_URL", Path::new(&url))];
    let (out, pid) = hook(&["plan-mode"], &input.to_string(), &env);
    assert!(out.status.success());
    assert_eq!(out.stdout, b"");
    let request = got.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(body(&request), json!({"name": "reflection_mode", "args": {"expect_session": "00000000-0000-4000-8000-000000000002"}}));
    assert_eq!(header_pid(&request), pid);
    assert!(!claude.join("awm-reflection-mode.log").exists(), "a switch that worked logs nothing");
}

#[test]
fn subagent_cap_asks_the_level_only_when_full_and_denies_below_trusted() {
    let tmp = tempfile::tempdir().unwrap();
    let (url, got) = gateway(r#"{"result": "{\"trust\": \"cooperative\"}"}"#);
    let input = json!({"session_id": "00000000-0000-4000-8000-000000000003", "hook_event_name": "PreToolUse", "tool_name": "Agent"});
    let state = tmp.path().join("state");
    let env = [("HOME", tmp.path()), ("XDG_STATE_HOME", &state), ("AWM_HUB_URL", Path::new(&url))];
    for _ in 0..5 {
        let (out, _) = hook(&["subagent-cap"], &input.to_string(), &env);
        assert!(out.status.success());
        assert_eq!(out.stdout, b"");
    }
    assert!(got.try_recv().is_err(), "the level is asked only when the cap is full");
    let (out, _) = hook(&["subagent-cap"], &input.to_string(), &env);
    let printed: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(printed["hookSpecificOutput"]["permissionDecision"], "deny");
    let request = got.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(body(&request), json!({"name": "reflection_whoami", "args": {}}));
    assert_eq!(header_pid(&request), std::process::id());
    assert!(state.join("awm/cch/subagents/00000000-0000-4000-8000-000000000003.json").exists());
}

#[test]
fn garbage_on_stdin_exits_zero_and_prints_nothing_for_every_hook() {
    let tmp = tempfile::tempdir().unwrap();
    let env = [("HOME", tmp.path()), ("XDG_STATE_HOME", tmp.path()), ("AWM_HUB_URL", Path::new("http://127.0.0.1:9"))];
    for args in [&["brief"][..], &["plan-mode"], &["name"], &["subagent-cap"], &["no-such-hook"], &[]] {
        let (out, _) = hook(args, "\u{0}not json at all{", &env);
        assert!(out.status.success(), "{args:?}");
        assert_eq!(out.stdout, b"", "{args:?}");
        assert_eq!(out.stderr, b"", "{args:?}");
    }
}
