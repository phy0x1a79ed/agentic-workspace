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

use super::user::SlackUser;
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
async fn the_sync_pages_and_catches_replies_on_old_threads() {
    let f = fake().await;
    let old = 1_000_000.0;
    let cursor = old + 2.0 * THREAD_WINDOW_S;
    let recent = cursor - 3600.0;
    let t = |x: f64| format!("{x:.6}");
    {
        let mut st = f.lock().unwrap();
        st.page = 2;
        let mut d1 = vec![
            json!({"ts": t(old), "user": "U2", "text": "old parent"}),
            json!({"ts": t(cursor - 60.0), "user": "U2", "text": "old-thread reply seen", "thread_ts": t(old)}),
            json!({"ts": t(recent), "user": "U2", "text": "recent parent"}),
            json!({"ts": t(cursor), "user": "U2", "text": "last seen"}),
            // Everything below arrived while the socket was down.
            json!({"ts": t(cursor + 1.0), "user": "U2", "text": "old-thread reply missed", "thread_ts": t(old)}),
            json!({"ts": t(cursor + 2.0), "user": "U2", "text": "recent-thread reply", "thread_ts": t(recent)}),
        ];
        d1.extend((0..5).map(|i| json!({"ts": t(cursor + 10.0 + i as f64), "user": "U2", "text": format!("new {i}")})));
        st.history.insert("D1".into(), d1);
    }
    let e = env();
    let seen = Inbound { channel_id: "D1".into(), message_id: t(cursor - 60.0), thread_id: t(old), ts: t(cursor - 60.0), text: "old-thread reply seen".into(), ..Default::default() };
    e.hooks.history.record(&seen, false, false).await;
    e.hooks.history.set_synced("D1", &t(cursor), "").await;
    let conn = bot(&f, &e);
    conn.sync().await.unwrap();

    let want = ["old-thread reply missed", "recent-thread reply", "new 0", "new 1", "new 2", "new 3", "new 4"];
    assert_eq!(row_texts(&rows(&e, Some(&t(cursor))).await), want);
    assert_eq!(e.hooks.history.synced_ts("D1").await, Some(t(cursor + 14.0)));
    // Everything after the cursor goes on, oldest first, marked resynced.
    assert_eq!(texts(&e.got), want);
    assert!(e.got.lock().unwrap().iter().all(|m| m.resynced && m.kind == ChatKind::Dm && m.server.is_empty()));
}

#[tokio::test]
async fn a_chat_first_synced_empty_syncs_once_it_has_messages() {
    let f = fake().await;
    let e = env();
    e.hooks.history.set_synced("D1", "0", "").await;
    bot(&f, &e).sync().await.unwrap();
    assert_eq!(row_texts(&rows(&e, None).await), ["old"]);
    assert_eq!(e.hooks.history.synced_ts("D1").await.as_deref(), Some("100.000001"));
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

#[tokio::test]
async fn outside_kind_and_server() {
    let f = fake().await;
    {
        let mut st = f.lock().unwrap();
        st.users.insert("U3".into(), json!({"id": "U3", "team_id": "T0TEST", "is_restricted": true, "name": "guest"}));
        st.users.insert("U4".into(), json!({"id": "U4", "team_id": "T0OTHER", "name": "foreign"}));
        st.users.insert("U6".into(), json!({"id": "U6", "team_id": "T0TEST", "is_ultra_restricted": true, "name": "single"}));
        st.push = vec![
            event("e1", json!({"channel": "C1", "channel_type": "channel", "user": "U3", "text": "a", "ts": "201.0"})),
            event("e2", json!({"channel": "G1", "channel_type": "mpim", "user": "U4", "text": "b", "ts": "202.0"})),
            event("e3", json!({"channel": "D1", "channel_type": "im", "user": "U5", "text": "c", "ts": "203.0"})),
            event("e4", json!({"channel": "C2", "channel_type": "group", "user": "U6", "text": "d", "ts": "204.0"})),
        ];
    }
    let e = env();
    let conn = bot(&f, &e);
    let task = tokio::spawn(conn.clone().start());
    until("four messages", || e.got.lock().unwrap().iter().filter(|m| !m.resynced).count() == 4).await;
    conn.close().await;
    task.await.unwrap().unwrap();
    let got: Vec<(String, bool, ChatKind, String)> =
        e.got.lock().unwrap().iter().filter(|m| !m.resynced).map(|m| (m.text.clone(), m.outside, m.kind, m.server.clone())).collect();
    assert_eq!(
        got,
        [
            ("a".into(), true, ChatKind::Channel, "Test Lab".into()),
            ("b".into(), true, ChatKind::Group, "Test Lab".into()),
            ("c".into(), false, ChatKind::Dm, "".into()),
            ("d".into(), true, ChatKind::Channel, "Test Lab".into()),
        ]
    );
}

#[tokio::test]
async fn message_text_comes_from_blocks_when_text_is_a_placeholder() {
    let f = fake().await;
    f.lock().unwrap().history.insert(
        "C1".into(),
        vec![json!({"ts": "1.1", "user": "U2", "text": "This message contains interactive elements.",
                    "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_preformatted", "elements": [{"type": "text", "text": "a,b"}]}]}]})],
    );
    let e = env();
    let got = bot(&f, &e).fetch("C1", 50, None).await.unwrap();
    assert_eq!(got[0].text, "```\na,b\n```");
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

#[tokio::test]
async fn the_bot_writes_once_the_person_wrote() {
    let f = fake().await;
    let e = env();
    let conn = bot(&f, &e);
    assert!(conn.writable("D1").await.unwrap());
    conn.send("D1", "hello back", None, &[]).await.unwrap();
    let posted = call(&f, "chat.postMessage");
    assert_eq!(posted[0], HashMap::from([("channel".to_string(), "D1".to_string()), ("text".to_string(), "hello back".to_string())]));
    conn.send("D1", "in thread", Some("1.0"), &[]).await.unwrap();
    assert_eq!(call(&f, "chat.postMessage")[1]["thread_ts"], "1.0");
}

#[tokio::test]
async fn the_bot_uploads_in_three_steps() {
    let f = fake().await;
    let e = env();
    let r = bot(&f, &e).send("D1", "the plan", Some("1.0"), &[plan()]).await.unwrap();
    let files: Vec<String> = methods(&f).into_iter().filter(|m| m.starts_with("files.")).collect();
    assert_eq!(files, ["files.getUploadURLExternal", "files.completeUploadExternal"]);
    assert_eq!(call(&f, "files.getUploadURLExternal")[0], HashMap::from([("filename".to_string(), "plan.md".to_string()), ("length".to_string(), "7".to_string())]));
    assert_eq!(f.lock().unwrap().uploads, [("F1".to_string(), b"# Plan\n".to_vec())]);
    let done = &call(&f, "files.completeUploadExternal")[0];
    assert_eq!((done["channel_id"].as_str(), done["initial_comment"].as_str(), done["thread_ts"].as_str()), ("D1", "the plan", "1.0"));
    assert_eq!(r["files"], json!([{"id": "F1", "title": "plan.md", "permalink": "https://p"}]));
    assert_eq!(r["message_id"], "");
}

#[tokio::test]
async fn the_bot_opens_the_dm_for_a_user_id() {
    let f = fake().await;
    {
        let mut st = f.lock().unwrap();
        st.dm_for.insert("U0TEST".into(), "D9".into());
        st.history.insert("D9".into(), vec![json!({"ts": "1.0", "user": "U0TEST", "text": "hi"})]);
    }
    let e = env();
    let r = bot(&f, &e).send("U0TEST", "", None, &[plan()]).await.unwrap();
    assert_eq!(r["channel_id"], "D9");
    let done = &call(&f, "files.completeUploadExternal")[0];
    assert_eq!(done["channel_id"], "D9");
    assert!(!done.contains_key("initial_comment"));
}

#[tokio::test]
async fn the_bot_without_files_write_refuses() {
    let f = fake().await;
    f.lock().unwrap().errors.insert("files.getUploadURLExternal".into(), "missing_scope".into());
    let e = env();
    let err = bot(&f, &e).send("D1", "x", None, &[plan()]).await.unwrap_err();
    assert_eq!(err.error_class, "RuntimeError");
    assert!(err.message.contains("files:write"), "{}", err.message);
    assert!(f.lock().unwrap().uploads.is_empty());
}

#[tokio::test]
async fn the_bot_adds_and_removes_reactions_by_name() {
    let f = fake().await;
    let e = env();
    let conn = bot(&f, &e);
    let r = conn.react("D1", "1.1", "👀", false).await.unwrap();
    assert_eq!(r, json!({"as": "reaction", "emoji": "eyes", "removed": false, "message_id": "1.1", "channel_id": "D1"}));
    conn.react("D1", "1.1", ":eyes:", true).await.unwrap();
    let want = |m: &str| (m.to_string(), HashMap::from([("channel".to_string(), "D1".to_string()), ("timestamp".to_string(), "1.1".to_string()), ("name".to_string(), "eyes".to_string())]));
    assert_eq!(f.lock().unwrap().calls, [want("reactions.add"), want("reactions.remove")]);
}

#[tokio::test]
async fn reaching_the_asked_reaction_state_is_ok_and_other_errors_refuse() {
    for (method, error, remove) in [("reactions.add", "already_reacted", false), ("reactions.remove", "no_reaction", true)] {
        let f = fake().await;
        f.lock().unwrap().errors.insert(method.into(), error.into());
        let e = env();
        assert_eq!(bot(&f, &e).react("D1", "1.1", "eyes", remove).await.unwrap()["as"], "reaction");
    }
    for (error, says) in [("missing_scope", "reactions:write"), ("message_not_found", "message_not_found")] {
        let f = fake().await;
        f.lock().unwrap().errors.insert("reactions.add".into(), error.into());
        let e = env();
        let err = bot(&f, &e).react("D1", "1.1", "eyes", false).await.unwrap_err();
        assert_eq!(err.error_class, "RuntimeError");
        assert!(err.message.contains(says), "{}", err.message);
    }
}

#[tokio::test]
async fn fetch_is_oldest_first_and_download_refetches_the_message() {
    let f = fake().await;
    let url = format!("{}/files/F7", f.lock().unwrap().base);
    f.lock().unwrap().history.insert(
        "C1".into(),
        vec![
            json!({"ts": "1.0", "user": "U2", "text": "first"}),
            json!({"ts": "2.0", "user": "U2", "subtype": "channel_join", "text": "joined"}),
            json!({"ts": "3.0", "user": "U2", "text": "with files", "files": [
                {"id": "F7", "name": "a.txt", "mimetype": "text/plain", "size": 8, "url_private": url},
                {"id": "F8", "mode": "tombstone"},
                {"id": "F9", "title": "b.bin", "url_private_download": url.replace("F7", "F9")}]}),
        ],
    );
    let e = env();
    let conn = bot(&f, &e);
    let got = conn.fetch("C1", 50, None).await.unwrap();
    assert_eq!(got.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["first", "with files"]);
    assert_eq!(got[1].attachments.iter().map(|a| a.filename.as_str()).collect::<Vec<_>>(), ["a.txt", "b.bin"]);
    let older = conn.fetch("C1", 50, Some("3.0")).await.unwrap();
    assert_eq!(older.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["first"]);
    assert_eq!(call(&f, "conversations.history")[1]["latest"], "3.0");

    let all = conn.download_attachments("C1", "3.0", None).await.unwrap();
    assert_eq!(all.iter().map(|d| (d.filename.as_str(), d.data.as_slice())).collect::<Vec<_>>(), [("a.txt", &b"bytes-F7"[..]), ("b.bin", &b"bytes-F9"[..])]);
    let one = conn.download_attachments("C1", "3.0", Some(2)).await.unwrap();
    assert_eq!(one.len(), 1);
    assert_eq!(one[0].filename, "b.bin");
    let err = conn.download_attachments("C1", "9.0", None).await.unwrap_err();
    assert_eq!(err.error_class, "ValueError");
}

#[tokio::test]
async fn the_bot_lists_seats_and_opens_dms_by_name() {
    let f = fake().await;
    {
        let mut st = f.lock().unwrap();
        st.page = 1;
        st.convs = vec![
            json!({"id": "C1", "name": "general", "is_channel": true}),
            json!({"id": "G1", "name": "secret", "is_private": true}),
            json!({"id": "D1", "user": "U2", "is_im": true}),
            json!({"id": "M1", "name": "mpdm-a--b", "is_mpim": true}),
        ];
        st.members = vec![json!({"id": "U1", "name": "someone"}), json!({"id": "U2", "name": "tester", "profile": {"display_name": "Test Er"}})];
        st.dm_for.insert("U2".into(), "D1".into());
    }
    let e = env();
    let conn = bot(&f, &e);
    let seats = conn.list_seats().await.unwrap();
    let kinds: Vec<(&str, &str, &str)> = seats.iter().map(|c| (c.id.as_str(), c.name.as_str(), c.kind.as_str())).collect();
    assert_eq!(kinds, [("C1", "general", "channel"), ("G1", "secret", "private"), ("D1", "tester", "dm"), ("M1", "mpdm-a--b", "group")]);
    assert_eq!(call(&f, "users.conversations")[0]["types"], "public_channel,private_channel,mpim,im");
    conn.list_channels(false).await.unwrap();
    assert_eq!(call(&f, "conversations.list")[0]["types"], "public_channel,private_channel");
    let dm = conn.open_dm("@test er").await.unwrap();
    assert_eq!((dm.id.as_str(), dm.kind.as_str()), ("D1", "dm"));
    let err = conn.open_dm("nobody").await.unwrap_err();
    assert_eq!(err.error_class, "ValueError");
    let me = conn.identity().await.unwrap();
    assert_eq!((me.id.as_str(), me.name.as_str()), ("UBOT", "tester-bot"));
}

#[tokio::test]
async fn the_token_routes_to_the_bot_or_the_read_only_account() {
    let e = env();
    let b = SlackConnector::new(account("opal", "xoxb-1", Some("xapp-1")), e.hooks.clone()).unwrap();
    assert!(b.keeps_history() && b.sends_files() && b.max_file_bytes() == 100 * 1024 * 1024);
    assert_eq!(b.account().app_token.as_deref(), Some("xapp-1"));
    for cfg in [account("me", "xoxp-1", Some("xapp-1")), account("me", "xoxc-1", None), AccountConfig { creds_cmd: Some("true".into()), ..account("me", "", None) }] {
        let u = SlackConnector::new(cfg, e.hooks.clone()).unwrap();
        assert!(!u.keeps_history() && !u.sends_files() && u.platform() == "slack");
    }
    assert!(!SlackUser::new(account("me", "xoxp-1", Some("xapp-1")), e.hooks.clone(), DEFAULT).session);
    assert!(SlackUser::new(account("me", "xoxc-1", None), e.hooks.clone(), DEFAULT).session);
    assert!(SlackUser::new(AccountConfig { creds_cmd: Some("ssh host creds".into()), ..account("me", "", None) }, e.hooks.clone(), DEFAULT).session);
}

const DEFAULT: &str = api::DEFAULT_API;

// -- Tony's account --------------------------------------------------------------

fn own_account(f: &Fake, e: &Env, cfg: AccountConfig) -> Arc<SlackUser> {
    Arc::new(SlackUser::new(cfg, e.hooks.clone(), &base(f)))
}

#[tokio::test]
async fn own_slack_is_read_only() {
    let f = fake().await;
    let e = env();
    for cfg in [account("own-slack", "xoxp-1", Some("xapp-1")), AccountConfig { cookie: Some("xoxd-1".into()), ..account("own-slack", "xoxc-1", None) }] {
        let c = own_account(&f, &e, cfg);
        let err = c.send("D1", "hi", None, &[]).await.unwrap_err();
        assert_eq!(err.error_class, "NotImplementedError");
        assert!(err.message.contains("read only"), "{}", err.message);
        assert_eq!(c.react("D1", "1.1", "eyes", false).await.unwrap_err().error_class, "NotImplementedError");
    }
    assert!(f.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn an_inline_xoxc_rides_with_its_cookie() {
    let f = fake().await;
    let e = env();
    let c = own_account(&f, &e, AccountConfig { cookie: Some("xoxd-ck".into()), ..account("own-slack", "xoxc-tok", None) });
    assert!(c.session);
    c.identity().await.unwrap();
    assert_eq!(f.lock().unwrap().cookies, ["d=xoxd-ck"]);
    assert_eq!(f.lock().unwrap().auths, ["Bearer xoxc-tok"]);
}

#[tokio::test]
async fn creds_cmd_runs_through_sh_and_a_failure_raises() {
    let f = fake().await;
    let e = env();
    let c = own_account(&f, &e, AccountConfig { creds_cmd: Some(r#"printf '{"token":"xoxc-AA","cookie":"xoxd-BB"}'"#.into()), ..account("own-slack", "", None) });
    c.api.fetch_creds().await.unwrap();
    assert_eq!(c.api.token().await, "xoxc-AA");
    c.identity().await.unwrap();
    assert_eq!(f.lock().unwrap().cookies, ["d=xoxd-BB"]);

    let c = own_account(&f, &e, AccountConfig { creds_cmd: Some("echo nope >&2; exit 2".into()), ..account("own-slack", "", None) });
    let err = c.api.fetch_creds().await.unwrap_err();
    assert_eq!(err.error_class, "RuntimeError");
    assert_eq!(err.message, "creds_cmd exited 2: nope");
    let c = own_account(&f, &e, AccountConfig { creds_cmd: Some("echo '{}'".into()), ..account("own-slack", "", None) });
    assert!(c.api.fetch_creds().await.unwrap_err().message.contains("missing token/cookie"));
}

#[tokio::test]
async fn stale_session_creds_are_refetched_once() {
    let f = fake().await;
    f.lock().unwrap().valid_token = Some("xoxc-NEW".into());
    let e = env();
    let cmd = r#"printf '{"token":"xoxc-NEW","cookie":"xoxd-NEW"}'"#;
    let c = own_account(&f, &e, AccountConfig { cookie: Some("xoxd-OLD".into()), creds_cmd: Some(cmd.into()), ..account("own-slack", "xoxc-OLD", None) });
    let me = c.identity().await.unwrap();
    assert_eq!(me.id, "UBOT");
    assert_eq!(f.lock().unwrap().auths, ["Bearer xoxc-OLD", "Bearer xoxc-NEW"]);
    assert_eq!(f.lock().unwrap().cookies.last().unwrap(), "d=xoxd-NEW");

    // With no creds_cmd there is nothing to refresh.
    let c = own_account(&f, &e, AccountConfig { cookie: Some("xoxd-OLD".into()), ..account("own-slack", "xoxc-OLD", None) });
    assert_eq!(c.identity().await.unwrap_err().message, "slack auth.test: invalid_auth");
}

#[tokio::test]
async fn the_session_poll_baselines_then_hands_on_only_new() {
    let f = fake().await;
    f.lock().unwrap().history.insert("D1".into(), vec![json!({"ts": "100.0", "user": "UOTHER", "text": "history"})]);
    let e = env();
    let c = own_account(&f, &e, AccountConfig { cookie: Some("xoxd-1".into()), ..account("own-slack", "xoxc-1", None) });
    *c.self_id.lock().unwrap() = "UME".into();
    let mut baseline = HashMap::new();
    c.poll_once(&mut baseline, true).await.unwrap();
    assert!(e.got.lock().unwrap().is_empty(), "the first poll never replays history");
    assert_eq!(baseline["D1"], "100.0");
    assert_eq!(call(&f, "conversations.history")[0].get("limit").map(String::as_str), Some("1"));

    f.lock().unwrap().history.get_mut("D1").unwrap().extend([
        json!({"ts": "101.0", "user": "UOTHER", "text": "older"}),
        json!({"ts": "102.0", "user": "UOTHER", "text": "hi"}),
        json!({"ts": "103.0", "user": "UME", "text": "my own send"}),
        json!({"ts": "104.0", "subtype": "channel_join", "text": "joined"}),
    ]);
    c.poll_once(&mut baseline, false).await.unwrap();
    assert_eq!(texts(&e.got), ["older", "hi"]);
    assert_eq!(baseline["D1"], "104.0", "advances past the skipped echo and join too");
    let m = e.got.lock().unwrap()[0].clone();
    assert_eq!((m.sender_name.as_str(), m.kind, m.server.as_str(), m.resynced), ("UOTHER", ChatKind::Dm, "", false));
    assert_eq!(call(&f, "conversations.history")[1]["oldest"], "100.0");
}

#[tokio::test]
async fn the_api_mode_socket_hands_on_plain_messages_only() {
    let f = fake().await;
    f.lock().unwrap().push = vec![
        event("e1", json!({"channel": "C1", "channel_type": "channel", "user": "U2", "text": "hi", "ts": "1.0"})),
        event("e2", json!({"subtype": "message_changed", "message": {"user": "U2", "text": "edit", "ts": "1.0"}})),
        event("e3", json!({"user": "UBOT", "text": "own", "ts": "2.0"})),
        json!({"type": "events_api", "envelope_id": "e4", "payload": {"event": {"type": "reaction_added"}}}),
    ];
    let e = env();
    let c = own_account(&f, &e, account("own-slack", "xoxp-1", Some("xapp-1")));
    let task = tokio::spawn(c.clone().start());
    until("four acks", || f.lock().unwrap().acks.len() == 4).await;
    until("the message", || !e.got.lock().unwrap().is_empty()).await;
    c.close().await;
    task.await.unwrap().unwrap();
    let got = e.got.lock().unwrap().clone();
    assert_eq!(got.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["hi"]);
    assert_eq!((got[0].kind, got[0].server.as_str()), (ChatKind::Channel, "Test Lab"));
}

#[tokio::test]
async fn own_channels_dms_and_users() {
    let f = fake().await;
    {
        let mut st = f.lock().unwrap();
        st.page = 1;
        st.convs = vec![
            json!({"id": "C1", "name": "general", "is_channel": true}),
            json!({"id": "G1", "name": "secret", "is_private": true}),
            json!({"id": "D1", "user": "U0PEER", "is_im": true}),
            json!({"id": "M1", "name": "mpdm-a--b", "is_mpim": true}),
        ];
        st.members = vec![json!({"id": "U0ME", "name": "me"}), json!({"id": "U0PEER", "name": "peer", "profile": {"display_name": "Peer P"}}), json!({"id": "U0GONE", "name": "gone", "deleted": true})];
        st.dm_for.insert("U0PEER".into(), "D1".into());
    }
    let e = env();
    let c = own_account(&f, &e, AccountConfig { cookie: Some("x".into()), ..account("own-slack", "xoxc-x", None) });
    f.lock().unwrap().page = 200;
    let chans = c.list_channels(false).await.unwrap();
    assert_eq!(methods(&f), ["conversations.list"]);
    assert_eq!(chans.len(), 4);
    let chans = c.list_channels(true).await.unwrap();
    assert_eq!(methods(&f)[1], "users.conversations");
    let kinds: HashMap<&str, (&str, &str)> = chans.iter().map(|c| (c.id.as_str(), (c.kind.as_str(), c.name.as_str()))).collect();
    assert_eq!(kinds, HashMap::from([("C1", ("channel", "general")), ("G1", ("private", "secret")), ("D1", ("dm", "U0PEER")), ("M1", ("group", "mpdm-a--b"))]));

    f.lock().unwrap().page = 1;
    let dm = c.open_dm("U0PEER").await.unwrap();
    assert_eq!((dm.id.as_str(), dm.kind.as_str(), dm.name.as_str()), ("D1", "dm", "U0PEER"));
    assert!(!methods(&f).iter().any(|m| m == "users.list"), "a bare id needs no lookup");
    let dm = c.open_dm("peer p").await.unwrap();
    assert_eq!(dm.id, "D1");
    assert_eq!(call(&f, "users.list").len(), 2, "paged across both pages");
    assert_eq!(call(&f, "conversations.open").last().unwrap()["users"], "U0PEER");
    let err = c.open_dm("gone").await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("RuntimeError", "no slack user matches 'gone'"));
}

#[tokio::test]
async fn own_search_and_download() {
    let f = fake().await;
    let url = format!("{}/files/F1", f.lock().unwrap().base);
    {
        let mut st = f.lock().unwrap();
        st.matches = vec![json!({"ts": "5.0", "user": "U2", "text": "found it", "channel": {"id": "C1", "name": "general"}, "permalink": "https://p"})];
        st.history.insert("C1".into(), vec![json!({"ts": "5.0", "user": "U2", "text": "x", "files": [{"id": "F1", "title": "t.txt", "mimetype": "text/plain", "url_private": url}]})]);
    }
    let e = env();
    let c = own_account(&f, &e, AccountConfig { cookie: Some("xoxd-1".into()), ..account("own-slack", "xoxc-1", None) });
    let got = c.search("found", 10, Some("general")).await.unwrap();
    assert_eq!((got[0].channel_id.as_str(), got[0].channel_name.as_str(), got[0].text.as_str(), got[0].sender_name.as_str()), ("C1", "general", "found it", "U2"));
    assert_eq!(call(&f, "search.messages")[0], HashMap::from([("query".to_string(), "found in:general".to_string()), ("count".to_string(), "10".to_string())]));

    let d = c.download_attachments("C1", "5.0", None).await.unwrap();
    assert_eq!((d[0].filename.as_str(), d[0].mime.as_str(), d[0].data.as_slice()), ("t.txt", "text/plain", &b"bytes-F1"[..]));
    assert_eq!(f.lock().unwrap().cookies.last().unwrap(), "d=xoxd-1");
    let err = c.download_attachments("C1", "6.0", None).await.unwrap_err();
    assert_eq!(err.message, "slack[own-slack] message '6.0' not found in 'C1'");

    f.lock().unwrap().errors.insert("search.messages".into(), "not_allowed_token_type".into());
    let err = c.search("x", 10, None).await.unwrap_err();
    assert!(err.message.contains("search:read") && err.message.contains("'not_allowed_token_type'"), "{}", err.message);
}
