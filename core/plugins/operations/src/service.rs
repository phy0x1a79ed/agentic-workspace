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
use crate::front::{Front, Outside, Stamp};
use crate::home::{Home, LocalClient};
use crate::keys::Keys;
use crate::relay::{async_trait_free::BoxFut, ClientError, Relay, StoreClient};
use crate::settings::Settings;
use crate::store::{BoardEvent, LiveSession, Store};

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
pub const NO_PLUGIN: [&str; 4] = ["devices", "device_revoke", "order_pull", "order_done"];
/// The verbs only the trilium writer may call.
pub const TRILIUM_ONLY: [&str; 1] = ["board_import"];
/// The verbs no session may call.
pub const NO_SESSION: [&str; 5] = ["board_changes", "board_import", "ops_list", "ops_post", "devices"];
/// The one plugin that writes the vault.
pub const VAULT_WRITER: &str = "trilium";

pub const BOARD_TOPIC: &str = "board";
pub const BOARD_EMITTER: &str = "Fires at each card event the store appends: {task_id, seq, ts}, where seq is the event's board_changes mark and ts is RFC 3339 UTC. Nothing at boot: a subscriber drains board_changes after its own mark.";

/// The prompt an `agent` order started from a note gets: one fixed line, no vault text.
pub fn agent_prompt(note_id: Option<&str>) -> Option<String> {
    note_id.map(|n| format!("Read the note {n} as context only, then wait for Tony."))
}

/// The `start_other` args of one `agent` order (CONTRACT.md b11adebc).
pub fn agent_start(body: &Value) -> Value {
    let p = &body["payload"];
    let mut start = json!({
        "task_id": p["task_id"], "role": "main", "remote_control": true, "suggest": true,
        "model": "opus", "effort": "high", "started_by": "Tony from the vault",
    });
    if let Some(prompt) = agent_prompt(p.get("note_id").and_then(Value::as_str)) {
        start["prompt"] = json!(prompt);
    }
    start
}

fn rfc3339(ts: f64) -> String {
    chrono::DateTime::from_timestamp((ts.floor()) as i64, ((ts.fract()) * 1e9) as u32)
        .map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))
        .unwrap_or_default()
}

pub fn board_event(e: &BoardEvent) -> Value {
    json!({"task_id": e.task_id, "seq": e.seq, "ts": rfc3339(e.ts)})
}

// -- the gateway sides of the front, Arbutus and the approver ------------------------------

pub struct HubOutside {
    pub hub: Hub,
    pub node: String,
}

fn operator_dm() -> (String, String) {
    let var = |n: &str| std::env::var(n).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
    (var("AWM_OPERATOR_DM_ACCOUNT").unwrap_or_else(|| "discord-bot".into()), var("AWM_OPERATOR_DM_CHANNEL").unwrap_or_else(|| "1522674357762261112".into()))
}

fn failed(out: &Value) -> bool {
    out.get("ok") == Some(&json!(false))
}

impl Outside for HubOutside {
    fn session_of(&self, pid: u32) -> BoxFut<'_, Option<Value>> {
        Box::pin(awm_plugin_sdk::sessions::session_of(&self.hub, pid))
    }

    fn tell<'a>(&'a self, peer: Option<&'a str>, job: &'a str, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            let peer = peer.filter(|p| *p != self.node).map(str::to_string);
            let opts = CallOpts { peer, timeout: Duration::from_secs(30), ..CallOpts::default() };
            match self.hub.call_with("reflection", "message", json!({"job": job, "text": text}), &opts).await {
                Ok(out) => !failed(&out),
                Err(e) => {
                    tracing::warn!("operations: could not tell {job}: {e}");
                    false
                }
            }
        })
    }

    fn dm<'a>(&'a self, text: &'a str) -> BoxFut<'a, bool> {
        Box::pin(async move {
            let (account, channel) = operator_dm();
            let opts = CallOpts { timeout: Duration::from_secs(30), ..CallOpts::default() };
            match self.hub.call_with("social", "send", json!({"account": account, "channel": channel, "text": text}), &opts).await {
                Ok(out) => !failed(&out),
                Err(e) => {
                    tracing::warn!("operations: could not DM Tony: {e}");
                    false
                }
            }
        })
    }
}

pub struct HubArbutus {
    pub hub: Hub,
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

    fn run<'a>(&'a self, name: &'a str, bundles: &'a std::path::Path, approved_by: &'a str) -> BoxFut<'a, (i32, String)> {
        Box::pin(arbutus::run_approve(name, bundles, approved_by))
    }

    fn tell<'a>(&'a self, job: &'a str, line: &'a str) -> BoxFut<'a, Result<(), String>> {
        Box::pin(async move { awm_plugin_sdk::sessions::tell(&self.hub, job, line).await.map(|_| ()).map_err(|e| e.to_string()) })
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
    Stamp { pid: call.caller_pid(), session_id: call.caller_session_id().map(str::to_string), claim: call.caller_claim().cloned() }
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
        if NO_SESSION.contains(&verb) && (st.is_session() || as_.is_some()) {
            return refusal(format!("{verb} takes this node's own services only"));
        }
        if TRILIUM_ONLY.contains(&verb) && plugin != Some(VAULT_WRITER) {
            return refusal(format!("{verb} is open to the {VAULT_WRITER} plugin only"));
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
            "board_changes" => {
                let h = match home() { Ok(h) => h, Err(e) => return e };
                let (after, limit) = (args.get("after").and_then(Value::as_i64).unwrap_or(0), args.get("limit").and_then(Value::as_i64));
                blocking(move || h.store.board_changes(after, limit)).await.unwrap_or_else(|e| refusal(e.to_string()))
            }
            "ops_list" => {
                let h = match home() { Ok(h) => h, Err(e) => return e };
                blocking(move || h.store.ops_list()).await.unwrap_or_else(|e| refusal(e.to_string()))
            }
            "board_import" => {
                let h = match home() { Ok(h) => h, Err(e) => return e };
                let cards = args.get("cards").and_then(Value::as_array).cloned().unwrap_or_default();
                let notes = args.get("ops_notes").and_then(Value::as_array).cloned().unwrap_or_default();
                let gap = args.get("accept_gap").cloned().unwrap_or(Value::Null);
                blocking(move || h.store.board_import(&cards, &notes, &gap)).await.unwrap_or_else(|e| refusal(e.to_string()))
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
    service.api("description", json!(api::DESCRIPTION)).emitter(BOARD_TOPIC, Some(BOARD_EMITTER))
}

// -- the home node's loops -------------------------------------------------------------------

/// The roster's live sessions per node, from `telemetry nodes` (CONTRACT.md d5c3a3f2).
pub fn roster_of(nodes: &Value) -> HashMap<String, Vec<LiveSession>> {
    nodes["nodes"].as_array().cloned().unwrap_or_default().iter().filter_map(|n| {
        let node = n["node"].as_str()?.to_string();
        let live = n["sessions"].as_array().cloned().unwrap_or_default().iter().map(|s| LiveSession {
            session_id: s["session_id"].as_str().filter(|x| !x.is_empty()).map(str::to_string),
            job: s["job"].as_str().filter(|x| !x.is_empty()).map(str::to_string),
        }).collect();
        Some((node, live))
    }).collect()
}

/// One `telemetry/sessions` event: a session that went live or went.
pub fn on_session_event(store: &Store, e: &Value) -> anyhow::Result<Vec<String>> {
    let node = e["node"].as_str().unwrap_or_default();
    let (sid, job) = (e["session_id"].as_str(), e["job"].as_str());
    Ok(match e["event"].as_str() {
        Some("gone") => store.session_gone(sid, job, node)?,
        Some("live") => store.session_live(sid, job, node)?,
        _ => vec![],
    })
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

/// Pull this node's `agent` orders and start each one through reflection here.
pub async fn order_loop(relay: Arc<Relay>, hub: Hub, grant_pub: [u8; 32]) {
    loop {
        let envelopes = match relay.poll("order_pull", json!({"node": relay.node, "kinds": crate::approver::ORDER_KINDS, "wait_s": 55})).await {
            Ok(Value::Array(e)) => e,
            Ok(_) => vec![],
            Err(ClientError::Unreachable(_)) | Err(ClientError::Refused(_)) => {
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        for env in envelopes {
            let body = match relay.take_order(&env, &grant_pub) {
                Ok(Some(b)) => b,
                Ok(None) => continue,
                Err(e) => {
                    tracing::warn!("operations: dropped an order: {e}");
                    continue;
                }
            };
            let id = body["order_id"].as_str().unwrap_or_default().to_string();
            let outcome = if body["kind"] != "agent" {
                Err(format!("this node takes agent orders only, not {}", body["kind"]))
            } else {
                let opts = CallOpts { timeout: Duration::from_secs(120), ..CallOpts::default() };
                match hub.call_with("reflection", "start_other", agent_start(&body), &opts).await {
                    Ok(out) if !failed(&out) => Ok(out),
                    Ok(out) => Err(out.get("reason").or(out.get("error")).cloned().unwrap_or(out).to_string()),
                    Err(e) => Err(e.to_string()),
                }
            };
            let (ok, note) = match outcome {
                Ok(out) => (true, out.to_string()),
                Err(e) => (false, e),
            };
            let note: String = note.chars().take(500).collect();
            if let Err(e) = relay.write("order_done", json!({"order_id": id, "ok": ok, "note": note})).await {
                tracing::warn!("operations: order {id} done not recorded: {e}");
            }
        }
    }
}

/// Wire the whole service from the environment and run it.
pub async fn serve() -> anyhow::Result<()> {
    let service = Service::from_env()?;
    let env = service.context().env().clone();
    let hub = service.context().hub().clone();
    let s = Settings::from_env()?;
    let node = crate::settings::node_name();
    let init = std::env::var("AWM_OPERATIONS_INIT").is_ok_and(|v| v == "1");
    crate::snapshot::take(&env.awm_dir)?;

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
        let ctx = service.context();
        store.set_on_event(Arc::new(move |e: &BoardEvent| {
            ctx.emit(BOARD_TOPIC, board_event(e));
        }));
        let h = Arc::new(Home { store, approver: ap.clone() });
        home = Some(h.clone());
        approver_handle = Some(ap);
        Arc::new(LocalClient { home: h })
    } else {
        let argv = crate::door::door_argv().ok_or_else(|| anyhow::anyhow!("set AWM_OPERATIONS_DOOR to alcor's singleton door, user@host:port"))?;
        Arc::new(crate::door::DoorClient { argv, timeout: crate::door::TIMEOUT })
    };
    let relay = Arc::new(Relay::open(&node, &s.relay_db, client)?);
    let arb = arbutus::approval_channel().map(|_| Arc::new(Approvals::new(Arc::new(HubArbutus { hub: hub.clone() }))));
    let outside: Arc<dyn Outside> = Arc::new(HubOutside { hub: hub.clone(), node: node.clone() });
    let front = Arc::new(Front {
        relay: relay.clone(), node: node.clone(), outside, arbutus: arb.clone(), card_url: s.card_url.clone(),
        approver_origin: s.origin.clone(), remind_s: s.remind_s, clock: Arc::new(crate::now),
    });
    if let Some(a) = &arb {
        let f = front.clone();
        a.set_on_decided(Arc::new(move |task: String, line: String| {
            let f = f.clone();
            Box::pin(async move { f.arbutus_decided(&task, &line).await })
        }));
    }
    let ops = Arc::new(Ops { front: front.clone(), home: home.clone() });
    let service = register(service, ops);

    let grant_pub = awm_trust::grant::grant_key_env(std::env::var(awm_trust::grant::GRANT_KEY_ENV).ok())
        .or_else(|_| awm_trust::grant::grant_key_file(&env.awm_dir)).ok();
    let (h2, r2, f2) = (hub.clone(), relay.clone(), front.clone());
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
        match grant_pub {
            Some(k) => {
                tokio::spawn(order_loop(r2, h2, k));
            }
            None => tracing::warn!("operations: no AWM_APPROVER_GRANT_PUBKEY: agent orders stay off on this node"),
        }
    };
    service.on_ready(started).run().await
}
