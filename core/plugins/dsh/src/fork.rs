//! The fork's RPC API on loopback: `POST /api/<method>` with `{type: "client-request", rpcId,
//! method, payload}`, answered `result: {ok, value | error{code, message}}`. It has no auth: a
//! call passes from loopback with a JSON content type and no cross-site Origin.

use std::time::Duration;

use serde_json::{json, Value};

/// The fork refused the call, or did not answer it.
#[derive(Debug, Clone, thiserror::Error)]
#[error("{0}")]
pub struct ForkError(pub String);

#[derive(Clone)]
pub struct Fork {
    client: reqwest::Client,
    pub port: u16,
}

fn py(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

impl Fork {
    pub fn new(port: u16) -> Self {
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(30))
            .build()
            .expect("an HTTP client builds");
        Self { client, port }
    }

    pub async fn rpc(&self, method: &str, payload: Value) -> Result<Value, ForkError> {
        let body = json!({"type": "client-request", "rpcId": uuid::Uuid::new_v4().simple().to_string(),
                          "method": method, "payload": payload});
        let url = format!("http://127.0.0.1:{}/api/{method}", self.port);
        let down = |e: &dyn std::fmt::Display| ForkError(format!("the DeepSeek harness did not answer {method}: {e}"));
        let resp = self.client.post(&url).json(&body).send().await.map_err(|e| down(&e))?;
        let reply: Value = resp.json().await.map_err(|e| down(&e))?;
        let result = reply.get("result").filter(|r| !r.is_null()).cloned().unwrap_or_else(|| json!({}));
        if result.get("ok").and_then(Value::as_bool) != Some(true) {
            let err = result.get("error").cloned().unwrap_or_else(|| json!({}));
            return Err(ForkError(format!("{method}: {}: {}", py(err.get("code")), py(err.get("message")))));
        }
        Ok(result.get("value").cloned().unwrap_or(Value::Null))
    }
}

/// An in-process stand-in for the fork's RPC API, for tests.
#[cfg(test)]
pub mod stub {
    use std::sync::{Arc, Mutex};

    use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
    use axum::extract::{Path, State};
    use axum::response::Response;
    use axum::Json;
    use serde_json::{json, Value};
    use tokio::sync::{mpsc, watch};

    #[derive(Default)]
    pub struct Inner {
        pub calls: Vec<(String, Value)>,
        pub items: Vec<Value>,
        pub history: Vec<Value>,
        /// Methods the stub refuses, with `{code, message}`.
        pub refuse: Vec<String>,
        /// One sender per open `events.mux` socket; `None` closes it.
        pub feeds: Vec<mpsc::UnboundedSender<Option<Value>>>,
        /// Set by [`Down::down`]: a call on a connection still open gets no answer.
        pub down: bool,
    }

    #[derive(Clone, Default)]
    pub struct Stub(pub Arc<Mutex<Inner>>);

    impl Stub {
        pub fn calls(&self) -> Vec<(String, Value)> {
            self.0.lock().unwrap().calls.clone()
        }

        pub fn methods(&self) -> Vec<String> {
            self.calls().into_iter().map(|(m, _)| m).collect()
        }

        pub fn last(&self) -> (String, Value) {
            self.calls().pop().expect("a call")
        }

        pub fn with<R>(&self, f: impl FnOnce(&mut Inner) -> R) -> R {
            f(&mut self.0.lock().unwrap())
        }

        /// How many times `method` was called.
        pub fn count(&self, method: &str) -> usize {
            self.calls().iter().filter(|(m, _)| m == method).count()
        }

        /// How many `events.mux` sockets are open.
        pub fn sockets(&self) -> usize {
            self.with(|g| {
                g.feeds.retain(|f| !f.is_closed());
                g.feeds.len()
            })
        }

        /// Push `frame` on every open `events.mux` socket.
        pub fn push(&self, frame: Value) {
            self.with(|g| g.feeds.iter().for_each(|f| _ = f.send(Some(frame.clone()))));
        }

        /// Close every open `events.mux` socket; the fork stays up.
        pub fn hang_up(&self) {
            self.with(|g| g.feeds.drain(..).for_each(|f| _ = f.send(None)));
        }
    }

    async fn mux(State(stub): State<Stub>, ws: WebSocketUpgrade) -> Response {
        ws.on_upgrade(move |socket| feed(stub, socket))
    }

    async fn feed(stub: Stub, mut socket: WebSocket) {
        let (tx, mut rx) = mpsc::unbounded_channel();
        stub.with(|g| g.feeds.push(tx));
        loop {
            tokio::select! {
                frame = rx.recv() => match frame {
                    Some(Some(v)) => if socket.send(Message::Text(v.to_string().into())).await.is_err() { break },
                    _ => break,
                },
                got = socket.recv() => if !matches!(got, Some(Ok(_))) { break },
            }
        }
        let _ = socket.send(Message::Close(None)).await;
    }

    async fn api(State(stub): State<Stub>, Path(method): Path<String>, Json(body): Json<Value>) -> Json<Value> {
        assert_eq!(body["type"], "client-request");
        assert_eq!(body["method"], method);
        let mut g = stub.0.lock().unwrap();
        g.calls.push((method.clone(), body["payload"].clone()));
        if g.down {
            return Json(json!({}));
        }
        if g.refuse.contains(&method) {
            return Json(json!({"result": {"ok": false, "error": {"code": "E_STUB", "message": "refused"}}}));
        }
        let value = match method.as_str() {
            "session.list" => json!({"items": g.items}),
            "session.history" => json!({"events": g.history}),
            _ => json!({"accepted": true}),
        };
        Json(json!({"type": "client-response", "rpcId": body["rpcId"], "result": {"ok": true, "value": value}}))
    }

    /// Serve the stub on an ephemeral loopback port and answer the port.
    pub async fn serve(stub: Stub) -> u16 {
        serve_until(stub).await.0
    }

    /// Takes the stub's fork down: the port stops listening and every socket closes.
    pub struct Down(watch::Sender<bool>, Stub);

    impl Down {
        pub fn down(&self) {
            self.1.with(|g| g.down = true);
            let _ = self.0.send(true);
            self.1.hang_up();
        }
    }

    /// [`serve`], with a handle that takes the fork down.
    pub async fn serve_until(stub: Stub) -> (u16, Down) {
        let app = axum::Router::new()
            .route("/api/events.mux", axum::routing::get(mux))
            .route("/api/{*method}", axum::routing::post(api))
            .with_state(stub.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, mut rx) = watch::channel(false);
        tokio::spawn(async move {
            let stop = async move {
                if rx.wait_for(|down| *down).await.is_err() {
                    std::future::pending::<()>().await;
                }
            };
            axum::serve(listener, app).with_graceful_shutdown(stop).await.unwrap()
        });
        (port, Down(tx, stub))
    }

    /// A loopback port nothing listens on.
    pub async fn dead_port() -> u16 {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        l.local_addr().unwrap().port()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_call_carries_the_envelope_and_a_refusal_names_the_code() {
        let s = stub::Stub::default();
        let fork = Fork::new(stub::serve(s.clone()).await);
        assert_eq!(fork.rpc("session.cancel", json!({"sessionId": "x"})).await.unwrap(), json!({"accepted": true}));
        assert_eq!(s.last(), ("session.cancel".into(), json!({"sessionId": "x"})));
        s.with(|g| g.refuse.push("session.rename".into()));
        let e = fork.rpc("session.rename", json!({})).await.unwrap_err();
        assert_eq!(e.0, "session.rename: E_STUB: refused");
    }

}
