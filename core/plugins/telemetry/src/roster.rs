//! The swarm's roster as alcor keeps it: what each node last reported, folded in report by
//! report. Pure state, so every rule here is tested without a gateway.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// A node's usage is its count of sessions in the last 7 days (fleet.md R147 item 5).
pub const USAGE_WINDOW_DAYS: i64 = 7;

pub type Ts = DateTime<Utc>;

/// An RFC 3339 string or unix seconds, as the emit payloads write `ts`.
pub fn parse_ts(v: Option<&Value>) -> Option<Ts> {
    match v? {
        Value::String(s) => DateTime::parse_from_rfc3339(s).ok().map(|t| t.with_timezone(&Utc)),
        Value::Number(n) => {
            let secs = n.as_f64()?;
            DateTime::from_timestamp(secs.trunc() as i64, ((secs.fract()) * 1e9) as u32)
        }
        _ => None,
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Session {
    pub job: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    /// The session's claude.ai link.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub link: Option<String>,
    #[serde(default)]
    pub remote_control: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started: Option<Ts>,
}

impl Session {
    /// The id `telemetry/sessions` names: the harness session id, else the job.
    pub fn id(&self) -> &str {
        self.session_id.as_deref().unwrap_or(&self.job)
    }
}

/// One service of a node's gateway.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ServiceRow {
    pub service: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plugin: Option<String>,
    #[serde(default)]
    pub tools: Vec<String>,
    #[serde(default)]
    pub up: bool,
}

/// One tool a node can use: its own, or a singleton behind another host's door.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ToolRow {
    pub tool: String,
    /// The singleton's host when another node serves it; None for a tool of this node.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
}

/// One fact a node part forwards. A report carries a batch of them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Item {
    /// The node's whole state, read once at connect: it replaces what the roster held.
    Hello {
        #[serde(default)]
        role: Option<String>,
        #[serde(default)]
        portal: Option<String>,
        #[serde(default)]
        sessions: Vec<Session>,
        #[serde(default)]
        services: Vec<ServiceRow>,
        #[serde(default)]
        tools: Vec<ToolRow>,
    },
    /// One `reflection/sessions` event.
    Session { event: String, session: Session, #[serde(default)] ts: Option<Ts> },
    /// One `awm/services` event.
    Service { service: String, #[serde(default)] plugin: Option<String>, up: bool, #[serde(default)] ts: Option<Ts> },
    /// The newest use of one tool on the node, coalesced from `awm/usage`.
    Usage { tool: String, #[serde(default)] last_used: Option<Ts>, #[serde(default)] last_ok: Option<Ts> },
    /// The node's services and tools again, after an `awm/plugins` change.
    Tools { services: Vec<ServiceRow>, tools: Vec<ToolRow> },
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ServiceState {
    #[serde(default)]
    pub plugin: Option<String>,
    #[serde(default)]
    pub tools: Vec<String>,
    pub up: bool,
    #[serde(default)]
    pub since: Option<Ts>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ToolUse {
    #[serde(default)]
    pub host: Option<String>,
    /// Whether the node's last check listed the tool. A tool it no longer lists keeps its history.
    pub available: bool,
    #[serde(default)]
    pub last_checked: Option<Ts>,
    #[serde(default)]
    pub last_used: Option<Ts>,
    /// The newest call that succeeded: for a tool the node hosts, its last response.
    #[serde(default)]
    pub last_ok: Option<Ts>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct NodeState {
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub portal: Option<String>,
    #[serde(default)]
    pub last_seen: Option<Ts>,
    #[serde(default)]
    pub sessions: BTreeMap<String, Session>,
    /// Each session seen live in the usage window, by job, with when it started.
    #[serde(default)]
    pub starts: BTreeMap<String, Ts>,
    #[serde(default)]
    pub services: BTreeMap<String, ServiceState>,
    #[serde(default)]
    pub tools: BTreeMap<String, ToolUse>,
}

/// A session that went live or went, for `telemetry/sessions`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SessionEvent {
    pub event: &'static str,
    pub session_id: String,
    pub job: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub node: String,
    pub ts: Ts,
}

/// What one report changed.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Applied {
    /// The parts of the roster that changed: `nodes`, `sessions`, `services`, `usage`, `seats`.
    pub changed: BTreeSet<&'static str>,
    pub sessions: Vec<SessionEvent>,
    /// The roster does not know the node: it must send a hello first.
    pub resync: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Roster {
    #[serde(default)]
    pub rev: u64,
    #[serde(default)]
    pub nodes: BTreeMap<String, NodeState>,
    #[serde(default)]
    pub seats: BTreeMap<String, Value>,
}

fn newest(a: Option<Ts>, b: Option<Ts>) -> Option<Ts> {
    a.max(b)
}

impl Roster {
    /// Fold one node's report into the roster.
    pub fn apply(&mut self, node: &str, items: Vec<Item>, now: Ts) -> Applied {
        let mut out = Applied::default();
        let has_hello = items.iter().any(|i| matches!(i, Item::Hello { .. }));
        if !self.nodes.contains_key(node) && !has_hello {
            out.resync = true;
            return out;
        }
        let first_seen = !self.nodes.contains_key(node);
        let state = self.nodes.entry(node.to_string()).or_default();
        if first_seen {
            out.changed.insert("nodes");
        }
        state.last_seen = Some(now);
        for item in items {
            apply_item(state, node, item, now, &mut out);
        }
        let cutoff = now - Duration::days(USAGE_WINDOW_DAYS);
        let before = state.starts.len();
        state.starts.retain(|_, t| *t >= cutoff);
        if state.starts.len() != before {
            out.changed.insert("nodes");
        }
        if !out.changed.is_empty() {
            self.rev += 1;
        }
        out
    }

    /// Replace the seats with social's roster.
    pub fn set_seats(&mut self, rows: &[Value]) -> Applied {
        let seats: BTreeMap<String, Value> = rows.iter().filter_map(|r| Some((seat_key(r)?, r.clone()))).collect();
        self.seat_change(seats != self.seats, |s| s.seats = seats)
    }

    /// One `social/seats` event.
    pub fn seat_event(&mut self, row: &Value) -> Applied {
        let Some(key) = seat_key(row) else { return Applied::default() };
        let changed = self.seats.get(&key) != Some(row);
        self.seat_change(changed, |s| {
            s.seats.insert(key, row.clone());
        })
    }

    fn seat_change(&mut self, changed: bool, f: impl FnOnce(&mut Self)) -> Applied {
        let mut out = Applied::default();
        if changed {
            f(self);
            self.rev += 1;
            out.changed.insert("seats");
        }
        out
    }

    pub fn sessions_in_window(&self, node: &str, now: Ts) -> usize {
        let cutoff = now - Duration::days(USAGE_WINDOW_DAYS);
        self.nodes.get(node).map_or(0, |n| n.starts.values().filter(|t| **t >= cutoff).count())
    }

    /// `telemetry nodes`: each node with its role, last seen, sessions, 7-day count and the
    /// services it can use.
    pub fn nodes_view(&self, only: Option<&str>, now: Ts) -> Value {
        let rows: Vec<Value> = self
            .nodes
            .iter()
            .filter(|(name, _)| only.is_none_or(|o| o == name.as_str()))
            .map(|(name, n)| {
                json!({
                    "node": name,
                    "role": n.role,
                    "portal": n.portal,
                    "last_seen": n.last_seen,
                    "sessions_7d": self.sessions_in_window(name, now),
                    "sessions": n.sessions.values().collect::<Vec<_>>(),
                    "services": n.services.iter().map(|(s, st)| json!({
                        "service": s, "plugin": st.plugin, "tools": st.tools, "up": st.up, "since": st.since,
                    })).collect::<Vec<_>>(),
                    "uses": n.tools.iter().map(|(t, u)| json!({
                        "tool": t, "host": u.host, "available": u.available,
                        "last_checked": u.last_checked, "last_used": u.last_used,
                    })).collect::<Vec<_>>(),
                })
            })
            .collect();
        json!({"rev": self.rev, "nodes": rows})
    }

    /// `telemetry services`: each singleton `(tool, host)` with its health on its host, when its
    /// host last answered a call to it, and when any node last used it.
    pub fn services_view(&self, singletons: &[(String, String)]) -> Value {
        let rows: Vec<Value> = singletons
            .iter()
            .map(|(tool, host)| {
                let on_host = self.nodes.get(host);
                let service = on_host.and_then(|n| n.services.iter().find(|(_, s)| s.tools.iter().any(|t| t == tool)));
                let health = match (on_host, service) {
                    (None, _) | (Some(_), None) => "unknown",
                    (Some(_), Some((_, s))) if s.up => "up",
                    _ => "down",
                };
                let last_used = self.nodes.values().filter_map(|n| n.tools.get(tool).and_then(|u| u.last_used)).max();
                json!({
                    "tool": tool,
                    "host": host,
                    "service": service.map(|(name, _)| name),
                    "health": health,
                    "since": service.and_then(|(_, s)| s.since),
                    "last_response": on_host.and_then(|n| n.tools.get(tool)).and_then(|u| u.last_ok),
                    "last_used": last_used,
                })
            })
            .collect();
        json!({"rev": self.rev, "services": rows})
    }

    pub fn seats_view(&self) -> Value {
        json!({"rev": self.rev, "seats": self.seats.values().collect::<Vec<_>>()})
    }
}

fn seat_key(row: &Value) -> Option<String> {
    row.get("seat").or_else(|| row.get("name")).and_then(Value::as_str).map(str::to_string)
}

fn live(node: &str, s: &Session, ts: Ts) -> SessionEvent {
    SessionEvent { event: "live", session_id: s.id().into(), job: s.job.clone(), task_id: s.task_id.clone(), node: node.into(), ts }
}

fn gone(node: &str, s: &Session, ts: Ts) -> SessionEvent {
    SessionEvent { event: "gone", ..live(node, s, ts) }
}

fn set_services(state: &mut NodeState, rows: Vec<ServiceRow>, now: Ts, out: &mut Applied) {
    let next: BTreeMap<String, ServiceState> = rows
        .into_iter()
        .map(|r| {
            let since = match state.services.get(&r.service) {
                Some(old) if old.up == r.up => old.since,
                _ => Some(now),
            };
            (r.service, ServiceState { plugin: r.plugin, tools: r.tools, up: r.up, since })
        })
        .collect();
    if next != state.services {
        state.services = next;
        out.changed.insert("services");
    }
}

fn set_tools(state: &mut NodeState, rows: Vec<ToolRow>, now: Ts, out: &mut Applied) {
    let listed: BTreeMap<String, Option<String>> = rows.into_iter().map(|r| (r.tool, r.host)).collect();
    for (tool, u) in state.tools.iter_mut() {
        if !listed.contains_key(tool) && u.available {
            u.available = false;
            out.changed.insert("services");
        }
    }
    for (tool, host) in listed {
        let u = state.tools.entry(tool).or_default();
        if !u.available || u.host != host {
            out.changed.insert("services");
        }
        (u.available, u.host, u.last_checked) = (true, host, Some(now));
    }
}

fn apply_item(state: &mut NodeState, node: &str, item: Item, now: Ts, out: &mut Applied) {
    match item {
        Item::Hello { role, portal, sessions, services, tools } => {
            if (state.role.as_ref(), state.portal.as_ref()) != (role.as_ref(), portal.as_ref()) {
                (state.role, state.portal) = (role, portal);
                out.changed.insert("nodes");
            }
            let next: BTreeMap<String, Session> = sessions.into_iter().map(|s| (s.job.clone(), s)).collect();
            for (job, s) in &state.sessions {
                if !next.contains_key(job) {
                    out.sessions.push(gone(node, s, now));
                }
            }
            for (job, s) in &next {
                if !state.sessions.contains_key(job) {
                    out.sessions.push(live(node, s, now));
                    state.starts.entry(job.clone()).or_insert(s.started.unwrap_or(now));
                }
            }
            if next != state.sessions {
                state.sessions = next;
                out.changed.insert("sessions");
            }
            set_services(state, services, now, out);
            set_tools(state, tools, now, out);
        }
        Item::Session { event, mut session, ts } => {
            let ts = ts.unwrap_or(now);
            match event.as_str() {
                "stopped" => {
                    if let Some(s) = state.sessions.remove(&session.job) {
                        out.sessions.push(gone(node, &s, ts));
                        out.changed.insert("sessions");
                    }
                }
                "started" | "remote_control" | "renamed" => {
                    let old = state.sessions.get(&session.job).cloned();
                    if let Some(old) = &old {
                        // An update event names only what changed; the rest stays as it was.
                        session.session_id = session.session_id.or_else(|| old.session_id.clone());
                        session.name = session.name.or_else(|| old.name.clone());
                        session.project = session.project.or_else(|| old.project.clone());
                        session.scope = session.scope.or_else(|| old.scope.clone());
                        session.kind = session.kind.or_else(|| old.kind.clone());
                        session.task_id = session.task_id.or_else(|| old.task_id.clone());
                        session.started = session.started.or(old.started);
                        if event != "remote_control" {
                            session.link = session.link.or_else(|| old.link.clone());
                            session.remote_control = session.remote_control || old.remote_control;
                        }
                    } else {
                        session.started = session.started.or(Some(ts));
                        out.sessions.push(live(node, &session, ts));
                        state.starts.entry(session.job.clone()).or_insert(session.started.unwrap_or(ts));
                    }
                    if old.as_ref() != Some(&session) {
                        state.sessions.insert(session.job.clone(), session);
                        out.changed.insert("sessions");
                    }
                }
                _ => {}
            }
        }
        Item::Service { service, plugin, up, ts } => {
            let s = state.services.entry(service).or_default();
            if s.up != up || s.since.is_none() {
                (s.up, s.since) = (up, Some(ts.unwrap_or(now)));
                out.changed.insert("services");
            }
            if plugin.is_some() {
                s.plugin = plugin;
            }
        }
        Item::Usage { tool, last_used, last_ok } => {
            let u = state.tools.entry(tool).or_default();
            let (used, ok) = (newest(u.last_used, last_used), newest(u.last_ok, last_ok));
            if (used, ok) != (u.last_used, u.last_ok) {
                (u.last_used, u.last_ok) = (used, ok);
                out.changed.insert("usage");
            }
        }
        Item::Tools { services, tools } => {
            set_services(state, services, now, out);
            set_tools(state, tools, now, out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(s: &str) -> Ts {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn sess(job: &str) -> Session {
        Session { job: job.into(), name: Some(format!("{job}-name")), link: Some(format!("https://claude.ai/code/{job}")), ..Session::default() }
    }

    fn hello(sessions: Vec<Session>) -> Item {
        Item::Hello {
            role: Some("home".into()),
            portal: Some("https://mizar:8443".into()),
            sessions,
            services: vec![ServiceRow { service: "social".into(), plugin: Some("social".into()), tools: vec!["social".into()], up: true }],
            tools: vec![ToolRow { tool: "journal".into(), host: None }, ToolRow { tool: "social".into(), host: Some("alcor".into()) }],
        }
    }

    #[test]
    fn a_node_unknown_to_the_roster_must_say_hello_first() {
        let mut r = Roster::default();
        let a = r.apply("mizar", vec![Item::Usage { tool: "x".into(), last_used: None, last_ok: None }], t("2026-11-07T10:00:00Z"));
        assert!(a.resync && r.nodes.is_empty() && r.rev == 0);
    }

    #[test]
    fn hello_then_start_and_stop_change_the_roster_and_name_each_session() {
        let mut r = Roster::default();
        let now = t("2026-11-07T10:00:00Z");
        let a = r.apply("mizar", vec![hello(vec![sess("a1")])], now);
        assert_eq!(a.sessions.iter().map(|e| (e.event, e.job.as_str())).collect::<Vec<_>>(), [("live", "a1")]);
        assert!(a.changed.contains("nodes") && a.changed.contains("sessions"));

        let started = Item::Session { event: "started".into(), session: Session { task_id: Some("CW".into()), ..sess("b2") }, ts: Some(now) };
        let a = r.apply("mizar", vec![started], now);
        assert_eq!((a.sessions[0].event, a.sessions[0].task_id.as_deref()), ("live", Some("CW")));
        assert_eq!(r.nodes["mizar"].sessions.len(), 2);

        let stopped = Item::Session { event: "stopped".into(), session: Session { job: "a1".into(), ..Session::default() }, ts: Some(now) };
        let a = r.apply("mizar", vec![stopped.clone()], now);
        assert_eq!((a.sessions[0].event, a.sessions[0].session_id.as_str()), ("gone", "a1"));
        assert_eq!(r.nodes["mizar"].sessions.keys().collect::<Vec<_>>(), ["b2"]);
        // A second stop of the same session changes nothing.
        let rev = r.rev;
        let a = r.apply("mizar", vec![stopped], now);
        assert!(a.sessions.is_empty() && !a.changed.contains("sessions") && r.rev == rev);
    }

    #[test]
    fn a_fresh_hello_ends_the_sessions_it_no_longer_lists() {
        let mut r = Roster::default();
        let now = t("2026-11-07T10:00:00Z");
        r.apply("mizar", vec![hello(vec![sess("a1"), sess("a2")])], now);
        let a = r.apply("mizar", vec![hello(vec![sess("a2"), sess("a3")])], now);
        let mut got: Vec<_> = a.sessions.iter().map(|e| (e.event, e.job.as_str())).collect();
        got.sort();
        assert_eq!(got, [("gone", "a1"), ("live", "a3")]);
    }

    #[test]
    fn remote_control_updates_the_link_and_keeps_the_rest() {
        let mut r = Roster::default();
        let now = t("2026-11-07T10:00:00Z");
        r.apply("mizar", vec![hello(vec![Session { link: None, ..sess("a1") }])], now);
        let on = Session { job: "a1".into(), link: Some("https://claude.ai/code/x".into()), remote_control: true, ..Session::default() };
        let a = r.apply("mizar", vec![Item::Session { event: "remote_control".into(), session: on, ts: None }], now);
        assert!(a.sessions.is_empty(), "an update is not a new session");
        let s = &r.nodes["mizar"].sessions["a1"];
        assert_eq!((s.link.as_deref(), s.remote_control, s.name.as_deref()), (Some("https://claude.ai/code/x"), true, Some("a1-name")));
        let off = Session { job: "a1".into(), ..Session::default() };
        r.apply("mizar", vec![Item::Session { event: "remote_control".into(), session: off, ts: None }], now);
        let s = &r.nodes["mizar"].sessions["a1"];
        assert_eq!((s.link.as_deref(), s.remote_control), (None, false));
    }

    #[test]
    fn usage_counts_the_sessions_of_the_last_seven_days() {
        let mut r = Roster::default();
        let day0 = t("2026-11-01T10:00:00Z");
        r.apply("mizar", vec![hello(vec![sess("old")])], day0);
        let day3 = t("2026-11-04T10:00:00Z");
        r.apply("mizar", vec![Item::Session { event: "started".into(), session: sess("mid"), ts: Some(day3) }], day3);
        r.apply("mizar", vec![Item::Session { event: "stopped".into(), session: sess("mid"), ts: Some(day3) }], day3);
        assert_eq!(r.sessions_in_window("mizar", day3), 2, "a stopped session still counts");
        let day8 = t("2026-11-09T11:00:00Z");
        assert_eq!(r.sessions_in_window("mizar", day8), 1);
        r.apply("mizar", vec![Item::Usage { tool: "journal".into(), last_used: Some(day8), last_ok: None }], day8);
        assert_eq!(r.nodes["mizar"].starts.keys().collect::<Vec<_>>(), ["mid"], "a report prunes the window");
        assert_eq!(r.nodes_view(None, day8)["nodes"][0]["sessions_7d"], 1);
    }

    #[test]
    fn singleton_health_last_response_and_last_use() {
        let mut r = Roster::default();
        let now = t("2026-11-07T10:00:00Z");
        r.apply("alcor", vec![hello(vec![])], now);
        r.apply("mizar", vec![hello(vec![])], now);
        let used = t("2026-11-07T10:05:00Z");
        r.apply("alcor", vec![Item::Usage { tool: "social".into(), last_used: Some(used), last_ok: Some(used) }], used);
        let later = t("2026-11-07T10:09:00Z");
        r.apply("mizar", vec![Item::Usage { tool: "social".into(), last_used: Some(later), last_ok: None }], later);
        let singles = vec![("social".to_string(), "alcor".to_string()), ("2fa".to_string(), "alcor".to_string()), ("relay".to_string(), "deneb".to_string())];
        let v = r.services_view(&singles);
        let row = &v["services"][0];
        assert_eq!((row["health"].as_str(), row["service"].as_str()), (Some("up"), Some("social")));
        assert_eq!(row["last_response"], json!(used));
        assert_eq!(row["last_used"], json!(later));
        assert_eq!(v["services"][1]["health"], "unknown", "alcor runs no service with the 2fa tool");
        assert_eq!(v["services"][2]["health"], "unknown", "deneb never reported");

        let down = Item::Service { service: "social".into(), plugin: None, up: false, ts: Some(later) };
        let a = r.apply("alcor", vec![down], later);
        assert!(a.changed.contains("services"));
        let row = &r.services_view(&singles)["services"][0];
        assert_eq!((row["health"].as_str(), &row["since"]), (Some("down"), &json!(later)));
    }

    #[test]
    fn a_tool_the_node_stops_listing_keeps_its_history() {
        let mut r = Roster::default();
        let now = t("2026-11-07T10:00:00Z");
        r.apply("mizar", vec![hello(vec![]), Item::Usage { tool: "journal".into(), last_used: Some(now), last_ok: Some(now) }], now);
        r.apply("mizar", vec![Item::Tools { services: vec![], tools: vec![ToolRow { tool: "social".into(), host: Some("alcor".into()) }] }], now);
        let j = &r.nodes["mizar"].tools["journal"];
        assert!(!j.available && j.last_used == Some(now));
        let uses = &r.nodes_view(Some("mizar"), now)["nodes"][0]["uses"];
        assert_eq!(uses.as_array().unwrap().len(), 2);
    }

    #[test]
    fn seats_replace_and_update_by_name() {
        let mut r = Roster::default();
        let empty = json!({"seat": "slack/C042", "label": "alice", "state": "empty", "remote_control": false, "node": "alcor"});
        let a = r.set_seats(std::slice::from_ref(&empty));
        assert!(a.changed.contains("seats"));
        assert!(r.set_seats(&[empty]).changed.is_empty());
        r.seat_event(&json!({"seat": "slack/C042", "label": "alice", "state": "occupied", "remote_control": true, "node": "alcor",
                             "job": "j1", "rc_url": "https://claude.ai/code/s", "ts": "2026-11-07T10:00:00Z"}));
        let v = r.seats_view();
        assert_eq!((v["seats"].as_array().unwrap().len(), v["seats"][0]["state"].as_str()), (1, Some("occupied")), "keyed on the seat id, not the label");
        assert!(r.seat_event(&json!({"state": "x"})).changed.is_empty(), "a row with no seat name is ignored");
    }

    #[test]
    fn timestamps_parse_from_strings_and_numbers() {
        assert_eq!(parse_ts(Some(&json!("2026-11-07T10:00:00Z"))), Some(t("2026-11-07T10:00:00Z")));
        assert_eq!(parse_ts(Some(&json!(1_762_509_600))), Some(t("2025-11-07T10:00:00Z")));
        assert_eq!(parse_ts(Some(&json!(null))), None);
    }

    #[test]
    fn the_roster_round_trips_through_json() {
        let mut r = Roster::default();
        r.apply("mizar", vec![hello(vec![sess("a1")])], t("2026-11-07T10:00:00Z"));
        let back: Roster = serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(back, r);
    }
}
