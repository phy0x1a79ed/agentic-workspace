//! Streams beside calls: a service's emit topics and its direct sessions.
//!
//! A subscriber opens `GET /hub/emit/<service>/<topic>` and gets each payload the service
//! emits on that topic as one JSON text frame. A session opens `GET /hub/session/<service>/<kind>`
//! and sends its init object as the first text frame. The gateway asks the service with a
//! `session.open` frame, the service opens its bridge socket at
//! `/hub/service/bridge/<service_id>/<bridge_id>`, and the gateway relays the two sockets byte
//! for byte. Either path with `peer=<node>` runs on that node over the edge (`peer.rs`).

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use awm_proto::{CallContext, WsMsg};
use axum::extract::ws::{CloseFrame, Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot};

use crate::gateway::{ApiError, Gateway};
use crate::peer::WsPipe;

const OPEN_TIMEOUT: Duration = Duration::from_secs(10);
/// How long a session waits for its client's init frame.
const INIT_TIMEOUT: Duration = Duration::from_secs(15);

struct Subscriber {
    tx: mpsc::UnboundedSender<WsMsg>,
    /// Withhold payloads the private marks cover: the subscriber is on another node.
    peer: bool,
}

#[derive(Default)]
pub struct Topics {
    subs: Mutex<HashMap<(String, String), Vec<Subscriber>>>,
}

impl Topics {
    fn subscribe(&self, service: &str, topic: &str, peer: bool) -> mpsc::UnboundedReceiver<WsMsg> {
        let (tx, rx) = mpsc::unbounded_channel();
        let mut subs = self.subs.lock().unwrap_or_else(|p| p.into_inner());
        subs.entry((service.into(), topic.into())).or_default().push(Subscriber { tx, peer });
        rx
    }

    /// Fan one `emit` frame out to the topic's subscribers, dropping those that left.
    pub fn publish(&self, gw: &Gateway, service: &str, topic: &str, payload: &Value) {
        let mut subs = self.subs.lock().unwrap_or_else(|p| p.into_inner());
        let Some(list) = subs.get_mut(&(service.to_string(), topic.to_string())) else { return };
        let text = payload.to_string();
        let mut hidden: Option<bool> = None;
        list.retain(|s| {
            if s.peer && *hidden.get_or_insert_with(|| gw.marks.emit_hidden(payload)) {
                return !s.tx.is_closed();
            }
            s.tx.send(WsMsg::Text(text.clone())).is_ok()
        });
        if list.is_empty() {
            subs.remove(&(service.to_string(), topic.to_string()));
        }
    }

    pub fn count(&self) -> usize {
        self.subs.lock().unwrap_or_else(|p| p.into_inner()).values().map(Vec::len).sum()
    }
}

struct Pending {
    service: String,
    opened: Option<oneshot::Sender<Result<(), String>>>,
    bridge: Option<oneshot::Sender<WebSocket>>,
    bridge_id: String,
}

#[derive(Default)]
pub struct Sessions {
    pending: Mutex<HashMap<String, Pending>>,
}

impl Sessions {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, Pending>> {
        self.pending.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// The service's `session.opened` answer.
    pub fn opened(&self, service: &str, frame: &Value) {
        let Some(id) = frame.get("session_id").and_then(Value::as_str) else { return };
        let mut map = self.lock();
        let Some(p) = map.get_mut(id).filter(|p| p.service == service) else { return };
        let answer = if frame.get("ok").and_then(Value::as_bool) == Some(true) {
            Ok(())
        } else {
            Err(frame.get("error").and_then(Value::as_str).unwrap_or("the service refused the session").to_string())
        };
        if let Some(tx) = p.opened.take() {
            let _ = tx.send(answer);
        }
    }

    /// Whether `bridge_id` waits for `service`'s bridge socket.
    pub fn expects_bridge(&self, service: &str, bridge_id: &str) -> bool {
        self.lock().values().any(|p| p.service == service && p.bridge_id == bridge_id && p.bridge.is_some())
    }

    pub fn bridge(&self, service: &str, bridge_id: &str, ws: WebSocket) {
        let tx = self
            .lock()
            .values_mut()
            .find(|p| p.service == service && p.bridge_id == bridge_id)
            .and_then(|p| p.bridge.take());
        if let Some(tx) = tx {
            let _ = tx.send(ws);
        }
    }
}

/// Split `/hub/emit/<service>/<topic>` or `/hub/session/<service>/<kind>` into its parts.
fn parts(path: &str) -> Option<(&str, &str, &str)> {
    let rest = path.strip_prefix("/hub/")?;
    let mut it = rest.splitn(3, '/');
    let (what, service, name) = (it.next()?, it.next()?, it.next()?);
    let name = name.split('?').next().unwrap_or(name);
    (!service.is_empty() && !name.is_empty() && !name.contains('/')).then_some((what, service, name))
}

impl Gateway {
    /// The tools `service` answers for: its name, the tools its plugin declares for it, and
    /// the tools it registered.
    fn service_tools(&self, service: &str) -> Vec<String> {
        let mut out = vec![service.to_string()];
        let load = self.read_load();
        out.extend(load.plugins.iter().flat_map(|p| &p.tools).filter(|t| t.services.iter().any(|s| s == service)).map(|t| t.name.clone()));
        out.extend(self.hub.verbs().into_iter().filter(|(s, _)| s == service).map(|(_, v)| v.tool));
        out
    }

    fn knows_service(&self, service: &str) -> bool {
        self.hub.status(service).is_some()
            || self.read_load().plugins.iter().flat_map(|p| &p.services).any(|s| s.name == service)
    }

    /// Open a stream path on this node for `ctx`. Refusals come back before any socket opens.
    pub fn open_local(&self, path: &str, ctx: &CallContext) -> Result<WsPipe, ApiError> {
        let Some((what, service, name)) = parts(path) else { return Err(ApiError::new(404, format!("no stream at {path}"))) };
        // The gateway's own topics, such as `awm/plugins`, have no service behind them.
        let own = what == "emit" && service == crate::lifecycle::SERVICE;
        if own && crate::privacy::from_peer(ctx) && crate::telemetry::is_local_only(name) {
            return Err(ApiError::new(404, format!("no stream at {path}")));
        }
        if !own && (!self.knows_service(service) || self.service_tools(service).iter().any(|t| self.singleton_refuses(t, &ctx.caller))) {
            return Err(ApiError::new(404, format!("Unknown service {service:?}")));
        }
        let peer = crate::privacy::from_peer(ctx);
        match what {
            "emit" => {
                let rx = self.topics.subscribe(service, name, peer);
                // A subscriber sends nothing: its messages go nowhere.
                let (tx, _) = mpsc::unbounded_channel();
                Ok(WsPipe { tx, rx })
            }
            "session" => {
                let declared = self.hub.api_of(service).get("sessions").and_then(Value::as_array).is_some_and(|ss| {
                    ss.iter().any(|s| s.get("kind").and_then(Value::as_str) == Some(name))
                });
                if self.hub.status(service) != Some(crate::hub::Status::Ready) {
                    return Err(ApiError::new(503, format!("service {service:?} is not up")));
                }
                if !declared {
                    return Err(ApiError::new(404, format!("service {service:?} has no session kind {name:?}")));
                }
                let (mine, theirs) = WsPipe::pair();
                let Some(gw) = self.weak().upgrade() else { return Err(ApiError::new(503, "the gateway is stopping")) };
                let (service, kind, ctx) = (service.to_string(), name.to_string(), ctx.clone());
                tokio::spawn(async move {
                    let WsPipe { tx, mut rx } = theirs;
                    match gw.start_session(&service, &kind, &ctx, &mut rx).await {
                        Ok(bridge) => relay(bridge, WsPipe { tx, rx }).await,
                        Err(why) => {
                            tracing::info!("session {service}/{kind} refused: {why}");
                            let _ = tx.send(WsMsg::Close(Some(why)));
                        }
                    }
                });
                Ok(mine)
            }
            _ => Err(ApiError::new(404, format!("no stream at {path}"))),
        }
    }

    /// Read the client's init, ask the service for a session and wait for its bridge socket.
    async fn start_session(
        &self,
        service: &str,
        kind: &str,
        ctx: &CallContext,
        client: &mut mpsc::UnboundedReceiver<WsMsg>,
    ) -> Result<WebSocket, String> {
        let init = match tokio::time::timeout(INIT_TIMEOUT, client.recv()).await {
            Ok(Some(WsMsg::Text(t))) => serde_json::from_str::<Value>(&t).map_err(|_| "the init frame is not JSON".to_string())?,
            Ok(Some(_)) => return Err("the first frame must be the init JSON object".into()),
            _ => return Err("no init frame".into()),
        };
        if self.marks.refuses(ctx, &init) {
            return Err(format!("Unknown session kind {kind:?}"));
        }
        let session_id = uuid::Uuid::new_v4().simple().to_string();
        let bridge_id = uuid::Uuid::new_v4().simple().to_string();
        let (opened_tx, opened_rx) = oneshot::channel();
        let (bridge_tx, bridge_rx) = oneshot::channel();
        self.stream_sessions.lock().insert(
            session_id.clone(),
            Pending { service: service.into(), opened: Some(opened_tx), bridge: Some(bridge_tx), bridge_id: bridge_id.clone() },
        );
        let forget = |gw: &Gateway| {
            gw.stream_sessions.lock().remove(&session_id);
        };
        let mut frame = json!({"kind": "session.open", "session_id": session_id, "session_kind": kind,
                               "bridge_id": bridge_id, "init": init, "caller": ctx.caller});
        if let Some(as_) = &ctx.as_ {
            frame["as"] = json!(as_);
        }
        if !self.hub.send(service, &frame) {
            forget(self);
            return Err(format!("service {service:?} is not connected"));
        }
        let answer = tokio::time::timeout(OPEN_TIMEOUT, opened_rx).await;
        let result = match answer {
            Ok(Ok(Ok(()))) => match tokio::time::timeout(OPEN_TIMEOUT, bridge_rx).await {
                Ok(Ok(ws)) => Ok(ws),
                _ => Err("the service opened no bridge".to_string()),
            },
            Ok(Ok(Err(why))) => Err(why),
            _ => Err("the service did not accept the session in time".into()),
        };
        forget(self);
        result
    }
}

/// Relay an axum WebSocket and a pipe until either ends.
pub async fn relay(ws: WebSocket, pipe: WsPipe) {
    let WsPipe { tx, mut rx } = pipe;
    let (mut sink, mut stream) = ws.split();
    loop {
        tokio::select! {
            out = rx.recv() => {
                let msg = match out {
                    Some(WsMsg::Text(t)) => Message::Text(t.into()),
                    Some(WsMsg::Binary(b)) => Message::Binary(b.into()),
                    Some(WsMsg::Close(reason)) => {
                        let frame = reason.map(|r| CloseFrame { code: 1011, reason: r.into() });
                        let _ = sink.send(Message::Close(frame)).await;
                        break;
                    }
                    None => {
                        let _ = sink.send(Message::Close(None)).await;
                        break;
                    }
                };
                if sink.send(msg).await.is_err() {
                    break;
                }
            }
            inc = stream.next() => {
                let msg = match inc {
                    Some(Ok(Message::Text(t))) => WsMsg::Text(t.to_string()),
                    Some(Ok(Message::Binary(b))) => WsMsg::Binary(b.to_vec()),
                    Some(Ok(Message::Close(f))) => {
                        let _ = tx.send(WsMsg::Close(f.map(|f| f.reason.to_string()).filter(|r| !r.is_empty())));
                        break;
                    }
                    Some(Ok(_)) => continue,
                    Some(Err(_)) | None => {
                        let _ = tx.send(WsMsg::Close(None));
                        break;
                    }
                };
                if tx.send(msg).is_err() {
                    break;
                }
            }
        }
    }
}
