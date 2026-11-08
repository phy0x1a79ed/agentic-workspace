//! `channel issue`, as the operator daemon runs it (tether's `crates/tether-operator/src/issuer.rs`):
//! one JSON object on stdin, one on stdout, and a non-zero exit with one line on stderr.

use std::io::Write;
use std::process::{Command, Stdio};

fn issue(stdin: &str, hub: &str) -> (i32, String, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_channel"))
        .arg("issue")
        .env_clear()
        .env("AWM_HUB_URL", hub)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(stdin.as_bytes()).unwrap();
    let out = child.wait_with_output().unwrap();
    (out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout).into_owned(), String::from_utf8_lossy(&out.stderr).into_owned())
}

#[test]
fn a_failed_issue_is_one_stderr_line_and_nothing_on_stdout() {
    let (code, out, err) = issue("[1, 2]", "http://127.0.0.1:1");
    assert_eq!((code, out.as_str(), err.trim()), (1, "", "the request is a JSON object"));

    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let (code, out, err) = issue("", &format!("http://127.0.0.1:{port}"));
    assert_eq!(code, 1);
    assert!(out.is_empty());
    assert!(err.starts_with("relay issue: the relay could not be reached"), "{err}");
    assert_eq!(err.trim().lines().count(), 1);
}
