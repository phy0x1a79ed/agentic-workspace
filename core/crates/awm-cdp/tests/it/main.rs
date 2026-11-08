use awm_cdp::*;
use serde_json::{json, Value};
use std::time::Duration;
use axum::extract::ws::{Message as AxMessage, WebSocketUpgrade};
use axum::routing::get;
use axum::{Json, Router};

/// A fake Chrome: `/json/version` advertises a wrong port, and the socket echoes each call's
/// params back, answers `Fail.me` with an error and sends one event per call.
async fn fake_chrome() -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/json/version", get(|| async { Json(json!({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/abc"})) }))
        .route("/json/protocol", get(|| async { Json(json!({"domains": []})) }))
        .route(
            "/devtools/browser/abc",
            get(|ws: WebSocketUpgrade| async {
                ws.on_upgrade(|mut sock| async move {
                    while let Some(Ok(AxMessage::Text(t))) = sock.recv().await {
                        let m: Value = serde_json::from_str(&t).unwrap();
                        let _ = sock.send(AxMessage::Text(json!({"method": "Evt.fired", "params": {}}).to_string().into())).await;
                        let reply = if m["method"] == "Fail.me" {
                            json!({"id": m["id"], "error": {"message": "nope"}})
                        } else if m["method"] == "Close.now" {
                            break;
                        } else {
                            json!({"id": m["id"], "result": {"echo": m["params"], "session": m.get("sessionId")}})
                        };
                        let _ = sock.send(AxMessage::Text(reply.to_string().into())).await;
                    }
                })
            }),
        );
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}")
}

#[tokio::test]
async fn calls_errors_sessions_and_events() {
    assert_eq!(rewrite_ws("http://172.25.176.1:19222/", "ws://localhost/devtools/browser/y"), "ws://172.25.176.1:19222/devtools/browser/y");
    assert_eq!(page_ws("http://127.0.0.1:5", "T1"), "ws://127.0.0.1:5/devtools/page/T1");
    let base = fake_chrome().await;
    wait_ready(&base, Duration::from_secs(5)).await.unwrap();
    assert_eq!(protocol(&base).await.unwrap()["domains"], json!([]));
    let (conn, mut events) = Connection::connect_with_events(&browser_ws(&base).await.unwrap()).await.unwrap();
    let r = conn.send("Page.navigate", json!({"url": "about:blank"}), Some("S1")).await.unwrap();
    assert_eq!(r["echo"]["url"], "about:blank");
    assert_eq!(r["session"], "S1");
    match conn.send("Fail.me", Value::Null, None).await {
        Err(CdpError::Method { message, .. }) => assert_eq!(message, "nope"),
        other => panic!("{other:?}"),
    }
    assert_eq!(events.recv().await.unwrap()["method"], "Evt.fired");
}

#[tokio::test]
async fn browser_redials_after_the_socket_closes() {
    let base = fake_chrome().await;
    let b = Browser::new(&base);
    b.send("A.b", json!({}), None).await.unwrap();
    assert_eq!(b.generation(), 1);
    assert!(matches!(b.send("Close.now", json!({}), None).await, Err(CdpError::Closed)));
    for _ in 0..50 {
        if b.connection().await.unwrap().send("A.b", json!({}), None).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(b.generation(), 2);
}
