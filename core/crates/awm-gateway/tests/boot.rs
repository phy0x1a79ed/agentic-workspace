//! The gateway serves from its cached peer tables while a slow or dead peer is still asked.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use awm_gateway::config::Config;
use awm_gateway::peer::{BoxFuture, PeerError, PeerTransport};
use awm_gateway::Gateway;
use awm_proto::CallContext;
use serde_json::{json, Value};

/// Each peer answers `tools` with its table after its delay.
struct DelayedPeers(BTreeMap<String, (Duration, Value)>);

impl PeerTransport for DelayedPeers {
    fn peers(&self) -> Vec<String> {
        self.0.keys().cloned().collect()
    }

    fn invoke<'a>(&'a self, peer: &'a str, _: &'a str, _: Value, _: &'a CallContext) -> BoxFuture<'a, Result<String, PeerError>> {
        Box::pin(async move { Err(PeerError::Unreachable { peer: peer.into(), reason: "test".into() }) })
    }

    fn tools<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, PeerError>> {
        Box::pin(async move {
            let (delay, table) = self.0[peer].clone();
            tokio::time::sleep(delay).await;
            Ok(table)
        })
    }
}

fn table(tool: &str) -> Value {
    json!([{"name": tool, "major": 1, "plugin": tool}])
}

async fn owner_peer(url: &str, tool: &str) -> Option<String> {
    let v: Value = reqwest::get(format!("{url}/hub/tool/{tool}")).await.ok()?.json().await.ok()?;
    v.get("peer").and_then(Value::as_str).map(str::to_string)
}

#[tokio::test]
async fn a_dead_peer_does_not_hold_up_serving() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join(".awm");
    let roots = dir.path().join("roots");
    std::fs::create_dir_all(&state).unwrap();
    std::fs::create_dir_all(&roots).unwrap();
    std::fs::write(
        state.join("config.toml"),
        format!("node = \"here\"\nlisten = \"127.0.0.1:0\"\nworkspace = {:?}\ncore_roots = [{:?}]\n", dir.path(), roots),
    )
    .unwrap();
    std::fs::write(
        state.join("peer-tools.json"),
        json!({"dead": table("dead_tool"), "gone": table("gone_tool")}).to_string(),
    )
    .unwrap();
    let peers = DelayedPeers(BTreeMap::from([
        ("dead".to_string(), (Duration::from_secs(3600), table("never_tool"))),
        ("late".to_string(), (Duration::from_millis(300), table("late_tool"))),
    ]));
    let cfg = Config::load(&state).unwrap();

    let gw = tokio::time::timeout(Duration::from_secs(2), Gateway::open_with(&state, cfg, Arc::new(peers)))
        .await
        .expect("open_with waited on a peer")
        .unwrap();
    assert_eq!(gw.owner("dead_tool").and_then(|o| o.peer).as_deref(), Some("dead"), "the cached table loads");
    assert!(gw.owner("gone_tool").is_none(), "a node that is no longer a peer is dropped from the cache");
    assert!(gw.owner("late_tool").is_none());

    tokio::spawn(awm_gateway::server::serve(gw.clone()));
    let url = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Some(u) = gw.url.get() {
                return u.clone();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("the gateway bound no listener");
    assert_eq!(owner_peer(&url, "dead_tool").await.as_deref(), Some("dead"));

    let late = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if let Some(p) = owner_peer(&url, "late_tool").await {
                return p;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("the late peer's table never arrived");
    assert_eq!(late, "late");

    let cache: Value = serde_json::from_str(&std::fs::read_to_string(state.join("peer-tools.json")).unwrap()).unwrap();
    assert_eq!(cache["late"][0]["name"], "late_tool");
    assert_eq!(cache["dead"][0]["name"], "dead_tool");
    assert!(cache.get("gone").is_none());
}
