//! The node part, on every node that opts in: it follows the node's own emit topics and
//! forwards each fact to the roster through `telemetry report`. It reads the node's whole
//! state only at connect, and again when the roster or a dropped stream says it missed some.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use awm_plugin_sdk::supervise::SubscriptionOpts;
use awm_plugin_sdk::{config, privacy, Hub, SupervisedSubscription};
use chrono::Utc;
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::roster::{parse_ts, Item, ServiceRow, Session, ToolRow, Ts};

/// The plugins whose own calls the node part does not forward, so a report never reports itself.
const OWN_PLUGINS: [&str; 2] = ["telemetry", "telemetry-node"];

/// The topics the node part follows on its own gateway: `(service, topic)`.
pub const TOPICS: [(&str, &str); 4] = [("reflection", "sessions"), ("awm", "services"), ("awm", "usage"), ("awm", "plugins")];

pub enum Msg {
    Item(Box<Item>),
    Usage { tool: String, ts: Ts, ok: bool },
    /// The plugin set changed: read the node's services and tools again.
    Refresh,
    /// The roster may lack something: send the node's whole state.
    Resync,
}

/// What the node part reads from its own node.
pub trait Probe: Send + Sync + 'static {
    /// The whole state, as a hello.
    fn hello(&self) -> BoxFuture<'_, anyhow::Result<Item>>;
    /// The services and tools alone.
    fn tools(&self) -> BoxFuture<'_, anyhow::Result<Item>>;
}

/// Where a report goes: `telemetry report` with these args.
pub type Forward = Arc<dyn Fn(Value) -> BoxFuture<'static, anyhow::Result<Value>> + Send + Sync>;

/// Whether a subscription that just connected may have missed events: its first connect, and
/// any connect after a stream that ended other than by the SDK's idle resubscribe.
pub fn missed_events(sub: Option<&SupervisedSubscription>) -> bool {
    sub.is_none_or(|s| s.health()["last_end"] != "idle")
}

/// One emit payload as the message the sender takes, or None for a payload it ignores.
pub fn message(service: &str, topic: &str, payload: &Value, here: &str) -> Option<Msg> {
    let s = |k: &str| payload.get(k).and_then(Value::as_str).map(str::to_string);
    match (service, topic) {
        ("reflection", "sessions") => {
            if s("node").is_some_and(|n| config::label(&n) != here) {
                return None;
            }
            let event = s("event")?;
            let mut session = session_of(payload)?;
            if event == "remote_control" {
                session.remote_control = payload.get("remote_control").and_then(Value::as_bool).unwrap_or(session.link.is_some());
            }
            Some(Msg::Item(Box::new(Item::Session { event, session, ts: parse_ts(payload.get("ts")) })))
        }
        ("awm", "services") => Some(Msg::Item(Box::new(Item::Service {
            service: s("service")?,
            plugin: s("plugin"),
            up: s("event")? == "up",
            ts: parse_ts(payload.get("ts")),
        }))),
        ("awm", "usage") => {
            let tool = s("tool")?;
            if tool == crate::verbs::TOOL || s("plugin").is_some_and(|p| OWN_PLUGINS.contains(&p.as_str())) {
                return None;
            }
            let ok = payload.get("ok").and_then(Value::as_bool).unwrap_or(true);
            Some(Msg::Usage { tool, ts: parse_ts(payload.get("ts")).unwrap_or_else(Utc::now), ok })
        }
        ("awm", "plugins") => Some(Msg::Refresh),
        _ => None,
    }
}

/// A session from a `reflection/sessions` payload or a `list_other` row.
pub fn session_of(v: &Value) -> Option<Session> {
    let s = |k: &str| v.get(k).and_then(Value::as_str).filter(|x| !x.is_empty()).map(str::to_string);
    let link = s("rc_url").or_else(|| s("link"));
    Some(Session {
        job: s("job")?,
        session_id: s("session_id"),
        name: s("name"),
        project: s("project"),
        scope: s("scope"),
        kind: s("kind").or_else(|| s("mode")),
        task_id: s("task_id"),
        remote_control: v.get("remote_control").and_then(Value::as_bool).unwrap_or(link.is_some()),
        link,
        started: parse_ts(v.get("started_at")).or_else(|| parse_ts(v.get("started"))),
    })
}

/// A session in a project or scope this node marks private leaves the node as its job alone
/// (contract § Peer calls item 6), so it still counts but says nothing.
pub fn redact(s: Session) -> Session {
    redact_with(s, privacy::hidden)
}

pub fn redact_with(s: Session, hidden: impl Fn(&str, Option<&str>) -> bool) -> Session {
    match &s.project {
        Some(p) if hidden(p, s.scope.as_deref()) => Session { job: s.job, kind: s.kind, started: s.started, ..Session::default() },
        _ => s,
    }
}

/// Whether this node marks a project, or a scope of it, private.
pub type Hidden = Arc<dyn Fn(&str, Option<&str>) -> bool + Send + Sync>;

/// The node's own marks (`privacy::hidden`).
pub fn marks() -> Hidden {
    Arc::new(privacy::hidden)
}

/// [`message`], with each session redacted by `hidden`.
pub fn message_redacted(service: &str, topic: &str, payload: &Value, here: &str, hidden: &Hidden) -> Option<Msg> {
    match message(service, topic, payload, here)? {
        Msg::Item(item) => match *item {
            Item::Session { event, session, ts } => Some(Msg::Item(Box::new(Item::Session { event, session: redact_with(session, |p, s| hidden(p, s)), ts }))),
            other => Some(Msg::Item(Box::new(other))),
        },
        m => Some(m),
    }
}

/// Send every message to the roster, batching what queued while a report was in flight.
pub async fn send_loop(mut rx: mpsc::UnboundedReceiver<Msg>, me: mpsc::UnboundedSender<Msg>, node: String, swarm: String, probe: Arc<dyn Probe>, forward: Forward) {
    let mut dirty = true;
    let mut usage: BTreeMap<String, (Option<Ts>, Option<Ts>)> = BTreeMap::new();
    while let Some(first) = rx.recv().await {
        let mut msgs = vec![first];
        while let Ok(m) = rx.try_recv() {
            msgs.push(m);
        }
        let (mut items, mut refresh) = (vec![], false);
        for m in msgs {
            match m {
                Msg::Item(i) => items.push(*i),
                Msg::Usage { tool, ts, ok } => {
                    let e = usage.entry(tool).or_default();
                    e.0 = e.0.max(Some(ts));
                    if ok {
                        e.1 = e.1.max(Some(ts));
                    }
                }
                Msg::Refresh => refresh = true,
                Msg::Resync => dirty = true,
            }
        }
        let mut hello = false;
        if dirty {
            match probe.hello().await {
                Ok(h) => {
                    items.insert(0, h);
                    hello = true;
                }
                Err(e) => tracing::warn!("could not read this node's state for a hello: {e:#}"),
            }
        } else if refresh {
            match probe.tools().await {
                Ok(t) => items.push(t),
                Err(e) => tracing::warn!("could not read this node's tools: {e:#}"),
            }
        }
        items.extend(usage.iter().map(|(tool, (used, ok))| Item::Usage { tool: tool.clone(), last_used: *used, last_ok: *ok }));
        if items.is_empty() {
            continue;
        }
        match forward(json!({"swarm": swarm, "node": node, "items": items})).await {
            Ok(answer) => {
                usage.clear();
                if hello {
                    dirty = false;
                }
                if answer.get("resync").and_then(Value::as_bool) == Some(true) && !hello {
                    let _ = me.send(Msg::Resync);
                }
            }
            Err(e) => {
                // The next event resends the whole state; the usage waits for it.
                tracing::warn!("telemetry report failed, so the next event resends this node's state: {e:#}");
                dirty = true;
            }
        }
    }
}

/// The node part's handle: the queue the subscriptions feed.
pub struct NodePart {
    pub tx: mpsc::UnboundedSender<Msg>,
    pub subscriptions: Vec<SupervisedSubscription>,
}

/// Start the sender and a subscription per topic. Each handler only queues.
pub fn start(hub: Hub, node: String, swarm: String, probe: Arc<dyn Probe>, forward: Forward, hidden: Hidden) -> NodePart {
    let (tx, rx) = mpsc::unbounded_channel();
    tokio::spawn(send_loop(rx, tx.clone(), node.clone(), swarm, probe, forward));
    let subscriptions = TOPICS
        .iter()
        .map(|&(service, topic)| {
            let cell: Arc<OnceLock<SupervisedSubscription>> = Arc::default();
            let (hub, seen, tx_c, tx_h, here, hidden) = (hub.clone(), cell.clone(), tx.clone(), tx.clone(), node.clone(), hidden.clone());
            let sub = SupervisedSubscription::spawn(
                &format!("{service}/{topic}"),
                SubscriptionOpts::default(),
                move || {
                    let (hub, seen, tx) = (hub.clone(), seen.clone(), tx_c.clone());
                    async move {
                        let stream = hub.subscribe(service, topic, None, None).await?;
                        if missed_events(seen.get()) {
                            let _ = tx.send(Msg::Resync);
                        }
                        Ok(stream)
                    }
                },
                move |payload: Value| {
                    let (tx, here, hidden) = (tx_h.clone(), here.clone(), hidden.clone());
                    async move {
                        if let Some(m) = message_redacted(service, topic, &payload, &here, &hidden) {
                            let _ = tx.send(m);
                        }
                        Ok(())
                    }
                },
            );
            let _ = cell.set(sub.clone());
            sub
        })
        .collect();
    NodePart { tx, subscriptions }
}

/// The report goes to the `telemetry` singleton with no peer: the gateway routes it to the
/// roster's host through that host's door, as the edge list names it (fleet.md R140).
pub fn forward_through(hub: Hub) -> Forward {
    Arc::new(move |args: Value| {
        let hub = hub.clone();
        Box::pin(async move {
            let opts = awm_plugin_sdk::CallOpts { timeout: Duration::from_secs(60), ..Default::default() };
            hub.call_with(crate::verbs::TOOL, "report", args, &opts).await.map_err(anyhow::Error::from)
        })
    })
}

/// The probe of a running node: reflection's `list_other`, the gateway's `/hub/table` and
/// `/hub/plugins`, and the edge list.
pub struct GatewayProbe {
    pub hub: Hub,
    pub node: String,
    http: reqwest::Client,
}

impl GatewayProbe {
    pub fn new(hub: Hub, node: String) -> Self {
        let http = reqwest::Client::builder().no_proxy().build().expect("reqwest client builds");
        Self { hub, node, http }
    }

    async fn get(&self, path: &str) -> anyhow::Result<Value> {
        let mut req = self.http.get(format!("{}{path}", self.hub.base_url())).timeout(Duration::from_secs(10));
        if let Some(t) = self.hub.token() {
            req = req.header("X-Awm-Service-Token", t);
        }
        let resp = req.send().await?.error_for_status()?;
        Ok(resp.json().await?)
    }

    async fn read_tools(&self) -> anyhow::Result<(Vec<ServiceRow>, Vec<ToolRow>)> {
        let table = self.get("/hub/table").await?;
        let plugins = self.get("/hub/plugins").await?;
        let edges = config::edges().unwrap_or_default();
        Ok(services_and_tools(&table, &plugins, &edges.door_singletons(&self.node).iter().map(|s| (s.tool.clone(), s.host.clone())).collect::<Vec<_>>()))
    }
}

/// The node's services, up when enabled and in the live table, and the tools it can use: its
/// own and the singletons behind other hosts' doors.
pub fn services_and_tools(table: &Value, plugins: &Value, doors: &[(String, String)]) -> (Vec<ServiceRow>, Vec<ToolRow>) {
    let rows = table.as_array().cloned().unwrap_or_default();
    let live_plugins: Vec<&str> = rows.iter().filter_map(|t| t.get("plugin").and_then(Value::as_str)).collect();
    let services = plugins
        .get("plugins")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .flat_map(|p| p.get("services").and_then(Value::as_array).cloned().unwrap_or_default())
        .filter_map(|s| {
            let name = s.get("name").and_then(Value::as_str)?.to_string();
            let plugin = s.get("plugin").and_then(Value::as_str).map(str::to_string);
            let tools: Vec<String> = s.get("tools").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str).map(str::to_string).collect();
            let enabled = s.get("enabled").and_then(Value::as_bool).unwrap_or(true);
            let in_table = tools.is_empty() || plugin.as_deref().is_some_and(|p| live_plugins.contains(&p));
            Some(ServiceRow { service: name, plugin, tools, up: enabled && in_table })
        })
        .collect();
    let mut tools: Vec<ToolRow> = rows.iter().filter_map(|t| t.get("name").and_then(Value::as_str)).map(|n| ToolRow { tool: n.into(), host: None }).collect();
    tools.extend(doors.iter().map(|(tool, host)| ToolRow { tool: tool.clone(), host: Some(host.clone()) }));
    (services, tools)
}

impl Probe for GatewayProbe {
    fn hello(&self) -> BoxFuture<'_, anyhow::Result<Item>> {
        Box::pin(async move {
            let (services, tools) = self.read_tools().await?;
            let listed = self.hub.call("reflection", "list_other", json!({}), None).await?;
            let sessions = listed
                .get("sessions")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter(|r| r.get("node").and_then(Value::as_str).is_none_or(|n| config::label(n) == self.node))
                .filter_map(session_of)
                .map(redact)
                .collect();
            let role = config::edges().ok().and_then(|e| e.nodes.get(&self.node).map(|n| n.role.clone()));
            Ok(Item::Hello { role, portal: config::edge_url(), sessions, services, tools })
        })
    }

    fn tools(&self) -> BoxFuture<'_, anyhow::Result<Item>> {
        Box::pin(async move {
            let (services, tools) = self.read_tools().await?;
            Ok(Item::Tools { services, tools })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payloads_map_to_messages() {
        let ev = json!({"event": "started", "job": "a1", "session_id": "s1", "name": "x", "node": "mizar", "project": "awm",
                        "scope": "lobby", "kind": "worker", "task_id": "CW", "rc_url": "https://claude.ai/code/s1", "ts": "2026-11-07T10:00:00Z"});
        let Some(Msg::Item(item)) = message("reflection", "sessions", &ev, "mizar") else { panic!() };
        let Item::Session { event, session, ts } = *item else { panic!() };
        assert_eq!((event.as_str(), session.session_id.as_deref(), session.link.as_deref()), ("started", Some("s1"), Some("https://claude.ai/code/s1")));
        assert!(session.remote_control && ts.is_some());
        assert!(message("reflection", "sessions", &ev, "lyra").is_none(), "another node's session is not ours to report");

        let up = json!({"service": "social", "plugin": "social", "event": "down", "pid": 9, "ts": 1});
        assert!(matches!(message("awm", "services", &up, "m"), Some(Msg::Item(i)) if matches!(*i, Item::Service { up: false, .. })));

        let own = json!({"plugin": "telemetry-node", "surface": "plugin", "tool": "telemetry", "verb": "report", "ok": true});
        assert!(message("awm", "usage", &own, "m").is_none(), "a report never reports itself");
        let used = json!({"plugin": "journal", "surface": "mcp", "tool": "journal", "verb": "post", "ok": false, "ts": "2026-11-07T10:00:00Z"});
        assert!(matches!(message("awm", "usage", &used, "m"), Some(Msg::Usage { ok: false, .. })));
        assert!(matches!(message("awm", "plugins", &json!({"plugin": "kb", "event": "updated"}), "m"), Some(Msg::Refresh)));
    }

    #[test]
    fn a_private_session_leaves_as_its_job_alone() {
        let s = Session { job: "j".into(), name: Some("n".into()), project: Some("secret".into()), link: Some("l".into()), ..Session::default() };
        let r = redact_with(s.clone(), |p, _| p == "secret");
        assert_eq!(r, Session { job: "j".into(), ..Session::default() });
        assert_eq!(redact_with(s.clone(), |_, _| false), s);
    }

    #[test]
    fn list_other_rows_become_sessions() {
        let row = json!({"job": "c6", "name": "plugin-redux/port-trilium", "node": "mizar", "project": "awm", "scope": "trilium-design",
                         "mode": "worker", "remote_control": true, "link": "https://claude.ai/code/session_1", "started_at": "2026-11-06T19:02:47+00:00"});
        let s = session_of(&row).unwrap();
        assert_eq!((s.job.as_str(), s.kind.as_deref(), s.link.as_deref(), s.remote_control), ("c6", Some("worker"), Some("https://claude.ai/code/session_1"), true));
        assert!(s.started.is_some());
        assert!(session_of(&json!({"name": "no job"})).is_none());
    }

    #[test]
    fn services_are_up_only_when_enabled_and_live() {
        let table = json!([{"name": "journal", "major": 1, "plugin": "journal"}]);
        let plugins = json!({"plugins": [
            {"name": "journal", "services": [{"name": "journal", "plugin": "journal", "enabled": true, "tools": ["journal"]}]},
            {"name": "kb", "services": [{"name": "kb", "plugin": "kb", "enabled": false, "tools": ["kb"]}]},
            {"name": "telemetry-node", "services": [{"name": "telemetry-node", "plugin": "telemetry-node", "enabled": true, "tools": []}]},
        ]});
        let (services, tools) = services_and_tools(&table, &plugins, &[("social".into(), "alcor".into())]);
        let up: Vec<_> = services.iter().map(|s| (s.service.as_str(), s.up)).collect();
        assert_eq!(up, [("journal", true), ("kb", false), ("telemetry-node", true)]);
        assert_eq!(tools, [ToolRow { tool: "journal".into(), host: None }, ToolRow { tool: "social".into(), host: Some("alcor".into()) }]);
    }
}
