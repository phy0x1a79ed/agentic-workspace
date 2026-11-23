//! The real binary end to end: `serve` dials one provider as a child and one over `ssh localhost`,
//! raw offer sets settle at each, a transform-shaped `offer` settles and copies its outputs home,
//! and MCP leaves out `watch`.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_outsource");

fn outsource(home: &Path, args: &[&str]) -> Value {
    let out = Command::new(BIN).args(args).env("HOME", home).env("OUTSOURCE_NODE", "near").env_remove("OUTSOURCE_CONNECT").output().unwrap();
    assert!(out.status.success(), "outsource {args:?}: {}", String::from_utf8_lossy(&out.stderr));
    serde_json::from_slice(&out.stdout).unwrap()
}

fn provider_toml(home: &Path, id: &str) -> String {
    let path = home.join(format!("{id}.toml"));
    std::fs::write(&path, format!("id = \"{id}\"\nstate_dir = \"{}\"\n[props]\nname = \"{id}\"\n", home.join(id).display())).unwrap();
    path.display().to_string()
}

/// Posts a raw offer set through the market socket, as a provider-facing client would: the
/// caller verb takes only the transform shape, which cannot pick a provider.
fn raw_offer(home: &Path, set: &Value) -> String {
    let env = json!({"v": 1, "id": "e2e", "from": "e2e", "to": "market", "selector": "offer", "args": {"set": set}});
    let mut sock = UnixStream::connect(home.join(".outsource/market.sock")).unwrap();
    writeln!(sock, "{env}").unwrap();
    let mut line = String::new();
    BufReader::new(sock).read_line(&mut line).unwrap();
    serde_json::from_str::<Value>(&line).unwrap()["args"]["promise"].as_str().unwrap().to_string()
}

/// Waits for a promise to settle. No caller verb reports a provider registering, so `post` is
/// called again after a `NoBids` while the dialled link comes up.
fn settled(home: &Path, what: &str, post: impl Fn() -> String) -> Value {
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut promise = String::new();
    loop {
        if promise.is_empty() {
            promise = post();
        }
        let status = outsource(home, &["status", "--promise", &promise]);
        let phase = &status["phase"];
        if phase.get("settled").is_some() {
            return status;
        }
        if phase["failed"]["name"] == "NoBids" {
            promise.clear();
        } else {
            assert!(phase.get("failed").is_none(), "{status}");
        }
        assert!(Instant::now() < deadline, "not settled ({what}): {status}");
        std::thread::sleep(Duration::from_millis(250));
    }
}

fn settle_at(home: &Path, place: &str) -> Value {
    let set = json!({
        "steps": [{"op": "task", "props": {"contract": "e2e", "place": place}, "slots": ["out"],
                   "spec": {"script": "echo hi > out.txt", "stage": {}, "outputs": ["out.txt"]}}],
        "place": {"name": place},
    });
    settled(home, place, || raw_offer(home, &set))
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
    std::fs::create_dir_all(home.join(".outsource")).unwrap();
    let links = json!([
        format!("{BIN} provider --config {local}"),
        format!("ssh -o BatchMode=yes localhost HOME={} {BIN} provider --config {remote}", home.display()),
    ]);
    std::fs::write(home.join(".outsource/outsource.toml"), format!("links = {links}\n")).unwrap();
    let _serve = Reap(Command::new(BIN).arg("serve").env("HOME", home).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap());
    while !home.join(".outsource/market.sock").exists() {
        std::thread::sleep(Duration::from_millis(50));
    }

    let work = home.join("work");
    std::fs::create_dir_all(&work).unwrap();
    std::fs::write(work.join("in.txt"), "data").unwrap();
    let args = ["offer", "--requires", "in.txt", "--produces", "res", "--protocol", "mkdir res && cp in.txt res/copy.txt", "--dest", "dest"];
    let post = || {
        let out = Command::new(BIN).args(args).current_dir(&work).env("HOME", home).env("OUTSOURCE_NODE", "near").output().unwrap();
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        serde_json::from_slice::<Value>(&out.stdout).unwrap()["promise"].as_str().unwrap().to_string()
    };
    settled(home, "transform offer", post);
    assert_eq!(std::fs::read_to_string(work.join("dest/res/copy.txt")).unwrap(), "data");

    for place in ["near", "far"] {
        let status = settle_at(home, place);
        let phase = &status["phase"]["settled"];
        assert_eq!(phase["provider"], place);
        let listing: Vec<&Value> = phase["listing"].as_object().unwrap().values().collect();
        assert_eq!(listing[0], &json!(["out.txt"]));
    }

    // The vocabulary rides the same socket and is kept under HOME.
    assert!(outsource(home, &["properties"])["properties"]["db"].is_object());
    outsource(home, &["property_add", "--name", "organism", "--description", "source organism", "--values", "Human"]);
    assert_eq!(outsource(home, &["properties"])["properties"]["organism"]["values"], json!(["human"]));
    assert!(home.join(".outsource/vocab.json").exists());

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
    assert_eq!(names, ["offer", "prep", "import", "property_add", "properties", "status", "cancel", "dispose", "review"]);
}
