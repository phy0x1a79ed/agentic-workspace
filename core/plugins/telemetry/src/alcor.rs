//! The roster part, on alcor: it takes each node part's reports, keeps the roster in a JSON
//! file in its state dir, serves it, and publishes `telemetry/roster` and `telemetry/sessions`.

use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use awm_plugin_sdk::supervise::SubscriptionOpts;
use awm_plugin_sdk::{config, Call, CallOpts, Context, Hub, Service, SupervisedSubscription, VerbError};
use chrono::Utc;
use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::roster::{Applied, Item, Roster};
use crate::verbs;

/// The singletons `telemetry services` lists, as `(tool, host)`.
pub type Singletons = Arc<dyn Fn() -> Vec<(String, String)> + Send + Sync>;

pub struct RosterPart {
    roster: Mutex<Roster>,
    file: PathBuf,
    swarm: Option<String>,
    singletons: Singletons,
    /// Rings at each roster change, for the push to the vault (fleet.md R79).
    changed: Notify,
}

/// The singletons of this node's edge list.
pub fn edge_singletons() -> Singletons {
    Arc::new(|| match config::edges() {
        Ok(e) => e.singletons.iter().map(|s| (s.tool.clone(), s.host.clone())).collect(),
        Err(e) => {
            tracing::warn!("edges.yml unreadable, so no singleton is listed: {e:#}");
            vec![]
        }
    })
}

impl RosterPart {
    /// Load the roster kept in `<state_dir>/roster.json`. A missing file is an empty roster.
    pub fn load(state_dir: PathBuf, swarm: Option<String>, singletons: Singletons) -> anyhow::Result<Arc<Self>> {
        let file = state_dir.join("roster.json");
        let roster = match std::fs::read_to_string(&file) {
            Ok(text) => serde_json::from_str(&text).map_err(|e| anyhow::anyhow!("{}: {e}", file.display()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Roster::default(),
            Err(e) => return Err(anyhow::anyhow!("{}: {e}", file.display())),
        };
        Ok(Arc::new(Self { roster: Mutex::new(roster), file, swarm, singletons, changed: Notify::new() }))
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Roster> {
        self.roster.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn save(&self, roster: &Roster) {
        let write = || -> anyhow::Result<()> {
            if let Some(dir) = self.file.parent() {
                std::fs::create_dir_all(dir)?;
            }
            let tmp = self.file.with_extension("json.tmp");
            std::fs::write(&tmp, serde_json::to_vec(roster)?)?;
            std::fs::rename(&tmp, &self.file)?;
            Ok(())
        };
        if let Err(e) = write() {
            tracing::error!("could not save the roster to {}: {e:#}", self.file.display());
        }
    }

    /// Persist and publish what one change did.
    fn publish(&self, ctx: &Context, node: Option<&str>, applied: &Applied, roster: &Roster) {
        if applied.changed.is_empty() {
            return;
        }
        self.save(roster);
        for ev in &applied.sessions {
            ctx.emit("sessions", ev);
        }
        ctx.emit("roster", json!({"rev": roster.rev, "node": node, "changed": applied.changed, "ts": Utc::now()}));
        self.changed.notify_one();
    }

    /// The roster as the vault's Ops note takes it.
    fn ops_push(&self) -> Value {
        let singles = (self.singletons)();
        let roster = self.lock();
        json!({
            "nodes": roster.nodes_view(None, Utc::now())["nodes"],
            "services": roster.services_view(&singles)["services"],
            "seats": roster.seats_view()["seats"],
        })
    }

    /// Carry each roster change to the vault on deneb, which makes no outbound call. A failed
    /// push waits for the next change, which sends the whole roster again.
    pub async fn push_to_vault(self: Arc<Self>, hub: Hub) {
        let opts = CallOpts { timeout: Duration::from_secs(120), ..CallOpts::default() };
        self.changed.notify_one();
        loop {
            self.changed.notified().await;
            if let Err(e) = hub.call_with("trilium", "ops_render", self.ops_push(), &opts).await {
                tracing::warn!("the roster push to the vault failed, and the next change retries it: {e}");
            }
        }
    }

    fn report(&self, call: &Call) -> Result<Value, VerbError> {
        let Some(swarm) = &self.swarm else {
            return Err(VerbError::permission("this node keeps no roster: its install config names no [telemetry] swarm"));
        };
        let theirs: String = call.req("swarm")?;
        if &theirs != swarm {
            return Err(VerbError::permission(format!("swarm {theirs:?} is not this roster's swarm {swarm:?}")));
        }
        let node = config::label(&call.req::<String>("node")?);
        let from = config::label(call.caller.origin.as_deref().unwrap_or(&call.caller.node));
        if !from.is_empty() && from != node {
            return Err(VerbError::permission(format!("a report for {node} must come from {node}, not {from}")));
        }
        let items: Vec<Item> = call.req("items")?;
        let mut roster = self.lock();
        let applied = roster.apply(&node, items, Utc::now());
        self.publish(&call.ctx, Some(&node), &applied, &roster);
        Ok(json!({"ok": true, "rev": roster.rev, "resync": applied.resync}))
    }

    fn seats(&self, ctx: &Context, f: impl FnOnce(&mut Roster) -> Applied) {
        let mut roster = self.lock();
        let applied = f(&mut roster);
        self.publish(ctx, None, &applied, &roster);
    }

    /// Follow `social/seats` here, reading social's roster at each connect that may have missed
    /// events: the first, and every one after a dropped stream.
    pub fn follow_seats(self: &Arc<Self>, ctx: Context) -> SupervisedSubscription {
        let me = self.clone();
        let sub: Arc<OnceLock<SupervisedSubscription>> = Arc::default();
        let cell = sub.clone();
        let hub = ctx.hub().clone();
        let snap_ctx = ctx.clone();
        let handle = SupervisedSubscription::spawn(
            "social/seats",
            SubscriptionOpts::default(),
            move || {
                let (hub, me, ctx, cell) = (hub.clone(), me.clone(), snap_ctx.clone(), cell.clone());
                async move {
                    let stream = hub.subscribe("social", "seats", None, None).await?;
                    if crate::node::missed_events(cell.get()) {
                        tokio::spawn(async move { me.read_seats(&hub, &ctx).await });
                    }
                    Ok(stream)
                }
            },
            {
                let me = self.clone();
                move |payload: Value| {
                    let (me, ctx) = (me.clone(), ctx.clone());
                    async move {
                        me.seats(&ctx, |r| r.seat_event(&payload));
                        Ok(())
                    }
                }
            },
        );
        let _ = sub.set(handle.clone());
        handle
    }

    /// Read social's roster, one `seats` call per live account. An account that fails logs and
    /// leaves the others in.
    async fn read_seats(&self, hub: &Hub, ctx: &Context) {
        let accounts = match hub.call("social", "accounts", json!({}), None).await {
            Ok(v) => v.get("accounts").and_then(Value::as_array).cloned().unwrap_or_default(),
            Err(e) => return tracing::warn!("social accounts: {e}"),
        };
        let mut rows = vec![];
        for a in accounts.iter().filter(|a| a["enabled"] == true && a["live"] == true) {
            let Some(name) = a["name"].as_str() else { continue };
            match hub.call("social", "seats", json!({"account": name}), None).await {
                Ok(v) => rows.extend(account_rows(name, &v)),
                Err(e) => tracing::warn!("social seats for {name}: {e}"),
            }
        }
        self.seats(ctx, |r| r.set_seats(&rows));
    }
}

/// The rows of one account's `social seats` reply, each tagged with its account.
fn account_rows(account: &str, reply: &Value) -> Vec<Value> {
    let rows = reply.get("seats").and_then(Value::as_array).into_iter().flatten();
    rows.map(|row| {
        let mut row = row.clone();
        row["account"] = json!(account);
        row
    })
    .collect()
}

/// The `telemetry` tool on alcor.
pub fn service(svc: Service, part: Arc<RosterPart>) -> Service {
    let (p1, p2, p3, p4) = (part.clone(), part.clone(), part.clone(), part);
    svc.emitter("roster", Some(verbs::ROSTER_TOPIC))
        .emitter("sessions", Some(verbs::SESSIONS_TOPIC))
        .verb(verbs::TOOL, "nodes", verbs::NODES, verbs::params("nodes"), move |c: Call| {
            let p = p1.clone();
            async move {
                let only = c.str_arg("node").map(config::label);
                Ok::<Value, VerbError>(p.lock().nodes_view(only.as_deref(), Utc::now()))
            }
        })
        .verb(verbs::TOOL, "services", verbs::SERVICES, verbs::params("services"), move |_c: Call| {
            let p = p2.clone();
            async move {
                let singles = (p.singletons)();
                Ok::<Value, VerbError>(p.lock().services_view(&singles))
            }
        })
        .verb(verbs::TOOL, "seats", verbs::SEATS, verbs::params("seats"), move |_c: Call| {
            let p = p3.clone();
            async move { Ok::<Value, VerbError>(p.lock().seats_view()) }
        })
        .verb(verbs::TOOL, "report", verbs::REPORT, verbs::params("report"), move |c: Call| {
            let p = p4.clone();
            async move { p.report(&c) }
        })
        .hidden()
}

#[cfg(test)]
mod tests {
    use super::*;
    use awm_plugin_sdk::{Caller, Env};

    pub(crate) fn env(dir: &std::path::Path, node: &str) -> Env {
        Env {
            hub_url: "http://127.0.0.1:1".into(),
            service: "telemetry".into(),
            plugin: "telemetry".into(),
            node: node.into(),
            awm_dir: dir.to_path_buf(),
            token_file: None,
        }
    }

    fn singles() -> Singletons {
        Arc::new(|| vec![("social".into(), "alcor".into())])
    }

    fn from(node: &str) -> Caller {
        Caller { node: node.into(), ..Caller::default() }
    }

    #[tokio::test]
    async fn reports_are_checked_kept_and_published() {
        let tmp = tempfile::tempdir().unwrap();
        let part = RosterPart::load(tmp.path().join("services/telemetry"), Some("tony".into()), singles()).unwrap();
        let mut t = service(Service::new(env(tmp.path(), "alcor")), part).into_test().await.unwrap();
        let hello = json!({"swarm": "tony", "node": "mizar", "items": [{"type": "hello", "role": "home",
            "sessions": [{"job": "a1", "link": "https://claude.ai/code/a1", "task_id": "CW"}]}]});

        let e = t.call_as("report", hello.clone(), from("lyra"), Some("peer:lyra")).await.unwrap_err();
        assert!(e.message.contains("must come from mizar"), "{e:?}");
        let e = t.call_as("report", json!({"swarm": "other", "node": "mizar", "items": []}), from("mizar"), None).await.unwrap_err();
        assert!(e.message.contains("not this roster's swarm"), "{e:?}");

        let got = t.call_as("report", hello, from("mizar"), Some("peer:mizar")).await.unwrap();
        assert_eq!((got["ok"].as_bool(), got["resync"].as_bool()), (Some(true), Some(false)));
        let frames = t.frames();
        let topics: Vec<_> = frames.iter().map(|f| f["topic"].as_str().unwrap_or("")).collect();
        assert_eq!(topics, ["sessions", "roster"]);
        assert_eq!(frames[0]["payload"]["event"], "live");
        assert_eq!(frames[0]["payload"]["task_id"], "CW");
        assert!(tmp.path().join("services/telemetry/roster.json").is_file());

        let nodes = t.call("nodes", json!({})).await.unwrap();
        assert_eq!(nodes["nodes"][0]["sessions"][0]["link"], "https://claude.ai/code/a1");
        assert_eq!(nodes["nodes"][0]["sessions_7d"], 1);

        // A report from a node the roster lost asks for a hello, and changes nothing.
        let got = t.call_as("report", json!({"swarm": "tony", "node": "lyra", "items": [{"type": "usage", "tool": "journal"}]}), from("lyra"), None).await.unwrap();
        assert_eq!(got["resync"], true);
        assert!(t.frames().is_empty());
    }

    #[tokio::test]
    async fn the_roster_survives_a_restart() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("state");
        let part = RosterPart::load(dir.clone(), Some("tony".into()), singles()).unwrap();
        let t = service(Service::new(env(tmp.path(), "alcor")), part).into_test().await.unwrap();
        t.call_as("report", json!({"swarm": "tony", "node": "alcor", "items": [{"type": "hello",
            "services": [{"service": "social", "tools": ["social"], "up": true}]}]}), from("alcor"), None).await.unwrap();
        drop(t);
        let part = RosterPart::load(dir, Some("tony".into()), singles()).unwrap();
        let t = service(Service::new(env(tmp.path(), "alcor")), part).into_test().await.unwrap();
        let v = t.call("services", json!({})).await.unwrap();
        assert_eq!(v["services"][0]["health"], "up");
    }
}
