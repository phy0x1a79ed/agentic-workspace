//! `awm edge-serve` and `awm port`: the far ends of an ssh login from another node.
//!
//! Both hand each call to this node's loopback gateway. `edge-serve` vouches for
//! the calling node with `X-Awm-Caller`, which the gateway honours only next to
//! the edge token in `<AWM_DIR>/edge.token`. `port` runs at the service lane.

use std::path::Path;

use awm_proto::{Caller, Lane};
use awm_trust::edges::{edges_path, EdgeList};
use serde_json::{json, Value};

use crate::{awm_dir, gateway_url};


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
    let frame = name == awm_edge::TOOLS_FRAME || name == awm_edge::TABLE_CHANGED_FRAME;
    let req = if name == awm_edge::TOOLS_FRAME {
        client.get(format!("{base}/hub/table"))
    } else if name == awm_edge::TABLE_CHANGED_FRAME {
        // The edge stamped the calling node; a peer reports only its own table.
        client.post(format!("{base}/hub/table/changed")).json(&json!({"node": caller.node}))
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
    match (ok, frame) {
        (true, true) => Ok(body),
        (true, false) => Ok(body.get("result").cloned().unwrap_or(Value::Null)),
        (false, _) => Err(match body.get("detail").cloned().unwrap_or(body) {
            Value::String(s) => s,
            other => other.to_string(),
        }),
    }
}

/// `awm edge-serve`: a login over an edge runs it, and serves stdin until it closes. A user
/// key's login names its node with `AWM_EDGE_FROM`. A service key's forced command names its node and
/// `--level service`, and sshd puts what the client sent in `SSH_ORIGINAL_COMMAND`, which is
/// never read for either.
pub async fn edge_serve(from: Option<&str>, level: Option<&str>) -> anyhow::Result<i32> {
    let dir = awm_dir();
    let here = node_name();
    let edges = EdgeList::load(&edges_path(&dir))?;
    let original = std::env::var("SSH_ORIGINAL_COMMAND").ok();
    let from = from.map(str::to_string).or_else(|| std::env::var("AWM_EDGE_FROM").ok());
    let login = awm_edge::login(from.as_deref(), level, original.as_deref(), &edges, &here).map_err(anyhow::Error::msg)?;
    let service = (login.level == awm_edge::EdgeLevel::Service)
        .then(|| awm_edge::ServiceGate { tools: edges.door_tools(&here, &login.from), log: Some(dir.join("port.log")) });
    let from = login.from;
    let stream_dir = dir.clone();
    awm_edge::serve(
        tokio::io::stdin(),
        tokio::io::stdout(),
        from,
        service,
        move |name, args, caller| {
            let dir = dir.clone();
            async move { invoke_local(&dir, &name, args, &caller).await }
        },
        move |path, caller| {
            let dir = stream_dir.clone();
            async move { open_local_stream(&dir, &path, &caller).await }
        },
    )
    .await;
    Ok(0)
}

/// Open a stream path on the loopback gateway as `caller`, vouched for by the edge token.
pub async fn open_local_stream(dir: &Path, path: &str, caller: &Caller) -> Result<awm_gateway::peer::WsPipe, String> {
    if !awm_gateway::peer::stream_path_allowed(path) {
        return Err(format!("{path} is not a stream path"));
    }
    let token = std::fs::read_to_string(dir.join("edge.token")).map_err(|e| format!("edge token: {e}"))?;
    let url = format!("{}{path}", gateway_url().replacen("http", "ws", 1));
    let headers = [
        ("x-awm-caller", serde_json::to_string(caller).expect("json")),
        ("x-awm-edge-token", token.trim().to_string()),
    ];
    awm_gateway::peer::connect_ws(&url, &headers).await
}

/// The request text: stdin when the caller asked for the bare `port`, else what the command
/// line carried (a node still on the older door client sends it there).
fn door_request_text() -> String {
    let original = std::env::var("SSH_ORIGINAL_COMMAND").unwrap_or_default();
    if original.trim().is_empty() || original.trim() == "port" {
        let mut body = String::new();
        let _ = std::io::Read::read_to_string(&mut std::io::stdin().lock(), &mut body);
        return body;
    }
    original
}

/// `awm port --key awm:<holder>:service`: one door call, read by `door_request_text`, for
/// a singleton here that admits the key's holder.
pub async fn port(key: &str) -> anyhow::Result<i32> {
    let dir = awm_dir();
    let here = node_name();
    let holder = key.split(':').nth(1).unwrap_or_default().to_string();
    let allowed = EdgeList::load(&edges_path(&dir))?.door_tools(&here, &holder);
    let original = door_request_text();
    let req = awm_trust::port::parse(&original);
    let result = match &req {
        Ok(r) if allowed.contains(&r.name) => {
            // The gateway checks the node a relayed call started from against the callers too.
            // The calling session, its level and scope are the calling node's word, as on an edge.
            let origin = r.origin.clone().filter(|o| *o != holder);
            let caller = Caller {
                node: holder.clone(),
                lane: Lane::Service,
                origin,
                session: r.session.clone(),
                trust: r.trust.clone(),
                scope: r.scope.clone(),
                name: r.session_name.clone(),
                kind: r.session_kind.clone(),
            };
            Some(invoke_local(&dir, &r.name, r.args.clone(), &caller).await)
        }
        _ => None,
    };
    let reply = awm_trust::port::serve(&original, key, &allowed, &dir.join("port.log"), |_| {
        result.expect("invoked only for an allowed tool")
    });
    println!("{reply}");
    Ok(if reply.contains(r#""ok":true"#) { 0 } else { 1 })
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
            let client = awm_edge::EdgeClient::ssh(&here, &edges);
            let peers = awm_edge::EdgePeers::new(here, edges, client);
            return awm_gateway::Gateway::open_with(state, cfg, std::sync::Arc::new(peers)).await;
        }
    }
    awm_gateway::Gateway::open(state).await
}
