//! The operations service: its verbs and their caller gates, and on alcor the store, the
//! approver's vault API, the singleton door, the board feed's emit, the Stale feed from
//! telemetry and the `agent` order executor.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Hub, Service, SubscriptionOpts, SupervisedSubscription, VerbError};
use serde_json::{json, Value};

use crate::api::{self, TOOL};
use crate::approver::{Approver, Config, Tools};
use crate::arbutus::{self, Approvals, ArbutusIo};
use crate::front::{Front, Outside, Reach, Stamp};
use crate::home::{Home, LocalClient};
use crate::keys::Keys;
use crate::relay::{async_trait_free::BoxFut, Relay, StoreClient};
use crate::settings::Settings;
use crate::store::{LiveSession, Store};

/// Python's truthiness for a JSON flag.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Verbs no plugin may call: the approver's devices and the orders. Any plugin may call any
/// tool, so these name the callers they admit: operator calls only.
pub const NO_PLUGIN: [&str; 2] = ["devices", "device_revoke"];
/// The order verbs: each node's reflection, or an operator.
pub const ORDER_VERBS: [&str; 2] = ["order_pull", "order_done"];
/// The verbs no session may call.
pub const NO_SESSION: [&str; 2] = ["ops_post", "devices"];
// -- the gateway sides of the front, Arbutus and the approver ------------------------------

pub struct HubOutside {
    pub hub: Hub,
    pub node: String,
}

fn operator_dm() -> (String, String) {
    let var = |n: &str| std::env::var(n).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
    (var("AWM_OPERATOR_DM_ACCOUNT").unwrap_or_else(|| "discord-bot".into()), var("AWM_OPERATOR_DM_CHANNEL").unwrap_or_else(|| "1522674357762261112".into()))
}

/// Tony's Discord DM with the bot, for a question that needs his answer.
const TONY_DM: (&str, &str) = ("discord-bot", "1469466265897668729");

fn failed(out: &Value) -> bool {
    out.get("ok") == Some(&json!(false))
}

impl HubOutside {
    async fn send(&self, account: &str, channel: &str, text: &str) -> bool {
        let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
        match self.hub.call_with("social", "send", json!({"account": account, "channel": channel, "text": text}), &opts).await {
            Ok(out) => !failed(&out),
            Err(e) => {
                tracing::warn!("operations: could not reach Tony: {e}");
                false
            }
        }
    }
}

impl Outside for HubOutside {
    fn session_of(&self, pid: u32) -> BoxFut<'_, Option<Value>> {
        Box::pin(awm_plugin_sdk::sessions::session_of(&self.hub, pid))
    }

    fn holds_seat<'a>(&'a self, session: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            let opts = CallOpts { timeout: Duration::from_secs(10), ..CallOpts::default() };
            match self.hub.call_with("seats", "list", json!({}), &opts).await {
                Ok(out) => out["seats"].as_array().is_some_and(|rows| rows.iter().any(|r| r["session_id"].as_str() == Some(session))),
                Err(e) => {
                    tracing::warn!("operations: could not ask seats about {session}: {e}");
                    false
                }
            }
        })
    }

    fn tell<'a>(&'a self, peer: Option<&'a str>, session: &'a str, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            let peer = peer.filter(|p| *p != self.node).map(str::to_string);
            let opts = CallOpts { peer, timeout: Duration::from_secs(30), ..CallOpts::default() };
            match self.hub.call_with("reflection", "message", json!({"to": {"id": session}, "text": text}), &opts).await {
                Ok(out) => !failed(&out),
                Err(e) => {
                    tracing::warn!("operations: could not tell {session}: {e}");
                    false
                }
            }
        })
    }

    fn dm<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            let (account, channel) = operator_dm();
            self.send(&account, &channel, text).await
        })
    }

    fn dm_tony<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(self.send(TONY_DM.0, TONY_DM.1, text))
    }

    fn reach<'a>(&'a self, node: &'a str, session: &'a str) -> BoxFut<'a, Reach> {
        Box::pin(async move {
            let peer = Some(node).filter(|n| *n != self.node).map(str::to_string);
            let opts = CallOpts { peer, timeout: Duration::from_secs(30), ..CallOpts::default() };
            match self.hub.call_with("reflection", "remote_control", json!({"on": true, "to": {"id": session}}), &opts).await {
                Ok(out) if out["ok"] == json!(true) => match out["link"].as_str() {
                    Some(url) => Reach::Link(url.to_string()),
                    None if out["applies"] == json!("turn_end") => Reach::Connecting,
                    None => Reach::Unavailable,
                },
                Ok(out) => {
                    tracing::warn!("operations: Remote Control refused for {session} on {node}: {out}");
                    Reach::Unavailable
                }
                Err(e) => {
                    tracing::warn!("operations: could not turn on Remote Control for {session} on {node}: {e}");
                    Reach::Unavailable
                }
            }
        })
    }

    fn links<'a>(&'a self, node: &'a str) -> BoxFut<'a, Option<HashMap<String, Option<String>>>> {
        Box::pin(async move {
            let opts = CallOpts { timeout: Duration::from_secs(10), ..CallOpts::default() };
            let out = self.hub.call_with("telemetry", "nodes", json!({"node": node}), &opts).await.ok()?;
            let sessions = out["nodes"].as_array()?.first()?["sessions"].as_array()?;
            Some(sessions.iter().filter_map(|s| Some((s["session_id"].as_str()?.to_string(), s["link"].as_str().map(str::to_string)))).collect())
        })
    }
}

pub struct HubArbutus {
    pub hub: Hub,
    pub approver: Arc<Approver>,
}

impl ArbutusIo for HubArbutus {
    fn post<'a>(&'a self, account: &'a str, channel: &'a str, text: &'a str, thread: Option<&'a str>, quiet: bool) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            let mut args = json!({"account": account, "channel": channel, "text": text});
            if quiet {
                args["quiet"] = json!(true);
            }
            if let Some(t) = thread.filter(|t| !t.is_empty()) {
                args["thread"] = json!(t);
            }
            let opts = CallOpts { peer: awm_plugin_sdk::config::peer_env("AWM_SOCIAL_PEER"), timeout: Duration::from_secs(30), ..CallOpts::default() };
            let out = self.hub.call_with("social", "send", args, &opts).await.map_err(|e| e.to_string())?;
            if failed(&out) {
                return Err(format!("social send refused: {}", out.get("error").cloned().unwrap_or(out.clone())));
            }
            Ok(if out.is_object() { out } else { json!({}) })
        })
    }

    fn request<'a>(&'a self, request_id: &'a str) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move { self.approver.approval_get(request_id).map_err(|e| e.message) })
    }

    fn decide<'a>(&'a self, request_id: &'a str, admin: &'a str, approve: bool) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move { self.approver.decide_by_admin(request_id, admin, approve).await.map_err(|e| e.message) })
    }

    fn tell<'a>(&'a self, session: &'a str, line: &'a str) -> BoxFut<'a, Result<(), String>> {
        Box::pin(async move { awm_plugin_sdk::sessions::tell(&self.hub, session, line).await.map(|_| ()).map_err(|e| e.to_string()) })
    }
}

pub struct HubTools {
    pub hub: Hub,
}

impl Tools for HubTools {
    fn twofa<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            let opts = CallOpts { timeout: Duration::from_secs(20), ..CallOpts::default() };
            self.hub.call_with("2fa", verb, args, &opts).await.map_err(|e| e.to_string())
        })
    }
}

// -- the verbs ------------------------------------------------------------------------------

/// Everything a verb handler reaches.
pub struct Ops {
    pub front: Arc<Front>,
    pub home: Option<Arc<Home>>,
}

fn stamp(call: &awm_plugin_sdk::Call) -> Stamp {
    Stamp { pid: call.caller_pid(), session_id: call.caller_session_id().map(str::to_string), claim: call.caller_claim().cloned(), peer: awm_plugin_sdk::config::caller_peer(call.as_.as_deref()).map(str::to_string), plugin: call.plugin.clone().filter(|_| call.as_.is_none()) }
}

fn refusal(msg: impl Into<String>) -> Value {
    json!({"ok": false, "error": msg.into()})
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(f).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

impl Ops {
    /// One call, after its caller gate.
    pub async fn handle(&self, verb: &str, args: Value, st: Stamp, as_: Option<&str>, plugin: Option<&str>) -> Value {
        if NO_PLUGIN.contains(&verb) {
            if let Some(p) = plugin {
                return refusal(format!("{verb} is not open to plugin {p:?}"));
            }
        }
        if ORDER_VERBS.contains(&verb) && plugin.is_some_and(|p| p != "reflection") {
            return refusal(format!("{verb} is not open to plugin {:?}", plugin.unwrap_or_default()));
        }
        if NO_SESSION.contains(&verb) && (st.is_session() || as_.is_some()) {
            return refusal(format!("{verb} takes this node's own services only"));
        }
        let home = || self.home.clone().ok_or_else(|| refusal("this verb lives on the home node (alcor)"));
        match verb {
            "ops_post" => {
                let fields: Vec<Option<&str>> = ["kind", "node", "title", "body"].iter().map(|k| args.get(*k).and_then(Value::as_str).filter(|s| !s.trim().is_empty())).collect();
                if fields.iter().any(Option::is_none) {
                    return refusal("kind, node, title and body are required");
                }
                match self.front.relay.write("ops_post", args).await {
                    Ok(out) => {
                        let mut o = json!({"ok": true});
                        if let (Some(m), Value::Object(x)) = (o.as_object_mut(), out) {
                            m.extend(x);
                        }
                        o
                    }
                    Err(e) => refusal(e.to_string()),
                }
            }
            "devices" | "device_revoke" => {
                let h = match home() { Ok(h) => h, Err(e) => return e };
                if as_.is_some() || st.is_session() {
                    return refusal("an operator verb: run operations on alcor");
                }
                let out = if verb == "devices" {
                    h.approver.devices()
                } else {
                    h.approver.device_revoke(args.get("device_id").and_then(Value::as_str).unwrap_or_default())
                };
                out.unwrap_or_else(|e| e.as_json())
            }
            _ => self.front.call(verb, &args, &st).await,
        }
    }
}

/// Add every verb and the board emitter to `service`.
pub fn register(mut service: Service, ops: Arc<Ops>) -> Service {
    for v in api::verbs() {
        let ops = ops.clone();
        let name = v.name;
        service = service.verb(TOOL, name, &v.description, v.params, move |call: awm_plugin_sdk::Call| {
            let ops = ops.clone();
            async move {
                let st = stamp(&call);
                Ok::<Value, VerbError>(ops.handle(name, call.declared_args(), st, call.as_.as_deref(), call.plugin.as_deref()).await)
            }
        });
        if v.hidden {
            service = service.hidden();
        }
        if ["approval_wait", "order_pull"].contains(&name) {
            service = service.timeout(Duration::from_secs(75));
        }
    }
    service.api("description", json!(api::DESCRIPTION))
}

// -- the home node's loops -------------------------------------------------------------------

fn text(v: &Value) -> Option<String> {
    v.as_str().filter(|x| !x.is_empty()).map(str::to_string)
}

/// The roster's live sessions per node, from `telemetry nodes` (CONTRACT.md d5c3a3f2).
pub fn roster_of(nodes: &Value) -> HashMap<String, Vec<LiveSession>> {
    nodes["nodes"].as_array().cloned().unwrap_or_default().iter().filter_map(|n| {
        let node = n["node"].as_str()?.to_string();
        let live = n["sessions"].as_array().cloned().unwrap_or_default().iter().map(|s| LiveSession {
            session_id: text(&s["session_id"]),
            name: text(&s["name"]), link: text(&s["link"]), project: text(&s["project"]), scope: text(&s["scope"]),
            state: text(&s["state"]), needs: text(&s["needs"]),
        }).collect();
        Some((node, live))
    }).collect()
}

/// One `telemetry/sessions` event: a session that went live, went, took a new name or
/// changed state.
pub fn on_session_event(store: &Store, e: &Value) -> anyhow::Result<Vec<String>> {
    let Some(sid) = e["session_id"].as_str().filter(|s| !s.is_empty()) else { return Ok(vec![]) };
    let mut moved = match e["event"].as_str() {
        Some("gone") => return Ok(store.session_gone(sid, e["retired"] == json!(true))?),
        Some("live") => store.session_live(sid)?,
        Some("renamed") => return Ok(store.session_renamed(sid, e["name"].as_str().unwrap_or_default())?),
        Some("state") => vec![],
        _ => return Ok(vec![]),
    };
    if let Some(state) = e["state"].as_str() {
        moved.extend(store.session_state(sid, state, e["needs"].as_str())?);
    }
    Ok(moved)
}

/// Follow telemetry's session feed, reconciling with its roster at each (re)connect.
pub fn follow_sessions(hub: Hub, store: Arc<Store>) -> SupervisedSubscription {
    let opts = SubscriptionOpts { idle_timeout: Duration::from_secs(3600), ..SubscriptionOpts::default() };
    let (h1, s1) = (hub.clone(), store.clone());
    SupervisedSubscription::spawn(
        "operations/telemetry.sessions",
        opts,
        move || {
            let (hub, store) = (h1.clone(), s1.clone());
            async move {
                let sub = hub.subscribe("telemetry", "sessions", None, None).await?;
                match hub.call("telemetry", "nodes", json!({}), None).await {
                    Ok(nodes) => {
                        let roster = roster_of(&nodes);
                        match blocking(move || store.reconcile(&roster)).await {
                            Ok(out) => tracing::info!("operations: reconciled with telemetry: {out}"),
                            Err(e) => tracing::warn!("operations: reconcile failed: {e}"),
                        }
                    }
                    Err(e) => tracing::warn!("operations: telemetry nodes: {e}"),
                }
                Ok(sub)
            }
        },
        move |event: Value| {
            let store = store.clone();
            async move {
                blocking(move || on_session_event(&store, &event)).await?;
                Ok(())
            }
        },
    )
}

async fn join_session_details(hub: &Hub, store: Arc<Store>) {
    match hub.call("telemetry", "nodes", json!({}), None).await {
        Ok(nodes) => {
            let roster = roster_of(&nodes);
            if let Err(e) = blocking(move || store.session_details(&roster)).await {
                tracing::warn!("operations: session details: {e}");
            }
        }
        Err(e) => tracing::warn!("operations: telemetry nodes: {e}"),
    }
}

/// Send each link still owed at every (re)connect and whenever a session changes in the roster.
pub fn follow_links(hub: Hub, front: Arc<Front>, store: Arc<Store>) -> SupervisedSubscription {
    let opts = SubscriptionOpts { idle_timeout: Duration::from_secs(3600), ..SubscriptionOpts::default() };
    let (f1, s1, h1) = (front.clone(), store.clone(), hub.clone());
    SupervisedSubscription::spawn(
        "operations/telemetry.roster",
        opts,
        move || {
            let (hub, front, s1) = (hub.clone(), f1.clone(), s1.clone());
            async move {
                let sub = hub.subscribe("telemetry", "roster", None, None).await?;
                front.links_due().await;
                join_session_details(&hub, s1).await;
                Ok(sub)
            }
        },
        move |event: Value| {
            let (front, store, h2) = (front.clone(), store.clone(), h1.clone());
            async move {
                if event["changed"].as_array().is_some_and(|c| c.contains(&json!("sessions"))) {
                    front.links_due().await;
                    join_session_details(&h2, store).await;
                }
                Ok(())
            }
        },
    )
}

/// Expire each pending request at its own expiry, waking early when a new one opens.
pub async fn sweep_loop(ap: Arc<Approver>) {
    loop {
        ap.sweep().await;
        let next = ap.next_expiry().unwrap_or(crate::now() + 3600.0);
        let wait = Duration::from_secs_f64((next - crate::now()).clamp(1.0, 3600.0));
        let woke = ap.sweep_signal();
        let _ = tokio::time::timeout(wait, woke.notified()).await;
    }
}

/// Wire the whole service from the environment and run it.
pub async fn serve() -> anyhow::Result<()> {
    let service = Service::from_env()?;
    let hub = service.context().hub().clone();
    let s = Settings::from_env()?;
    let node = crate::settings::node_name();
    let init = std::env::var("AWM_OPERATIONS_INIT").is_ok_and(|v| v == "1");

    let mut home = None;
    let mut approver_handle = None;
    let client: Arc<dyn StoreClient> = if node == s.home {
        let store = Arc::new(Store::open(&s.queue_db, init)?);
        let keys_dir = s.approver_dir.join("keys");
        let keys = if init && !keys_dir.exists() { Keys::generate(&keys_dir)? } else { Keys::load(&keys_dir)? };
        let cfg = Config { origin: s.origin.clone(), allowed_orders: s.allowed_orders.clone(), rate_max: s.rate_max, portals: s.portals.clone() };
        let tools: Arc<dyn Tools> = Arc::new(HubTools { hub: hub.clone() });
        let ap = Arc::new(Approver::open(&s.approver_dir.join("approver.db"), keys, cfg, store.clone(), tools, init, Arc::new(crate::now))?);
        let a2 = ap.clone();
        store.set_approvals(Arc::new(move |t: &str| a2.approvals_of(t)));
        let h = Arc::new(Home { store, approver: ap.clone() });
        home = Some(h.clone());
        approver_handle = Some(ap);
        Arc::new(LocalClient { home: h })
    } else {
        let argv = crate::door::door_argv().ok_or_else(|| anyhow::anyhow!("set AWM_OPERATIONS_DOOR to alcor's singleton door, user@host:port"))?;
        Arc::new(crate::door::DoorClient { argv, timeout: crate::door::TIMEOUT })
    };
    let relay = Arc::new(Relay::open(&node, &s.relay_db, client)?);
    let arb = home.as_ref().filter(|_| arbutus::approval_channel().is_some())
        .map(|h| Arc::new(Approvals::new(Arc::new(HubArbutus { hub: hub.clone(), approver: h.approver.clone() }))));
    let outside: Arc<dyn Outside> = Arc::new(HubOutside { hub: hub.clone(), node: node.clone() });
    let front = Arc::new(Front {
        relay: relay.clone(), node: node.clone(), outside, arbutus: arb.clone(), card_url: s.card_url.clone(),
        approver_origin: s.origin.clone(), remind_s: s.remind_s, clock: Arc::new(crate::now),
        link_wait: Duration::from_secs(15), asking: Default::default(), roster_moved: Default::default(),
    });
    let ops = Arc::new(Ops { front: front.clone(), home: home.clone() });
    let service = register(service, ops);

    let (h2, r2, f2, f3) = (hub.clone(), relay.clone(), front.clone(), front.clone());
    let started = async move {
        let r = r2.clone();
        tokio::spawn(async move {
            loop {
                r.flush().await;
                f2.remind_due().await;
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        });
        if let Some(a) = arb {
            let hub = h2.clone();
            SupervisedSubscription::spawn(
                "operations/social.message",
                SubscriptionOpts::default(),
                move || {
                    let hub = hub.clone();
                    async move { hub.subscribe("social", "message", awm_plugin_sdk::config::peer_env("AWM_SOCIAL_PEER").as_deref(), None).await }
                },
                move |event: Value| {
                    let a = a.clone();
                    async move {
                        a.handle(&event).await;
                        Ok(())
                    }
                },
            );
        }
        if let (Some(h), Some(ap)) = (home, approver_handle) {
            follow_sessions(h2.clone(), h.store.clone());
            follow_links(h2.clone(), f3, h.store.clone());
            let vault = Arc::new(crate::vault_push::HubVault { hub: h2.clone() });
            tokio::spawn(crate::vault_push::Pusher::attach(h.store.clone(), vault).run());
            tokio::spawn(sweep_loop(ap.clone()));
            let vault = Arc::new(crate::server::Vault {
                approver: ap, board_origins: s.board_origins.clone(),
                forwarded_header: Some(s.forwarded_header.clone()).filter(|f| !f.is_empty()), peers: s.peers.clone(), hub: Some(h2.clone()),
            });
            if let Err(e) = crate::server::serve(vault, &s.bind).await {
                tracing::error!("operations: the vault API did not start on {}: {e}", s.bind);
            }
            if let Err(e) = crate::door::serve(h, &crate::door::socket_path()).await {
                tracing::error!("operations: the door socket did not start: {e}");
            }
        }
    };
    service.on_ready(started).run().await
}
