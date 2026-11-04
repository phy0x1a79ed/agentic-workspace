//! The service registry and the call path over each service's control WebSocket.
//!
//! The protocol is today's Python gateway protocol (contract § Service wire protocol), so a
//! `ServiceAdapter` service runs here unchanged.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use awm_proto::Caller;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot, Notify};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    /// Registered, control WebSocket not yet ready.
    Starting,
    Ready,
    /// Its `ready` manifest broke the registration rule.
    Refused,
    Down,
}

/// One function a service published: the public tool and verb it answers to.
#[derive(Debug, Clone, Serialize)]
pub struct Verb {
    pub tool: String,
    pub verb: String,
    pub function: String,
    pub spec: Value,
}

struct Svc {
    id: String,
    plugin: String,
    status: Status,
    pid: Option<i64>,
    verbs: Vec<Verb>,
    refusal: Option<String>,
    tx: Option<mpsc::UnboundedSender<String>>,
    pending: HashMap<String, oneshot::Sender<Reply>>,
    /// When the control WebSocket last closed, for the respawn watchdog.
    down_since: Option<Instant>,
    /// The `api` of its last `ready` frame: its functions, session kinds and config block.
    api: Value,
    /// The loopback URL of its own HTTP server, served at `/s/<service>/`.
    mount: Option<String>,
}

#[derive(Debug)]
struct Reply {
    ok: bool,
    result: Value,
    error: String,
    error_class: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum CallError {
    #[error("service {0:?} is not up")]
    NotUp(String),
    #[error("service {service:?} did not answer {function} within {secs:.0}s")]
    Timeout { service: String, function: String, secs: f64 },
    #[error("{error}")]
    Remote { error: String, error_class: Option<String> },
    #[error("service {0:?} closed its control connection during the call")]
    Closed(String),
}

#[derive(Debug, Clone, Serialize)]
pub struct ServiceView {
    pub name: String,
    pub plugin: String,
    pub id: String,
    pub status: Status,
    pub pid: Option<i64>,
    pub verbs: usize,
    pub refusal: Option<String>,
    pub mount: Option<String>,
}

#[derive(Default)]
pub struct Hub {
    services: Mutex<HashMap<String, Svc>>,
    /// Signalled on every status change, so a call can wait for a tool to come up.
    pub changed: Notify,
}

pub enum Claim {
    Unknown,
    Held,
    Claimed { name: String, rx: mpsc::UnboundedReceiver<String> },
}

#[derive(Debug)]
pub struct NameHeld;

impl Hub {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, Svc>> {
        self.services.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// `POST /hub/service/register`: a fresh service id, unless a live instance holds the name.
    pub fn register(&self, name: &str, plugin: &str, pid: Option<i64>, mount: Option<String>) -> Result<String, NameHeld> {
        let mut map = self.lock();
        if let Some(old) = map.get(name) {
            if old.tx.is_some() {
                return Err(NameHeld);
            }
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let pending = map.remove(name).map(|s| s.pending).unwrap_or_default();
        map.insert(
            name.to_string(),
            Svc {
                id: id.clone(),
                plugin: plugin.to_string(),
                status: Status::Starting,
                pid,
                verbs: vec![],
                refusal: None,
                tx: None,
                pending,
                down_since: Some(Instant::now()),
                api: Value::Null,
                mount,
            },
        );
        Ok(id)
    }

    /// Take the control lease of service `id` for a new WebSocket.
    pub fn claim(&self, id: &str) -> Claim {
        let mut map = self.lock();
        let Some((name, svc)) = map.iter_mut().find(|(_, s)| s.id == id) else { return Claim::Unknown };
        if svc.tx.is_some() {
            return Claim::Held;
        }
        let (tx, rx) = mpsc::unbounded_channel();
        svc.tx = Some(tx);
        svc.down_since = None;
        Claim::Claimed { name: name.clone(), rx }
    }

    /// The `ready` frame: bind each published function to one of the service's tools and a verb.
    pub fn ready(&self, name: &str, api: &Value, tools: &[String]) {
        let functions = api.get("functions").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut verbs = Vec::new();
        let mut problems = Vec::new();
        for f in &functions {
            let Some(fname) = f.get("name").and_then(Value::as_str) else { continue };
            let published = f.get("tool").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| format!("{name}_{fname}"));
            let bound = tools
                .iter()
                .filter_map(|t| published.strip_prefix(&format!("{t}_")).map(|v| (t, v)))
                .max_by_key(|(t, _)| t.len());
            match bound {
                Some((tool, verb)) if !verb.is_empty() => {
                    if verbs.iter().any(|v: &Verb| &v.tool == tool && v.verb == verb) {
                        problems.push(format!("{published:?} is registered twice"));
                    }
                    verbs.push(Verb { tool: tool.clone(), verb: verb.to_string(), function: fname.to_string(), spec: f.clone() })
                }
                _ => problems.push(format!("{published:?} (function {fname:?}) is not named '<tool>_<verb>' for a tool of {name:?}")),
            }
        }
        let mut map = self.lock();
        for (other, svc) in map.iter() {
            if other == name || svc.status != Status::Ready {
                continue;
            }
            for v in &verbs {
                if svc.verbs.iter().any(|o| o.tool == v.tool && o.verb == v.verb) {
                    problems.push(format!("'{}_{}' is already held by service {other:?}", v.tool, v.verb));
                }
            }
        }
        if let Some(svc) = map.get_mut(name) {
            svc.api = api.clone();
            if problems.is_empty() {
                svc.verbs = verbs;
                svc.status = Status::Ready;
                svc.refusal = None;
            } else {
                let msg = format!("service {name:?} refused: {}", problems.join("; "));
                tracing::error!("{msg}");
                svc.verbs.clear();
                svc.status = Status::Refused;
                svc.refusal = Some(msg);
            }
        }
        drop(map);
        self.changed.notify_waiters();
    }

    pub fn reply(&self, name: &str, frame: &Value) {
        let Some(id) = frame.get("id").and_then(Value::as_str) else { return };
        let tx = self.lock().get_mut(name).and_then(|s| s.pending.remove(id));
        if let Some(tx) = tx {
            let _ = tx.send(Reply {
                ok: frame.get("ok").and_then(Value::as_bool).unwrap_or(false),
                result: frame.get("result").cloned().unwrap_or(Value::Null),
                error: frame.get("error").and_then(Value::as_str).unwrap_or("call failed").to_string(),
                error_class: frame.get("error_class").and_then(Value::as_str).map(str::to_string),
            });
        }
    }

    /// The control WebSocket of service `id` closed. Pending calls fail at once. A socket of an
    /// older instance that closes after a restart under the same name touches nothing.
    pub fn disconnected(&self, name: &str, id: &str) {
        let mut map = self.lock();
        if let Some(svc) = map.get_mut(name).filter(|s| s.id == id) {
            svc.tx = None;
            svc.status = Status::Down;
            svc.down_since = Some(Instant::now());
            svc.pending.clear();
        }
        drop(map);
        self.changed.notify_waiters();
    }

    /// How long a known service has been off its control WebSocket.
    pub fn down_for(&self, name: &str) -> Option<Duration> {
        self.lock().get(name).and_then(|s| s.down_since).map(|t| t.elapsed())
    }

    /// The `api` of a service's last `ready` frame, `Null` before it sent one.
    pub fn api_of(&self, name: &str) -> Value {
        self.lock().get(name).map(|s| s.api.clone()).unwrap_or(Value::Null)
    }

    pub fn id_of(&self, name: &str) -> Option<String> {
        self.lock().get(name).map(|s| s.id.clone())
    }

    /// The service a control id belongs to.
    pub fn name_of_id(&self, id: &str) -> Option<String> {
        self.lock().iter().find(|(_, s)| s.id == id).map(|(n, _)| n.clone())
    }

    pub fn mount_of(&self, name: &str) -> Option<String> {
        self.lock().get(name).filter(|s| s.status == Status::Ready).and_then(|s| s.mount.clone())
    }

    pub fn plugin_of(&self, name: &str) -> Option<String> {
        self.lock().get(name).map(|s| s.plugin.clone())
    }

    pub fn status(&self, name: &str) -> Option<Status> {
        self.lock().get(name).map(|s| s.status)
    }

    pub fn services(&self) -> Vec<ServiceView> {
        let mut v: Vec<_> = self
            .lock()
            .iter()
            .map(|(name, s)| ServiceView {
                name: name.clone(),
                plugin: s.plugin.clone(),
                id: s.id.clone(),
                status: s.status,
                pid: s.pid,
                verbs: s.verbs.len(),
                refusal: s.refusal.clone(),
                mount: s.mount.clone(),
            })
            .collect();
        v.sort_by(|a, b| a.name.cmp(&b.name));
        v
    }

    /// Every ready verb, as `(service, verb)`.
    pub fn verbs(&self) -> Vec<(String, Verb)> {
        self.lock()
            .iter()
            .filter(|(_, s)| s.status == Status::Ready)
            .flat_map(|(n, s)| s.verbs.iter().map(move |v| (n.clone(), v.clone())))
            .collect()
    }

    /// Send a frame to one connected service. False when it has no control connection.
    pub fn send(&self, name: &str, frame: &Value) -> bool {
        self.lock().get(name).and_then(|s| s.tx.as_ref()).is_some_and(|tx| tx.send(frame.to_string()).is_ok())
    }

    /// Drop a service the loader no longer runs, so its verbs leave the catalog.
    pub fn forget(&self, name: &str) {
        self.lock().remove(name);
        self.changed.notify_waiters();
    }

    /// Send a frame to every connected service.
    pub fn broadcast(&self, frame: &Value) {
        let text = frame.to_string();
        for svc in self.lock().values() {
            if let Some(tx) = &svc.tx {
                let _ = tx.send(text.clone());
            }
        }
    }

    pub fn connected(&self) -> usize {
        self.lock().values().filter(|s| s.tx.is_some()).count()
    }

    /// Call `function` on `service`. Dropping the future sends `cancel` to the service.
    pub async fn call(
        &self,
        service: &str,
        function: &str,
        args: Value,
        as_: Option<&str>,
        caller: &Caller,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        let (tx, rx) = oneshot::channel();
        let mut frame = json!({"kind": "call", "id": id, "fn": function, "args": args, "caller": caller});
        if let Some(as_) = as_ {
            frame["as"] = json!(as_);
        }
        let ctl = {
            let mut map = self.lock();
            let svc = map.get_mut(service).filter(|s| s.status == Status::Ready && s.tx.is_some());
            let Some(svc) = svc else { return Err(CallError::NotUp(service.into())) };
            svc.pending.insert(id.clone(), tx);
            svc.tx.clone().expect("checked above")
        };
        if ctl.send(frame.to_string()).is_err() {
            return Err(CallError::NotUp(service.into()));
        }
        let mut guard = CancelOnDrop { hub: self, service, id: &id, ctl: &ctl, armed: true };
        let outcome = tokio::time::timeout(timeout, rx).await;
        guard.armed = false;
        match outcome {
            Err(_) => {
                self.lock().get_mut(service).map(|s| s.pending.remove(&id));
                Err(CallError::Timeout { service: service.into(), function: function.into(), secs: timeout.as_secs_f64() })
            }
            Ok(Err(_)) => Err(CallError::Closed(service.into())),
            Ok(Ok(r)) if r.ok => Ok(r.result),
            Ok(Ok(r)) => Err(CallError::Remote { error: r.error, error_class: r.error_class }),
        }
    }
}

struct CancelOnDrop<'a> {
    hub: &'a Hub,
    service: &'a str,
    id: &'a str,
    ctl: &'a mpsc::UnboundedSender<String>,
    armed: bool,
}

impl Drop for CancelOnDrop<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.hub.lock().get_mut(self.service).map(|s| s.pending.remove(self.id));
            let _ = self.ctl.send(json!({"kind": "cancel", "id": self.id}).to_string());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn register_claim_ready_call() {
        let hub = Hub::default();
        let id = hub.register("journal", "journal", Some(1), None).unwrap();
        let Claim::Claimed { mut rx, .. } = hub.claim(&id) else { panic!() };
        assert!(matches!(hub.claim(&id), Claim::Held));
        assert!(hub.register("journal", "journal", None, None).is_err());
        let bind = ["journal".to_string()];
        hub.ready("journal", &json!({"functions": [{"name": "post"}, {"name": "x", "tool": "journal_fetch"}]}), &bind);
        assert_eq!(hub.status("journal"), Some(Status::Ready));
        let verbs: Vec<_> = hub.verbs().into_iter().map(|(_, v)| v.verb).collect();
        assert!(verbs.contains(&"post".to_string()) && verbs.contains(&"fetch".to_string()));

        let caller = Caller::default();
        let call = hub.call("journal", "post", json!({"body": "hi"}), None, &caller, Duration::from_secs(5));
        let answer = async {
            let frame: Value = serde_json::from_str(&rx.recv().await.unwrap()).unwrap();
            assert_eq!(frame["kind"], "call");
            hub.reply("journal", &json!({"kind": "reply", "id": frame["id"], "ok": true, "result": {"id": 7}}));
        };
        let (res, _) = tokio::join!(call, answer);
        assert_eq!(res.unwrap()["id"], 7);

        let id2 = hub.register("journal2", "journal2", None, None).unwrap();
        let _c = hub.claim(&id2);
        hub.ready("journal2", &json!({"functions": [{"name": "post", "tool": "journal_post"}]}), &bind);
        assert_eq!(hub.status("journal2"), Some(Status::Refused));

        hub.ready("journal", &json!({"functions": [{"name": "post", "tool": "other_post"}]}), &bind);
        assert_eq!(hub.status("journal"), Some(Status::Refused));
        hub.disconnected("journal", "not-its-id");
        assert_eq!(hub.status("journal"), Some(Status::Refused), "a stale socket must not take a service down");
        hub.disconnected("journal", &id);
        assert!(hub.down_for("journal").is_some());
        assert!(hub.register("journal", "journal", None, None).is_ok());
        hub.forget("journal");
        assert_eq!(hub.status("journal"), None);
    }
}
