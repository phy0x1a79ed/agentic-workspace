//! The channel plugin's suites, ported from the Python channel, relay and tether tests.
//!
//! The room tests run a real `tether-relay` on loopback: `AWM_TETHER_RELAY_BIN` names one, else
//! the newest in a local tether checkout's cargo target. Without one they skip. Member nodes
//! reach the relay half through a stand-in for its door, which stamps the calling node.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::push::Outcome;
use base64::Engine;
use serde_json::{json, Value};

use crate::channel::machines::{ControlFn, Machines};
use crate::channel::{lines, Channels, Holder, Parts, RelayCall, Write};
use crate::relay::rooms::Rooms;
use crate::relay::store::Member;
use crate::relay::Relay;
use crate::tether::control::Unavailable;
use crate::tether::paths::{Paths, Role};
use crate::tether::stream::{Mark, SessionLogs, Stream};

const BEARER: &str = "a-test-bearer-nothing-guards";
const NODE: &str = "testnode";

fn holder(c: char, job: &str, name: &str, level: &str) -> Holder {
    Holder { node: NODE.into(), session: format!("cc{}", c.to_string().repeat(32)), job: job.into(), name: name.into(), level: level.into(), isolated: false }
}

fn tester() -> Holder {
    holder('a', "job-aaaa", "tester", "trusted")
}

async fn eventually<F: Fn() -> bool>(what: &str, f: F) {
    for _ in 0..200 {
        if f() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("never happened: {what}");
}

fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.as_f64()),
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(o) => Value::Object(o.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        other => other.clone(),
    }
}

fn env(service: &str) -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env { hub_url: "http://127.0.0.1:1".into(), service: service.into(), plugin: "channel".into(), node: NODE.into(), awm_dir: PathBuf::from("/tmp"), token_file: None }
}

// -- parity ------------------------------------------------------------------

#[tokio::test]
async fn both_manifests_match_the_python_ones() {
    let tmp = tempfile::tempdir().unwrap();
    let node = MemberNode::new(tmp.path(), NODE, Arc::new(|_, _, _| Box::pin(async { Err("no relay".to_string()) })), Captured::default(), Sessions::default());
    let golden: Value = serde_json::from_str(include_str!("../tests/golden/channel.api.json")).unwrap();
    assert_eq!(norm(&crate::channel_service(awm_plugin_sdk::Service::new(env("channel")), node.ch.clone()).manifest()), norm(&golden));

    let relay = Relay::new(crate::relay::store::Store::open(&tmp.path().join("relay.db")).unwrap(), Rooms::new(Some("http://127.0.0.1:1".into()), Some(BEARER.into()), None), 180.0);
    let child = crate::tether::adopted::Adopted::new(Paths::new(Role::Relay, tmp.path()), fake_probe(tmp.path().join("none")), Default::default());
    let golden: Value = serde_json::from_str(include_str!("../tests/golden/relay.api.json")).unwrap();
    assert_eq!(norm(&crate::relay_service(awm_plugin_sdk::Service::new(env("relay")), relay, child).manifest()), norm(&golden));
}

// -- the relay half's door ------------------------------------------------------

#[tokio::test]
async fn the_relay_takes_the_node_from_the_door_and_refuses_a_session() {
    let tmp = tempfile::tempdir().unwrap();
    let relay = Relay::new(crate::relay::store::Store::open(&tmp.path().join("relay.db")).unwrap(), Rooms::new(Some("http://127.0.0.1:1".into()), Some(BEARER.into()), None), 180.0);
    let child = crate::tether::adopted::Adopted::new(Paths::new(Role::Relay, tmp.path()), fake_probe(tmp.path().join("none")), Default::default());
    let svc = crate::relay_service(awm_plugin_sdk::Service::new(env("relay")), relay, child).into_test().await.unwrap();
    let caller = awm_plugin_sdk::Caller { node: "mizar".into(), ..Default::default() };
    let member = json!({"session": "cc11", "name": "s", "level": "trusted"});
    let as_session = svc
        .call_as("status", json!({"member": member, "_caller_trust": "trusted", "_caller_session": {"session_id": "cc11", "node": "mizar"}}), caller.clone(), Some("peer:mizar"))
        .await
        .unwrap_err();
    assert!(as_session.message.contains("through its own node's `channel`"), "{as_session}");
    let untrusted = svc.call_as("status", json!({"member": member, "_caller_trust": "cooperative"}), caller.clone(), Some("peer:mizar")).await.unwrap_err();
    assert!(untrusted.message.contains("only a member node's channel service"));
    let ok = svc.call_as("status", json!({"member": member, "_caller_trust": "trusted"}), caller.clone(), Some("peer:mizar")).await.unwrap();
    assert_eq!(ok, json!({"rooms": []}));
    let half = svc.call_as("issue", json!({"slot": "3", "_caller_trust": "trusted"}), caller, Some("peer:mizar")).await.unwrap_err();
    assert!(half.message.contains("a restore names"));
}

// -- member nodes -------------------------------------------------------------

#[derive(Clone, Default)]
struct Captured {
    lines: Arc<Mutex<Vec<(String, String, String)>>>,
    gone: Arc<Mutex<HashSet<String>>>,
}

impl Captured {
    fn texts(&self) -> Vec<String> {
        self.lines.lock().unwrap().iter().map(|l| l.2.clone()).collect()
    }
    fn any(&self, needle: &str) -> bool {
        self.texts().iter().any(|t| t.contains(needle))
    }
    fn send(&self) -> awm_plugin_sdk::push::SendFn {
        let me = self.clone();
        Arc::new(move |node, job, text| {
            let me = me.clone();
            Box::pin(async move {
                if me.gone.lock().unwrap().contains(&job) {
                    return Outcome::Gone;
                }
                me.lines.lock().unwrap().push((node, job, text));
                Outcome::Delivered
            })
        })
    }
}

/// What `list_other` says runs on a node: None is "cannot tell".
#[derive(Clone, Default)]
struct Sessions(Arc<Mutex<Option<HashSet<String>>>>);

impl Sessions {
    fn of(ids: &[&Holder]) -> Self {
        Self(Arc::new(Mutex::new(Some(ids.iter().map(|h| h.session.clone()).collect()))))
    }
}

fn no_daemon() -> ControlFn {
    Arc::new(|_, _, _| Box::pin(async { Err(Unavailable("no daemon in this test".into())) }))
}

struct MemberNode {
    ch: Arc<Channels>,
    stream: Arc<Stream>,
}

impl MemberNode {
    fn new(dir: &Path, node: &str, relay: RelayCall, pushed: Captured, sessions: Sessions) -> Self {
        Self::with_control(dir, node, relay, pushed, sessions, no_daemon())
    }

    fn with_control(dir: &Path, node: &str, relay: RelayCall, pushed: Captured, sessions: Sessions, control: ControlFn) -> Self {
        let state = dir.join(node);
        let paths = Paths::new(Role::Operator, &state);
        let stream = Stream::new(Mark::load(&paths.stream_state()), SessionLogs::new(paths.sessions_dir()));
        let machines = Arc::new(Machines::new(stream.clone(), None, paths, control));
        let ch = Channels::new(Parts {
            here: node.into(),
            store: crate::channel::store::Store::open(&state.join("services/channel/channels.db")).unwrap(),
            relay,
            machines,
            send: pushed.send(),
            transcripts: lines::Transcripts { root: state.join("services/channel/transcripts") },
            list_sessions: Arc::new(move || {
                let s = sessions.0.lock().unwrap().clone();
                Box::pin(async move { s })
            }),
            feed_wait_s: 1.0,
        });
        ch.resume();
        tokio::spawn(ch.clone().feed_forever());
        Self { ch, stream }
    }
}

impl Drop for MemberNode {
    fn drop(&mut self) {
        self.ch.stop();
    }
}

/// The door stand-in: the relay half's verbs, with the calling node stamped.
fn door(relay: Arc<Mutex<Arc<Relay>>>, node: &str) -> RelayCall {
    let node = node.to_string();
    Arc::new(move |verb, args, _| {
        let (r, node) = (relay.lock().unwrap().clone(), node.clone());
        Box::pin(async move {
            let m = args.get("member").map(|m| {
                let name = crate::channel::text(m, "name");
                let session = crate::channel::text(m, "session");
                Member { who: format!("{}@{node}", if name.is_empty() { &session } else { &name }), node: node.clone(), session, name, level: crate::channel::text(m, "level") }
            });
            let name = crate::channel::text(&args, "name");
            let out = match verb {
                "open" => r.open(&m.unwrap(), args["persistent"] == true).await,
                "join" => r.join(&m.unwrap(), &name).await,
                "write" => r.write(&m.unwrap(), &name, &args["message"]).await,
                "close" => r.close(&m.unwrap(), &name).await,
                "feed" => {
                    let live = args.get("live").and_then(Value::as_array).map(|l| l.iter().map(|s| s.as_str().unwrap().to_string()).collect());
                    let acks = args["acks"].as_array().cloned().unwrap_or_default();
                    Ok(r.feed(&node, live, &acks, args["wait"].as_f64().unwrap_or(0.0)).await)
                }
                other => panic!("the door has no {other}"),
            };
            out.map_err(|e| e.message)
        })
    })
}

fn relay_bin() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("AWM_TETHER_RELAY_BIN") {
        return Some(PathBuf::from(p));
    }
    let projects = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let mut found: Vec<PathBuf> = std::fs::read_dir(projects.join("tether"))
        .ok()?
        .flatten()
        .flat_map(|scope| ["debug", "release"].map(|p| scope.path().join("target").join(p).join("tether-relay")))
        .filter(|p| p.is_file())
        .collect();
    found.sort_by_key(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok());
    found.pop()
}

/// A real tether relay on loopback, and the relay half over it with its state on disk.
struct Host {
    base: String,
    proc: std::process::Child,
    stderr: PathBuf,
    state: PathBuf,
    relay: Arc<Mutex<Arc<Relay>>>,
    lease_s: f64,
}

impl Host {
    fn start(dir: &Path, lease_s: f64) -> Option<Self> {
        let Some(bin) = relay_bin() else {
            eprintln!("skipping: needs a tether-relay binary (AWM_TETHER_RELAY_BIN)");
            return None;
        };
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let stderr = dir.join("relay.stderr");
        let proc = std::process::Command::new(bin)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("AWM_TETHER_ISSUE_TOKEN", BEARER)
            .env("AWM_TETHER_PORT", port.to_string())
            .stdout(std::process::Stdio::null())
            .stderr(std::fs::File::create(&stderr).unwrap())
            .spawn()
            .unwrap();
        let base = format!("http://127.0.0.1:{port}");
        for _ in 0..200 {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let state = dir.join("host");
        let relay = Arc::new(Mutex::new(Self::build(&state, &base, lease_s)));
        Some(Self { base, proc, stderr, state, relay, lease_s })
    }

    fn build(state: &Path, base: &str, lease_s: f64) -> Arc<Relay> {
        let store = crate::relay::store::Store::open(&state.join("services/relay/channels.db")).unwrap();
        let r = Relay::new(store, Rooms::new(Some(base.into()), Some(BEARER.into()), Some(base.into())), lease_s);
        r.resume();
        r
    }

    fn relay(&self) -> Arc<Relay> {
        self.relay.lock().unwrap().clone()
    }

    fn door(&self, node: &str) -> RelayCall {
        door(self.relay.clone(), node)
    }

    /// Stop the relay half and start it again on its state.
    fn restart(&self) {
        self.relay().stop();
        *self.relay.lock().unwrap() = Self::build(&self.state, &self.base, self.lease_s);
    }

    fn files(&self) -> Vec<PathBuf> {
        fn walk(d: &Path, out: &mut Vec<PathBuf>) {
            for e in std::fs::read_dir(d).into_iter().flatten().flatten() {
                if e.path().is_dir() {
                    walk(&e.path(), out);
                } else {
                    out.push(e.path());
                }
            }
        }
        let mut out = vec![];
        walk(&self.state, &mut out);
        out
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        self.relay().stop();
        let _ = self.proc.kill();
        let _ = self.proc.wait();
        let err = std::fs::read_to_string(&self.stderr).unwrap_or_default();
        assert!(!err.contains("secret-body-marker"), "the relay logs no message body, ever");
    }
}

/// An outside agent that knows only a room's link, and reads the rest from it.
struct Guest {
    link: String,
    name: String,
    after: i64,
    http: reqwest::Client,
    host_key: String,
    my_key: String,
    n: i64,
}

impl Guest {
    fn new(link: &str) -> Self {
        Self { link: link.into(), name: "visitor".into(), after: 0, http: reqwest::Client::builder().no_proxy().build().unwrap(), host_key: String::new(), my_key: String::new(), n: 0 }
    }

    fn room(&self) -> String {
        self.link.trim_end_matches('/').rsplit('/').next().unwrap().to_string()
    }

    async fn how_to(&self) -> String {
        self.http.get(&self.link).send().await.unwrap().text().await.unwrap()
    }

    async fn post(&self, fields: Value) -> reqwest::Response {
        let mut body = json!({"from": self.name});
        body.as_object_mut().unwrap().extend(fields.as_object().unwrap().clone());
        self.http.post(format!("{}/send", self.link)).json(&body).send().await.unwrap()
    }

    async fn say(&self, text: &str) -> i64 {
        let r = self.post(json!({"text": text})).await;
        assert!(r.status().is_success(), "{}", r.status());
        r.json::<Value>().await.unwrap()["seq"].as_i64().unwrap()
    }

    /// The host's next messages, or the end of the room. Skips my own.
    async fn hear(&mut self) -> Value {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            let r = self.http.get(format!("{}/recv", self.link)).query(&[("after", self.after.to_string()), ("wait", "2".into())]).send().await.unwrap();
            let mut body: Value = r.json().await.unwrap();
            self.after = body["next"].as_i64().unwrap_or(self.after);
            let host: Vec<Value> = body["messages"].as_array().unwrap().iter().filter(|m| m["by"] == "host").cloned().collect();
            body["messages"] = json!(host);
            let ended = !body["ended"].is_null() && body["ended"] != false;
            if !host.is_empty() || ended || tokio::time::Instant::now() > deadline {
                return body;
            }
        }
    }

    async fn offer(&mut self) -> String {
        loop {
            for m in self.hear().await["messages"].as_array().unwrap() {
                if let Some(s) = m["spake2"].as_str() {
                    return s.to_string();
                }
            }
        }
    }

    /// The how-to's ENCRYPTION steps, then the first sealed message, "ready".
    async fn answer_offer(&mut self, offer_b64: &str, password: &str) {
        use spake2::{Ed25519Group, Identity, Password, Spake2};
        let b64 = base64::engine::general_purpose::STANDARD;
        let id = format!("tether-room:{}", self.room());
        let (s, mine) = Spake2::<Ed25519Group>::start_symmetric(&Password::new(password.as_bytes()), &Identity::new(id.as_bytes()));
        self.post(json!({"spake2": b64.encode(mine)})).await;
        let k = s.finish(&b64.decode(offer_b64).unwrap()).unwrap();
        self.host_key = hex::encode(crate::channel::crypto::derive(&k, &self.room(), "tether-room host to guest"));
        self.my_key = hex::encode(crate::channel::crypto::derive(&k, &self.room(), "tether-room guest to host"));
        self.n = 0;
        self.say_sealed("ready").await;
    }

    async fn say_sealed(&mut self, text: &str) {
        let sealed = crate::channel::crypto::seal(&self.my_key, self.n, &self.room(), text).unwrap();
        let r = self.post(json!({"n": self.n, "sealed": sealed})).await;
        self.n += 1;
        assert!(r.status().is_success());
    }

    fn unseal(&self, m: &Value) -> String {
        crate::channel::crypto::open(&self.host_key, m["n"].as_i64().unwrap(), &self.room(), m["sealed"].as_str().unwrap()).unwrap()
    }

    async fn leave(&self) {
        assert!(self.http.post(format!("{}/leave", self.link)).send().await.unwrap().status().is_success());
    }
}

/// A host and one member node, `testnode`, whose sessions are the tester and one other.
async fn setup(tmp: &Path) -> Option<(Host, MemberNode, Captured, Sessions)> {
    let host = Host::start(tmp, 180.0)?;
    let pushed = Captured::default();
    let sessions = Sessions::of(&[&tester(), &holder('b', "job-bbbb", "someone-else", "cooperative")]);
    let node = MemberNode::new(tmp, NODE, host.door(NODE), pushed.clone(), sessions.clone());
    Some((host, node, pushed, sessions))
}

#[tokio::test]
async fn a_guest_with_only_the_link_talks_and_our_writes_are_not_pushed_back() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((_host, node, pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    assert_eq!((opened["kind"].as_str(), opened["lasts"].as_str(), opened["trust"].as_str()), (Some("room"), Some("task"), Some("untrusted")));
    let name = opened["name"].as_str().unwrap().to_string();
    assert_eq!(name.split('-').count(), 3);
    let mut guest = Guest::new(opened["link"].as_str().unwrap());
    assert!(guest.how_to().await.contains("recv"));
    guest.say("hello from outside").await;
    eventually("the guest's line is pushed", || pushed.any("from visitor: hello from outside")).await;
    let line = pushed.texts().into_iter().find(|t| t.contains("hello from outside")).unwrap();
    assert_eq!(line, format!("room {name} [untrusted] from visitor: hello from outside"));
    assert_eq!(pushed.lines.lock().unwrap()[0].1, "job-aaaa");

    let sent = node.ch.write(&tester(), "and hello back", None, Write { target: "say".into(), ..Write::default() }).await.unwrap();
    assert_eq!(sent["sealed"], false);
    let heard = guest.hear().await;
    assert_eq!(heard["messages"][0]["text"], "and hello back");
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!pushed.any("and hello back"), "our own write is not pushed back");
    let transcript = std::fs::read_to_string(node.ch.transcripts.path(&name)).unwrap();
    assert!(transcript.contains("hello from outside") && transcript.contains("and hello back"));
}

#[tokio::test]
async fn either_side_leaving_ends_the_room_on_the_other() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((_host, node, pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let guest = Guest::new(opened["link"].as_str().unwrap());
    guest.leave().await;
    eventually("the holder hears the room end", || pushed.any("It is closed on our side.")).await;
    let status = node.ch.status(&tester(), None).await.unwrap();
    assert_eq!(status["channels"][0]["open"], false);

    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let mut guest = Guest::new(opened["link"].as_str().unwrap());
    node.ch.close(&tester(), None, None).await.unwrap();
    let heard = guest.hear().await;
    assert!(!heard["ended"].is_null(), "the guest sees our close: {heard}");
}

#[tokio::test]
async fn who_may_hold_a_channel() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((_host, node, _pushed, _)) = setup(tmp.path()).await else { return };
    let untrusted = holder('d', "job-dddd", "u", "untrusted");
    assert!(node.ch.open(&untrusted, "room", None, false).await.unwrap_err().message.contains("only when it is isolated"));
    let isolated = Holder { isolated: true, ..untrusted.clone() };
    assert!(node.ch.open(&isolated, "room", None, false).await.is_ok());
    let elsewhere = Holder { node: "othernode".into(), ..tester() };
    assert!(node.ch.open(&elsewhere, "room", None, false).await.unwrap_err().message.contains("on its own node"));
    assert!(node.ch.open(&tester(), "machine", Some("unix"), true).await.unwrap_err().message.contains("only a room can be persistent"));
    assert!(node.ch.open(&tester(), "tunnel", None, false).await.unwrap_err().message.contains("kind is one of room, machine"));
}

#[tokio::test]
async fn a_room_and_its_unread_messages_survive_a_host_restart() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((host, node, pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let guest = Guest::new(opened["link"].as_str().unwrap());
    host.restart();
    guest.say("after the restart").await;
    eventually("the message arrives", || pushed.any("after the restart")).await;
    assert_eq!(pushed.texts().iter().filter(|t| t.contains("after the restart")).count(), 1);
}

#[tokio::test]
async fn a_member_whose_node_stops_renewing_lapses_and_its_room_ends() {
    let tmp = tempfile::tempdir().unwrap();
    let Some(host) = Host::start(tmp.path(), 0.5) else { return };
    let m = Member { node: "n".into(), session: "cc1".into(), name: "s".into(), level: "trusted".into(), who: "s@n".into() };
    let opened = host.relay().open(&m, false).await.unwrap();
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(host.relay().prune().await, ["cc1@n"]);
    let room = host.relay().store.room(opened["name"].as_str().unwrap()).unwrap();
    assert!(!room.open && room.token.is_empty(), "the host token is forgotten");
}

// -- encryption ----------------------------------------------------------------

#[tokio::test]
async fn a_live_room_moves_to_encryption_and_carries_on() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((host, node, pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let mut guest = Guest::new(opened["link"].as_str().unwrap());
    guest.say("before").await;
    eventually("plain first", || pushed.any("before")).await;
    let offer = node.ch.encrypt(&tester(), None).await.unwrap();
    let pw = offer["password"].as_str().unwrap();
    assert!(pw.len() == 14 && pw.split('-').all(|g| g.len() == 4));
    let theirs = guest.offer().await;
    guest.answer_offer(&theirs, pw).await;
    eventually("encrypted", || pushed.any("is encrypted now")).await;
    assert!(pushed.any("answered the encryption offer"));
    assert!(!pushed.texts().iter().any(|t| t.ends_with(": ready")), "ready is the handshake");

    let w = Write { target: "say".into(), ..Write::default() };
    assert_eq!(node.ch.write(&tester(), "the secret plan", None, w).await.unwrap()["sealed"], true);
    let heard = guest.hear().await;
    let last = heard["messages"].as_array().unwrap().last().unwrap().clone();
    assert!(last.get("text").is_none(), "the relay only ever sees the sealed form");
    assert_eq!(guest.unseal(&last), "the secret plan");
    guest.say_sealed("sealed hello").await;
    eventually("a sealed line", || pushed.texts().iter().any(|t| t.ends_with("from visitor: sealed hello"))).await;
    let status = node.ch.status(&tester(), None).await.unwrap();
    assert_eq!((status["channels"][0]["open"].clone(), status["channels"][0]["encrypted"].clone()), (json!(true), json!(true)));
    let name = opened["name"].as_str().unwrap();
    let transcript = std::fs::read_to_string(node.ch.transcripts.path(name)).unwrap();
    assert!(transcript.contains("the secret plan") && transcript.contains("sealed hello"));
    for path in host.files() {
        let data = std::fs::read(&path).unwrap();
        let text = String::from_utf8_lossy(&data);
        assert!(!text.contains("the secret plan") && !text.contains("sealed hello"), "{}", path.display());
    }
    assert!(node.ch.encrypt(&tester(), None).await.unwrap_err().message.contains("encrypted already"));
}

#[tokio::test]
async fn plaintext_after_the_escalation_and_a_replay_are_refused() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((_host, node, pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let mut guest = Guest::new(opened["link"].as_str().unwrap());
    let offer = node.ch.encrypt(&tester(), None).await.unwrap();
    let theirs = guest.offer().await;
    guest.answer_offer(&theirs, offer["password"].as_str().unwrap()).await;
    eventually("the room is encrypted", || pushed.any("is encrypted now")).await;
    guest.say("a plaintext slip").await;
    eventually("refused", || pushed.any("refused a plaintext message")).await;
    assert!(!pushed.texts().iter().any(|t| t.ends_with(": a plaintext slip")));
    guest.say_sealed("once").await;
    guest.n -= 1;
    guest.say_sealed("once").await;
    guest.say_sealed("after").await;
    eventually("after", || pushed.texts().iter().any(|t| t.ends_with(": after"))).await;
    assert_eq!(pushed.texts().iter().filter(|t| t.ends_with(": once")).count(), 1);
}

#[tokio::test]
async fn the_how_to_states_every_constant_the_guest_and_our_side_use() {
    let tmp = tempfile::tempdir().unwrap();
    let Some((_host, node, _pushed, _)) = setup(tmp.path()).await else { return };
    let opened = node.ch.open(&tester(), "room", None, false).await.unwrap();
    let text = Guest::new(opened["link"].as_str().unwrap()).how_to().await;
    let name = opened["name"].as_str().unwrap();
    for needle in [
        format!("\"tether-room:{name}\""),
        crate::channel::crypto::HOST_TO_GUEST.into(),
        crate::channel::crypto::GUEST_TO_HOST.into(),
        format!("salt \"{name}\""),
        format!("associated data is \"{name}\""),
        "4 zero bytes, then".into(),
        "n as 8 bytes big-endian".into(),
        "Count n from 0".into(),
        "text \"ready\"".into(),
        "spake2 0.4".into(),
        "SPAKE2_Symmetric".into(),
        "HKDF-SHA256".into(),
        "ChaCha20-Poly1305".into(),
        "standard alphabet with padding".into(),
        "KEYS FOR NEXT TIME".into(),
    ] {
        assert!(text.contains(&needle), "{needle}");
    }
}

// -- machines ------------------------------------------------------------------

/// The operator daemon's verbs, answered in memory, with every call recorded.
#[derive(Clone, Default)]
struct FakeDaemon {
    calls: Arc<Mutex<Vec<(String, Value)>>>,
    tasks: Arc<Mutex<Vec<i64>>>,
    gone: Arc<Mutex<bool>>,
    os: Arc<Mutex<Option<String>>>,
}

impl FakeDaemon {
    fn control(&self) -> ControlFn {
        let me = self.clone();
        Arc::new(move |verb, args, _| {
            let me = me.clone();
            Box::pin(async move {
                me.calls.lock().unwrap().push((verb.clone(), args.clone()));
                if *me.gone.lock().unwrap() && verb != "status" {
                    return Ok(json!({"ok": false, "error": "code names no session"}));
                }
                Ok(match verb.as_str() {
                    "invite" => json!({"slot": 4, "code": "4 acre-anchor", "command": "curl -fsSL https://relay.test/tether | sh -s 4 acre-anchor"}),
                    "status" => json!({"sessions": [{"slot": 4, "phase": "open", "owner": {"os": me.os.lock().unwrap().clone()}}]}),
                    "run" | "shell" => {
                        let mut t = me.tasks.lock().unwrap();
                        let id = t.len() as i64 + 1;
                        t.push(id);
                        json!({"ok": true, "task": id})
                    }
                    "tasks" => json!({"tasks": me.tasks.lock().unwrap().iter().map(|t| json!({"task": t})).collect::<Vec<_>>()}),
                    _ => json!({"ok": true}),
                })
            })
        })
    }

    fn last(&self, verb: &str) -> Option<Value> {
        self.calls.lock().unwrap().iter().rev().find(|(v, _)| v == verb).map(|(_, a)| a.clone())
    }
}

fn machine_node(dir: &Path, daemon: &FakeDaemon, pushed: &Captured) -> MemberNode {
    let relay: RelayCall = Arc::new(|_, _, _| Box::pin(async { Err("no relay".to_string()) }));
    let node = MemberNode::with_control(dir, NODE, relay, pushed.clone(), Sessions::of(&[&tester()]), daemon.control());
    node.stream.begin(0x2_0001);
    node
}

fn event(stream: &Stream, seq: i64, body: Value) {
    let mut e = body;
    e["seq"] = json!(seq);
    e["slot"] = json!(4);
    stream.take(Some(0x2_0001), e);
}

/// Open a machine whose mint lands on the stream at `seq`, after the invite answered.
async fn open_at(node: &MemberNode, seq: i64) -> String {
    node.stream.journal.connected(true);
    let stream = node.stream.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(50)).await;
        event(&stream, seq, json!({"type": "session.minted"}));
    });
    node.ch.open(&tester(), "machine", Some("unix"), false).await.unwrap()["name"].as_str().unwrap().to_string()
}

async fn open_machine(node: &MemberNode) -> String {
    open_at(node, 0).await
}

#[tokio::test]
async fn owner_text_is_pushed_and_output_is_only_drained() {
    let tmp = tempfile::tempdir().unwrap();
    let (daemon, pushed) = (FakeDaemon::default(), Captured::default());
    let node = machine_node(tmp.path(), &daemon, &pushed);
    let name = open_machine(&node).await;
    event(&node.stream, 1, json!({"type": "session.phase", "phase": "open"}));
    event(&node.stream, 2, json!({"type": "owner.said", "text": "hi there"}));
    let b64 = base64::engine::general_purpose::STANDARD;
    event(&node.stream, 3, json!({"type": "task.output", "task": 1, "stream": "stdout", "bytes": 3, "data": b64.encode("out")}));
    event(&node.stream, 4, json!({"type": "task.exited", "task": 1, "code": 0}));
    eventually("the owner's line", || pushed.any(&format!("machine {name} [untrusted] from the owner: hi there"))).await;
    assert!(pushed.any("the owner connected."));
    assert!(!pushed.any("out"), "output is never pushed");

    let w = |target: &str| Write { target: target.into(), ..Write::default() };
    assert_eq!(node.ch.write(&tester(), "hello", None, w("say")).await.unwrap()["sent"], true);
    assert_eq!(daemon.last("send").unwrap()["text"], "hello");
    let ran = node.ch.write(&tester(), "ls", None, w("shell")).await.unwrap();
    assert_eq!(ran["task"], 1);
    assert_eq!(daemon.last("run").unwrap(), json!({"code": "4", "command": "ls"}));

    let drained = node.ch.drain(&tester(), None, 0.0, None, None, false).await.unwrap();
    let kinds: Vec<&str> = drained["events"].as_array().unwrap().iter().map(|e| e["type"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["task.output", "task.exited"]);
    assert_eq!(drained["events"][0]["text"], "out");
    assert_eq!(node.ch.drain(&tester(), None, 0.0, None, None, false).await.unwrap()["events"], json!([]), "a drain reads once");
    let transcript = std::fs::read_to_string(node.ch.transcripts.path(&name)).unwrap();
    assert!(transcript.contains("hi there") && !transcript.contains("out\n"));

    let stream = node.stream.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        event(&stream, 5, json!({"type": "task.output", "task": 2, "stream": "stdout", "bytes": 1, "data": b64.encode("z")}));
        tokio::time::sleep(Duration::from_millis(200)).await;
        event(&stream, 6, json!({"type": "task.exited", "task": 2}));
    });
    let waited = node.ch.drain(&tester(), None, 5.0, Some(2), None, false).await.unwrap();
    assert_eq!(waited["events"].as_array().unwrap().last().unwrap()["type"], "task.exited", "until waits for its task");
}

#[tokio::test]
async fn keys_go_to_the_newest_task_and_close_stops_a_task_or_cuts() {
    let tmp = tempfile::tempdir().unwrap();
    let (daemon, pushed) = (FakeDaemon::default(), Captured::default());
    let node = machine_node(tmp.path(), &daemon, &pushed);
    open_machine(&node).await;
    let keys = Write { target: "shell".into(), keys: true, ..Write::default() };
    assert!(node.ch.write(&tester(), "\u{3}", None, keys.clone()).await.unwrap_err().message.contains("no task is open"));
    for cmd in ["a", "b"] {
        node.ch.write(&tester(), cmd, None, Write { target: "shell".into(), ..Write::default() }).await.unwrap();
    }
    assert_eq!(node.ch.write(&tester(), "\u{3}", None, keys).await.unwrap()["task"], 2);
    assert_eq!(daemon.last("keys").unwrap()["data"], base64::engine::general_purpose::STANDARD.encode("\u{3}"));
    assert_eq!(node.ch.close(&tester(), None, Some(1)).await.unwrap()["stopped"], 1);
    assert_eq!(daemon.last("close").unwrap()["task"], 1);
    node.ch.close(&tester(), None, None).await.unwrap();
    assert!(daemon.last("cut").unwrap()["reason"].as_str().unwrap().starts_with("the channel closed"));
}

// -- adoption ------------------------------------------------------------------

use crate::tether::adopted::{Adopted, Launch, Probe, ProbeFn};

/// Answering while the fake binary that wrote `mark` runs, with the live sessions in `mark.sessions`.
fn fake_probe(mark: PathBuf) -> ProbeFn {
    Arc::new(move |_| {
        let mark = mark.clone();
        Box::pin(async move {
            let pid: Option<i32> = std::fs::read_to_string(&mark).ok().and_then(|p| p.trim().parse().ok());
            // SAFETY: kill with signal 0 only checks that the process exists.
            let alive = pid.is_some_and(|p| unsafe { libc::kill(p, 0) } == 0);
            if !alive {
                return (Probe::Absent, json!({}));
            }
            let held: usize = std::fs::read_to_string(mark.with_extension("sessions")).ok().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
            (Probe::Answering, json!({"sessions": vec![json!({"phase": "open"}); held]}))
        })
    })
}

struct Fake {
    _tmp: tempfile::TempDir,
    paths: Paths,
    mark: PathBuf,
}

impl Fake {
    fn new(body: &str) -> Self {
        use std::os::unix::fs::PermissionsExt;
        let tmp = tempfile::tempdir().unwrap();
        let mut paths = Paths::new(Role::Operator, tmp.path());
        paths.bin_override = None;
        paths.bin_dir = paths.state_dir.join("bin");
        std::fs::create_dir_all(&paths.bin_dir).unwrap();
        std::fs::write(paths.binary(), format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(paths.binary(), std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(paths.version_file(), "v1\n").unwrap();
        let mark = tmp.path().join("fake.pid");
        Self { _tmp: tmp, paths, mark }
    }

    fn adopted(&self, issuer: &str) -> Arc<Adopted> {
        let argv = vec![issuer.to_string(), "issue".to_string()];
        let launch = Launch {
            env: vec![("PATH".into(), "/usr/bin:/bin".into()), ("MARK".into(), self.mark.display().to_string()), ("TETHER_ISSUER".into(), serde_json::to_string(&argv).unwrap())],
            secrets: vec![],
            issuer: Some(argv),
            use_manager: false,
        };
        Adopted::new(self.paths.clone(), fake_probe(self.mark.clone()), launch)
    }

    fn pid(&self) -> i32 {
        std::fs::read_to_string(&self.mark).unwrap().trim().parse().unwrap()
    }
}

const RUNS: &str = "echo $$ > \"$MARK\"\ntrap 'exit 0' TERM\nwhile :; do sleep 1; done";

impl Drop for Fake {
    fn drop(&mut self) {
        if let Ok(p) = std::fs::read_to_string(&self.mark) {
            if let Ok(pid) = p.trim().parse::<i32>() {
                // SAFETY: kill takes plain integers.
                unsafe { libc::kill(pid, libc::SIGKILL) };
            }
        }
    }
}

#[tokio::test]
async fn a_stale_binary_waits_for_its_sessions_then_is_replaced() {
    let fake = Fake::new(RUNS);
    assert_eq!(fake.adopted("/bin/channel").reconcile().await["action"], "started");
    let pid = fake.pid();
    std::fs::write(fake.paths.version_file(), "v2\n").unwrap();
    std::fs::write(fake.mark.with_extension("sessions"), "1").unwrap();
    let next = fake.adopted("/bin/channel");
    let out = next.reconcile().await;
    assert_eq!(out["action"], "deferred");
    assert_eq!(out["reason"], "it runs v1 and v2 is installed");
    std::fs::write(fake.mark.with_extension("sessions"), "0").unwrap();
    assert_eq!(next.reconcile().await["action"], "replaced");
    assert_ne!(fake.pid(), pid);
}

#[tokio::test]
async fn a_daemon_with_the_python_issuer_is_replaced_once_idle() {
    let fake = Fake::new(RUNS);
    assert_eq!(fake.adopted("/usr/bin/python").reconcile().await["action"], "started");
    let pid = fake.pid();
    let rust = fake.adopted("/bin/channel");
    let out = rust.reconcile().await;
    assert_eq!(out["action"], "replaced", "{out}");
    assert!(out["reason"].as_str().unwrap().contains("issuer"));
    assert_ne!(fake.pid(), pid);
    assert_eq!(rust.reconcile().await["action"], "none");
}
