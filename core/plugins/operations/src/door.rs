//! alcor's singleton door for operations (fleet.md R98 item 2, R106 item 1).
//!
//! A relay on another node reaches the home verbs over ssh to alcor's door port, with a
//! service key that alcor's `authorized_keys` locks to one command, `operations door --node
//! <node>`. That command reads one JSON call from stdin, hands it to the service on a local
//! unix socket and prints the answer. The key names the node, so a node cannot speak for
//! another. The same socket takes the operator's commands (`operations enrol-link`, R85),
//! only from a process that runs as the service's own user.

use std::io::{BufRead, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

use crate::home::{Home, VERBS};
use crate::relay::{async_trait_free::BoxFut, ClientError, StoreClient};

pub const MAX_LINE: usize = 1 << 20;
pub const TIMEOUT: Duration = Duration::from_secs(75);
pub const OPERATOR_COMMANDS: [&str; 2] = ["enrol_link", "devices"];

pub fn socket_path() -> PathBuf {
    std::env::var("AWM_OPERATIONS_DOOR_SOCKET").ok().filter(|s| !s.is_empty()).map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/".into())).join(".awm/operations/door.sock")
    })
}

/// One local command from an operator on alcor (R85).
pub fn operator_command(home: &Home, req: &Value) -> Value {
    match req.get("cmd").and_then(Value::as_str) {
        Some("enrol_link") => {
            let minutes = req.get("minutes").and_then(Value::as_i64).unwrap_or(15);
            match home.approver.enrol_link(minutes, None) {
                Ok(mut out) => {
                    if let Some(m) = out.as_object_mut() {
                        m.remove("token");
                    }
                    out
                }
                Err(e) => json!({"error": e.message}),
            }
        }
        Some("devices") => home.approver.devices().unwrap_or_else(|e| json!({"error": e.message})),
        _ => json!({"error": format!("commands: {}", OPERATOR_COMMANDS.join(", "))}),
    }
}

/// Serve the door socket: one call per connection, `{node, verb, args}` in, `{ok, …}` out.
pub async fn serve(home: Arc<Home>, path: &Path) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d)?;
    }
    let _ = std::fs::remove_file(path);
    let listener = tokio::net::UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660))?;
    let me = unsafe { libc::getuid() };
    Ok(tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else { continue };
            let home = home.clone();
            tokio::spawn(async move {
                let peer = stream.peer_cred().ok().map(|c| c.uid());
                let (r, mut w) = stream.into_split();
                let mut line = String::new();
                let mut reader = BufReader::new(r.take(MAX_LINE as u64));
                let _ = reader.read_line(&mut line).await;
                let req: Value = serde_json::from_str(line.trim()).unwrap_or(json!({}));
                let out = if req.get("cmd").is_some() {
                    if peer == Some(me) { operator_command(&home, &req) } else { json!({"error": "operator commands run as the service's own user"}) }
                } else {
                    let verb = req.get("verb").and_then(Value::as_str).unwrap_or_default();
                    match home.call(verb, req.get("args").cloned().unwrap_or(json!({})), req.get("node").and_then(Value::as_str)).await {
                        Ok(result) => json!({"ok": true, "result": result}),
                        Err(e) => json!({"ok": false, "refused": true, "error": e}),
                    }
                };
                let _ = w.write_all(format!("{out}\n").as_bytes()).await;
            });
        }
    }))
}

/// `operations door --node <node>`: the command a door key is locked to.
pub fn forced_command(node: &str, path: Option<&Path>) -> i32 {
    let mut line = String::new();
    let _ = std::io::stdin().lock().take(MAX_LINE as u64).read_line(&mut line);
    let req: Value = serde_json::from_str(line.trim()).unwrap_or(json!({}));
    let verb = req.get("verb").and_then(Value::as_str).unwrap_or_default().to_string();
    if !VERBS.contains(&verb.as_str()) {
        println!("{}", json!({"ok": false, "refused": true, "error": format!("the door does not take {verb:?}")}));
        return 0;
    }
    let path = path.map(Path::to_path_buf).unwrap_or_else(socket_path);
    let answer = (|| -> std::io::Result<String> {
        let mut s = std::os::unix::net::UnixStream::connect(&path)?;
        s.set_read_timeout(Some(TIMEOUT))?;
        s.write_all(format!("{}\n", json!({"node": node, "verb": verb, "args": req.get("args").cloned().unwrap_or(json!({}))})).as_bytes())?;
        let mut out = String::new();
        std::io::BufReader::new(s).read_line(&mut out)?;
        Ok(out)
    })();
    match answer {
        Ok(out) => print!("{out}"),
        Err(e) => println!("{}", json!({"ok": false, "error": format!("door socket: {e}")})),
    }
    0
}

/// One operator command over the socket, from `operations enrol-link` or `devices`.
pub fn control(req: &Value) -> std::io::Result<Value> {
    let mut s = std::os::unix::net::UnixStream::connect(socket_path())?;
    s.write_all(format!("{req}\n").as_bytes())?;
    let mut out = String::new();
    std::io::BufReader::new(s).read_line(&mut out)?;
    Ok(serde_json::from_str(&out).unwrap_or(Value::Null))
}

/// The ssh command that reaches alcor's door, from `AWM_OPERATIONS_DOOR` (`user@host:port`).
pub fn door_argv() -> Option<Vec<String>> {
    let door = std::env::var("AWM_OPERATIONS_DOOR").ok()?.trim().to_string();
    if door.is_empty() {
        return None;
    }
    let (target, port) = door.rsplit_once(':')?;
    let mut argv: Vec<String> = ["ssh", "-T", "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15"]
        .iter().map(|s| s.to_string()).collect();
    if let Some(key) = std::env::var("AWM_OPERATIONS_DOOR_KEY").ok().filter(|k| !k.is_empty()) {
        argv.extend(["-o".into(), "IdentitiesOnly=yes".into(), "-i".into(), key]);
    }
    argv.extend([target.to_string(), "door".into()]);
    Some(argv)
}

/// A relay's way to the home verbs from any node but alcor.
pub struct DoorClient {
    pub argv: Vec<String>,
    pub timeout: Duration,
}

impl StoreClient for DoorClient {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, ClientError>> {
        Box::pin(async move {
            let mut cmd = tokio::process::Command::new(&self.argv[0]);
            cmd.args(&self.argv[1..]).stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped()).kill_on_drop(true);
            let mut child = cmd.spawn().map_err(|e| ClientError::Unreachable(format!("door: {e}")))?;
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(format!("{}\n", json!({"verb": verb, "args": args})).as_bytes()).await;
            }
            let out = tokio::time::timeout(self.timeout, child.wait_with_output()).await
                .map_err(|_| ClientError::Unreachable("door: timed out".into()))?
                .map_err(|e| ClientError::Unreachable(format!("door: {e}")))?;
            let text = String::from_utf8_lossy(&out.stdout);
            let Some(reply) = text.trim().lines().last().and_then(|l| serde_json::from_str::<Value>(l).ok()) else {
                let err = String::from_utf8_lossy(&out.stderr);
                let tail: String = err.chars().rev().take(200).collect::<Vec<_>>().into_iter().rev().collect();
                return Err(ClientError::Unreachable(format!("door exit {:?}: {tail}", out.status.code())));
            };
            if reply.get("ok") == Some(&json!(true)) {
                return Ok(reply.get("result").cloned().unwrap_or(Value::Null));
            }
            let e = reply.get("error").and_then(Value::as_str).unwrap_or("door fault").to_string();
            if reply.get("refused") == Some(&json!(true)) { Err(ClientError::Refused(e)) } else { Err(ClientError::Unreachable(e)) }
        })
    }
}
