//! The singleton door (fleet.md R98 item 2): a locked command, one call per connection.
//! Ported from test_door.py. The locked command is this crate's own `operations door`.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use awm_plugin_operations::door::{self, DoorClient};
use awm_plugin_operations::relay::{ClientError, StoreClient};
use crate::common::{s, Env, ME};
use serde_json::{json, Value};

const BIN: &str = env!("CARGO_BIN_EXE_operations");

struct Served {
    env: Env,
    path: PathBuf,
    server: tokio::task::JoinHandle<()>,
}

impl Drop for Served {
    fn drop(&mut self) {
        self.server.abort();
    }
}

async fn served() -> Served {
    let env = Env::new();
    let path = env.path("door.sock");
    let server = door::serve(env.home.clone(), &path).await.unwrap();
    Served { env, path, server }
}

/// What sshd runs for a door key: the forced command, here without ssh.
fn locked(path: &Path, node: &str) -> DoorClient {
    let argv = ["env".to_string(), format!("AWM_OPERATIONS_DOOR_SOCKET={}", path.display()), BIN.into(), "door".into(), "--node".into(), node.into()];
    DoorClient { argv: argv.to_vec(), timeout: Duration::from_secs(20) }
}

fn refused(r: Result<Value, ClientError>, needle: &str) {
    match r {
        Err(ClientError::Refused(m)) => assert!(m.contains(needle), "{m:?} lacks {needle:?}"),
        other => panic!("expected a refusal with {needle:?}, got {other:?}"),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_call_crosses_the_door_with_its_node() {
    let sv = served().await;
    let client = locked(&sv.path, "mizar");
    let args = json!({"title": "from mizar", "session_id": ME, "node": "alcor", "op_id": "mizar:1"});
    let out = client.call("task_open", args.clone()).await.unwrap();
    let card = sv.env.store.task_get(&s(&out["task_id"])).unwrap();
    assert_eq!(card["sessions"][0]["node"], "mizar", "the door's key names the node, not the args");
    let again = client.call("task_open", args).await.unwrap();
    assert_eq!(again, out, "the op_id makes the replay a no-op");
}

#[tokio::test(flavor = "multi_thread")]
async fn the_door_refuses_what_is_not_a_home_verb() {
    let sv = served().await;
    refused(locked(&sv.path, "mizar").call("card_action", json!({"action": "close"})).await, "does not take");
    refused(locked(&sv.path, "mizar").call("devices", json!({})).await, "does not take");
    refused(locked(&sv.path, "mizar").call("task_report", json!({"task_id": "nosuchid", "state": "done"})).await, "nosuchid");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_dead_door_is_unreachable() {
    let dir = tempfile::tempdir().unwrap();
    let gone = DoorClient { argv: vec!["/nonexistent/ssh".into()], timeout: Duration::from_secs(5) };
    assert!(matches!(gone.call("keys", json!({})).await, Err(ClientError::Unreachable(_))));
    let missing = locked(&dir.path().join("missing.sock"), "mizar").call("keys", json!({})).await;
    assert!(matches!(missing, Err(ClientError::Unreachable(_))), "{missing:?}");
}

fn ask(path: &Path, req: Value) -> Value {
    let mut sock = std::os::unix::net::UnixStream::connect(path).unwrap();
    sock.write_all(format!("{req}\n").as_bytes()).unwrap();
    let mut line = String::new();
    std::io::BufReader::new(sock).read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn an_operator_command_runs_for_the_services_own_user() {
    // The refusal of another uid needs a second user, which a test cannot be: this checks
    // the commands themselves over the socket and in process.
    let sv = served().await;
    let p = sv.path.clone();
    let devices = tokio::task::spawn_blocking(move || ask(&p, json!({"cmd": "devices"}))).await.unwrap();
    assert_eq!(devices, json!([]));
    let p = sv.path.clone();
    let link = tokio::task::spawn_blocking(move || ask(&p, json!({"cmd": "enrol_link", "minutes": 5}))).await.unwrap();
    assert!(s(&link["url"]).starts_with("https://auth.example.test/enrol#"));
    assert!(link.get("token").is_none(), "the socket never prints the bare token");
    let other = door::operator_command(&sv.env.home, &json!({"cmd": "drop_table"}));
    assert_eq!(other, json!({"error": "commands: enrol_link, devices"}));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_node_pulls_only_its_own_orders() {
    let sv = served().await;
    sv.env.ap.issue_order("agent", "mizar", json!({"task_id": "ABEiM0RV", "node": "mizar"})).unwrap();
    refused(locked(&sv.path, "deneb").call("order_pull", json!({"node": "mizar"})).await, "only its own orders");
    assert_eq!(locked(&sv.path, "deneb").call("order_pull", json!({})).await.unwrap(), json!([]));
    let mine = locked(&sv.path, "mizar").call("order_pull", json!({})).await.unwrap();
    assert_eq!(mine.as_array().unwrap().len(), 1);
}
