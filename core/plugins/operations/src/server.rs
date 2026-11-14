//! The approver's own origin, auth.tony-xy-liu.com: the vault API the board calls, the
//! approval modal, the sign-in and the enrolment pages (queue.md R64, fleet.md R84,
//! CONTRACT.md b11adebc).
//!
//! deneb forwards this origin's TLS unopened, so only alcor sees what passes here. Every
//! `/api/*` call carries the device credential, a `__Host-` cookie, and an `Origin` that is
//! this origin or one of `board_origins`; CORS answers that one origin with credentials.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, Path, Query, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::approver::{Approver, ApproverError};

pub const COOKIE: &str = "__Host-awm_device";

const VAULT_JS: &str = include_str!("static/vault.js");
const VAULT_CSS: &str = include_str!("static/vault.css");
const PAGE: &str = include_str!("static/page.html");

pub struct Vault {
    pub approver: Arc<Approver>,
    pub board_origins: Vec<String>,
    pub forwarded_header: Option<String>,
    pub peers: Vec<String>,
    pub hub: Option<awm_plugin_sdk::Hub>,
}

type S = State<Arc<Vault>>;

fn refused(e: ApproverError) -> Response {
    let code = StatusCode::from_u16(e.status()).unwrap_or(StatusCode::BAD_REQUEST);
    (code, Json(e.as_json())).into_response()
}

fn deny(code: &'static str, msg: &str) -> Response {
    refused(ApproverError::new(code, msg))
}

fn cookie_of(h: &HeaderMap) -> Option<String> {
    h.get_all(header::COOKIE).iter().filter_map(|v| v.to_str().ok()).flat_map(|v| v.split(';')).find_map(|kv| {
        let (k, v) = kv.trim().split_once('=')?;
        (k == COOKIE).then(|| v.to_string())
    })
}

fn set_cookie(value: &str, max_age: i64) -> HeaderValue {
    HeaderValue::from_str(&format!("{COOKIE}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={max_age}")).unwrap_or(HeaderValue::from_static(""))
}

/// The client's address, or None when it is unknown. Unknown never counts as trusted.
fn client_ip(v: &Vault, peer: Option<SocketAddr>, h: &HeaderMap) -> Option<String> {
    let peer = peer?;
    if peer.ip().is_loopback() {
        let name = v.forwarded_header.as_deref()?;
        return h.get(name).and_then(|x| x.to_str().ok()).map(|s| s.split(',').next().unwrap_or(s).trim().to_string()).filter(|s| !s.is_empty());
    }
    Some(peer.ip().to_string())
}

impl Vault {
    fn own_origin(&self) -> String {
        self.approver.config.origin.trim_end_matches('/').to_string()
    }

    fn origin_ok(&self, o: &str) -> bool {
        o == self.own_origin() || self.board_origins.iter().any(|b| b.trim_end_matches('/') == o)
    }

    fn frame_ancestors(&self) -> String {
        std::iter::once("'self'".to_string()).chain(self.board_origins.iter().cloned()).collect::<Vec<_>>().join(" ")
    }

    /// The device behind the cookie, after the origin rule. A POST must name its origin.
    fn device(&self, method: &Method, h: &HeaderMap) -> Result<String, Response> {
        self.check_origin(method, h)?;
        let Some(c) = cookie_of(h) else { return Err(deny("need_signin", "sign in on the vault first")) };
        match self.approver.session_device(&c) {
            Ok(Some(d)) => Ok(d),
            Ok(None) => Err(deny("need_signin", "the sign-in ended: sign in again")),
            Err(e) => Err(refused(e)),
        }
    }

    fn check_origin(&self, method: &Method, h: &HeaderMap) -> Result<(), Response> {
        match h.get(header::ORIGIN).and_then(|o| o.to_str().ok()) {
            Some(o) if self.origin_ok(o) => Ok(()),
            Some(_) => Err(deny("bad_origin", "this origin may not call the vault API")),
            None if method == Method::GET => Ok(()),
            None => Err(deny("bad_origin", "a vault API write names its origin")),
        }
    }
}

async fn headers(State(v): S, req: Request, next: Next) -> Response {
    let origin = req.headers().get(header::ORIGIN).and_then(|o| o.to_str().ok()).map(str::to_string);
    let is_api = req.uri().path().starts_with("/api/");
    if req.method() == Method::OPTIONS && is_api {
        let ok = origin.as_deref().is_some_and(|o| v.origin_ok(o));
        let mut resp = if ok { StatusCode::NO_CONTENT.into_response() } else { deny("bad_origin", "this origin may not call the vault API") };
        if let (true, Some(o)) = (ok, origin.as_deref()) {
            cors(resp.headers_mut(), o);
            resp.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_METHODS, HeaderValue::from_static("GET, POST"));
            resp.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_HEADERS, HeaderValue::from_static("Content-Type"));
            resp.headers_mut().insert(header::ACCESS_CONTROL_MAX_AGE, HeaderValue::from_static("600"));
        }
        return secure(&v, resp);
    }
    let mut resp = next.run(req).await;
    if let Some(o) = origin.as_deref().filter(|o| is_api && v.board_origins.iter().any(|b| b.trim_end_matches('/') == *o)) {
        cors(resp.headers_mut(), o);
    }
    secure(&v, resp)
}

/// The security headers every response carries, a preflight included.
fn secure(v: &Vault, mut resp: Response) -> Response {
    let h = resp.headers_mut();
    let fa = v.frame_ancestors();
    let allow = std::iter::once("self".to_string()).chain(v.board_origins.iter().map(|o| format!("\"{o}\""))).collect::<Vec<_>>().join(" ");
    let csp = format!("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors {fa}; base-uri 'none'; form-action 'self'");
    for (k, val) in [
        ("content-security-policy", csp),
        ("permissions-policy", format!("publickey-credentials-get=({allow}), publickey-credentials-create=({allow})")),
        ("referrer-policy", "no-referrer".into()),
        ("x-content-type-options", "nosniff".into()),
        ("cache-control", "no-store".into()),
    ] {
        if let Ok(val) = HeaderValue::from_str(&val) {
            h.insert(k, val);
        }
    }
    resp
}

fn cors(h: &mut HeaderMap, origin: &str) {
    if let Ok(o) = HeaderValue::from_str(origin) {
        h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, o);
        h.insert(header::ACCESS_CONTROL_ALLOW_CREDENTIALS, HeaderValue::from_static("true"));
        h.append(header::VARY, HeaderValue::from_static("Origin"));
    }
}

fn page(v: &Vault, kind: &str, title: &str, data: Value) -> Response {
    let html = PAGE.replace("{{title}}", &html_escape(title)).replace("{{page}}", kind).replace("{{data}}", &html_escape(&data.to_string()));
    let _ = v;
    ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], html).into_response()
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#39;")
}

fn obj(b: &Value) -> Result<&serde_json::Map<String, Value>, Response> {
    b.as_object().ok_or_else(|| deny("bad_request", "the body is a JSON object"))
}

fn sv<'a>(b: &'a Value, k: &str) -> &'a str {
    b.get(k).and_then(Value::as_str).unwrap_or_default()
}

fn ok(v: Result<Value, ApproverError>) -> Response {
    match v {
        Ok(v) => Json(v).into_response(),
        Err(e) => refused(e),
    }
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(f).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

// -- pages -------------------------------------------------------------------------------

#[derive(serde::Deserialize, Default)]
struct SigninQ {
    next: Option<String>,
    frame: Option<String>,
}

async fn signin_page(State(v): S, h: HeaderMap, Query(q): Query<SigninQ>) -> Response {
    let device = cookie_of(&h).and_then(|c| v.approver.session_device(&c).ok().flatten());
    if let (Some(d), Some(next)) = (device.as_deref(), q.next.as_deref().filter(|n| !n.is_empty())) {
        return match v.approver.portal_grant(next, d) {
            Ok(url) => redirect(&url),
            Err(e) => refused(e),
        };
    }
    let portals: Vec<Value> = {
        let mut p: Vec<(&String, &String)> = v.approver.config.portals.iter().collect();
        p.sort();
        p.into_iter().map(|(host, node)| json!({"host": host, "node": node})).collect()
    };
    let data = json!({"signed_in": device.is_some(), "next": q.next, "frame": q.frame.as_deref() == Some("1"),
                      "portals": portals, "board_origins": v.board_origins});
    page(&v, "signin", "Sign in", data)
}

fn redirect(url: &str) -> Response {
    let mut r = StatusCode::SEE_OTHER.into_response();
    if let Ok(u) = HeaderValue::from_str(url) {
        r.headers_mut().insert(header::LOCATION, u);
    }
    r
}

#[derive(serde::Deserialize)]
struct GoQ {
    next: String,
}

/// One portal link: a fresh `portal_login` grant for the page, minted when Tony opens it.
async fn signin_go(State(v): S, h: HeaderMap, Query(q): Query<GoQ>) -> Response {
    let Some(d) = cookie_of(&h).and_then(|c| v.approver.session_device(&c).ok().flatten()) else {
        let back = url::form_urlencoded::Serializer::new(String::new()).append_pair("next", &q.next).finish();
        return redirect(&format!("/signin?{back}"));
    };
    match v.approver.portal_grant(&q.next, &d) {
        Ok(url) => redirect(&url),
        Err(e) => refused(e),
    }
}

async fn modal_approve(State(v): S, Path(request_id): Path<String>) -> Response {
    page(&v, "approve", "Approve", json!({"request_id": request_id, "board_origins": v.board_origins}))
}

#[derive(serde::Deserialize)]
struct TwofaQ {
    count: Option<i64>,
    device: Option<String>,
}

async fn modal_twofa(State(v): S, Query(q): Query<TwofaQ>) -> Response {
    let count = q.count.unwrap_or(1);
    if !crate::approver::BURST_COUNTS.contains(&count) {
        return deny("bad_request", "count is 1, 2 or 4");
    }
    page(&v, "twofa", "2fa", json!({"count": count, "device": q.device, "board_origins": v.board_origins}))
}

async fn enrol_page(State(v): S) -> Response {
    page(&v, "enrol", "Enrol a device", json!({}))
}

async fn devices_page(State(v): S) -> Response {
    page(&v, "devices", "Devices", json!({}))
}

async fn asset(Path(name): Path<String>) -> Response {
    match name.as_str() {
        "vault.js" => ([(header::CONTENT_TYPE, "text/javascript; charset=utf-8")], VAULT_JS).into_response(),
        "vault.css" => ([(header::CONTENT_TYPE, "text/css; charset=utf-8")], VAULT_CSS).into_response(),
        _ => (StatusCode::NOT_FOUND, Json(json!({"error": "not found"}))).into_response(),
    }
}

// -- the API -------------------------------------------------------------------------------

async fn keys(State(v): S) -> Response {
    let a = &v.approver;
    Json(json!({"ack_pub": a.keys.ack_pub_spki_b64u(), "grant_pub": a.keys.grant_pub_b64u(), "rp_id": a.config.rp_id(),
                "origin": a.config.origin, "board_origins": v.board_origins, "peers": v.peers})).into_response()
}

async fn enrol_begin(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.check_origin(&method, &h).and(obj(&b).map(|_| ())) {
        return r;
    }
    let name: String = sv(&b, "name").chars().take(60).collect();
    ok(v.approver.enrol_begin(sv(&b, "token"), &name))
}

async fn enrol_finish(State(v): S, ConnectInfo(peer): ConnectInfo<SocketAddr>, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.check_origin(&method, &h).and(obj(&b).map(|_| ())) {
        return r;
    }
    let ip = client_ip(&v, Some(peer), &h);
    let name: String = sv(&b, "name").chars().take(60).collect();
    let out = v.approver.enrol_finish(sv(&b, "token"), sv(&b, "reg_id"), b.get("credential").unwrap_or(&Value::Null), &name, ip.as_deref());
    match out.and_then(|o| {
        let d = o["device"].as_str().unwrap_or_default().to_string();
        v.approver.session_new(&d, ip.as_deref()).map(|c| (o, c))
    }) {
        Ok((o, c)) => {
            let mut r = Json(o).into_response();
            r.headers_mut().insert(header::SET_COOKIE, set_cookie(&c, crate::approver::SESSION_TTL_S as i64));
            r
        }
        Err(e) => refused(e),
    }
}

async fn enrol_ask(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.check_origin(&method, &h) {
        return r;
    }
    let name: String = sv(&b, "name").chars().take(60).collect();
    ok(v.approver.enrol_ask(&name).await)
}

async fn enrol_status(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.check_origin(&method, &h) {
        return r;
    }
    ok(v.approver.enrol_ask_status(sv(&b, "request_id"), sv(&b, "poll_secret")))
}

async fn signin_begin(State(v): S, method: Method, h: HeaderMap) -> Response {
    if let Err(r) = v.check_origin(&method, &h) {
        return r;
    }
    ok(v.approver.signin_begin())
}

async fn signin_finish(State(v): S, ConnectInfo(peer): ConnectInfo<SocketAddr>, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.check_origin(&method, &h) {
        return r;
    }
    let ip = client_ip(&v, Some(peer), &h);
    match v.approver.signin_finish(sv(&b, "nonce"), b.get("assertion").unwrap_or(&Value::Null), ip.as_deref()) {
        Ok((device, c)) => {
            let next = b.get("next").and_then(Value::as_str).filter(|n| !n.is_empty());
            let redirect = next.map(|n| v.approver.portal_grant(n, &device)).transpose();
            match redirect {
                Ok(r) => {
                    let mut resp = Json(json!({"ok": true, "device": device, "redirect": r})).into_response();
                    resp.headers_mut().insert(header::SET_COOKIE, set_cookie(&c, crate::approver::SESSION_TTL_S as i64));
                    resp
                }
                Err(e) => refused(e),
            }
        }
        Err(e) => refused(e),
    }
}

async fn session(State(v): S, method: Method, h: HeaderMap) -> Response {
    match v.device(&method, &h) {
        Ok(d) => Json(json!({"ok": true, "device": d})).into_response(),
        Err(r) => r,
    }
}

async fn signout(State(v): S, method: Method, h: HeaderMap) -> Response {
    if let Err(r) = v.check_origin(&method, &h) {
        return r;
    }
    if let Some(c) = cookie_of(&h) {
        let _ = v.approver.session_end(&c);
    }
    let mut r = Json(json!({"ok": true})).into_response();
    r.headers_mut().insert(header::SET_COOKIE, set_cookie("", 0));
    r
}

/// An everyday action: the cookie alone, counted against the rate signal.
fn everyday(v: &Vault, method: &Method, h: &HeaderMap, action: &str) -> Result<String, Response> {
    let d = v.device(method, h)?;
    v.approver.note_action(&d, action).map_err(refused)?;
    Ok(d)
}

async fn retire(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = everyday(&v, &method, &h, "retire") {
        return r;
    }
    ok(v.approver.retire(sv(&b, "node"), sv(&b, "session_id")))
}

async fn agent(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = everyday(&v, &method, &h, "agent") {
        return r;
    }
    let pick = |key| b.get(key).and_then(Value::as_str);
    let note = pick("note_id").filter(|s| !s.is_empty());
    ok(v.approver.agent(sv(&b, "node"), pick("model"), pick("effort"), note).await)
}

async fn card_action(State(v): S, method: Method, h: HeaderMap, Path((task_id, action)): Path<(String, String)>) -> Response {
    if let Err(r) = everyday(&v, &method, &h, &action) {
        return r;
    }
    let store = v.approver.store.clone();
    let out = match action.as_str() {
        "close" => blocking(move || store.card_close(&task_id)).await,
        _ => return deny("unknown_action", "a card closes"),
    };
    ok(out.map_err(ApproverError::from))
}

async fn seat_rc(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = everyday(&v, &method, &h, "seat_remote_control") {
        return r;
    }
    let on = b.get("on").is_some_and(crate::service::truthy);
    social(&v, "seat_remote_control", json!({"seat": sv(&b, "seat"), "on": on})).await
}

/// An Ops seat switch, answered by social.
async fn social(v: &Vault, verb: &str, args: Value) -> Response {
    let Some(hub) = v.hub.as_ref() else { return deny("failed", "no gateway") };
    match hub.call("social", verb, args, None).await {
        Ok(out) => Json(out).into_response(),
        Err(e) => deny("failed", &e.to_string()),
    }
}

async fn seat_refresh(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = everyday(&v, &method, &h, "seat_refresh") {
        return r;
    }
    social(&v, "seat_refresh", json!({"seat": sv(&b, "seat"), "model": sv(&b, "model"), "effort": sv(&b, "effort")})).await
}

async fn service_toggle(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    let d = match everyday(&v, &method, &h, "service_toggle") {
        Ok(d) => d,
        Err(r) => return r,
    };
    ok(v.approver.service_toggle(&d, sv(&b, "node"), sv(&b, "service"), b.get("on").is_some_and(crate::service::truthy)).await)
}

async fn request_reject(State(v): S, method: Method, h: HeaderMap, Path(request_id): Path<String>) -> Response {
    let d = match everyday(&v, &method, &h, "reject_request") {
        Ok(d) => d,
        Err(r) => return r,
    };
    ok(v.approver.reject(&request_id, &d).await)
}

async fn request_view(State(v): S, method: Method, h: HeaderMap, Path(request_id): Path<String>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(v.approver.approval_view(&request_id))
}

async fn request_options(State(v): S, method: Method, h: HeaderMap, Path(request_id): Path<String>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(v.approver.approve_options(&request_id))
}

async fn request_approve(State(v): S, method: Method, h: HeaderMap, Path(request_id): Path<String>, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(for_browser(v.approver.approve(&request_id, b.get("assertion").unwrap_or(&Value::Null)).await))
}

async fn twofa_state(State(v): S, method: Method, h: HeaderMap) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(v.approver.twofa_state().await)
}

async fn twofa_options(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(v.approver.burst_options(b.get("count").and_then(Value::as_i64).unwrap_or(0), sv(&b, "device")))
}

async fn twofa_burst(State(v): S, method: Method, h: HeaderMap, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    let count = b.get("count").and_then(Value::as_i64).unwrap_or(0);
    ok(v.approver.burst(sv(&b, "nonce"), count, sv(&b, "device"), b.get("assertion").unwrap_or(&Value::Null)).await)
}

async fn devices(State(v): S, method: Method, h: HeaderMap) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    ok(v.approver.devices())
}

async fn revoke_options(State(v): S, method: Method, h: HeaderMap, Path(device_id): Path<String>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    let n = v.approver.nonce();
    ok(v.approver.passkey_options(&revoke_challenge(&n, &device_id)).map(|o| json!({"nonce": n, "options": o})))
}

/// The challenge a device revoke's passkey signs.
pub fn revoke_challenge(nonce: &str, device_id: &str) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    Sha256::digest(awm_trust::grant::canonical(&json!({"kind": "device_revoke", "device_id": device_id, "nonce": nonce}))).to_vec()
}

async fn revoke(State(v): S, method: Method, h: HeaderMap, Path(device_id): Path<String>, Json(b): Json<Value>) -> Response {
    if let Err(r) = v.device(&method, &h) {
        return r;
    }
    let n = sv(&b, "nonce");
    let out = v.approver.spend(n).and_then(|_| v.approver.check_passkey(b.get("assertion").unwrap_or(&Value::Null), &revoke_challenge(n, &device_id)))
        .and_then(|_| v.approver.device_revoke(&device_id));
    ok(out)
}

async fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"error": "not found"}))).into_response()
}

pub fn router(v: Arc<Vault>) -> Router {
    Router::new()
        .route("/signin", get(signin_page))
        .route("/signin/go", get(signin_go))
        .route("/enrol", get(enrol_page))
        .route("/devices", get(devices_page))
        .route("/modal/approve/{request_id}", get(modal_approve))
        .route("/modal/twofa", get(modal_twofa))
        .route("/static/{name}", get(asset))
        .route("/api/keys", get(keys))
        .route("/api/enrol/begin", post(enrol_begin))
        .route("/api/enrol/finish", post(enrol_finish))
        .route("/api/enrol/ask", post(enrol_ask))
        .route("/api/enrol/status", post(enrol_status))
        .route("/api/signin/begin", post(signin_begin))
        .route("/api/signin/finish", post(signin_finish))
        .route("/api/session", get(session))
        .route("/api/signout", post(signout))
        .route("/api/agent", post(agent))
        .route("/api/session/retire", post(retire))
        .route("/api/card/{task_id}/{action}", post(card_action))
        .route("/api/seat/remote_control", post(seat_rc))
        .route("/api/seat/refresh", post(seat_refresh))
        .route("/api/service/toggle", post(service_toggle))
        .route("/api/request/{request_id}", get(request_view))
        .route("/api/request/{request_id}/options", post(request_options))
        .route("/api/request/{request_id}/approve", post(request_approve))
        .route("/api/request/{request_id}/reject", post(request_reject))
        .route("/api/twofa", get(twofa_state))
        .route("/api/twofa/options", post(twofa_options))
        .route("/api/twofa/burst", post(twofa_burst))
        .route("/api/devices", get(devices))
        .route("/api/device/{device_id}/revoke/options", post(revoke_options))
        .route("/api/device/{device_id}/revoke", post(revoke))
        .fallback(not_found)
        .layer(middleware::from_fn_with_state(v.clone(), headers))
        .with_state(v)
}

/// Serve the vault on `bind` (plain HTTP on loopback; alcor's front ends TLS).
pub async fn serve(v: Arc<Vault>, bind: &str) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let listener = tokio::net::TcpListener::bind(bind).await?;
    let app = router(v).into_make_service_with_connect_info::<SocketAddr>();
    Ok(tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!("operations: the vault server stopped: {e}");
        }
    }))
}

/// A decided request as the browser sees it: what was decided, never the grant or cert it
/// minted, which only the requester collects.
fn for_browser(out: Result<Value, ApproverError>) -> Result<Value, ApproverError> {
    out.map(|mut v| {
        if let Some(m) = v.as_object_mut() {
            for k in ["grant", "ssh_cert", "cert", "enrol_token", "poll_secret_sha"] {
                m.remove(k);
            }
        }
        v
    })
}
