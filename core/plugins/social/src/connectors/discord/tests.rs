//! The REST side against a loopback stand-in for discord.com, and the `/approve` gate.

use std::sync::Mutex as StdMutex;

use axum::extract::State;
use serde_json::{json, Value};

use super::events::tests::{interaction, message_json, user_json};
use super::*;
use crate::connectors::{ChatKind, FileBody, OnCommand};
use crate::db::{Db, History};

type Answer = Arc<dyn Fn(&str, &str) -> Option<(u16, Vec<u8>)> + Send + Sync>;

#[derive(Debug, Clone)]
struct Seen {
    method: String,
    path: String,
    ctype: String,
    body: Vec<u8>,
}

impl Seen {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or(Value::Null)
    }
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
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
    let ctype = req.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    let body = axum::body::to_bytes(req.into_body(), usize::MAX).await.unwrap().to_vec();
    let (status, out) = (s.answer)(&method, &path).or_else(|| base(&method, &path)).unwrap_or((404, br#"{"message": "Unknown", "code": 10000}"#.to_vec()));
    s.seen.lock().unwrap().push(Seen { method, path, ctype, body });
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
    base: String,
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
    Rig { conn, stand, db, commands, base: format!("http://{addr}"), _tmp: tmp }
}

async fn rig() -> Rig {
    rig_with(|_, _| None).await
}

fn text_file(name: &str, text: &str) -> OutFile {
    OutFile { filename: name.into(), body: FileBody::Text(text.into()) }
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

#[tokio::test]
async fn a_dm_never_unfurls_its_links() {
    let r = rig().await;
    r.conn.send("dm:4242", "see https://example.com", None, &[]).await.unwrap();
    r.conn.send("7", "see https://example.com", None, &[]).await.unwrap();
    assert_eq!(r.stand.calls("POST", "channels/555/messages")[0].json()["flags"], json!(4));
    assert_eq!(r.stand.calls("POST", "channels/7/messages")[0].json().get("flags"), None);
}

#[tokio::test]
async fn send_attaches_text_files() {
    let r = rig().await;
    r.conn.send("dm:4242", "here", None, &[text_file("prompt.txt", "x")]).await.unwrap();
    r.conn.send("dm:4242", "bare", None, &[]).await.unwrap();
    let posts = r.stand.calls("POST", "channels/555/messages");
    assert!(posts[0].ctype.starts_with("multipart/form-data"), "{}", posts[0].ctype);
    let body = posts[0].text();
    assert!(body.contains(r#"filename="prompt.txt""#) && body.contains(r#""content":"here""#) && body.contains(r#""flags":4"#), "{body}");
    assert_eq!(posts[1].ctype, "application/json");
    assert_eq!(posts[1].json().get("attachments"), None);
}

#[tokio::test]
async fn send_attaches_a_path() {
    let r = rig().await;
    let plan = r._tmp.path().join("plan.md");
    std::fs::write(&plan, "# Plan\n").unwrap();
    let f = OutFile { filename: "plan.md".into(), body: FileBody::Path(plan) };
    r.conn.send("7", "", None, &[f]).await.unwrap();
    let body = r.stand.calls("POST", "channels/7/messages")[0].text();
    assert!(body.contains(r#"filename="plan.md""#) && body.contains("# Plan\n"), "{body}");
    assert!(r.conn.sends_files() && r.conn.max_file_bytes() == 10 * 1024 * 1024);
}

#[tokio::test]
async fn send_into_a_thread_targets_the_thread() {
    let r = rig().await;
    r.conn.cache().put_channel(&serde_json::from_value(json!({"id": "8", "type": 11, "name": "t", "guild_id": "9"})).unwrap());
    r.conn.send("7", "in thread", Some("8"), &[]).await.unwrap();
    assert_eq!(r.stand.calls("POST", "channels/8/messages").len(), 1);
}

#[tokio::test]
async fn open_dm_accepts_bare_id_and_dm_form() {
    let r = rig().await;
    let ch = r.conn.open_dm("4242").await.unwrap();
    assert_eq!((ch.id.as_str(), ch.kind.as_str(), ch.name.as_str()), ("555", "dm", "tester#0001"));
    assert_eq!(r.conn.open_dm("dm:4242").await.unwrap().id, "555");
    assert_eq!(r.stand.calls("POST", "users/@me/channels").len(), 1);
}

#[tokio::test]
async fn a_bad_channel_id_is_a_value_error() {
    let r = rig().await;
    let e = r.conn.send("general", "x", None, &[]).await.unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("ValueError", "invalid literal for int() with base 10: 'general'"));
}

#[tokio::test]
async fn verbs_before_start_say_so() {
    let r = rig().await;
    r.conn.started.store(false, Ordering::SeqCst);
    assert_eq!(r.conn.send("7", "x", None, &[]).await.unwrap_err().message, "discord[bot] not started");
    assert_eq!(r.conn.identity().await.unwrap_err().message, "discord[bot] not started");
}

// -- seats ----------------------------------------------------------------------

#[tokio::test]
async fn an_operators_dm_is_a_seat() {
    let r = rig().await;
    r.db.add_operator("discord", "4242", "tony").await.unwrap();
    r.db.add_operator("slack", "U1", "tony").await.unwrap();
    let seats = r.conn.list_seats().await.unwrap();
    assert_eq!(seats.iter().map(|c| (c.id.as_str(), c.kind.as_str())).collect::<Vec<_>>(), [("555", "dm")]);
}

#[tokio::test]
async fn a_dm_without_an_operator_is_no_seat() {
    let r = rig().await;
    assert!(r.conn.list_seats().await.unwrap().is_empty());
}

#[tokio::test]
async fn an_unreachable_operator_never_hides_the_rest() {
    let r = rig_with(|m, p| (m == "POST" && p == "users/@me/channels").then(|| (403, br#"{"message": "Cannot send messages to this user", "code": 50007}"#.to_vec()))).await;
    r.conn.cache().put_guild(9, "Lab Server", &[serde_json::from_value(json!({"id": "7", "type": 0, "name": "general", "position": 0})).unwrap()]);
    r.db.add_operator("discord", "4242", "tony").await.unwrap();
    let seats = r.conn.list_seats().await.unwrap();
    assert_eq!(seats, vec![Channel { id: "7".into(), name: "Lab Server#general".into(), kind: "text".into() }]);
}

#[tokio::test]
async fn identity_is_the_ready_user() {
    let r = rig().await;
    assert_eq!(r.conn.identity().await.unwrap(), Identity { id: "1".into(), name: "opal-bot".into() });
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

#[tokio::test]
async fn the_look_back_stops_at_a_thousand_messages() {
    let r = rig_with(|m, p| {
        (m == "GET" && p.starts_with("channels/555/messages")).then(|| {
            let page: Vec<Value> = (0..100).map(|i| message_json((5000 - i) << 22, DM, user_json(ME, "opal-bot", true), "", 0)).collect();
            (200, serde_json::to_vec(&page).unwrap())
        })
    })
    .await;
    r.conn.cache().put_channel(&serde_json::from_value(json!({"id": "555", "type": 1})).unwrap());
    assert!(!r.conn.writable("555").await.unwrap());
    let gets = r.stand.calls("GET", "channels/555/messages");
    assert_eq!(gets.len(), 10, "{:?}", gets.iter().map(|g| &g.path).collect::<Vec<_>>());
    assert!(gets[1].path.contains(&format!("before={}", (5000u64 - 99) << 22)), "{}", gets[1].path);
}

#[tokio::test]
async fn a_channel_is_always_writable() {
    let r = rig().await;
    assert!(r.conn.writable("7").await.unwrap());
    assert!(r.stand.calls("GET", "channels/7/messages").is_empty());
}

// -- fetch, search and downloads ---------------------------------------------------

#[tokio::test]
async fn fetch_returns_oldest_first_with_the_server() {
    let r = rig_with(|m, p| {
        (m == "GET" && p.starts_with("channels/7/messages")).then(|| {
            let page = vec![message_json(3 << 22, 7, user_json(4242, "tester", false), "newer", 0), message_json(2 << 22, 7, user_json(66, "helper", true), "older", 0)];
            (200, serde_json::to_vec(&page).unwrap())
        })
    })
    .await;
    r.conn.cache().put_guild(9, "Lab Server", &[]);
    let got = r.conn.fetch("7", 2, Some("5")).await.unwrap();
    assert_eq!(got.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["older", "newer"]);
    let m = &got[0];
    assert_eq!((m.channel_name.as_str(), m.server.as_str(), m.kind, m.sender_bot, m.to_holder), ("general", "Lab Server", ChatKind::Channel, true, true));
    let q = &r.stand.calls("GET", "channels/7/messages")[0].path;
    assert!(q.contains("before=5") && q.contains("limit=2"), "{q}");
}

#[tokio::test]
async fn fetch_pages_past_a_hundred() {
    let r = rig_with(|m, p| {
        (m == "GET" && p.starts_with("channels/7/messages")).then(|| {
            let n: u64 = if p.contains("before=") { 50 } else { 100 };
            let top: u64 = if p.contains("before=") { 900 } else { 1000 };
            let page: Vec<Value> = (0..n).map(|i| message_json((top - i) << 22, 7, user_json(4242, "tester", false), "", 0)).collect();
            (200, serde_json::to_vec(&page).unwrap())
        })
    })
    .await;
    assert_eq!(r.conn.fetch("7", 150, None).await.unwrap().len(), 150);
    let gets = r.stand.calls("GET", "channels/7/messages");
    assert!(gets[0].path.contains("limit=100") && gets[1].path.contains("limit=50"), "{:?}", gets.iter().map(|g| &g.path).collect::<Vec<_>>());
}

#[tokio::test]
async fn search_is_not_supported() {
    let r = rig().await;
    let e = r.conn.search("x", 10, None).await.unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("NotImplementedError", "discord connector does not support search"));
}

#[tokio::test]
async fn download_refetches_the_message_and_reads_each_file() {
    let addr: Arc<StdMutex<String>> = Default::default();
    let a = addr.clone();
    let r = rig_with(move |m, p| match (m, p) {
        ("GET", "channels/7/messages/42") => {
            let base = a.lock().unwrap().clone();
            let mut v = message_json(42, 7, user_json(4242, "tester", false), "", 0);
            v["attachments"] = json!([
                {"id": "1", "filename": "a.txt", "size": 2, "url": format!("{base}/cdn/a.txt"), "proxy_url": "", "content_type": "text/plain"},
                {"id": "2", "filename": "b.bin", "size": 2, "url": format!("{base}/cdn/b.bin"), "proxy_url": ""},
            ]);
            ok(v)
        }
        ("GET", "/cdn/a.txt") => Some((200, b"aa".to_vec())),
        ("GET", "/cdn/b.bin") => Some((200, b"bb".to_vec())),
        _ => None,
    })
    .await;
    *addr.lock().unwrap() = r.base.clone();
    let all = r.conn.download_attachments("7", "42", None).await.unwrap();
    assert_eq!(all.iter().map(|d| (d.filename.as_str(), d.mime.as_str(), d.data.as_slice())).collect::<Vec<_>>(), [("a.txt", "text/plain", &b"aa"[..]), ("b.bin", "", &b"bb"[..])]);
    let one = r.conn.download_attachments("7", "42", Some(1)).await.unwrap();
    assert_eq!(one.len(), 1);
    assert_eq!(one[0].filename, "b.bin");
}

// -- reactions ------------------------------------------------------------------------

#[tokio::test]
async fn adds_and_removes_a_reaction() {
    let r = rig().await;
    let got = r.conn.react("7", "42", ":eyes:", false).await.unwrap();
    assert_eq!(got, json!({"as": "reaction", "emoji": "👀", "removed": false, "message_id": "42", "channel_id": "7"}));
    r.conn.react("7", "42", "👀", true).await.unwrap();
    assert_eq!(r.stand.calls("PUT", "channels/7/messages/42/reactions/")[0].path, "channels/7/messages/42/reactions/👀/@me");
    assert_eq!(r.stand.calls("DELETE", "channels/7/messages/42/reactions/")[0].path, "channels/7/messages/42/reactions/👀/@me");
}

#[tokio::test]
async fn a_custom_emoji_passes_through() {
    let r = rig().await;
    let got = r.conn.react("7", "42", "<:opal:123>", false).await.unwrap();
    assert_eq!(got["emoji"], json!("<:opal:123>"));
    assert_eq!(r.stand.calls("PUT", "channels/7/messages/42/reactions/")[0].path, "channels/7/messages/42/reactions/opal:123/@me");
}

#[tokio::test]
async fn a_forbidden_reaction_is_a_refusal() {
    let r = rig_with(|m, p| (m == "PUT" && p.contains("/reactions/")).then(|| (403, br#"{"message": "Missing Permissions", "code": 50013}"#.to_vec()))).await;
    let e = r.conn.react("7", "42", "👀", false).await.unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("RuntimeError", "discord[bot] may not react in 7: Missing Permissions"));
    let r = rig_with(|m, p| (m == "PUT" && p.contains("/reactions/")).then(|| (400, br#"{"message": "Unknown Emoji", "code": 10014}"#.to_vec()))).await;
    let e = r.conn.react("7", "42", "nope", false).await.unwrap_err();
    assert_eq!(e.message, "discord[bot] rejected 'nope': Unknown Emoji");
}

#[test]
fn reaction_types_parse_custom_forms() {
    assert!(matches!(reaction_type("👀"), RequestReactionType::Unicode { name: "👀" }));
    for e in ["<:opal:123>", "<a:opal:123>", "opal:123"] {
        assert!(matches!(reaction_type(e), RequestReactionType::Custom { name: Some("opal"), .. }), "{e}");
    }
}

// -- /approve -------------------------------------------------------------------------

#[tokio::test]
async fn the_registration_puts_discord_py_s_payload() {
    let r = rig().await;
    r.conn.register(8).await;
    let puts = r.stand.calls("PUT", "applications/8/commands");
    assert_eq!(puts.len(), 1);
    assert_eq!(puts[0].json(), events::approve_payload());
}

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

#[tokio::test]
async fn a_stranger_is_refused_privately() {
    let r = rig().await;
    r.db.add_operator("discord", "4242", "tony").await.unwrap();
    r.conn.on_interaction(interaction(None, 777, None)).await;
    let ack = r.stand.calls("POST", "interactions/")[0].json();
    assert_eq!(ack, json!({"type": 4, "data": {"content": "You are not an operator of this bot.", "flags": 68}}));
    assert!(r.commands.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_guild_invocation_is_refused_privately() {
    let r = rig().await;
    r.db.add_operator("discord", "4242", "tony").await.unwrap();
    r.conn.on_interaction(interaction(Some(9), 4242, None)).await;
    let ack = r.stand.calls("POST", "interactions/")[0].json();
    assert_eq!(ack, json!({"type": 4, "data": {"content": "DM me to use this command.", "flags": 68}}));
    assert!(r.commands.lock().unwrap().is_empty());
}
