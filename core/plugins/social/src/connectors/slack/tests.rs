//! Both Slack accounts against a loopback stand-in for Slack: the Web API, the Socket Mode
//! websocket, the upload URL and the file host.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::ws::{Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Form, Json, Router};
use futures_util::{FutureExt, SinkExt};
use serde_json::{json, Value};
use tokio::sync::{Notify, Semaphore};

use super::*;
use crate::connectors::{FileBody, OutFile};
use crate::db::{Db, History};

// -- the stand-in --------------------------------------------------------------

#[derive(Default)]
struct St {
    base: String,
    /// Every message in a chat, replies included (a reply carries `thread_ts`).
    history: HashMap<String, Vec<Value>>,
    page: usize,
    convs: Vec<Value>,
    info: HashMap<String, Value>,
    users: HashMap<String, Value>,
    members: Vec<Value>,
    dm_for: HashMap<String, String>,
    errors: HashMap<String, String>,
    matches: Vec<Value>,
    /// When set, any other Bearer token gets `invalid_auth`.
    valid_token: Option<String>,
    calls: Vec<(String, HashMap<String, String>)>,
    auths: Vec<String>,
    cookies: Vec<String>,
    uploads: Vec<(String, Vec<u8>)>,
    acks: Vec<String>,
    push: Vec<Value>,
    second_push: Vec<Value>,
    connects: usize,
    drop_first: Option<Arc<Notify>>,
    hold_users: Option<Arc<Semaphore>>,
}

type Fake = Arc<Mutex<St>>;

fn f64_of(form: &HashMap<String, String>, k: &str) -> Option<f64> {
    form.get(k).and_then(|v| v.parse().ok())
}

fn page(st: &St, key: &str, items: Vec<Value>, form: &HashMap<String, String>) -> Value {
    let start: usize = form.get("cursor").and_then(|c| c.parse().ok()).unwrap_or(0);
    let size = form.get("limit").and_then(|l| l.parse().ok()).unwrap_or(usize::MAX).min(st.page);
    let end = (start + size).min(items.len());
    let mut body = json!({"ok": true, key: items[start.min(end)..end]});
    if end < items.len() {
        body["response_metadata"] = json!({"next_cursor": end.to_string()});
    }
    body
}

fn ts(m: &Value) -> f64 {
    m["ts"].as_str().unwrap_or("0").parse().unwrap_or(0.0)
}

/// The top-level messages of a chat, newest first, as `conversations.history` gives them.
fn top_level(msgs: &[Value], form: &HashMap<String, String>) -> Vec<Value> {
    let inclusive = form.get("inclusive").is_some_and(|v| v == "true");
    let (oldest, latest) = (f64_of(form, "oldest"), f64_of(form, "latest"));
    let mut out = vec![];
    for m in msgs {
        let t = ts(m);
        if m.get("thread_ts").is_some_and(|tt| tt != &m["ts"]) {
            continue;
        }
        if oldest.is_some_and(|o| if inclusive { t < o } else { t <= o }) || latest.is_some_and(|l| if inclusive { t > l } else { t >= l }) {
            continue;
        }
        let replies: Vec<&Value> = msgs.iter().filter(|r| r.get("thread_ts") == Some(&m["ts"]) && r["ts"] != m["ts"]).collect();
        let mut m = m.clone();
        if !replies.is_empty() {
            let latest = replies.iter().max_by(|a, b| ts(a).total_cmp(&ts(b))).unwrap()["ts"].clone();
            m["thread_ts"] = m["ts"].clone();
            m["reply_count"] = json!(replies.len());
            m["latest_reply"] = latest;
        }
        out.push(m);
    }
    out.sort_by(|a, b| ts(b).total_cmp(&ts(a)));
    out
}

async fn api(State(f): State<Fake>, Path(method): Path<String>, headers: HeaderMap, Form(form): Form<HashMap<String, String>>) -> Json<Value> {
    let auth = headers.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    let hold = {
        let mut st = f.lock().unwrap();
        st.calls.push((method.clone(), form.clone()));
        st.auths.push(auth.clone());
        st.cookies.push(headers.get("cookie").and_then(|v| v.to_str().ok()).unwrap_or("").to_string());
        st.hold_users.clone().filter(|_| method == "users.info")
    };
    if let Some(sem) = hold {
        let _ = sem.acquire().await;
    }
    let st = &mut *f.lock().unwrap();
    if let Some(valid) = &st.valid_token {
        if method != "apps.connections.open" && auth != format!("Bearer {valid}") {
            return Json(json!({"ok": false, "error": "invalid_auth"}));
        }
    }
    if let Some(e) = st.errors.get(&method) {
        return Json(json!({"ok": false, "error": e}));
    }
    let ch = form.get("channel").cloned().unwrap_or_default();
    Json(match method.as_str() {
        "auth.test" => json!({"ok": true, "user_id": "UBOT", "bot_id": "BBOT", "user": "tester-bot", "team": "Test Team", "team_id": "T0TEST"}),
        "team.info" => json!({"ok": true, "team": {"name": "Test Lab"}}),
        "apps.connections.open" => {
            assert_eq!(auth, "Bearer xapp-1");
            json!({"ok": true, "url": format!("{}/ws", st.base.replace("http", "ws"))})
        }
        "users.conversations" | "conversations.list" => {
            let convs = st.convs.clone();
            page(st, "channels", convs, &form)
        }
        "conversations.history" => {
            if f64_of(&form, "oldest").is_some_and(|o| o <= 0.0) {
                return Json(json!({"ok": false, "error": "invalid_ts_oldest"}));
            }
            let msgs = top_level(st.history.get(&ch).map(Vec::as_slice).unwrap_or(&[]), &form);
            page(st, "messages", msgs, &form)
        }
        "conversations.replies" => {
            let since = f64_of(&form, "oldest").unwrap_or(0.0);
            let parent = form["ts"].clone();
            let mut thread: Vec<Value> = st.history.get(&ch).into_iter().flatten().filter(|m| m["ts"] == parent || m["thread_ts"] == parent).cloned().collect();
            thread.sort_by(|a, b| ts(a).total_cmp(&ts(b)));
            let mut out: Vec<Value> = thread.iter().take(1).cloned().collect();
            out.extend(thread.iter().skip(1).filter(|m| ts(m) > since).cloned());
            page(st, "messages", out, &form)
        }
        "conversations.open" => {
            let id = st.dm_for.get(&form["users"]).cloned().unwrap_or_else(|| "D1".into());
            json!({"ok": true, "channel": {"id": id}})
        }
        "conversations.info" => {
            let info = st.info.get(&ch).cloned().unwrap_or_else(|| json!({"id": ch, "is_im": true}));
            json!({"ok": true, "channel": info})
        }
        "users.info" => {
            let u = st.users.get(&form["user"]).cloned().unwrap_or_else(|| json!({"id": form["user"], "team_id": "T0TEST", "profile": {"display_name": "tester"}}));
            json!({"ok": true, "user": u})
        }
        "users.list" => {
            let members = st.members.clone();
            page(st, "members", members, &form)
        }
        "chat.postMessage" => {
            json!({"ok": true, "channel": ch, "ts": "300.000001", "message": {"ts": "300.000001", "user": "UBOT", "bot_id": "BBOT", "text": form["text"]}})
        }
        "files.getUploadURLExternal" => json!({"ok": true, "upload_url": format!("{}/upload/F1", st.base), "file_id": "F1"}),
        "files.completeUploadExternal" => {
            let shared: Vec<Value> = serde_json::from_str(&form["files"]).unwrap();
            let files: Vec<Value> = shared.iter().map(|f| json!({"id": f["id"], "title": f["title"], "permalink": "https://p"})).collect();
            json!({"ok": true, "files": files})
        }
        "reactions.add" | "reactions.remove" => json!({"ok": true}),
        "search.messages" => json!({"ok": true, "messages": {"matches": st.matches}}),
        _ => json!({"ok": false, "error": "unknown_method"}),
    })
}

async fn upload(State(f): State<Fake>, Path(id): Path<String>, body: Bytes) -> &'static str {
    f.lock().unwrap().uploads.push((id, body.to_vec()));
    "OK"
}

async fn file(Path(id): Path<String>, headers: HeaderMap) -> impl IntoResponse {
    if headers.get("authorization").is_none() {
        return (StatusCode::FORBIDDEN, Vec::new());
    }
    (StatusCode::OK, format!("bytes-{id}").into_bytes())
}

async fn ws(State(f): State<Fake>, up: WebSocketUpgrade) -> impl IntoResponse {
    up.on_upgrade(move |sock| socket(f, sock))
}

async fn socket(f: Fake, mut sock: WebSocket) {
    let (n, push, drop_first) = {
        let mut st = f.lock().unwrap();
        st.connects += 1;
        let push = if st.connects == 1 { st.push.clone() } else { st.second_push.clone() };
        (st.connects, push, st.drop_first.clone())
    };
    let _ = sock.send(WsMessage::Text(json!({"type": "hello"}).to_string().into())).await;
    for env in push {
        let _ = sock.send(WsMessage::Text(env.to_string().into())).await;
    }
    if let (Some(d), 1) = (drop_first, n) {
        d.notified().await;
        let _ = sock.close().await;
        return;
    }
    while let Some(Ok(frame)) = sock.recv().await {
        if let WsMessage::Text(t) = frame {
            let v: Value = serde_json::from_str(&t).unwrap();
            f.lock().unwrap().acks.push(v["envelope_id"].as_str().unwrap().to_string());
        }
    }
}

async fn fake() -> Fake {
    let f: Fake = Arc::new(Mutex::new(St { page: 200, ..Default::default() }));
    {
        let mut st = f.lock().unwrap();
        st.history.insert("D1".into(), vec![json!({"ts": "100.000001", "user": "U2", "text": "old"})]);
        st.convs = vec![json!({"id": "D1", "is_im": true, "user": "U2"})];
    }
    let app = Router::new()
        .route("/api/{method}", post(api))
        .route("/upload/{id}", post(upload))
        .route("/files/{id}", get(file))
        .route("/ws", get(ws))
        .with_state(f.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    f.lock().unwrap().base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    f
}

fn base(f: &Fake) -> String {
    format!("{}/api", f.lock().unwrap().base)
}

fn methods(f: &Fake) -> Vec<String> {
    f.lock().unwrap().calls.iter().map(|(m, _)| m.clone()).collect()
}

fn call(f: &Fake, method: &str) -> Vec<HashMap<String, String>> {
    f.lock().unwrap().calls.iter().filter(|(m, _)| m == method).map(|(_, p)| p.clone()).collect()
}

fn event(eid: &str, ev: Value) -> Value {
    let mut e = json!({"type": "message", "channel": "D1", "channel_type": "im"});
    e.as_object_mut().unwrap().extend(ev.as_object().unwrap().clone());
    json!({"type": "events_api", "envelope_id": eid, "payload": {"event": e}})
}

async fn until(what: &str, cond: impl Fn() -> bool) {
    for _ in 0..400 {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("condition not met: {what}");
}

type Got = Arc<Mutex<Vec<Inbound>>>;

struct Env {
    _tmp: tempfile::TempDir,
    db: Db,
    hooks: Hooks,
    got: Got,
}

fn env() -> Env {
    let tmp = tempfile::tempdir().unwrap();
    let db = Db::open(&tmp.path().join("social.db")).unwrap();
    let got: Got = Default::default();
    let sink = got.clone();
    let hooks = Hooks {
        on_message: Arc::new(move |m| {
            sink.lock().unwrap().push(m);
            async {}.boxed()
        }),
        on_command: None,
        history: History::new(db.clone(), "bot"),
        db: db.clone(),
    };
    Env { _tmp: tmp, db, hooks, got }
}

fn account(name: &str, token: &str, app: Option<&str>) -> AccountConfig {
    AccountConfig { name: name.into(), platform: "slack".into(), token: token.into(), app_token: app.map(str::to_string), enabled: true, ..Default::default() }
}

fn bot(f: &Fake, e: &Env) -> Arc<SlackConnector> {
    Arc::new(SlackConnector::bot(account("bot", "xoxb-1", Some("xapp-1")), e.hooks.clone(), &base(f)))
}

fn texts(got: &Got) -> Vec<String> {
    got.lock().unwrap().iter().map(|m| m.text.clone()).collect()
}

async fn rows(e: &Env, since: Option<&str>) -> Vec<Value> {
    e.db.history("bot", Some("D1"), since, 1000, false).await.unwrap()
}

fn row_texts(rows: &[Value]) -> Vec<String> {
    rows.iter().map(|r| r["text"].as_str().unwrap().to_string()).collect()
}

fn plan() -> OutFile {
    OutFile { filename: "plan.md".into(), body: FileBody::Text("# Plan\n".into()) }
}

// -- the bot: receive and sync ---------------------------------------------------

#[tokio::test]
async fn receive_sync_and_send() {
    let f = fake().await;
    f.lock().unwrap().push = vec![
        event("e1", json!({"user": "U2", "text": "hi bot", "ts": "200.000001"})),
        event("e2", json!({"user": "UBOT", "bot_id": "BBOT", "text": "my own echo", "ts": "200.000002"})),
        event("e3", json!({"subtype": "message_changed", "message": {"user": "U2", "text": "hi bot (edited)", "ts": "200.000001"}})),
    ];
    let e = env();
    let conn = bot(&f, &e);
    let task = tokio::spawn(conn.clone().start());
    until("three acks", || f.lock().unwrap().acks.len() == 3).await;
    let h = e.hooks.history.clone();
    for _ in 0..200 {
        let r = rows(&e, None).await;
        if h.synced_ts("D1").await.is_some() && r.len() >= 3 && r.iter().any(|r| r["edited"] == true) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(conn.live());
    let sent = conn.send("U2", "hello back", None, &[]).await.unwrap();
    conn.close().await;
    task.await.unwrap().unwrap();

    // Only the other person's message is handed on, never the bot's echo.
    assert_eq!(texts(&e.got), ["hi bot"]);
    let m = e.got.lock().unwrap()[0].clone();
    assert_eq!((m.sender_name.as_str(), m.kind, m.server.as_str(), m.outside, m.resynced, m.to_holder), ("tester", ChatKind::Dm, "", false, false, true));
    assert_eq!(sent["channel_id"], "D1");
    assert_eq!(call(&f, "chat.postMessage")[0]["channel"], "D1");
    let rows = rows(&e, None).await;
    let got: Vec<(String, bool, bool)> = rows.iter().map(|r| (r["text"].as_str().unwrap().into(), r["own"].as_bool().unwrap(), r["edited"].as_bool().unwrap())).collect();
    assert_eq!(
        got,
        [("old".into(), false, false), ("hi bot (edited)".into(), false, true), ("my own echo".into(), true, false), ("hello back".into(), true, false)]
    );
    assert_eq!(row_texts(&e.db.history("bot", None, Some("200.000001"), 100, false).await.unwrap()), ["my own echo", "hello back"]);
}

#[tokio::test]
async fn a_slow_handler_never_delays_the_ack() {
    let f = fake().await;
    let hold = Arc::new(Semaphore::new(0));
    {
        let mut st = f.lock().unwrap();
        st.hold_users = Some(hold.clone());
        st.push = vec![event("e1", json!({"user": "U7", "text": "hello", "ts": "200.000001"}))];
    }
    let e = env();
    let conn = bot(&f, &e);
    let task = tokio::spawn(conn.clone().start());
    until("the ack", || f.lock().unwrap().acks == ["e1"]).await;
    assert!(e.got.lock().unwrap().is_empty(), "handled before users.info answered");
    hold.add_permits(1000);
    until("the message", || e.got.lock().unwrap().len() == 1).await;
    conn.close().await;
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn a_live_event_after_a_reconnect_never_skips_the_gap() {
    let f = fake().await;
    let drop = Arc::new(Notify::new());
    f.lock().unwrap().drop_first = Some(drop.clone());
    let e = env();
    let conn = bot(&f, &e);
    let task = tokio::spawn(conn.clone().start());
    let h = e.hooks.history.clone();
    for _ in 0..200 {
        if f.lock().unwrap().connects == 1 && h.synced_ts("D1").await.as_deref() == Some("100.000001") {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(h.synced_ts("D1").await.as_deref(), Some("100.000001"));
    {
        // While the socket is down: one message nobody pushes, then a live one that the second
        // socket delivers right behind its hello.
        let mut st = f.lock().unwrap();
        let d1 = st.history.get_mut("D1").unwrap();
        d1.push(json!({"ts": "150.000001", "user": "U2", "text": "missed"}));
        d1.push(json!({"ts": "200.000001", "user": "U2", "text": "live"}));
        st.second_push = vec![event("e9", json!({"user": "U2", "text": "live", "ts": "200.000001"}))];
    }
    drop.notify_one();
    until("the second socket's ack", || {
        let st = f.lock().unwrap();
        st.connects == 2 && st.acks == ["e9"]
    })
    .await;
    for _ in 0..200 {
        if h.synced_ts("D1").await.as_deref() == Some("200.000001") {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    until("the gap handed on", || e.got.lock().unwrap().iter().any(|m| m.text == "missed")).await;
    conn.close().await;
    task.await.unwrap().unwrap();
    assert_eq!(h.synced_ts("D1").await.as_deref(), Some("200.000001"));
    assert_eq!(row_texts(&rows(&e, None).await), ["old", "missed", "live"]);
    // The gap reaches on_message as resynced; the plugin dedupes the live copy on message id.
    let got = e.got.lock().unwrap().clone();
    let missed: Vec<&Inbound> = got.iter().filter(|m| m.text == "missed").collect();
    assert!(!missed.is_empty() && missed.iter().all(|m| m.resynced));
    assert!(got.iter().any(|m| m.text == "live" && !m.resynced));
    assert!(!got.iter().any(|m| m.text == "old"));
}

#[tokio::test]
async fn a_first_sight_backfill_only_records_and_a_later_sync_routes_resynced() {
    let f = fake().await;
    let e = env();
    let conn = bot(&f, &e);
    conn.sync().await.unwrap();
    assert_eq!(row_texts(&rows(&e, None).await), ["old"]);
    assert!(e.got.lock().unwrap().is_empty(), "nothing older than the last sync point reaches on_message");

    f.lock().unwrap().history.get_mut("D1").unwrap().push(json!({"ts": "120.000001", "user": "U2", "text": "while away"}));
    f.lock().unwrap().history.get_mut("D1").unwrap().push(json!({"ts": "121.000001", "user": "UBOT", "bot_id": "BBOT", "text": "own"}));
    conn.authenticate().await.unwrap();
    conn.sync().await.unwrap();
    let got = e.got.lock().unwrap().clone();
    assert_eq!(got.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["while away"]);
    assert!(got[0].resynced && got[0].kind == ChatKind::Dm);
}

// -- the bot: verbs --------------------------------------------------------------

#[tokio::test]
async fn the_bot_never_writes_first() {
    let f = fake().await;
    f.lock().unwrap().dm_for.insert("U0NEW".into(), "D2".into());
    let e = env();
    let conn = bot(&f, &e);
    let err = conn.send("U0NEW", "a plan for you", None, &[]).await.unwrap_err();
    assert_eq!(err.error_class, "FirstContactRefused");
    assert!(err.message.contains("never messages a person first"));
    let err = conn.send("D2", "x", None, &[plan()]).await.unwrap_err();
    assert_eq!(err.error_class, "FirstContactRefused");
    assert!(!conn.writable("D2").await.unwrap());
    assert!(!methods(&f).iter().any(|m| m == "chat.postMessage" || m.starts_with("files.")));
    assert!(f.lock().unwrap().uploads.is_empty());

    f.lock().unwrap().info.insert("C1".into(), json!({"id": "C1", "is_channel": true}));
    assert!(conn.writable("C1").await.unwrap(), "a channel is always writable");
}
