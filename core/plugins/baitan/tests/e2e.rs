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

fn offer(home: &Path, place: &str) -> String {
    let set = json!({
        "steps": [{"op": "task", "props": {"contract": "e2e", "place": place}, "slots": ["out"],
                   "spec": {"script": "echo hi > out.txt", "stage": {}, "outputs": ["out.txt"]}}],
        "place": {"name": place},
    });
    let reply = baitan(home, &["offer", "--set", &set.to_string()]);
    reply["promise"].as_str().unwrap().to_string()
}

fn settled(home: &Path, promise: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let status = baitan(home, &["status", "--promise", promise]);
        if status["phase"].get("settled").is_some() {
            return status;
        }
        assert!(status["phase"].get("failed").is_none(), "{status}");
        assert!(Instant::now() < deadline, "not settled: {status}");
        std::thread::sleep(Duration::from_millis(200));
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
    let mut serve = Command::new(BIN).arg("serve").env("HOME", home).spawn().unwrap();
    std::thread::sleep(Duration::from_secs(3));

    for place in ["near", "far"] {
        let promise = offer(home, place);
        let status = settled(home, &promise);
        let phase = &status["phase"]["settled"];
        assert_eq!(phase["provider"], place);
        let listing: Vec<&Value> = phase["listing"].as_object().unwrap().values().collect();
        assert_eq!(listing[0], &json!(["out.txt"]));
    }

    let mut mcp = Command::new(BIN).arg("mcp").env("HOME", home).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
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
    let _ = mcp.kill();
    let _ = serve.kill();
}
