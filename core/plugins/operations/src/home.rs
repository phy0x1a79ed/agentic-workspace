//! The home node's side of operations: the store and the approver behind one dispatch
//! (fleet.md R106, R97). Every relay reaches these verbs through `Home::call`, its own
//! node's included. A relay on another node arrives through the singleton door, which names
//! the node it came from. Tony's own actions are not here: they reach the store and the
//! approver only from the vault API.

use std::sync::Arc;

use serde_json::{json, Map, Value};

use crate::approver::Approver;
use crate::store::{Open, Seat, Store};

pub const VERBS: [&str; 22] = [
    "task_autotitle", "task_open", "task_attach", "task_join", "task_report", "task_session_end", "task_claim", "task_detail", "task_message",
    "task_messages", "task_for_session", "task_sessions", "task_get", "task_list", "approval_request", "approval_get", "approval_wait",
    "approval_cancel", "order_pull", "order_done", "keys", "ops_post",
];

/// The verbs whose `node` the door's key decides: a node speaks only for itself.
const NODE_STAMPED: [&str; 5] = ["task_open", "task_attach", "task_join", "task_claim", "task_message"];

pub struct Home {
    pub store: Arc<Store>,
    pub approver: Arc<Approver>,
}

fn s(a: &Value, k: &str) -> Option<String> {
    a.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)
}

fn seat(a: &Value) -> Seat {
    Seat { node: s(a, "node"), name: s(a, "name"), rc_url: s(a, "rc_url"), job: s(a, "job") }
}

fn need(a: &Value, k: &str) -> Result<String, String> {
    s(a, k).ok_or_else(|| format!("'{k}'"))
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Result<T, String> {
    tokio::task::spawn_blocking(f).await.map_err(|e| e.to_string())
}

impl Home {
    /// One verb from a relay. An `Err` is a refusal the relay must not replay.
    pub async fn call(&self, verb: &str, args: Value, node: Option<&str>) -> Result<Value, String> {
        let mut a = if args.is_object() { args } else { json!({}) };
        if !verb.starts_with("task_") {
            if let Some(m) = a.as_object_mut() {
                m.remove("op_id");
            }
        }
        if let Some(n) = node {
            if NODE_STAMPED.contains(&verb) {
                a["node"] = json!(n);
            }
        }
        let op = s(&a, "op_id");
        let st = self.store.clone();
        let ap = self.approver.clone();
        let q = |r: Result<Value, crate::store::QueueError>| r.map_err(|e| e.to_string());
        let ae = |e: crate::approver::ApproverError| format!("{}: {}", e.code, e.message);
        match verb {
            "task_open" => {
                let title = s(&a, "title").unwrap_or_default();
                let tags = a.get("tags").and_then(Value::as_array).map(|t| t.iter().filter_map(|x| x.as_str().map(str::to_string)).collect());
                let o = Open {
                    session_id: s(&a, "session_id"), task_id: s(&a, "task_id"), kind: s(&a, "kind"), peer: s(&a, "peer"), node: s(&a, "node"),
                    section: s(&a, "section"), status: s(&a, "status"), tags, meta: a.get("meta").and_then(Value::as_object).cloned(),
                    name: s(&a, "name"), rc_url: s(&a, "rc_url"), job: s(&a, "job"), source_note: None,
                };
                blocking(move || q(st.task_open(&title, o, op.as_deref()))).await?
            }
            "task_attach" => {
                let (t, sid) = (need(&a, "task_id")?, need(&a, "session_id")?);
                let role = s(&a, "role").unwrap_or_else(|| "worker".into());
                blocking(move || q(st.task_attach(&t, &sid, &role, seat(&a), op.as_deref()))).await?
            }
            "task_join" => {
                let (t, sid) = (need(&a, "task_id")?, need(&a, "session_id")?);
                blocking(move || q(st.task_join(&t, &sid, seat(&a), op.as_deref()))).await?
            }
            "task_report" => {
                let t = need(&a, "task_id")?;
                blocking(move || q(st.task_report(&t, s(&a, "session_id").as_deref(), &s(&a, "state").unwrap_or_default(),
                                                  s(&a, "reason").as_deref(), s(&a, "rc_url").as_deref(), op.as_deref()))).await?
            }
            "task_autotitle" => {
                let t = need(&a, "task_id")?;
                let from_plan = a["from_plan"].as_bool().unwrap_or(false);
                blocking(move || q(st.task_autotitle(&t, &s(&a, "title").unwrap_or_default(), from_plan, op.as_deref()))).await?
            }
            "task_session_end" => {
                let (t, sid) = (need(&a, "task_id")?, need(&a, "session_id")?);
                blocking(move || q(st.task_session_end(&t, &sid, op.as_deref()))).await?
            }
            "task_claim" => {
                let (t, sid) = (need(&a, "task_id")?, need(&a, "session_id")?);
                blocking(move || q(st.task_claim(&t, &sid, seat(&a), op.as_deref()))).await?
            }
            "task_detail" => {
                let t = need(&a, "task_id")?;
                blocking(move || {
                    let todos = a.get("todos").and_then(Value::as_array).cloned();
                    q(st.task_detail(&t, a.get("plan").and_then(Value::as_str), todos.as_deref(), op.as_deref()))
                }).await?
            }
            "task_message" => {
                let t = need(&a, "task_id")?;
                blocking(move || q(st.task_message(&t, a.get("body").and_then(Value::as_str).unwrap_or_default(), s(&a, "author").as_deref(),
                                                   s(&a, "node").as_deref(), s(&a, "session_id").as_deref(), op.as_deref()))).await?
            }
            "task_messages" => {
                let t = need(&a, "task_id")?;
                let after = a.get("after").and_then(Value::as_i64).unwrap_or(0);
                blocking(move || st.task_messages(&t, after).map(Value::Array).map_err(|e| e.to_string())).await?
            }
            "task_for_session" => {
                let sid = need(&a, "session_id")?;
                blocking(move || st.task_for_session(&sid).map(|t| json!(t)).map_err(|e| e.to_string())).await?
            }
            "task_sessions" => {
                let t = need(&a, "task_id")?;
                blocking(move || st.task_sessions(&t).map(Value::Array).map_err(|e| e.to_string())).await?
            }
            "task_get" => {
                let t = need(&a, "task_id")?;
                blocking(move || st.task_get(&t).map_err(|e| e.to_string())).await?
            }
            "task_list" => blocking(move || {
                q(st.task_list(s(&a, "section").as_deref(), s(&a, "q").as_deref(), a.get("before").and_then(Value::as_f64), a.get("limit").and_then(Value::as_i64)))
            }).await?,
            "approval_get" => ap.approval_get(&need(&a, "request_id")?).map_err(ae),
            "order_done" => ap.order_done(&need(&a, "order_id")?, a.get("ok").is_some_and(crate::service::truthy), &s(&a, "note").unwrap_or_default()).map_err(ae),
            "keys" => Ok(json!({"grant_pub": ap.keys.grant_pub_b64u(), "ack_pub": ap.keys.ack_pub_spki_b64u(), "ssh_user_ca": ap.keys.ssh_ca_pub()})),
            "approval_request" => {
                if let Some(rid) = s(&a, "request_id") {
                    if let Ok(seen) = ap.approval_get(&rid) {
                        if seen["requester_session"] == a.get("session_id").cloned().unwrap_or(Value::Null) {
                            let pick: Map<String, Value> = ["request_id", "task_id", "expires"].iter().map(|k| ((*k).to_string(), seen[*k].clone())).collect();
                            return Ok(Value::Object(pick));
                        }
                    }
                }
                let kind = need(&a, "kind")?;
                let minutes = a.get("minutes").and_then(|m| m.as_i64().or_else(|| m.as_str().and_then(|s| s.parse().ok()))).unwrap_or(15);
                ap.approval_request(&kind, s(&a, "node").as_deref(), &s(&a, "reason").unwrap_or_default(), minutes,
                                    a.get("payload").cloned().unwrap_or(Value::Null), s(&a, "session_id").as_deref(), s(&a, "request_id").as_deref())
                    .await.map_err(ae)
            }
            "approval_wait" => ap.approval_wait(&need(&a, "request_id")?, a.get("wait_s").and_then(Value::as_f64).unwrap_or(55.0)).await.map_err(ae),
            "approval_cancel" => {
                let rid = need(&a, "request_id")?;
                let seen = ap.approval_get(&rid).map_err(ae)?;
                if seen["status"] == "cancelled" && seen["requester_session"] == a.get("session_id").cloned().unwrap_or(Value::Null) {
                    return Ok(seen);
                }
                ap.approval_cancel(&rid, s(&a, "session_id").as_deref()).await.map_err(ae)
            }
            "ops_post" => {
                let (k, n, t, b) = (need(&a, "kind")?, need(&a, "node")?, need(&a, "title")?, need(&a, "body")?);
                blocking(move || q(st.ops_post(&k, &n, &t, &b, None))).await?
            }
            "order_pull" => {
                let want = s(&a, "node");
                if let (Some(n), Some(w)) = (node, want.as_deref()) {
                    if n != w {
                        return Err(format!("{n} pulls only its own orders, not {w}'s"));
                    }
                }
                let target = want.or(node.map(str::to_string)).ok_or("'node'")?;
                let kinds: Option<Vec<String>> = a.get("kinds").and_then(Value::as_array).map(|k| k.iter().filter_map(|x| x.as_str().map(str::to_string)).collect());
                ap.order_pull(&target, kinds.as_deref(), a.get("wait_s").and_then(Value::as_f64).unwrap_or(0.0)).await.map_err(ae)
            }
            _ => Err(format!("{verb} is not a home verb")),
        }
    }
}

/// The home node's relay calls its own `Home` in process.
pub struct LocalClient {
    pub home: Arc<Home>,
}

impl crate::relay::StoreClient for LocalClient {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> crate::relay::async_trait_free::BoxFut<'a, Result<Value, crate::relay::ClientError>> {
        Box::pin(async move { self.home.call(verb, args, None).await.map_err(crate::relay::ClientError::Refused) })
    }
}
