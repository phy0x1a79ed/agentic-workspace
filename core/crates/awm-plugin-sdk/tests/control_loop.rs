//! The service loop against a stand-in gateway: register, ready, concurrent calls, cancel,
//! emit and usage frames, and the shutdown that ends `run`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use awm_plugin_sdk::axum::routing::{get, post};
use awm_plugin_sdk::axum::{Json, Router};
use awm_plugin_sdk::{Call, Env, Service, VerbError};
use serde_json::{json, Value};
use tokio::sync::mpsc;

struct DropFlag(Arc<AtomicBool>);

impl Drop for DropFlag {
    fn drop(&mut self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

#[tokio::test]
async fn calls_cancel_and_shutdown() {
    let (to_test, mut from_gw) = mpsc::unbounded_channel::<Value>();
    let (to_gw, from_test) = mpsc::unbounded_channel::<Value>();
    let from_test = Arc::new(tokio::sync::Mutex::new(from_test));
    let app = Router::new()
        .route("/hub/service/register", post(|Json(body): Json<Value>| async move {
            assert_eq!(body["name"], "svc");
            Json(json!({"service_id": "s1", "canonical_workspace": ""}))
        }))
        .route("/hub/service/control/s1", get(move |ws: WebSocketUpgrade| {
            let (to_test, from_test) = (to_test.clone(), from_test.clone());
            async move { ws.on_upgrade(move |socket| relay(socket, to_test, from_test)) }
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { awm_plugin_sdk::axum::serve(listener, app).await.unwrap() });

    let dropped = Arc::new(AtomicBool::new(false));
    let flag = dropped.clone();
    let env = Env { hub_url: url, service: "svc".into(), plugin: "svc".into(), node: "n".into(), awm_dir: "/tmp".into(), token_file: None };
    let svc = Service::new(env)
        .verb("svc", "echo", "", json!([{"name": "text", "required": true}]), |c: Call| async move {
            c.ctx.emit("news", json!({"n": 1}));
            c.ctx.usage("job", true, 3);
            Ok::<_, VerbError>(json!({"text": c.args["text"], "as": c.as_, "node": c.caller.node}))
        })
        .verb("svc", "slow", "", json!([]), move |c: Call| {
            let guard = DropFlag(flag.clone());
            async move {
                let _guard = guard;
                c.cancel.cancelled().await;
                Ok::<_, VerbError>(json!("never"))
            }
        });
    let run = tokio::spawn(svc.run());

    let ready = recv(&mut from_gw).await;
    assert_eq!(ready["kind"], "ready");
    let tools: Vec<&str> = ready["api"]["functions"].as_array().unwrap().iter().filter_map(|f| f["tool"].as_str()).collect();
    assert_eq!(tools, ["svc_echo", "svc_slow"]);

    to_gw.send(json!({"kind": "call", "id": "a", "fn": "slow", "args": {}, "caller": {"node": "n", "lane": "local"}})).unwrap();
    to_gw.send(json!({"kind": "call", "id": "b", "fn": "echo", "args": {"text": "hi"}, "as": "tony", "caller": {"node": "far", "lane": "edge"}})).unwrap();
    let mut seen = vec![];
    for _ in 0..3 {
        seen.push(recv(&mut from_gw).await);
    }
    let kinds: Vec<&str> = seen.iter().filter_map(|f| f["kind"].as_str()).collect();
    assert_eq!(kinds, ["emit", "usage", "reply"], "{seen:?}");
    assert_eq!(seen[2]["id"], "b");
    assert_eq!(seen[2]["result"], json!({"text": "hi", "as": "tony", "node": "far"}));

    to_gw.send(json!({"kind": "call", "id": "c", "fn": "echo", "args": {}, "caller": {}})).unwrap();
    let missing = recv(&mut from_gw).await;
    assert_eq!((missing["id"].as_str(), missing["ok"].as_bool(), missing["error_class"].as_str()), (Some("c"), Some(false), Some("ValueError")));

    to_gw.send(json!({"kind": "cancel", "id": "a"})).unwrap();
    for _ in 0..50 {
        if dropped.load(Ordering::SeqCst) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(dropped.load(Ordering::SeqCst), "a cancelled call's task must be dropped");

    to_gw.send(json!({"kind": "shutdown"})).unwrap();
    tokio::time::timeout(Duration::from_secs(10), run).await.expect("run returns on shutdown").unwrap().unwrap();
    assert!(from_gw.try_recv().map(|f| f["id"] != "a").unwrap_or(true), "a cancelled call is never answered");
}

async fn recv(rx: &mut mpsc::UnboundedReceiver<Value>) -> Value {
    tokio::time::timeout(Duration::from_secs(10), rx.recv()).await.expect("frame in time").expect("socket open")
}

async fn relay(socket: WebSocket, to_test: mpsc::UnboundedSender<Value>, from_test: Arc<tokio::sync::Mutex<mpsc::UnboundedReceiver<Value>>>) {
    use futures_util::{SinkExt, StreamExt};
    let (mut sink, mut stream) = socket.split();
    let mut from_test = from_test.lock().await;
    loop {
        tokio::select! {
            msg = stream.next() => match msg {
                Some(Ok(Message::Text(t))) => { let _ = to_test.send(serde_json::from_str(t.as_str()).unwrap()); }
                Some(Ok(_)) => {}
                _ => break,
            },
            out = from_test.recv() => match out {
                Some(v) => { if sink.send(Message::Text(v.to_string().into())).await.is_err() { break } }
                None => break,
            },
        }
    }
}
