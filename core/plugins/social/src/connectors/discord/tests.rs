//! The REST side against a loopback stand-in for discord.com, and the `/approve` gate.

use std::sync::Mutex as StdMutex;

use axum::extract::State;
use serde_json::{json, Value};

use super::events::tests::{interaction, message_json, user_json};
use super::*;
use crate::connectors::{OnCommand};
use crate::db::{Db, History};

type Answer = Arc<dyn Fn(&str, &str) -> Option<(u16, Vec<u8>)> + Send + Sync>;

#[derive(Debug, Clone)]
struct Seen {
    method: String,
    path: String,
    body: Vec<u8>,
}

impl Seen {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or(Value::Null)
    }
}

#[derive(Clone)]
struct Stand {
    seen: Arc<StdMutex<Vec<Seen>>>,
    answer: Answer,
}

impl Stand {
    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
    fn calls(&self, method: &str, prefix: &str) -> Vec<Seen> {
        self.seen().into_iter().filter(|s| s.method == method && s.path.starts_with(prefix)).collect()
    }
}

async fn handle(State(s): State<Stand>, req: axum::extract::Request) -> axum::response::Response {
    let method = req.method().to_string();
    let uri = req.uri().to_string();
    let path = urlencoding::decode(&uri).unwrap().into_owned();
    let path = path.strip_prefix("/api/v10/").unwrap_or(&path).to_string();
    let body = axum::body::to_bytes(req.into_body(), usize::MAX).await.unwrap().to_vec();
    let (status, out) = (s.answer)(&method, &path).or_else(|| base(&method, &path)).unwrap_or((404, br#"{"message": "Unknown", "code": 10000}"#.to_vec()));
    s.seen.lock().unwrap().push(Seen { method, path, body });
    axum::response::Response::builder().status(status).header("content-type", "application/json").body(axum::body::Body::from(out)).unwrap()
}

fn ok(v: Value) -> Option<(u16, Vec<u8>)> {
    Some((200, serde_json::to_vec(&v).unwrap()))
}

const ME: u64 = 1;
const DM: u64 = 555;
const SENT: u64 = 77 << 22;

/// What discord.com answers unless a test says otherwise: the DM with user 4242 is channel 555,
/// where 4242 has written, and channel 7 is a guild text channel.
fn base(method: &str, path: &str) -> Option<(u16, Vec<u8>)> {
    let dm = json!({"id": DM.to_string(), "type": 1, "recipients": [{"id": "4242", "username": "tester", "discriminator": "0001", "avatar": null}]});
    match (method, path) {
        ("POST", "users/@me/channels") => ok(dm),
        ("GET", p) if p.starts_with("channels/555/messages") => ok(json!([message_json(9 << 22, DM, user_json(4242, "tester", false), "hi", 0)])),
        ("POST", p) if p.starts_with("channels/") && p.ends_with("/messages") => {
            let ch: u64 = p.split('/').nth(1).unwrap().parse().unwrap();
            ok(message_json(SENT, ch, user_json(ME, "opal-bot", true), "", 0))
        }
        ("GET", "channels/7") => ok(json!({"id": "7", "type": 0, "name": "general", "guild_id": "9", "position": 0})),
        ("PUT" | "DELETE", p) if p.contains("/reactions/") => Some((204, vec![])),
        ("POST", p) if p.starts_with("interactions/") => Some((204, vec![])),
        ("PUT", p) if p.starts_with("applications/") => ok(json!([])),
        _ => None,
    }
}

struct Rig {
    conn: Arc<DiscordConnector>,
    stand: Stand,
    db: Db,
    commands: Arc<StdMutex<Vec<Command>>>,
    _tmp: tempfile::TempDir,
}

async fn rig_with(answer: impl Fn(&str, &str) -> Option<(u16, Vec<u8>)> + Send + Sync + 'static) -> Rig {
    let stand = Stand { seen: Default::default(), answer: Arc::new(answer) };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = axum::Router::new().fallback(handle).with_state(stand.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });

    let tmp = tempfile::tempdir().unwrap();
    let db = Db::open(&tmp.path().join("social.db")).unwrap();
    let commands: Arc<StdMutex<Vec<Command>>> = Default::default();
    let cap = commands.clone();
    let on_command: OnCommand = Arc::new(move |c| {
        let cap = cap.clone();
        Box::pin(async move { cap.lock().unwrap().push(c) })
    });
    let hooks = Hooks { on_message: Arc::new(|_| Box::pin(async {})), on_command: Some(on_command), history: History::new(db.clone(), "bot"), db: db.clone() };
    let cfg = AccountConfig { name: "bot".into(), platform: "discord".into(), token: "t".into(), enabled: true, ..Default::default() };
    let conn = Arc::new(DiscordConnector::build(cfg, hooks, Some(&format!("http://{addr}")), None, false).unwrap());
    conn.started.store(true, Ordering::SeqCst);
    conn.mark_ready();
    conn.cache().me = Some((ME, "opal-bot".into()));
    Rig { conn, stand, db, commands, _tmp: tmp }
}

async fn rig() -> Rig {
    rig_with(|_, _| None).await
}

// -- the dm: form, send and open_dm -------------------------------------------

#[tokio::test]
async fn send_to_dm_opens_it_once() {
    let r = rig().await;
    let first = r.conn.send("dm:4242", "one", None, &[]).await.unwrap();
    r.conn.send("dm:4242", "two", None, &[]).await.unwrap();
    assert_eq!(first.get("channel_id"), Some(&json!("555")));
    assert_eq!(first.get("message_id"), Some(&json!(SENT.to_string())));
    assert_eq!(first.get("ts"), Some(&json!(events::snowflake_iso(SENT))));
    assert_eq!(r.stand.calls("POST", "users/@me/channels").len(), 1);
    assert_eq!(r.stand.calls("POST", "users/@me/channels")[0].json(), json!({"recipient_id": "4242"}));
    let sent: Vec<Value> = r.stand.calls("POST", "channels/555/messages").iter().map(|s| s.json()["content"].clone()).collect();
    assert_eq!(sent, [json!("one"), json!("two")]);
    // The first-contact look-back ran once and was cached.
    assert_eq!(r.stand.calls("GET", "channels/555/messages").len(), 1);
}

// -- seats ----------------------------------------------------------------------

#[tokio::test]
async fn a_new_session_fetches_what_each_seat_missed_since_its_cursor() {
    let r = rig_with(|m, p| {
        (m == "GET" && p.starts_with("channels/555/messages")).then(|| {
            let person = || user_json(4242, "tester", false);
            let page = vec![message_json(13 << 22, 555, user_json(ME, "opal-bot", true), "mine", 0), message_json(12 << 22, 555, person(), "second", 0), message_json(11 << 22, 555, person(), "first", 0)];
            (200, serde_json::to_vec(&page).unwrap())
        })
    })
    .await;
    r.db.mount(crate::db::Mount { account: "bot".into(), channel_id: "555".into(), channel_name: "".into(), node: "n".into(), session_id: "j".into(), session_name: "".into(), mounted_at: "".into() }).await.unwrap();
    let (tx, mut rx) = mpsc::unbounded_channel();

    r.conn.catch_up(&tx).await;
    assert!(rx.try_recv().is_err(), "the first sight only sets the cursor");
    assert_eq!(r.db.cursor("bot", "555").await.unwrap(), (13u64 << 22).to_string());

    r.db.run(|c| c.execute("DELETE FROM seat_cursor", [])).await.unwrap();
    r.db.set_cursor("bot", "555", &(10u64 << 22).to_string()).await;
    r.conn.catch_up(&tx).await;
    let (a, b) = (rx.try_recv().unwrap(), rx.try_recv().unwrap());
    assert_eq!((a.text.as_str(), b.text.as_str(), a.resynced && b.resynced), ("first", "second", true), "oldest first, and no copy of our own");
    assert!(rx.try_recv().is_err());
    assert_eq!(r.db.cursor("bot", "555").await.unwrap(), (13u64 << 22).to_string());
}

// -- first contact ----------------------------------------------------------------

#[tokio::test]
async fn a_dm_nobody_else_wrote_in_is_refused() {
    let r = rig_with(|m, p| {
        (m == "GET" && p.starts_with("channels/555/messages")).then(|| {
            let page = vec![message_json(9 << 22, DM, user_json(ME, "opal-bot", true), "notice", 0), message_json(8 << 22, DM, user_json(66, "other-bot", true), "x", 0)];
            (200, serde_json::to_vec(&page).unwrap())
        })
    })
    .await;
    assert!(!r.conn.writable("dm:4242").await.unwrap());
    let e = r.conn.send("dm:4242", "hello", None, &[]).await.unwrap_err();
    assert_eq!(e.error_class, "PermissionError");
    assert_eq!(e.message, "discord 555: nobody has written to Opal here yet, and Opal never messages a person first");
    assert!(r.stand.calls("POST", "channels/555/messages").is_empty());
    // A message from the person on the live feed opens the DM with no look-back.
    let before = r.stand.calls("GET", "channels/555/messages").len();
    let m: Message = serde_json::from_value(message_json(10 << 22, DM, user_json(4242, "tester", false), "hi", 0)).unwrap();
    assert!(r.conn.cache().message("bot", &m).is_some());
    assert!(r.conn.writable("dm:4242").await.unwrap());
    r.conn.send("dm:4242", "hello", None, &[]).await.unwrap();
    assert_eq!(r.stand.calls("GET", "channels/555/messages").len(), before);
}

// -- fetch, search and downloads ---------------------------------------------------

// -- reactions ------------------------------------------------------------------------

// -- /approve -------------------------------------------------------------------------

#[tokio::test]
async fn an_operator_gets_the_command_after_the_ack() {
    let r = rig().await;
    r.db.add_operator("discord", "4242", "tony").await.unwrap();
    r.conn.on_interaction(interaction(None, 4242, None)).await;
    let acks = r.stand.calls("POST", "interactions/31337/itoken/callback");
    assert_eq!(acks.len(), 1);
    assert_eq!(acks[0].json(), json!({"type": 4, "data": {"content": "⏳ Arming `cwl` Duo approvals for 5 min…", "flags": 4}}));
    let cmds = r.commands.lock().unwrap().clone();
    let mut options = serde_json::Map::new();
    options.insert("device".into(), json!("cwl"));
    assert_eq!(
        cmds,
        vec![Command {
            account: "bot".into(),
            platform: "discord".into(),
            name: "approve".into(),
            options,
            user_id: "4242".into(),
            user_name: "tester".into(),
            channel_id: "999".into(),
            is_dm: true,
            operator: "tony".into(),
        }]
    );
    r.conn.on_interaction(interaction(None, 4242, Some("alliance"))).await;
    assert_eq!(r.commands.lock().unwrap()[1].options.get("device"), Some(&json!("alliance")));
}
