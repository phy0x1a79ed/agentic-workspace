//! The real binary end to end: `serve` dials one provider as a child and one over `ssh localhost`,
//! a caller offers through the socket, and MCP leaves out `watch`.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_baitan");

fn baitan(home: &Path, args: &[&str]) -> Value {
    let out = Command::new(BIN).args(args).env("HOME", home).env_remove("BAITAN_CONNECT").output().unwrap();
    assert!(out.status.success(), "baitan {args:?}: {}", String::from_utf8_lossy(&out.stderr));
    serde_json::from_slice(&out.stdout).unwrap()
}

fn provider_toml(home: &Path, id: &str) -> String {
    let path = home.join(format!("{id}.toml"));
    std::fs::write(&path, format!("id = \"{id}\"\nstate_dir = \"{}\"\n[props]\nname = \"{id}\"\n", home.join(id).display())).unwrap();
    path.display().to_string()
}

/// Offers a task at `place` and waits for it to settle. No caller verb reports a provider
/// registering, so a `NoBids` while the dialled link comes up is offered again.
fn settle_at(home: &Path, place: &str) -> Value {
    let set = json!({
        "steps": [{"op": "task", "props": {"contract": "e2e", "place": place}, "slots": ["out"],
                   "spec": {"script": "echo hi > out.txt", "stage": {}, "outputs": ["out.txt"]}}],
        "place": {"name": place},
    });
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut promise = String::new();
    loop {
        if promise.is_empty() {
            promise = baitan(home, &["offer", "--set", &set.to_string()])["promise"].as_str().unwrap().to_string();
        }
        let status = baitan(home, &["status", "--promise", &promise]);
        let phase = &status["phase"];
        if phase.get("settled").is_some() {
            return status;
        }
        if phase["failed"]["name"] == "NoBids" {
            promise.clear();
        } else {
            assert!(phase.get("failed").is_none(), "{status}");
        }
        assert!(Instant::now() < deadline, "not settled at {place}: {status}");
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// Kills the child even when an assertion panics, so it cannot hold the test harness's pipes open.
struct Reap(std::process::Child);

impl Drop for Reap {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn serve_dials_providers_and_callers_offer_through_the_socket() {
    let home = tempfile::tempdir().unwrap();
    let home = home.path();
    let local = provider_toml(home, "near");
    let remote = provider_toml(home, "far");
    std::fs::create_dir_all(home.join(".baitan")).unwrap();
    let links = json!([
        format!("{BIN} provider --config {local}"),
        format!("ssh -o BatchMode=yes localhost HOME={} {BIN} provider --config {remote}", home.display()),
    ]);
    std::fs::write(home.join(".baitan/baitan.toml"), format!("links = {links}\n")).unwrap();
    let _serve = Reap(Command::new(BIN).arg("serve").env("HOME", home).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap());
    while !home.join(".baitan/market.sock").exists() {
        std::thread::sleep(Duration::from_millis(50));
    }

    for place in ["near", "far"] {
        let status = settle_at(home, place);
        let phase = &status["phase"]["settled"];
        assert_eq!(phase["provider"], place);
        let listing: Vec<&Value> = phase["listing"].as_object().unwrap().values().collect();
        assert_eq!(listing[0], &json!(["out.txt"]));
    }

    let mut mcp = Reap(Command::new(BIN).arg("mcp").env("HOME", home).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap());
    let mcp = &mut mcp.0;
    let mut stdin = mcp.stdin.take().unwrap();
    writeln!(stdin, "{}", json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).unwrap();
    let mut line = String::new();
    BufReader::new(mcp.stdout.take().unwrap()).read_line(&mut line).unwrap();
    let names: Vec<String> = serde_json::from_str::<Value>(&line).unwrap()["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(names, ["offer", "status", "cancel", "dispose", "review"]);
}
