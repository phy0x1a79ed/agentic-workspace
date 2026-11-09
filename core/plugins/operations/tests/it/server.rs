//! The vault API in process: the cookie, the origin rule, CORS, the security headers, each
//! `/api` action, the sign-in and the modal pages. Every id, host and key here is synthetic.

use std::net::SocketAddr;
use std::sync::Arc;

use awm_plugin_operations::passkeys::soft::SoftAuthenticator;
use awm_plugin_operations::server::{router, Vault, COOKIE};
use awm_plugin_operations::store::Open;
use awm_trust::grant::{unb64u, verify_grant, Expect};
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{header, HeaderMap, Method, Request, StatusCode};
use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;
use crate::vault_common::{env, env_with, s, Env, AGENT_NODE, ORIGIN, PORTAL, PORTAL_NODE, RP_ID};

const BOARD: &str = "https://board.example.test";
const EVIL: &str = "https://evil.example.test";
const CLIENT: [u8; 4] = [203, 0, 113, 5];

struct Resp {
    status: StatusCode,
    headers: HeaderMap,
    body: Vec<u8>,
}

impl Resp {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or(Value::Null)
    }

    fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }

    fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    fn code(&self) -> Value {
        self.json()["code"].clone()
    }
}

struct Vt {
    e: Env,
    app: Router,
}

fn vt() -> Vt {
    vt_with(env())
}

fn vt_with(e: Env) -> Vt {
    let vault = Vault { approver: e.ap.clone(), board_origins: vec![BOARD.into()], forwarded_header: None, peers: vec!["deneb".into()], hub: None };
    Vt { app: router(Arc::new(vault)), e }
}

impl Vt {
    async fn send(&self, method: Method, path: &str, origin: Option<&str>, cookie: Option<&str>, body: Option<Value>) -> Resp {
        let mut b = Request::builder().method(method).uri(path);
        if let Some(o) = origin {
            b = b.header(header::ORIGIN, o);
        }
        if let Some(c) = cookie {
            b = b.header(header::COOKIE, format!("theme=dark; {COOKIE}={c}"));
        }
        let body = match body {
            Some(v) => {
                b = b.header(header::CONTENT_TYPE, "application/json");
                Body::from(v.to_string())
            }
            None => Body::empty(),
        };
        let mut req = b.body(body).unwrap();
        req.extensions_mut().insert(ConnectInfo(SocketAddr::from((CLIENT, 40000))));
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let (status, headers) = (resp.status(), resp.headers().clone());
        let body = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap().to_vec();
        Resp { status, headers, body }
    }

    /// A GET from the board.
    async fn get(&self, path: &str, cookie: Option<&str>) -> Resp {
        self.send(Method::GET, path, Some(BOARD), cookie, None).await
    }

    /// A POST from the board with a JSON body.
    async fn post(&self, path: &str, cookie: Option<&str>, body: Value) -> Resp {
        self.send(Method::POST, path, Some(BOARD), cookie, Some(body)).await
    }

    /// An enrolled device with a live sign-in: its id, passkey and cookie.
    fn signed_in(&self, name: &str) -> (String, SoftAuthenticator, String) {
        let (d, pk) = self.e.enrol(name);
        let c = self.e.ap.session_new(&d, None).unwrap();
        (d, pk, c)
    }
}

fn cookie_from(r: &Resp) -> String {
    let line = r.header("set-cookie").expect("a set-cookie");
    let prefix = format!("{COOKIE}=");
    let suffix = "; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=43200";
    assert!(line.starts_with(&prefix) && line.ends_with(suffix), "{line}");
    let value = &line[prefix.len()..line.len() - suffix.len()];
    assert_eq!(unb64u(value).map(|b| b.len()), Some(32), "{line}");
    value.to_string()
}

fn portal_url() -> String {
    format!("https://{PORTAL}/trilium/")
}

fn check_portal_redirect(e: &Env, location: &str, device: &str) {
    let prefix = format!("https://{PORTAL}/__auth/portal?");
    assert!(location.starts_with(&prefix), "{location}");
    let q: std::collections::HashMap<String, String> = url::form_urlencoded::parse(&location.as_bytes()[prefix.len()..]).into_owned().collect();
    assert_eq!(q["next"], "/trilium/");
    let grant: Value = serde_json::from_slice(&unb64u(&q["grant"]).unwrap()).unwrap();
    let payload = json!({"site": PORTAL, "return": "/trilium/"});
    let body = verify_grant(&grant, &e.grant_pub(), &Expect { kind: Some("portal_login"), node: Some(PORTAL_NODE), payload: Some(&payload), ..Expect::default() }, e.now()).unwrap();
    assert_eq!(body["approved_by_device"], device);
}

fn security_headers(r: &Resp, what: &str) {
    let csp = r.header("content-security-policy").unwrap_or_else(|| panic!("{what}: no CSP"));
    assert!(csp.contains(&format!("frame-ancestors 'self' {BOARD}")), "{what}: {csp}");
    let pp = r.header("permissions-policy").unwrap_or_else(|| panic!("{what}: no Permissions-Policy"));
    assert!(pp.contains(&format!("publickey-credentials-get=(self \"{BOARD}\")")), "{what}: {pp}");
    assert!(pp.contains(&format!("publickey-credentials-create=(self \"{BOARD}\")")), "{what}: {pp}");
    assert_eq!(r.header("referrer-policy"), Some("no-referrer"), "{what}");
    assert_eq!(r.header("x-content-type-options"), Some("nosniff"), "{what}");
    assert_eq!(r.header("cache-control"), Some("no-store"), "{what}");
}

/// Every `/api` route that needs the sign-in, with a body its handler accepts.
fn guarded(rid: &str, device: &str) -> Vec<(Method, String, Option<Value>)> {
    let p = |m: Method, path: String, b: Option<Value>| (m, path, b);
    vec![
        p(Method::GET, "/api/session".into(), None),
        p(Method::POST, "/api/agent".into(), Some(json!({"node": AGENT_NODE}))),
        p(Method::POST, "/api/card/AAAAAAAA/close".into(), Some(json!({}))),
        p(Method::POST, "/api/seat/remote_control".into(), Some(json!({"seat": "s1", "on": true}))),
        p(Method::POST, "/api/service/toggle".into(), Some(json!({"node": AGENT_NODE, "service": "dsh", "on": false}))),
        p(Method::GET, format!("/api/request/{rid}"), None),
        p(Method::POST, format!("/api/request/{rid}/options"), Some(json!({}))),
        p(Method::POST, format!("/api/request/{rid}/approve"), Some(json!({"assertion": {}}))),
        p(Method::POST, format!("/api/request/{rid}/reject"), Some(json!({}))),
        p(Method::GET, "/api/twofa".into(), None),
        p(Method::POST, "/api/twofa/options".into(), Some(json!({"count": 2, "device": "phone"}))),
        p(Method::POST, "/api/twofa/burst".into(), Some(json!({"nonce": "n", "count": 2, "device": "phone", "assertion": {}}))),
        p(Method::GET, "/api/devices".into(), None),
        p(Method::POST, format!("/api/device/{device}/revoke/options"), Some(json!({}))),
        p(Method::POST, format!("/api/device/{device}/revoke"), Some(json!({"nonce": "n", "assertion": {}}))),
    ]
}

// -- the cookie and the origin rule -------------------------------------------------------------

#[tokio::test]
async fn every_guarded_route_needs_a_live_signin() {
    let t = vt();
    let (d, _, live) = t.signed_in("phone");
    let r = t.e.hold(None).await;
    let rid = s(&r["request_id"]).to_string();
    let card = t.e.store.task_open("A card", Open::default(), None).unwrap();
    let expired = t.e.ap.session_new(&d, None).unwrap();
    t.e.clock.advance(12.0 * 3600.0 + 1.0);
    let live = {
        assert_eq!(t.e.ap.session_device(&live).unwrap(), None);
        t.e.ap.session_new(&d, None).unwrap()
    };
    let garbage = awm_trust::grant::b64u(&[9u8; 32]);
    for (m, path, body) in guarded(&rid, &d) {
        let path = path.replace("AAAAAAAA", s(&card["task_id"]));
        for cookie in [None, Some("not-a-cookie"), Some(garbage.as_str()), Some(expired.as_str())] {
            let resp = t.send(m.clone(), &path, Some(BOARD), cookie, body.clone()).await;
            assert_eq!(resp.status, StatusCode::UNAUTHORIZED, "{m} {path} {cookie:?}: {}", resp.text());
            assert_eq!(resp.code(), "need_signin", "{m} {path}");
        }
    }
    assert_eq!(t.e.ap.approval_get(&rid).unwrap()["status"], "pending");
    assert_eq!(t.e.store.task_get(s(&card["task_id"])).unwrap()["section"], "working");
    assert!(t.e.tools.calls().is_empty());
    assert!(t.e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty());
    assert_eq!(t.e.ap.devices().unwrap()[0]["revoked_at"], Value::Null);
    let ok = t.get("/api/session", Some(&live)).await;
    assert_eq!(ok.status, StatusCode::OK);
    assert_eq!(ok.json(), json!({"ok": true, "device": d}));
}

#[tokio::test]
async fn keys_enrol_and_signin_routes_need_no_cookie() {
    let t = vt();
    let keys = t.get("/api/keys", None).await;
    assert_eq!(keys.status, StatusCode::OK);
    let k = keys.json();
    assert_eq!(k["rp_id"], RP_ID);
    assert_eq!(k["origin"], ORIGIN);
    assert_eq!(k["grant_pub"], t.e.ap.keys.grant_pub_b64u().as_str());
    assert_eq!(k["ack_pub"], t.e.ap.keys.ack_pub_spki_b64u().as_str());
    assert_eq!(k["board_origins"], json!([BOARD]));
    assert_eq!(k["peers"], json!(["deneb"]));
    assert_eq!(t.post("/api/signin/begin", None, json!({})).await.status, StatusCode::OK);
    let begin = t.post("/api/enrol/begin", None, json!({"token": "nope", "name": "x"})).await;
    assert_eq!((begin.status, begin.code()), (StatusCode::BAD_REQUEST, json!("bad_link")));
    let ask = t.post("/api/enrol/ask", None, json!({"name": "laptop"})).await;
    assert_eq!(ask.status, StatusCode::OK);
    let a = ask.json();
    let st = t.post("/api/enrol/status", None, json!({"request_id": a["request_id"], "poll_secret": a["poll_secret"]})).await;
    assert_eq!(st.json(), json!({"status": "pending", "token": null}));
    let wrong = t.post("/api/enrol/status", None, json!({"request_id": a["request_id"], "poll_secret": "wrong"})).await;
    assert_eq!((wrong.status, wrong.code()), (StatusCode::NOT_FOUND, json!("unknown_request")));
    assert_eq!(t.get("/api/nope", None).await.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn a_post_names_a_known_origin() {
    let t = vt();
    let (_, _, c) = t.signed_in("phone");
    let body = Some(json!({"node": AGENT_NODE}));
    for origin in [Some(EVIL), Some("null"), Some("https://board.example.test.evil.example"), None] {
        let r = t.send(Method::POST, "/api/agent", origin, Some(&c), body.clone()).await;
        assert_eq!((r.status, r.code()), (StatusCode::FORBIDDEN, json!("bad_origin")), "{origin:?}");
    }
    assert!(t.e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty());
    for origin in [BOARD, ORIGIN] {
        let r = t.send(Method::POST, "/api/agent", Some(origin), Some(&c), body.clone()).await;
        assert_eq!(r.status, StatusCode::OK, "{origin}: {}", r.text());
    }
    // The origin rule comes before the sign-in, and covers the sign-in routes too.
    let r = t.send(Method::POST, "/api/signin/begin", Some(EVIL), None, Some(json!({}))).await;
    assert_eq!(r.code(), "bad_origin");
    let r = t.send(Method::POST, "/api/enrol/ask", None, None, Some(json!({"name": "x"}))).await;
    assert_eq!(r.code(), "bad_origin");
}

// -- CORS --------------------------------------------------------------------------------------

#[tokio::test]
async fn cors_answers_the_board_origin_alone() {
    let t = vt();
    let (_, _, c) = t.signed_in("phone");
    let r = t.get("/api/devices", Some(&c)).await;
    assert_eq!(r.header("access-control-allow-origin"), Some(BOARD));
    assert_eq!(r.header("access-control-allow-credentials"), Some("true"));
    assert!(r.headers.get_all("vary").iter().any(|v| v.to_str().unwrap_or("").contains("Origin")));
    let refused = t.get("/api/devices", None).await;
    assert_eq!(refused.status, StatusCode::UNAUTHORIZED);
    assert_eq!(refused.header("access-control-allow-origin"), Some(BOARD), "the board reads the refusal");
    let own = t.send(Method::GET, "/api/devices", Some(ORIGIN), Some(&c), None).await;
    assert_eq!(own.status, StatusCode::OK);
    assert_eq!(own.header("access-control-allow-origin"), None);
    assert_eq!(own.header("access-control-allow-credentials"), None);
    let evil = t.send(Method::GET, "/api/devices", Some(EVIL), Some(&c), None).await;
    assert_eq!(evil.header("access-control-allow-origin"), None);
    let page = t.send(Method::GET, "/signin", Some(BOARD), None, None).await;
    assert_eq!(page.header("access-control-allow-origin"), None, "only /api answers CORS");
}

async fn preflight(t: &Vt, origin: Option<&str>) -> Resp {
    let mut b = Request::builder().method(Method::OPTIONS).uri("/api/agent").header("access-control-request-method", "POST").header("access-control-request-headers", "content-type");
    if let Some(o) = origin {
        b = b.header(header::ORIGIN, o);
    }
    let mut req = b.body(Body::empty()).unwrap();
    req.extensions_mut().insert(ConnectInfo(SocketAddr::from((CLIENT, 40000))));
    let resp = t.app.clone().oneshot(req).await.unwrap();
    let (status, headers) = (resp.status(), resp.headers().clone());
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap().to_vec();
    Resp { status, headers, body }
}

#[tokio::test]
async fn preflight_follows_the_origin_rule() {
    let t = vt();
    let ok = preflight(&t, Some(BOARD)).await;
    assert_eq!(ok.status, StatusCode::NO_CONTENT);
    assert_eq!(ok.header("access-control-allow-origin"), Some(BOARD));
    assert_eq!(ok.header("access-control-allow-credentials"), Some("true"));
    assert_eq!(ok.header("access-control-allow-methods"), Some("GET, POST"));
    assert_eq!(ok.header("access-control-allow-headers"), Some("Content-Type"));
    for origin in [Some(EVIL), None] {
        let r = preflight(&t, origin).await;
        assert_eq!((r.status, r.code()), (StatusCode::FORBIDDEN, json!("bad_origin")), "{origin:?}");
        assert_eq!(r.header("access-control-allow-origin"), None);
    }
}

// -- security headers --------------------------------------------------------------------------

#[tokio::test]
async fn security_headers_on_every_response() {
    let t = vt();
    let (_, _, c) = t.signed_in("phone");
    security_headers(&t.get("/api/keys", None).await, "keys");
    security_headers(&t.get("/api/devices", Some(&c)).await, "devices");
    security_headers(&t.get("/api/devices", None).await, "a 401");
    security_headers(&t.send(Method::POST, "/api/agent", Some(EVIL), Some(&c), Some(json!({}))).await, "a 403");
    security_headers(&t.send(Method::GET, "/signin", None, None, None).await, "the sign-in page");
    security_headers(&t.send(Method::GET, "/modal/twofa?count=3", None, None, None).await, "a refused page");
    security_headers(&t.send(Method::GET, "/static/vault.js", None, None, None).await, "a static file");
    security_headers(&t.send(Method::GET, "/no/such/page", None, None, None).await, "a 404");
    security_headers(&t.send(Method::GET, &format!("/signin?next={}", enc(&portal_url())), None, Some(&c), None).await, "a 303");
    security_headers(&preflight(&t, Some(BOARD)).await, "a preflight");
    security_headers(&preflight(&t, Some(EVIL)).await, "a refused preflight");
}

fn enc(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}

// -- everyday actions --------------------------------------------------------------------------

#[tokio::test]
async fn cards_close_to_history() {
    let t = vt();
    let (_, _, c) = t.signed_in("phone");
    let one = t.e.store.task_open("Close me", Open::default(), None).unwrap();
    let r = t.post(&format!("/api/card/{}/close", s(&one["task_id"])), Some(&c), json!({})).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    let card = t.e.store.task_get(s(&one["task_id"])).unwrap();
    assert_eq!((card["section"].as_str(), card["status"].as_str()), (Some("history"), Some("done")));
    let r = t.post(&format!("/api/card/{}/frobnicate", s(&one["task_id"])), Some(&c), json!({})).await;
    assert_eq!((r.status, r.code()), (StatusCode::BAD_REQUEST, json!("unknown_action")));
    let r = t.post("/api/card/ZZZZZZZZ/close", Some(&c), json!({})).await;
    assert!(!r.status.is_success(), "{}", r.text());
}

#[tokio::test]
async fn more_than_rate_max_everyday_actions_a_minute_is_refused() {
    let t = vt_with(env_with(3));
    let (_, _, c) = t.signed_in("phone");
    for _ in 0..3 {
        assert_eq!(t.post("/api/agent", Some(&c), json!({"node": "deneb"})).await.code(), "not_allowed");
    }
    let r = t.post("/api/agent", Some(&c), json!({"node": AGENT_NODE})).await;
    assert_eq!((r.status, r.code()), (StatusCode::TOO_MANY_REQUESTS, json!("rate")));
    assert!(t.e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty());
    let card = t.e.store.task_open("Close me", Open::default(), None).unwrap();
    let r = t.post(&format!("/api/card/{}/close", s(&card["task_id"])), Some(&c), json!({})).await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
    // A read is not an everyday action.
    assert_eq!(t.get("/api/devices", Some(&c)).await.status, StatusCode::OK);
    let (_, _, other) = t.signed_in("laptop");
    assert_eq!(t.post("/api/agent", Some(&other), json!({"node": AGENT_NODE})).await.status, StatusCode::OK);
    t.e.clock.advance(61.0);
    assert_eq!(t.post("/api/agent", Some(&c), json!({"node": AGENT_NODE})).await.status, StatusCode::OK);
}

// -- the modal: a request and its passkey ---------------------------------------------------------

#[tokio::test]
async fn the_modal_reads_and_approves_one_request() {
    let t = vt();
    let (d, mut pk, c) = t.signed_in("phone");
    let payload = json!({"session_id": "cc-1", "from": "low", "to": "high"});
    let r = t.e.ap.approval_request("trust_raise", Some("mizar"), "needs high", 30, payload.clone(), Some("cc-1"), None).await.unwrap();
    let other = t.e.hold(None).await;
    let rid = s(&r["request_id"]).to_string();
    let view = t.get(&format!("/api/request/{rid}"), Some(&c)).await;
    assert_eq!(view.status, StatusCode::OK);
    let v = view.json();
    assert_eq!((v["kind"].as_str(), v["node"].as_str(), v["reason"].as_str(), v["status"].as_str()), (Some("trust_raise"), Some("mizar"), Some("needs high"), Some("pending")));
    assert_eq!(v["payload"], payload);
    assert!(v.get("grant").is_none() && v.get("cert").is_none());
    assert_eq!(t.get("/api/request/qZZZZZZZZZZZZ", Some(&c)).await.status, StatusCode::NOT_FOUND);

    let opts = t.post(&format!("/api/request/{rid}/options"), Some(&c), json!({})).await.json();
    assert_eq!(opts["challenge"], awm_trust::grant::b64u(&awm_plugin_operations::approver::approval_challenge(&rid, "trust_raise", &payload)).as_str());
    let other_opts = t.post(&format!("/api/request/{}/options", s(&other["request_id"])), Some(&c), json!({})).await.json();
    let wrong = pk.assert(&other_opts);
    let r1 = t.post(&format!("/api/request/{rid}/approve"), Some(&c), json!({"assertion": wrong})).await;
    assert_eq!((r1.status, r1.code()), (StatusCode::FORBIDDEN, json!("bad_passkey")));
    let r2 = t.post(&format!("/api/request/{rid}/approve"), Some(&c), json!({})).await;
    assert_eq!((r2.status, r2.code()), (StatusCode::UNAUTHORIZED, json!("need_passkey")));
    let a = pk.assert(&opts);
    let ok = t.post(&format!("/api/request/{rid}/approve"), Some(&c), json!({"assertion": a})).await;
    assert_eq!(ok.status, StatusCode::OK, "{}", ok.text());
    assert_eq!(ok.json()["status"], "approved");
    assert!(ok.json().get("grant").is_none(), "the browser never gets the grant: only the requester collects it");
    let got = t.e.ap.approval_get(&rid).unwrap();
    let body = verify_grant(&got["grant"], &t.e.grant_pub(), &Expect { kind: Some("trust_raise"), node: Some("mizar"), payload: Some(&payload), ..Expect::default() }, t.e.now()).unwrap();
    assert_eq!(body["approved_by_device"], d.as_str());
    let a2 = pk.assert(&opts);
    let again = t.post(&format!("/api/request/{rid}/approve"), Some(&c), json!({"assertion": a2})).await;
    assert_eq!((again.status, again.code()), (StatusCode::CONFLICT, json!("decided")));
}

#[tokio::test]
async fn twofa_reads_and_bursts() {
    let t = vt();
    let (_, mut pk, c) = t.signed_in("phone");
    let st = t.get("/api/twofa", Some(&c)).await;
    assert_eq!(st.status, StatusCode::OK);
    assert_eq!(st.json()["devices"], json!([{"name": "phone"}]));
    let bad = t.post("/api/twofa/options", Some(&c), json!({"count": 3, "device": "phone"})).await;
    assert_eq!((bad.status, bad.code()), (StatusCode::BAD_REQUEST, json!("bad_request")));
    let o = t.post("/api/twofa/options", Some(&c), json!({"count": 2, "device": "phone"})).await.json();
    let a = pk.assert(&o["options"]);
    let body = json!({"nonce": o["nonce"], "count": 2, "device": "phone", "assertion": a});
    let r = t.post("/api/twofa/burst", Some(&c), body.clone()).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    assert_eq!(r.json(), json!({"ok": true}));
    let again = t.post("/api/twofa/burst", Some(&c), body).await;
    assert_eq!(again.code(), "bad_login");
    let bursts: Vec<_> = t.e.tools.calls().into_iter().filter(|c| c.0 == "burst").collect();
    assert_eq!(bursts, vec![("burst".to_string(), json!({"device": "phone", "count": 2}))]);
}

#[tokio::test]
async fn devices_list_and_revoke_with_a_passkey() {
    let t = vt();
    let (phone, mut pk, c) = t.signed_in("phone");
    let (laptop, _, lc) = t.signed_in("laptop");
    let list = t.get("/api/devices", Some(&c)).await.json();
    let ids: Vec<&str> = list.as_array().unwrap().iter().map(|d| s(&d["device_id"])).collect();
    assert_eq!(ids, [phone.as_str(), laptop.as_str()]);

    let o = t.post(&format!("/api/device/{laptop}/revoke/options"), Some(&c), json!({})).await.json();
    let a = pk.assert(&o["options"]);
    let wrong = t.post(&format!("/api/device/{phone}/revoke"), Some(&c), json!({"nonce": o["nonce"], "assertion": a})).await;
    assert_eq!((wrong.status, wrong.code()), (StatusCode::FORBIDDEN, json!("bad_passkey")), "the assertion names one device");
    let o = t.post(&format!("/api/device/{laptop}/revoke/options"), Some(&c), json!({})).await.json();
    let a = pk.assert(&o["options"]);
    let r = t.post(&format!("/api/device/{laptop}/revoke"), Some(&c), json!({"nonce": o["nonce"], "assertion": a.clone()})).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    assert_eq!(r.json(), json!({"device_id": laptop, "revoked": true, "sessions_ended": 1}));
    assert_eq!(t.get("/api/devices", Some(&lc)).await.code(), "need_signin");
    let replay = t.post(&format!("/api/device/{laptop}/revoke"), Some(&c), json!({"nonce": o["nonce"], "assertion": a})).await;
    assert_eq!(replay.code(), "bad_login");
    assert_eq!(t.e.ap.devices().unwrap()[0]["revoked_at"], Value::Null);
    assert_eq!(t.get("/api/session", Some(&c)).await.status, StatusCode::OK);
}

// -- the sign-in and its cookie --------------------------------------------------------------------

#[tokio::test]
async fn signin_finish_sets_the_cookie() {
    let t = vt();
    let (d, mut pk) = t.e.enrol("phone");
    let b = t.send(Method::POST, "/api/signin/begin", Some(ORIGIN), None, None).await;
    assert_eq!(b.status, StatusCode::OK, "{}", b.text());
    let b = b.json();
    let a = pk.assert(&b["options"]);
    let r = t.send(Method::POST, "/api/signin/finish", Some(ORIGIN), None, Some(json!({"nonce": b["nonce"], "assertion": a}))).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    assert_eq!(r.json(), json!({"ok": true, "device": d, "redirect": null}));
    let c = cookie_from(&r);
    assert_eq!(t.get("/api/session", Some(&c)).await.json()["device"], d.as_str());
    let db = rusqlite::Connection::open(t.e.dir.path().join("approver.db")).unwrap();
    let ip: Option<String> = db.query_row("SELECT ip FROM device_sessions ORDER BY created DESC LIMIT 1", [], |r| r.get(0)).unwrap();
    assert_eq!(ip.as_deref(), Some("203.0.113.5"));
}

#[tokio::test]
async fn signin_finish_with_next_answers_a_portal_redirect() {
    let t = vt();
    let (d, mut pk) = t.e.enrol("phone");
    let b = t.post("/api/signin/begin", None, json!({})).await.json();
    let a = pk.assert(&b["options"]);
    let r = t.post("/api/signin/finish", None, json!({"nonce": b["nonce"], "assertion": a, "next": portal_url()})).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    cookie_from(&r);
    check_portal_redirect(&t.e, s(&r.json()["redirect"]), &d);
}

#[tokio::test]
async fn a_failed_signin_sets_no_cookie() {
    let t = vt();
    let (_, mut pk) = t.e.enrol("phone");
    let b = t.post("/api/signin/begin", None, json!({})).await.json();
    let b2 = t.post("/api/signin/begin", None, json!({})).await.json();
    let a = pk.assert(&b2["options"]);
    let r = t.post("/api/signin/finish", None, json!({"nonce": b["nonce"], "assertion": a})).await;
    assert_eq!((r.status, r.code()), (StatusCode::FORBIDDEN, json!("bad_passkey")));
    assert_eq!(r.header("set-cookie"), None);
    let a = pk.assert(&b2["options"]);
    let r = t.post("/api/signin/finish", None, json!({"nonce": b2["nonce"], "assertion": a, "next": "https://evil.example.test/"})).await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
    assert_eq!(r.header("set-cookie"), None);
}

#[tokio::test]
async fn enrol_finish_sets_the_cookie() {
    let t = vt();
    let token = t.e.ap.enrol_link(15, None).unwrap()["token"].as_str().unwrap().to_string();
    let mut pk = SoftAuthenticator::new(ORIGIN, RP_ID);
    let b = t.send(Method::POST, "/api/enrol/begin", Some(ORIGIN), None, Some(json!({"token": token, "name": "phone"}))).await.json();
    let cred = pk.register(&b["options"]);
    let r = t.send(Method::POST, "/api/enrol/finish", Some(ORIGIN), None, Some(json!({"token": token, "reg_id": b["reg_id"], "credential": cred, "name": "phone"}))).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text());
    let out = r.json();
    let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["device"]);
    let c = cookie_from(&r);
    assert_eq!(t.get("/api/session", Some(&c)).await.json()["device"], out["device"]);
    let again = t.send(Method::POST, "/api/enrol/finish", Some(ORIGIN), None, Some(json!({"token": token, "reg_id": b["reg_id"], "credential": cred, "name": "phone"}))).await;
    assert_eq!(again.code(), "bad_link");
    assert_eq!(again.header("set-cookie"), None);
}

#[tokio::test]
async fn signout_clears_the_cookie() {
    let t = vt();
    let (_, _, c) = t.signed_in("phone");
    let r = t.post("/api/signout", Some(&c), json!({})).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.header("set-cookie"), Some(format!("{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0").as_str()));
    assert_eq!(t.get("/api/session", Some(&c)).await.code(), "need_signin");
    let r = t.send(Method::POST, "/api/signout", Some(EVIL), Some(&c), Some(json!({}))).await;
    assert_eq!(r.code(), "bad_origin");
}

// -- the pages ------------------------------------------------------------------------------------

#[tokio::test]
async fn signin_page_redirects_a_live_session_to_the_portal() {
    let t = vt();
    let (d, _, c) = t.signed_in("phone");
    let path = format!("/signin?next={}", enc(&portal_url()));
    let r = t.send(Method::GET, &path, None, Some(&c), None).await;
    assert_eq!(r.status, StatusCode::SEE_OTHER, "{}", r.text());
    check_portal_redirect(&t.e, r.header("location").unwrap(), &d);
    let bad = t.send(Method::GET, &format!("/signin?next={}", enc("https://evil.example.test/")), None, Some(&c), None).await;
    assert_eq!(bad.status, StatusCode::BAD_REQUEST);
    assert_eq!(bad.header("location"), None);
}
