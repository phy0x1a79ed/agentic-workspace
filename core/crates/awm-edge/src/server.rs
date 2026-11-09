//! The serving side: `awm edge-serve`, run over a plain ssh login (fleet.md R92, R93).

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;

use awm_gateway::peer::WsPipe;
use awm_proto::{Caller, Lane, WsMsg};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, Mutex};

use awm_trust::edges::EdgeList;

use crate::frame::{Request, Response, WS_CLOSE, WS_OPEN, WS_SEND};
use crate::transport::{TABLE_CHANGED_FRAME, TOOLS_FRAME};

/// The level an edge login serves at (fleet.md R92).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeLevel {
    /// A user key: a shell login as Tony, so its own `--from` is believed.
    User,
    /// A service key: the forced command in this node's `authorized_keys` names the node.
    Service,
}

/// The node an edge login serves, and at what level.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Login {
    pub from: String,
    pub level: EdgeLevel,
}

/// Resolve an `awm edge-serve` login from its own `--from` and `--level`.
///
/// Under a forced command sshd runs the `authorized_keys` command whatever the client sent, and
/// puts what the client sent in `SSH_ORIGINAL_COMMAND`. The argv is then the forced command, so
/// it is the authority, and `original_command` is never read for a node or a level. A forced
/// command must say `--level service`: R92 forces a command only on a service key. Without a
/// forced command the argv is the client's own, which a user key may send as it likes.
pub fn login(from: Option<&str>, level: Option<&str>, original_command: Option<&str>, edges: &EdgeList, here: &str)
    -> Result<Login, String> {
    let level = match level {
        None | Some("user") => EdgeLevel::User,
        Some("service") => EdgeLevel::Service,
        Some(other) => return Err(format!("--level {other:?} is not user or service")),
    };
    let forced = original_command.is_some_and(|c| !c.trim().is_empty());
    if forced && level != EdgeLevel::Service {
        return Err("a forced command runs a service key, so it must say --level service".into());
    }
    let from = from.map(str::trim).filter(|f| !f.is_empty()).ok_or("--from <node> names the calling node")?;
    if !edges.nodes.contains_key(from) {
        return Err(format!("{from} is not a node in edges.yml"));
    }
    if from == here || !edges.reaches(from, here) {
        return Err(format!("no edge from {from} to {here}"));
    }
    Ok(Login { from: from.to_string(), level })
}

/// What a service-level login may reach: the tools a service key's door serves its node, with
/// every call logged as the door logs it (R92 item 3).
#[derive(Debug, Clone)]
pub struct ServiceGate {
    pub tools: Vec<String>,
    pub log: Option<PathBuf>,
}

impl ServiceGate {
    fn record(&self, from: &str, tool: &str, args: &Value, result: &str) {
        if let Some(log) = &self.log {
            let verb = args.get("verb").and_then(Value::as_str).unwrap_or("");
            awm_trust::port::log_call(log, &format!("awm:{from}:service"), tool, verb, result);
        }
    }

    /// Keep only this gate's tools in a `/hub/table` reply.
    fn filter_table(&self, table: Value) -> Value {
        match table {
            Value::Array(rows) => Value::Array(
                rows.into_iter()
                    .filter(|r| r.get("name").and_then(Value::as_str).is_some_and(|n| self.tools.iter().any(|t| t == n)))
                    .collect(),
            ),
            _ => Value::Array(Vec::new()),
        }
    }
}

async fn write_line<W: AsyncWrite + Unpin>(writer: &Mutex<W>, resp: &Response) {
    let mut out = serde_json::to_vec(resp).expect("json");
    out.push(b'\n');
    let mut w = writer.lock().await;
    let _ = w.write_all(&out).await;
    let _ = w.flush().await;
}

/// Serve requests from `reader` until it closes. Each request runs as `from`, at the trust
/// level the caller's node vouched for: on the edge lane for a user key, and on the service
/// lane, as the door stamps it, when `service` gates the login. `invoke` runs a call;
/// `open_ws` opens a stream path on this node's gateway.
pub async fn serve<R, W, F, Fut, O, OFut>(reader: R, writer: W, from: String, service: Option<ServiceGate>, invoke: F, open_ws: O)
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
    F: Fn(String, Value, Caller) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<Value, String>> + Send + 'static,
    O: Fn(String, Caller) -> OFut + Send + Sync + 'static,
    OFut: Future<Output = Result<WsPipe, String>> + Send + 'static,
{
    let writer = Arc::new(Mutex::new(writer));
    let invoke = Arc::new(invoke);
    let open_ws = Arc::new(open_ws);
    // Each open stream's sender into this node's socket, by stream id.
    let streams: Arc<Mutex<HashMap<u64, mpsc::UnboundedSender<WsMsg>>>> = Arc::default();
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(req) = serde_json::from_str::<Request>(&line) else { continue };
        let lane = if service.is_some() { Lane::Service } else { Lane::Edge };
        let caller = Caller { node: from.clone(), lane, session: req.caller.session, trust: req.caller.trust,
                              scope: req.caller.scope, origin: req.caller.origin.filter(|o| *o != from),
                              job: req.caller.job, name: req.caller.name };
        match req.name.as_str() {
            WS_SEND => {
                if let (Some(tx), Ok(msg)) = (streams.lock().await.get(&req.id), serde_json::from_value::<WsMsg>(req.args)) {
                    let _ = tx.send(msg);
                }
            }
            WS_CLOSE => {
                streams.lock().await.remove(&req.id);
            }
            WS_OPEN if service.is_some() => {
                let path = req.args.get("path").and_then(Value::as_str).unwrap_or_default();
                if let Some(gate) = &service {
                    gate.record(&from, path, &Value::Null, "refused");
                }
                let error = Some("a service key opens no stream".to_string());
                write_line(&writer, &Response { id: req.id, ok: false, result: None, error, more: false }).await;
            }
            WS_OPEN => {
                let (writer, open_ws, streams) = (writer.clone(), open_ws.clone(), streams.clone());
                let path = req.args.get("path").and_then(Value::as_str).unwrap_or_default().to_string();
                let id = req.id;
                // Messages may follow the open before the stream is up: hold them until it is.
                let (early_tx, mut early_rx) = mpsc::unbounded_channel();
                streams.lock().await.insert(id, early_tx);
                tokio::spawn(async move {
                    let pipe = match open_ws(path, caller).await {
                        Ok(p) => p,
                        Err(e) => {
                            streams.lock().await.remove(&id);
                            write_line(&writer, &Response { id, ok: false, result: None, error: Some(e), more: false }).await;
                            return;
                        }
                    };
                    // Only the forwarder holds the sender, so a WS_CLOSE ends the far socket.
                    let WsPipe { tx: into, rx: mut from_far } = pipe;
                    tokio::spawn(async move {
                        while let Some(m) = early_rx.recv().await {
                            if into.send(m).is_err() {
                                break;
                            }
                        }
                    });
                    let mut reason = None;
                    while let Some(msg) = from_far.recv().await {
                        if let WsMsg::Close(r) = msg {
                            reason = r;
                            break;
                        }
                        let result = serde_json::to_value(&msg).ok();
                        write_line(&writer, &Response { id, ok: true, result, error: None, more: true }).await;
                    }
                    streams.lock().await.remove(&id);
                    write_line(&writer, &Response { id, ok: true, result: None, error: reason, more: false }).await;
                });
            }
            _ => {
                let (writer, invoke, gate, from) = (writer.clone(), invoke.clone(), service.clone(), from.clone());
                tokio::spawn(async move {
                    let frame = req.name == TOOLS_FRAME || req.name == TABLE_CHANGED_FRAME;
                    let outcome = match &gate {
                        Some(g) if !frame && !g.tools.contains(&req.name) => {
                            g.record(&from, &req.name, &req.args, "refused");
                            Err(format!("{} not installed", req.name))
                        }
                        _ => {
                            let (name, args) = (req.name.clone(), req.args.clone());
                            let out = invoke(req.name.clone(), req.args, caller).await;
                            match &gate {
                                Some(g) if name == TOOLS_FRAME => out.map(|t| g.filter_table(t)),
                                Some(g) if !frame => {
                                    g.record(&from, &name, &args, if out.is_ok() { "ok" } else { "error" });
                                    out
                                }
                                _ => out,
                            }
                        }
                    };
                    let resp = match outcome {
                        Ok(v) => Response { id: req.id, ok: true, result: Some(v), error: None, more: false },
                        Err(e) => Response { id: req.id, ok: false, result: None, error: Some(e), more: false },
                    };
                    write_line(&writer, &resp).await;
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edges() -> EdgeList {
        EdgeList::parse(
            "nodes:\n  mizar: {role: homebase}\n  alcor: {role: standby}\n  altair: {role: station}\n  lyra: {role: laptop}\n\
             edges:\n  mizar: {alcor: lan}\n  altair: {alcor: {network: tailscale, key: service}}\n",
        )
        .unwrap()
    }

    #[test]
    fn a_user_key_names_its_node_with_from() {
        let got = login(Some("mizar"), None, None, &edges(), "alcor").unwrap();
        assert_eq!(got, Login { from: "mizar".into(), level: EdgeLevel::User });
        assert_eq!(login(Some("mizar"), Some("user"), None, &edges(), "alcor").unwrap().level, EdgeLevel::User);
    }

    #[test]
    fn a_service_key_takes_its_node_and_level_from_the_forced_command() {
        let got = login(Some("altair"), Some("service"), Some("PATH=x awm edge-serve --from altair"), &edges(), "alcor").unwrap();
        assert_eq!(got, Login { from: "altair".into(), level: EdgeLevel::Service });
    }

    #[test]
    fn the_forced_command_overrides_what_the_client_sent() {
        // The client asked for mizar at user level. sshd ran the forced command instead.
        let sent = Some("PATH=\"$HOME/.local/bin:$PATH\" awm edge-serve --from mizar --level user");
        let got = login(Some("altair"), Some("service"), sent, &edges(), "alcor").unwrap();
        assert_eq!(got, Login { from: "altair".into(), level: EdgeLevel::Service });
        let err = login(Some("altair"), None, sent, &edges(), "alcor").unwrap_err();
        assert!(err.contains("--level service"), "a forced command without --level service is refused: {err}");
    }

    #[test]
    fn an_unknown_or_unlinked_from_is_refused() {
        assert!(login(Some("nowhere"), None, None, &edges(), "alcor").unwrap_err().contains("not a node"));
        assert!(login(Some("lyra"), None, None, &edges(), "alcor").unwrap_err().contains("no edge from lyra"));
        assert!(login(Some("alcor"), None, None, &edges(), "mizar").unwrap_err().contains("no edge from alcor"));
        assert!(login(Some("alcor"), None, None, &edges(), "alcor").is_err(), "a node is no edge to itself");
        assert!(login(None, None, None, &edges(), "alcor").unwrap_err().contains("--from"));
        assert!(login(Some(" "), None, None, &edges(), "alcor").is_err());
        assert!(login(Some("mizar"), Some("admin"), None, &edges(), "alcor").unwrap_err().contains("admin"));
    }

    #[test]
    fn a_service_gate_keeps_only_its_tools_in_the_table() {
        let gate = ServiceGate { tools: vec!["2fa".into()], log: None };
        let table = serde_json::json!([{"name": "2fa", "major": 1}, {"name": "journal", "major": 1}]);
        assert_eq!(gate.filter_table(table), serde_json::json!([{"name": "2fa", "major": 1}]));
    }
}
