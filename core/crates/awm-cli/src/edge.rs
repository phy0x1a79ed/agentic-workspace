//! `awm edge-serve` and `awm port`: the two forced ends of an ssh login.
//!
//! Both hand each call to this node's loopback gateway. `edge-serve` vouches for
//! the calling node with `X-Awm-Caller`, which the gateway honours only next to
//! the edge token in `<AWM_DIR>/edge.token`. `port` runs at the service lane.

use std::path::{Path, PathBuf};

use awm_proto::{Caller, Lane};
use awm_trust::edges::{edges_path, EdgeList};
use serde_json::{json, Value};

use crate::gateway_url;

fn awm_dir() -> anyhow::Result<PathBuf> {
    std::env::var_os("AWM_DIR").map(PathBuf::from).ok_or_else(|| anyhow::anyhow!("AWM_DIR is not set"))
}

fn node_name() -> String {
    std::env::var("AWM_NODE_NAME").ok().filter(|s| !s.trim().is_empty()).unwrap_or_else(|| {
        std::fs::read_to_string("/etc/hostname").map(|s| s.trim().to_string()).unwrap_or_default()
    })
}

/// POST one call to the loopback gateway as `caller`. Returns `result`, or the error detail.
pub async fn invoke_local(dir: &Path, name: &str, args: Value, caller: &Caller) -> Result<Value, String> {
    let token = std::fs::read_to_string(dir.join("edge.token")).map_err(|e| format!("edge token: {e}"))?;
    let client = reqwest::Client::new();
    let base = gateway_url();
    let req = if name == awm_edge::TOOLS_FRAME {
        client.get(format!("{base}/hub/uuids"))
    } else {
        client.post(format!("{base}/invoke")).json(&json!({"name": name, "args": args}))
    };
    let resp = req
        .header("X-Awm-Caller", serde_json::to_string(caller).expect("json"))
        .header("X-Awm-Edge-Token", token.trim())
        .send()
        .await
        .map_err(|e| format!("gateway unreachable: {e}"))?;
    let ok = resp.status().is_success();
    let body: Value = resp.json().await.unwrap_or(Value::Null);
    match (ok, name == awm_edge::TOOLS_FRAME) {
        (true, true) => Ok(body),
        (true, false) => Ok(body.get("result").cloned().unwrap_or(Value::Null)),
        (false, _) => Err(match body.get("detail").cloned().unwrap_or(body) {
            Value::String(s) => s,
            other => other.to_string(),
        }),
    }
}

/// `awm edge-serve`: sshd runs it for a node's user key. Serves stdin until it closes.
pub async fn edge_serve() -> anyhow::Result<i32> {
    let dir = awm_dir()?;
    let auth = std::env::var_os("SSH_USER_AUTH")
        .ok_or_else(|| anyhow::anyhow!("SSH_USER_AUTH is not set: sshd needs ExposeAuthInfo yes"))?;
    let keys = dirs_home().join(".ssh/authorized_keys");
    let from = awm_edge::caller_node(&std::fs::read_to_string(auth)?, &std::fs::read_to_string(&keys)?)
        .ok_or_else(|| anyhow::anyhow!("this login's key carries no awm:<node>:user comment"))?;
    let here = node_name();
    let edges = EdgeList::load(&edges_path(&dir))?;
    if !edges.reaches(&from, &here) {
        anyhow::bail!("no edge from {from} to {here}");
    }
    awm_edge::serve(tokio::io::stdin(), tokio::io::stdout(), from, move |name, args, caller| {
        let dir = dir.clone();
        async move { invoke_local(&dir, &name, args, &caller).await }
    })
    .await;
    Ok(0)
}

/// `awm port --key awm:<holder>:service`: one door call from `SSH_ORIGINAL_COMMAND`.
pub async fn port(key: &str) -> anyhow::Result<i32> {
    let dir = awm_dir()?;
    let here = node_name();
    let allowed = EdgeList::load(&edges_path(&dir))?.door_tools(&here);
    let original = std::env::var("SSH_ORIGINAL_COMMAND").unwrap_or_default();
    let holder = key.split(':').nth(1).unwrap_or_default().to_string();
    let caller = Caller { node: holder, lane: Lane::Service, session: None, trust: None };
    let req = awm_trust::port::parse(&original);
    let result = match &req {
        Ok(r) if allowed.contains(&r.name) => Some(invoke_local(&dir, &r.name, r.args.clone(), &caller).await),
        _ => None,
    };
    let reply = awm_trust::port::serve(&original, key, &allowed, &dir.join("port.log"), |_| {
        result.expect("invoked only for an allowed tool")
    });
    println!("{reply}");
    Ok(if reply.contains(r#""ok":true"#) { 0 } else { 1 })
}

fn dirs_home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// The gateway with peers over the edges, when the install config lists no
/// HTTP peers and the edges file names this node. Else the config's peers.
pub async fn open_gateway(state: &Path) -> anyhow::Result<std::sync::Arc<awm_gateway::Gateway>> {
    let cfg = awm_gateway::config::Config::load(state)?;
    let path = cfg.edges_path(state);
    let here = cfg.node_name();
    if cfg.peer.is_empty() && path.exists() {
        let edges = EdgeList::load(&path)?;
        if edges.nodes.contains_key(&here) {
            let peers = awm_edge::EdgePeers::new(here, edges, awm_edge::EdgeClient::ssh());
            return awm_gateway::Gateway::open_with(state, cfg, std::sync::Arc::new(peers)).await;
        }
    }
    awm_gateway::Gateway::open(state).await
}
