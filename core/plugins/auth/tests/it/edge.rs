//! The edge end to end: a real `Edge` and `Front` on loopback in plain HTTP, in front of a scratch
//! upstream that records what reaches it.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use awm_plugin_auth::edge::{self, Edge, EdgeConfig};
use awm_plugin_auth::front::{Front, Upstreams};
use awm_plugin_auth::fronts::{self, Base, Fronts};
use awm_plugin_auth::gate::{AuthGate, Material, MaterialSource, Used};
use awm_plugin_auth::landing::Landing;
use awm_plugin_auth::policy::board;
use awm_plugin_auth::portal::{self, Grants};
use awm_plugin_auth::tokens;
use awm_plugin_sdk::Hub;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver};

const SECRET: &str = "edge-test-secret";
const CA: &str = "-----BEGIN CERTIFICATE-----\ntest-ca\n";
const CARD: &str = "0123456789abcdef0123456789abcdef";

struct Fixed;

impl MaterialSource for Fixed {
    fn fetch(&self, _: &Used) -> anyhow::Result<Material> {
        Ok(Material { secret: SECRET.into(), device_tokens: vec![], session_ttl_seconds: 3600.0, max_session_seconds: 0.0 })
    }
}

/// What the scratch upstream received.
struct Seen {
    target: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Seen {
    fn header(&self, name: &str) -> Vec<&str> {
        self.headers.iter().filter(|(k, _)| k == name).map(|(_, v)| v.as_str()).collect()
    }
}

/// An upstream that records each request, answers 200, and echoes a WebSocket upgrade.
async fn upstream() -> (SocketAddr, UnboundedReceiver<Seen>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = unbounded_channel();
    tokio::spawn(async move {
        loop {
            let (stream, _) = listener.accept().await.unwrap();
            let tx = tx.clone();
            let svc = service_fn(move |mut req: hyper::Request<hyper::body::Incoming>| {
                let tx = tx.clone();
                async move {
                    let target = req.uri().path_and_query().map(|p| p.to_string()).unwrap_or_default();
                    let headers = req.headers().iter().map(|(k, v)| (k.to_string(), String::from_utf8_lossy(v.as_bytes()).into_owned())).collect();
                    let upgrade = req.headers().contains_key("sec-websocket-key").then(|| hyper::upgrade::on(&mut req));
                    let body = req.into_body().collect().await.map(|b| b.to_bytes().to_vec()).unwrap_or_default();
                    tx.send(Seen { target, headers, body }).unwrap();
                    let mut resp = hyper::Response::new(Full::new(Bytes::from_static(b"upstream")));
                    if let Some(upgrade) = upgrade {
                        *resp.status_mut() = hyper::StatusCode::SWITCHING_PROTOCOLS;
                        resp.headers_mut().insert("upgrade", "websocket".parse().unwrap());
                        resp.headers_mut().insert("connection", "Upgrade".parse().unwrap());
                        resp.headers_mut().insert("sec-websocket-accept", "x".parse().unwrap());
                        tokio::spawn(async move {
                            let mut io = TokioIo::new(upgrade.await.unwrap());
                            let (mut r, mut w) = tokio::io::split(&mut io);
                            let _ = tokio::io::copy(&mut r, &mut w).await;
                        });
                    }
                    Ok::<_, std::convert::Infallible>(resp)
                }
            });
            tokio::spawn(hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream), svc).with_upgrades());
        }
    });
    (addr, rx)
}

struct Rig {
    edge: SocketAddr,
    seen: UnboundedReceiver<Seen>,
    _dir: tempfile::TempDir,
}

/// The edge as `service.rs` builds it, with the gateway, the vault-less front and the board door
/// all pointing at one scratch upstream.
async fn rig(public: bool) -> Rig {
    let (up, seen) = upstream().await;
    let dir = tempfile::tempdir().unwrap();
    let ca_path = dir.path().join("ca.pem");
    std::fs::write(&ca_path, CA).unwrap();
    let origin = format!("http://{up}");
    let upstreams = Upstreams { vault: None, tether: None, board: Some(origin.clone()) };
    let front = Front::new(public, upstreams, Hub::new("http://127.0.0.1:1", None), Landing::open(&dir.path().join("landing.db")).unwrap());
    let config = EdgeConfig { upstream: origin, ca_path, ca_download: !public, same_site: if public { "strict" } else { "lax" }, behind_proxy: true, identity_cookies: &[] };
    let edge = Arc::new(Edge::new(Arc::new(AuthGate::new(Arc::new(Fixed))), Arc::new(Grants::new(None, "node")), config, Box::new(front)));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    edge::serve_plain(edge, listener);
    Rig { edge: addr, seen, _dir: dir }
}

struct Reply {
    status: u16,
    headers: Vec<(String, String)>,
    body: String,
}

impl Reply {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

fn request(method: &str, target: &str, headers: &[&str]) -> String {
    let mut out = format!("{method} {target} HTTP/1.1\r\nHost: node.test\r\nConnection: close\r\n");
    for h in headers {
        out.push_str(h);
        out.push_str("\r\n");
    }
    out + "\r\n"
}

/// One request written as bytes, so the target reaches the edge exactly as given. The head may
/// declare a body the caller never sends.
async fn send(edge: SocketAddr, head: String, body: &str) -> Reply {
    let mut tcp = TcpStream::connect(edge).await.unwrap();
    tcp.write_all(head.as_bytes()).await.unwrap();
    tcp.write_all(body.as_bytes()).await.unwrap();
    let mut raw = Vec::new();
    let _ = tcp.read_to_end(&mut raw).await;
    let text = String::from_utf8_lossy(&raw).into_owned();
    let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let mut lines = head.lines();
    let status = lines.next().unwrap().split(' ').nth(1).unwrap().parse().unwrap();
    let headers = lines.filter_map(|l| l.split_once(": ")).map(|(k, v)| (k.to_lowercase(), v.to_string())).collect();
    Reply { status, headers, body: body.to_string() }
}

fn session(sub: &str) -> String {
    format!("Cookie: awm_session={}", tokens::session_mint(SECRET, sub, 3600.0, None, awm_plugin_auth::now()))
}

#[derive(Clone, Default)]
struct Logs(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for Logs {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Logs {
    type Writer = Logs;
    fn make_writer(&'a self) -> Logs {
        self.clone()
    }
}

/// What a row expects: the answer, and whether the upstream saw the request.
enum Want {
    Status(u16),
    Redirect,
    Proxied,
}

#[tokio::test]
async fn private_profile() {
    let logs = Logs::default();
    let _logging = tracing::subscriber::set_default(tracing_subscriber::fmt().with_writer(logs.clone()).with_ansi(false).with_max_level(tracing::Level::DEBUG).finish());
    let mut rig = rig(false).await;
    let alice = session("alice");
    let forged = ["X-Awm-As: user:root", "X-Awm-Caller: boss", "X-Awm-Service-Token: t", "X-Awm-Edge-Token: e", "Connection-Upgrade-Test: kept"];
    let bad_bearer = "Authorization: Bearer awmut1.deadbeef.nope";
    let html = "Accept: text/html";
    let json = "Accept: application/json";

    let mut with_forged = vec![alice.as_str()];
    with_forged.extend(forged);
    let rows: Vec<(&str, &str, Vec<&str>, Want)> = vec![
        ("no session, html", "/hub/plugins", vec![html], Want::Redirect),
        ("no session, json", "/hub/plugins", vec![json], Want::Status(401)),
        ("session, raw target kept", "/a/./b%23c?x=1&grant=SECRET", with_forged, Want::Proxied),
        ("bad bearer does not fall back to the cookie", "/hub/plugins", vec![bad_bearer, alice.as_str(), json], Want::Status(401)),
        ("encoded dot-dot", "/a/%2e%2e/b", vec![alice.as_str()], Want::Status(404)),
        ("ca download needs no session", "/ca.crt", vec![], Want::Status(200)),
        ("refused portal grant", "/__auth/portal?next=/&grant=SECRETGRANT", vec![], Want::Status(403)),
    ];
    for (label, target, headers, want) in rows {
        let reply = send(rig.edge, request("GET", target, &headers), "").await;
        let seen = rig.seen.try_recv().ok();
        match want {
            Want::Status(n) => assert_eq!((reply.status, seen.is_none()), (n, true), "{label}"),
            Want::Redirect => {
                assert_eq!(reply.status, 302, "{label}");
                assert!(reply.header("location").unwrap().starts_with(&format!("{}/signin?next=", portal::approver_origin())), "{label}");
                assert!(seen.is_none(), "{label}");
            }
            Want::Proxied => {
                assert_eq!((reply.status, reply.body.as_str()), (200, "upstream"), "{label}");
                let seen = seen.expect(label);
                assert_eq!(seen.target, "/a/./b%23c?x=1&grant=SECRET", "{label}");
                assert_eq!(seen.header("x-awm-as"), ["user:alice"], "{label}");
                for stripped in ["x-awm-caller", "x-awm-service-token", "x-awm-edge-token"] {
                    assert!(seen.header(stripped).is_empty(), "{label}: {stripped}");
                }
                assert_eq!(seen.header("connection-upgrade-test"), ["kept"], "{label}");
                assert!(seen.body.is_empty());
            }
        }
    }
    let ca = send(rig.edge, request("GET", "/ca.crt", &[]), "").await;
    assert_eq!(ca.body, CA);

    let logged = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(logged.contains("portal grant refused"), "the refused grant must be logged, or this check is empty:\n{logged}");
    assert!(!logged.contains("grant=") && !logged.contains("SECRET") && !logged.contains("x=1"), "{logged}");
}

#[tokio::test]
async fn public_profile() {
    let mut rig = rig(true).await;
    let alice = session("alice");
    let content_length = |body: &str| format!("Content-Length: {}", body.len());
    let private_invoke = r#"{"name":"auth","args":{"verb":"device_list"}}"#;
    let rows: Vec<(&str, &str, &str, &str, bool)> = vec![
        ("ca download", "GET", "/ca.crt", "", false),
        ("gateway listing", "GET", "/hub/plugins", "", false),
        ("landing edit", "POST", "/__landing/tags", r#"{"page":"p","tag":"t"}"#, false),
        ("private invoke body", "POST", "/invoke", private_invoke, false),
        ("trilium page", "GET", "/p/trilium/trilium/index.html", "", true),
    ];
    for (label, method, target, body, admitted) in rows {
        let cl = content_length(body);
        let reply = send(rig.edge, request(method, target, &[alice.as_str(), cl.as_str()]), body).await;
        let seen = rig.seen.try_recv().ok();
        assert_eq!((reply.status, seen.is_some()), (if admitted { 200 } else { 404 }, admitted), "{label}");
    }
}

#[tokio::test]
async fn board_door_caps_bodies_before_the_upstream() {
    const { assert!(board::MAX_FILE_BODY >= 16 * 1024 * 1024, "the board service accepts files up to AWM_BOARD_FILE_MAX, 16 MiB by default") };
    let mut rig = rig(false).await;
    let sent = send(rig.edge, request("POST", "/board/cards", &["Content-Length: 5"]), "hello").await;
    assert_eq!(sent.status, 200);
    assert_eq!(rig.seen.try_recv().unwrap().body, b"hello");

    let upload = format!("/board/cards/{CARD}/files");
    let over_cards = format!("Content-Length: {}", board::MAX_BODY + 1);
    let over_file = format!("Content-Length: {}", board::MAX_FILE_BODY + 1);
    for (target, length) in [("/board/cards", over_cards.as_str()), (upload.as_str(), over_file.as_str())] {
        let reply = send(rig.edge, request("POST", target, &[length]), "").await;
        assert_eq!(reply.status, 413, "{target}");
        assert!(rig.seen.try_recv().is_err(), "{target}: the upstream must not see it");
    }
}

/// The response head of a handshake, and the socket it was read from.
async fn handshake(edge: SocketAddr, head: String) -> (String, TcpStream) {
    let mut tcp = TcpStream::connect(edge).await.unwrap();
    tcp.write_all(head.as_bytes()).await.unwrap();
    let mut got = Vec::new();
    while !got.ends_with(b"\r\n\r\n") {
        got.push(tcp.read_u8().await.unwrap());
    }
    (String::from_utf8_lossy(&got).into_owned(), tcp)
}

#[tokio::test]
async fn websocket_needs_a_session() {
    let mut rig = rig(false).await;
    let upgrade = ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"];
    let open = |cookie: Option<&str>| {
        let mut head = request("GET", "/ws/x", &upgrade).replace("Connection: close\r\n", "");
        if let Some(c) = cookie {
            head = head.replace("\r\n\r\n", &format!("\r\n{c}\r\n\r\n"));
        }
        head
    };

    assert!(handshake(rig.edge, open(None)).await.0.starts_with("HTTP/1.1 403"));
    assert!(rig.seen.try_recv().is_err());

    let (head, mut tcp) = handshake(rig.edge, open(Some(&session("alice")))).await;
    assert!(head.starts_with("HTTP/1.1 101"));
    assert_eq!(rig.seen.recv().await.unwrap().header("x-awm-as"), ["user:alice"]);
    tcp.write_all(b"ping").await.unwrap();
    let mut echo = [0u8; 4];
    tcp.read_exact(&mut echo).await.unwrap();
    assert_eq!(&echo, b"ping");
}

#[tokio::test]
async fn a_service_front_gates_relays_and_follows_the_table() {
    let (up, mut seen) = upstream().await;
    let (signin, mut signin_seen) = upstream().await;
    let dir = tempfile::tempdir().unwrap();
    let base = Base {
        gate: Arc::new(AuthGate::new(Arc::new(Fixed))),
        grants: Arc::new(Grants::new(None, "node")),
        config: EdgeConfig { upstream: String::new(), ca_path: dir.path().join("ca.pem"), ca_download: true, same_site: "lax", behind_proxy: true, identity_cookies: &[] },
        tls: None,
        san: None,
        sans_file: dir.path().join("sans"),
    };
    let fronts = Fronts::new(base);
    let taken = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let free = TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap();
    let front = json!([{"port": free.port(), "upstream": format!("http://{up}/"), "rewrite_origin": true, "name": "gui", "paths": {"/_signin": format!("http://{signin}/")}}]);
    let blocked = json!([{"port": taken.local_addr().unwrap().port(), "upstream": format!("http://{up}/"), "name": "gui"}]);
    let off = json!([{"port": 1, "upstream": format!("http://{up}/"), "name": "gui"}]);
    let table = json!([
        {"name": "sci", "enabled": true, "front": front},
        {"name": "blocked", "enabled": true, "front": blocked},
        {"name": "off", "enabled": false, "front": off},
    ]);
    fronts.reconcile(fronts::declared(&table)).await;

    let alice = session("alice");
    let origin = "Origin: https://mesh:12201";
    assert_eq!(send(free, request("GET", "/a", &["Accept: application/json", origin]), "").await.status, 401);
    assert!(seen.try_recv().is_err(), "no session reaches the upstream");

    let reply = send(free, request("GET", "/a%23b?x=1", &[alice.as_str(), origin, "X-Awm-As: user:root"]), "").await;
    assert_eq!(reply.status, 200);
    let got = seen.try_recv().unwrap();
    assert_eq!(got.target, "/a%23b?x=1");
    assert_eq!(got.header("x-awm-as"), ["user:alice"]);
    assert_eq!(got.header("origin"), [format!("http://{up}").as_str()]);
    assert_eq!(got.header("host"), [up.to_string().as_str()]);
    assert_eq!(got.header("x-forwarded-host"), ["node.test"]);
    assert_eq!(got.header("x-forwarded-for"), ["127.0.0.1"]);

    send(free, request("GET", "/a", &[alice.as_str()]), "").await;
    assert!(seen.try_recv().unwrap().header("origin").is_empty(), "an Origin is never minted");

    send(free, request("GET", "/_signin/x", &[alice.as_str(), origin]), "").await;
    let got = signin_seen.try_recv().unwrap();
    assert_eq!((got.target.as_str(), got.header("origin")), ("/_signin/x", vec![format!("http://{signin}").as_str()]));
    send(free, request("GET", "/_signinx", &[alice.as_str()]), "").await;
    assert_eq!(seen.try_recv().unwrap().target, "/_signinx", "a prefix ends at a segment");

    assert_eq!(send(free, request("GET", "/a/%2e%2e/b", &[alice.as_str()]), "").await.status, 404);
    assert!(seen.try_recv().is_err());

    let upgrade = ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13", origin];
    let open = |cookie: &str| request("GET", "/ws", &[&upgrade[..], &[cookie]].concat()).replace("Connection: close\r\n", "");
    assert!(handshake(free, open("X: y")).await.0.starts_with("HTTP/1.1 403"));
    let (head, _tcp) = handshake(free, open(&alice)).await;
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    let got = seen.recv().await.unwrap();
    assert_eq!((got.header("x-awm-as"), got.header("origin")), (vec!["user:alice"], vec![format!("http://{up}").as_str()]));
    assert_eq!(got.header("x-forwarded-for"), ["127.0.0.1"]);

    let sci = fronts.state("sci", "gui").await;
    assert_eq!((&sci["serving"], &sci["listener_port"], &sci["error"]), (&json!(true), &json!(free.port()), &json!(null)));
    let blocked = fronts.state("blocked", "gui").await;
    assert_eq!(blocked["serving"], false);
    assert!(blocked["error"].as_str().unwrap().contains("could not listen"), "{blocked}");
    assert_eq!(fronts.state("off", "gui").await["listener_port"], json!(null));

    fronts.reconcile(Default::default()).await;
    assert!(TcpStream::connect(free).await.is_err(), "a front no longer declared closes");
}

#[tokio::test]
async fn a_front_with_wake_wakes_on_authenticated_traffic_and_on_refusal() {
    let (up, mut seen) = upstream().await;
    let (wake, mut woken) = upstream().await;
    let dir = tempfile::tempdir().unwrap();
    let base = Base {
        gate: Arc::new(AuthGate::new(Arc::new(Fixed))),
        grants: Arc::new(Grants::new(None, "node")),
        config: EdgeConfig { upstream: String::new(), ca_path: dir.path().join("ca.pem"), ca_download: true, same_site: "lax", behind_proxy: true, identity_cookies: &[] },
        tls: None,
        san: None,
        sans_file: dir.path().join("sans"),
    };
    let fronts = Fronts::new(base);
    let up_port = |addr: SocketAddr| TcpListener::bind(addr);
    let live = TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap();
    let down = TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap();
    let wake_url = format!("http://{wake}/_wake");
    let table = json!([
        {"name": "sci", "enabled": true, "front": [{"port": live.port(), "upstream": format!("http://{up}/"), "name": "gui", "wake": wake_url}]},
        {"name": "dead", "enabled": true, "front": [{"port": down.port(), "upstream": format!("http://127.0.0.1:{}/", up_port(([127, 0, 0, 1], 0).into()).await.unwrap().local_addr().unwrap().port()), "name": "gui", "wake": wake_url}]},
    ]);
    fronts.reconcile(fronts::declared(&table)).await;
    let alice = session("alice");

    assert_eq!(send(live, request("GET", "/a", &["Accept: application/json"]), "").await.status, 401);
    tokio::task::yield_now().await;
    assert!(woken.try_recv().is_err(), "an unauthenticated request never wakes");

    assert_eq!(send(live, request("GET", "/a", &[alice.as_str()]), "").await.status, 200);
    assert_eq!(woken.recv().await.unwrap().header("x-awm-as"), ["user:alice"]);
    seen.recv().await.unwrap();
    send(live, request("GET", "/b", &[alice.as_str()]), "").await;
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    assert!(woken.try_recv().is_err(), "a second hit inside the minute does not wake again");

    let page = send(down, request("GET", "/", &[alice.as_str(), "Accept: text/html"]), "").await;
    assert_eq!(page.status, 503);
    assert!(page.body.contains("Starting the workbench"), "{}", page.body);
    assert_eq!(woken.recv().await.unwrap().target, "/_wake", "a refused upstream wakes at once");
    let api = send(down, request("GET", "/x", &[alice.as_str(), "Accept: application/json"]), "").await;
    assert_eq!(api.status, 503);
    assert!(!api.body.contains("<meta"));
    woken.recv().await.unwrap();
}
