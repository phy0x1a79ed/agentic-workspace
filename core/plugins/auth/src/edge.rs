//! The browser edge: the session gate in front of the gateway, served over hyper on the streams
//! [`crate::tls`] hands out (the port of `awm.tlsfront.proxy`).
//!
//! Routing reads the decoded path and forwarding sends the raw one. Neither may be simplified into
//! the other: the decoded path is what the caller means, but rebuilding a URL from it loses the
//! difference between `%23` and `#` and between `%2e%2e` and a directory climb. [`re_segments`]
//! closes the gap by refusing a target that means one thing to the router and another upstream.
//!
//! A [`Dispatch`] decides what happens to a request after the edge's own routes.

use std::future::Future;
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Empty, Full};
use hyper::body::Incoming;
use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use hyper::http::request::Parts;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::upgrade::OnUpgrade;
use hyper::{Method, StatusCode, Uri};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use serde_json::{json, Value};
use tokio::net::TcpStream;

use crate::gate::{bearer_of, AuthGate, Decision, AS_COOKIE_NAME, COOKIE_NAME};
use crate::portal::{self, Grants};
use crate::tls::EdgeStream;
use crate::tokens::{looks_like_token, Shape};

pub type BoxError = Box<dyn std::error::Error + Send + Sync>;
pub type Body = BoxBody<Bytes, BoxError>;
pub type Response = hyper::Response<Body>;
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
pub type Upstream = Client<HttpConnector, Body>;

/// Hop-by-hop headers (RFC 7230 6.1), which are not forwarded. `host` goes too: the client
/// derives it from the upstream URL.
pub const HOP: [&str; 9] = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"];

/// Headers the gateway reads as who is calling or which session this is. None is a browser's to
/// present, and `x-awm-as` is written only by the edge.
pub const CALLER_HEADERS: [&str; 8] = [
    "x-awm-as",
    "x-awm-caller-pid",
    "x-awm-session-pid",
    "x-awm-peer-redirect",
    "x-awm-service-token",
    "x-awm-caller-token",
    "x-awm-caller",
    "x-awm-edge-token",
];

/// The methods the edge forwards.
const METHODS: [&str; 7] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"];

const WS_OPEN_TIMEOUT: Duration = Duration::from_secs(10);
/// uvicorn's keep-alive timeout.
const KEEP_ALIVE: Duration = Duration::from_secs(75);

pub struct EdgeConfig {
    /// The gateway's origin, with no trailing slash.
    pub upstream: String,
    /// The root `/ca.crt` serves. Empty when no cert dir exists.
    pub ca_path: PathBuf,
    /// Off on the public profile.
    pub ca_download: bool,
    pub same_site: &'static str,
    /// Plain HTTP on loopback behind a TLS-terminating proxy, which names the client in
    /// `X-Forwarded-For`.
    pub behind_proxy: bool,
    /// Upstream cookies dropped when the awm identity changes, at sign-in and sign-out.
    pub identity_cookies: &'static [&'static str],
}

/// One request, with the paths the edge routes on.
pub struct Req {
    pub parts: Parts,
    pub body: Incoming,
    pub peer: SocketAddr,
    /// The decoded path, for routing and policy.
    pub path: String,
    /// The path as it arrived, for forwarding.
    pub raw: Vec<u8>,
    pub query: Vec<u8>,
}

impl Req {
    pub fn header(&self, name: &str) -> Option<String> {
        header(&self.parts.headers, name)
    }

    /// The body for an upstream leg: a body of known length streams through, because a peer's
    /// file hop can carry gigabytes. Any other is read first, so the upstream leg is framed by
    /// length and a caller's `Transfer-Encoding` never reaches it.
    pub async fn relayed_body(self) -> Result<Body, Response> {
        let streamed = self.parts.headers.get("content-length").is_some_and(|v| !v.is_empty() && v.as_bytes() != b"0");
        if streamed {
            return Ok(self.body.map_err(Into::into).boxed());
        }
        match self.body.collect().await {
            Ok(c) => Ok(full(c.to_bytes())),
            Err(_) => Err(text(400, "bad request body")),
        }
    }

    pub fn shape(&self, websocket: bool) -> Shape<'_> {
        Shape { method: self.parts.method.as_str(), path: &self.path, raw: &self.raw, query: &self.query, websocket }
    }
}

/// What happens to a request after the edge's own routes (`/ca.crt`, `/__auth/*`).
pub trait Dispatch: Send + Sync + 'static {
    fn http<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response>;
    fn ws<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response>;
}

/// How [`Edge::forward`] differs from relaying the request as it came.
#[derive(Default)]
pub struct Forward<'a> {
    /// A body the caller has already read, sent in place of the request's own.
    pub body: Option<Bytes>,
    /// The answer when the upstream cannot be reached, in place of the 502.
    pub unreachable: Option<&'a (dyn Fn() -> Response + Sync)>,
}

pub struct Edge {
    pub gate: Arc<AuthGate>,
    pub grants: Arc<Grants>,
    pub config: EdgeConfig,
    client: Upstream,
    dispatch: Box<dyn Dispatch>,
}

impl Edge {
    pub fn new(gate: Arc<AuthGate>, grants: Arc<Grants>, config: EdgeConfig, dispatch: Box<dyn Dispatch>) -> Edge {
        Edge { gate, grants, config, client: client(None, usize::MAX), dispatch }
    }

    async fn handle(self: Arc<Self>, req: hyper::Request<Incoming>, peer: SocketAddr) -> Response {
        let (parts, body) = req.into_parts();
        let raw = match parts.uri.path() {
            "" => b"/".to_vec(),
            p => p.as_bytes().to_vec(),
        };
        let query = parts.uri.query().unwrap_or("").as_bytes().to_vec();
        let req = Req { path: unquote(&raw), raw, query, parts, body, peer };
        if is_websocket(&req.parts.headers) {
            return self.dispatch.ws(&self, req).await;
        }
        if let Some(own) = self.own_route(&req) {
            return own;
        }
        if !METHODS.contains(&req.parts.method.as_str()) {
            return with_header(text(405, "Method Not Allowed"), "allow", "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT");
        }
        self.dispatch.http(&self, req).await
    }

    /// The routes the edge answers itself, ahead of any dispatch. A method a route does not
    /// take falls through to the dispatch, as a catch-all behind it would.
    fn own_route(&self, req: &Req) -> Option<Response> {
        let get = matches!(req.parts.method, Method::GET | Method::HEAD);
        match req.path.as_str() {
            "/ca.crt" | "/ca.pem" if get && self.config.ca_download => Some(self.ca()),
            portal::CALLBACK if get => Some(self.portal_callback(req)),
            "/__auth/logout" if get || req.parts.method == Method::POST => Some(self.logout()),
            "/__auth/whoami" if get => Some(self.whoami(req)),
            _ => None,
        }
    }

    // -- authentication ------------------------------------------------------------------

    pub fn authenticate(&self, req: &Req, shape: Option<&Shape<'_>>) -> Decision {
        let cookie = cookie_of(&req.parts.headers, COOKIE_NAME);
        let authorization = req.header("authorization");
        self.gate.authenticate(cookie.as_deref(), bearer_of(authorization.as_deref()), shape, crate::now())
    }

    /// The approver's passkey sign-in for a browser GET, else 401.
    pub fn deny(&self, req: &Req) -> Response {
        let html = req.header("accept").is_some_and(|a| a.contains("text/html"));
        if req.parts.method == Method::GET && html {
            let host = req.header("host").unwrap_or_default();
            let path = portal::local_path(Some(&req.path)).unwrap_or("/");
            return redirect(302, &portal::login_url(&format!("https://{host}{path}")), &[("cache-control", "no-store")]);
        }
        json_reply(401, &json!({"error": "unauthenticated"}))
    }

    /// Set the slid session cookie again.
    pub fn refresh_cookie(&self, resp: &mut Response, refreshed: Option<&str>) {
        if let Some(token) = refreshed {
            let ttl = self.gate.session_ttl_seconds() as i64;
            add_cookie(resp, &set_cookie(COOKIE_NAME, token, Some(ttl), "/", true, self.config.same_site));
        }
    }

    // -- the edge's own routes -----------------------------------------------------------

    /// The local root, so a device can trust the edge once. No session: that is how a device
    /// comes to trust it.
    fn ca(&self) -> Response {
        match std::fs::read(&self.config.ca_path) {
            Ok(pem) => with_header(reply(200, "application/x-x509-ca-cert", pem), "content-disposition", r#"attachment; filename="awm-ca.crt""#),
            Err(_) => text(404, "CA not available"),
        }
    }

    /// `GET /__auth/portal?grant=&next=`: the approver's way back. The only success is a 302 to
    /// `next` with the session set, so nothing renders under the address that carried the grant.
    fn portal_callback(&self, req: &Req) -> Response {
        let no_store = |r: Response| with_header(with_header(r, "cache-control", "no-store"), "referrer-policy", "no-referrer");
        let next = query_param(&req.query, "next");
        let Some(path) = portal::local_path(next.as_deref()) else {
            return no_store(json_reply(400, &json!({"error": "next is a local path"})));
        };
        let grant = query_param(&req.query, "grant").unwrap_or_default();
        let sub = match self.grants.accept(&grant, &portal::host_of(req.header("host").as_deref()), path) {
            Ok(sub) => sub,
            Err(e) => {
                tracing::info!("edge: portal grant refused: {e}");
                return no_store(json_reply(403, &json!({"error": "the sign-in grant is refused"})));
            }
        };
        let Some((token, ttl)) = Some(&sub).filter(|s| !s.is_empty()).and_then(|s| self.gate.mint_session(s, crate::now())) else {
            return no_store(json_reply(503, &json!({"error": "no session can be signed here"})));
        };
        let site = self.config.same_site;
        let mut resp = no_store(redirect(302, &quote(path, LOCATION_SAFE), &[]));
        add_cookie(&mut resp, &set_cookie(COOKIE_NAME, &token, Some(ttl), "/", true, site));
        add_cookie(&mut resp, &set_cookie(AS_COOKIE_NAME, &sub, Some(ttl), "/", false, site));
        self.clear_identity_cookies(&mut resp);
        resp
    }

    fn logout(&self) -> Response {
        let mut resp = json_reply(200, &json!({"ok": true}));
        add_cookie(&mut resp, &delete_cookie(COOKIE_NAME));
        add_cookie(&mut resp, &delete_cookie(AS_COOKIE_NAME));
        self.clear_identity_cookies(&mut resp);
        resp
    }

    fn whoami(&self, req: &Req) -> Response {
        match self.authenticate(req, None).sub() {
            Some(sub) => json_reply(200, &json!({"user": sub})),
            None => json_reply(401, &json!({"error": "unauthenticated"})),
        }
    }

    pub fn clear_identity_cookies(&self, resp: &mut Response) {
        for name in self.config.identity_cookies {
            add_cookie(resp, &delete_cookie(name));
        }
    }

    // -- relaying ------------------------------------------------------------------------

    /// Relay one authenticated request to `up` as `sub`. A body of known length streams
    /// through, because a peer's file hop can carry gigabytes. Any other is read first, so the
    /// upstream leg is framed by length and a caller's `Transfer-Encoding` never reaches it.
    pub async fn forward(&self, req: Req, up: &str, raw: &[u8], sub: &str, refreshed: Option<&str>, opts: Forward<'_>) -> Response {
        let Some(uri) = target(up, raw, &req.query) else {
            return not_found();
        };
        let mut headers = self.upstream_headers(&req, Some(sub));
        let (path, method) = (req.path.clone(), req.parts.method.clone());
        let body = match opts.body {
            Some(read) => {
                headers.remove("content-length");
                full(read)
            }
            None => match req.relayed_body().await {
                Ok(body) => body,
                Err(refusal) => return refusal,
            },
        };
        match self.send(&method, uri, headers, body).await {
            Ok(mut resp) => {
                self.refresh_cookie(&mut resp, refreshed);
                resp
            }
            Err(e) => {
                tracing::debug!("edge: upstream failed for {path}: {e}");
                opts.unreachable.map_or_else(|| text(502, "upstream gateway unreachable"), |answer| answer())
            }
        }
    }

    /// One request on the shared upstream client.
    pub async fn send(&self, method: &Method, uri: Uri, headers: HeaderMap, body: Body) -> Result<Response, BoxError> {
        relay(&self.client, method, uri, headers, body).await
    }

    /// The request headers the upstream sees: the browser's, less the hop and caller headers, and
    /// the identity the edge asserts, when there is one.
    pub fn upstream_headers(&self, req: &Req, sub: Option<&str>) -> HeaderMap {
        let mut out = HeaderMap::new();
        for (k, v) in &req.parts.headers {
            if !HOP.contains(&k.as_str()) && !CALLER_HEADERS.contains(&k.as_str()) {
                out.append(k.clone(), v.clone());
            }
        }
        if consumed_edge_token(req.header("authorization").as_deref()) {
            out.remove("authorization");
        }
        out.insert("x-forwarded-proto", HeaderValue::from_static("https"));
        if let Some(sub) = sub {
            out.insert("x-awm-as", as_header(sub));
        }
        // `host` is dropped, which leaves a reverse-proxied third party unable to tell what
        // address the browser used.
        if let Some(host) = req.parts.headers.get("host") {
            out.insert("x-forwarded-host", host.clone());
        }
        if let Ok(ip) = HeaderValue::from_str(&self.client_ip(req)) {
            out.insert("x-forwarded-for", ip);
        }
        out
    }

    pub fn client_ip(&self, req: &Req) -> String {
        if self.config.behind_proxy && req.peer.ip().is_loopback() {
            let hops = req.header("x-forwarded-for").unwrap_or_default();
            let named = hops.rsplit(',').map(str::trim).find(|h| h.parse::<IpAddr>().map_or(!h.is_empty(), |ip| !ip.is_loopback()));
            if let Some(ip) = named {
                return ip.to_string();
            }
        }
        req.peer.ip().to_string()
    }

    /// Open `up` at `raw` with the handshake headers `fwd`, and splice the client's socket to it
    /// once both sides agree. A refused upstream handshake answers the client with its status,
    /// never accept-then-close: the tether relay's clients read a 404 on a held slot.
    pub async fn bridge(&self, mut req: Req, up: &str, raw: &[u8], fwd: HeaderMap) -> Response {
        let (Some(key), Some(version)) = (req.parts.headers.get("sec-websocket-key"), req.parts.headers.get("sec-websocket-version")) else {
            return refuse(400);
        };
        let Some(on_client) = req.parts.extensions.remove::<OnUpgrade>() else {
            return refuse(400);
        };
        let Some(uri) = target(up, raw, &req.query) else {
            return refuse(403);
        };
        let mut handshake = hyper::Request::new(Empty::<Bytes>::new());
        *handshake.uri_mut() = uri.clone();
        let h = handshake.headers_mut();
        h.extend(fwd);
        h.insert("upgrade", HeaderValue::from_static("websocket"));
        h.insert("connection", HeaderValue::from_static("Upgrade"));
        h.insert("sec-websocket-key", key.clone());
        h.insert("sec-websocket-version", version.clone());
        // Subprotocols are negotiated end to end: a browser that asks for one and gets none back
        // fails the connection (RFC 6455).
        if let Some(p) = req.parts.headers.get("sec-websocket-protocol") {
            h.insert("sec-websocket-protocol", p.clone());
        }
        let authority = uri.authority().map(|a| a.as_str().to_string()).unwrap_or_default();
        let Ok(host) = HeaderValue::from_str(&authority) else {
            return refuse(502);
        };
        h.insert("host", host);

        let opened = tokio::time::timeout(WS_OPEN_TIMEOUT, async {
            let tcp = TcpStream::connect(&authority).await?;
            let (mut send, conn) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await?;
            tokio::spawn(conn.with_upgrades());
            Ok::<_, BoxError>(send.send_request(handshake).await?)
        })
        .await;
        // Path only, never the query string.
        let mut resp = match opened {
            Ok(Ok(r)) => r,
            Ok(Err(e)) => {
                tracing::debug!("edge: ws upstream connect failed for {}: {e}", req.path);
                return refuse(502);
            }
            Err(_) => {
                tracing::debug!("edge: ws upstream connect timed out for {}", req.path);
                return refuse(502);
            }
        };
        if resp.status() != StatusCode::SWITCHING_PROTOCOLS {
            let status = resp.status().as_u16();
            return refuse(if (400..600).contains(&status) { status } else { 502 });
        }
        let mut accepted = hyper::Response::new(empty());
        *accepted.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
        let h = accepted.headers_mut();
        h.insert("upgrade", HeaderValue::from_static("websocket"));
        h.insert("connection", HeaderValue::from_static("Upgrade"));
        for name in ["sec-websocket-accept", "sec-websocket-protocol"] {
            if let Some(v) = resp.headers().get(name) {
                h.insert(HeaderName::from_static(name), v.clone());
            }
        }
        let on_up = hyper::upgrade::on(&mut resp);
        tokio::spawn(async move {
            let (Ok(client), Ok(upstream)) = (on_client.await, on_up.await) else { return };
            let _ = tokio::io::copy_bidirectional(&mut TokioIo::new(client), &mut TokioIo::new(upstream)).await;
        });
        accepted
    }
}

/// An upstream client, with `connect` as the time to open a connection and `idle` as the idle
/// connections kept per host.
pub fn client(connect: Option<Duration>, idle: usize) -> Upstream {
    let mut http = HttpConnector::new();
    http.set_nodelay(true);
    http.set_connect_timeout(connect);
    Client::builder(TokioExecutor::new()).pool_max_idle_per_host(idle).build(http)
}

/// Send one request and answer the upstream's response, less its hop-by-hop headers. The body
/// is the upstream's, as it arrives.
pub async fn relay(client: &Upstream, method: &Method, uri: Uri, headers: HeaderMap, body: Body) -> Result<Response, BoxError> {
    let mut out = hyper::Request::new(body);
    *out.method_mut() = method.clone();
    *out.uri_mut() = uri;
    *out.headers_mut() = headers;
    let (head, body) = client.request(out).await?.into_parts();
    let mut resp = hyper::Response::new(body.map_err(Into::into).boxed());
    *resp.status_mut() = head.status;
    for (k, v) in &head.headers {
        if !HOP.contains(&k.as_str()) {
            resp.headers_mut().append(k.clone(), v.clone());
        }
    }
    Ok(resp)
}

// -- serving ------------------------------------------------------------------------------

/// Bind the edge and serve it in the background. With `tls` it listens on every IPv4 interface,
/// else on loopback in plain HTTP.
pub async fn start(edge: Arc<Edge>, port: u16, tls: Option<Arc<rustls::ServerConfig>>) -> std::io::Result<tokio::task::JoinHandle<()>> {
    Ok(match tls {
        Some(config) => {
            let listener = crate::tls::bind_tls(port).await?;
            tokio::spawn(crate::tls::serve_tls(listener, config, move |s, peer| connection(edge.clone(), s, peer)))
        }
        None => {
            let listener = crate::tls::bind_plain(port).await?;
            tokio::spawn(crate::tls::serve_plain(listener, move |s, peer| connection(edge.clone(), s, peer)))
        }
    })
}

async fn connection<I: EdgeStream>(edge: Arc<Edge>, io: I, peer: SocketAddr) {
    let svc = service_fn(move |req| {
        let edge = edge.clone();
        async move { Ok::<_, std::convert::Infallible>(edge.handle(req, peer).await) }
    });
    let served = http1::Builder::new().timer(TokioTimer::new()).header_read_timeout(KEEP_ALIVE).serve_connection(TokioIo::new(io), svc).with_upgrades().await;
    if let Err(e) = served {
        tracing::debug!("edge: connection from {peer} ended: {e}");
    }
}

// -- paths and headers ----------------------------------------------------------------------

/// Whether this target means one thing to the router and another upstream. An encoded
/// separator is one segment here and two there, and a `..`, plain or as `%2e%2e`, is a level up
/// to a client that normalises before sending. Refused outright: nothing the edge serves needs
/// either form.
pub fn re_segments(raw: &[u8], decoded: &str) -> bool {
    let low = raw.to_ascii_lowercase();
    low.windows(3).any(|w| w == b"%2f" || w == b"%5c") || decoded.split('/').any(|s| s == "..")
}

/// `urllib.parse.unquote`: percent escapes decoded as UTF-8, a bad sequence replaced.
fn unquote(raw: &[u8]) -> String {
    let hex = |b: u8| (b as char).to_digit(16);
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        match (raw[i], raw.get(i + 1).and_then(|b| hex(*b)), raw.get(i + 2).and_then(|b| hex(*b))) {
            (b'%', Some(hi), Some(lo)) => {
                out.push((hi * 16 + lo) as u8);
                i += 3;
            }
            (b, ..) => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `up` + the target, byte for byte. `None` when the result is no URI.
pub fn target(up: &str, raw: &[u8], query: &[u8]) -> Option<Uri> {
    let mut url = String::from(up);
    url.push_str(std::str::from_utf8(raw).ok()?);
    if !query.is_empty() {
        url.push('?');
        url.push_str(std::str::from_utf8(query).ok()?);
    }
    url.parse().ok()
}

/// A header's text, read as Latin-1 as Starlette does.
fn header(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get(name).map(|v| v.as_bytes().iter().map(|&b| b as char).collect())
}

/// One cookie of the first `Cookie` header, as Starlette parses it: later duplicates win.
pub fn cookie_of(headers: &HeaderMap, name: &str) -> Option<String> {
    let mut found = None;
    for chunk in header(headers, "cookie")?.split(';') {
        let (key, val) = chunk.split_once('=').unwrap_or(("", chunk));
        let val = val.trim();
        if key.trim() == name {
            found = Some(val.strip_prefix('"').and_then(|v| v.strip_suffix('"')).unwrap_or(val).to_string());
        }
    }
    found
}

/// The last value of `key` in a query string.
pub fn query_param(query: &[u8], key: &str) -> Option<String> {
    url::form_urlencoded::parse(query).filter(|(k, _)| k == key).map(|(_, v)| v.into_owned()).last()
}

fn is_websocket(headers: &HeaderMap) -> bool {
    let has = |name: &str, token: &str| header(headers, name).is_some_and(|v| v.split(',').any(|t| t.trim().eq_ignore_ascii_case(token)));
    has("upgrade", "websocket") && has("connection", "upgrade")
}

/// Whether this `Authorization` is a device token the edge has read. Any `awmut1.` bearer
/// counts, verified or not: one that failed here may still be live where it was meant for, and an
/// upstream app that saw it could replay it there.
pub fn consumed_edge_token(authorization: Option<&str>) -> bool {
    bearer_of(authorization).is_some_and(looks_like_token)
}

/// The identity stamped on a forwarded request.
pub fn as_header(sub: &str) -> HeaderValue {
    HeaderValue::from_str(&format!("user:{sub}")).unwrap_or_else(|_| HeaderValue::from_static("user:operator"))
}

/// The handshake headers a socket carries upstream, before any identity. Only these are copied,
/// so no caller header rides along.
pub fn ws_headers(req: &Req) -> HeaderMap {
    let mut fwd = HeaderMap::new();
    for name in ["cookie", "authorization", "origin"] {
        if let Some(v) = req.parts.headers.get(name).filter(|v| !v.is_empty()) {
            fwd.insert(HeaderName::from_static(name), v.clone());
        }
    }
    if consumed_edge_token(req.header("authorization").as_deref()) {
        fwd.remove("authorization");
    }
    fwd.insert("x-forwarded-proto", HeaderValue::from_static("https"));
    if let Some(host) = req.parts.headers.get("host") {
        fwd.insert("x-forwarded-host", host.clone());
    }
    fwd
}

// -- replies and cookies --------------------------------------------------------------------

pub fn full(bytes: impl Into<Bytes>) -> Body {
    Full::new(bytes.into()).map_err(|never| match never {}).boxed()
}

fn empty() -> Body {
    full(Bytes::new())
}

pub fn reply(status: u16, content_type: &str, body: impl Into<Bytes>) -> Response {
    let mut r = hyper::Response::new(full(body));
    *r.status_mut() = StatusCode::from_u16(status).expect("a valid status");
    r.headers_mut().insert("content-type", HeaderValue::from_str(content_type).expect("a valid content type"));
    r
}

pub fn text(status: u16, body: &str) -> Response {
    reply(status, "text/plain; charset=utf-8", body.to_string())
}

pub fn not_found() -> Response {
    text(404, "not found")
}

pub fn json_reply(status: u16, v: &Value) -> Response {
    reply(status, "application/json", v.to_string())
}

/// A handshake refused with `status`: no body, and no accept first.
pub fn refuse(status: u16) -> Response {
    let mut r = hyper::Response::new(empty());
    *r.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
    r
}

pub fn redirect(status: u16, location: &str, headers: &[(&'static str, &str)]) -> Response {
    let mut r = refuse(status);
    r.headers_mut().insert("location", HeaderValue::from_str(location).unwrap_or_else(|_| HeaderValue::from_static("/")));
    headers.iter().fold(r, |r, (k, v)| with_header(r, k, v))
}

pub fn with_header(mut r: Response, name: &'static str, value: &str) -> Response {
    if let Ok(v) = HeaderValue::from_str(value) {
        r.headers_mut().insert(HeaderName::from_static(name), v);
    }
    r
}

pub fn add_cookie(r: &mut Response, cookie: &str) {
    if let Ok(v) = HeaderValue::from_str(cookie) {
        r.headers_mut().append("set-cookie", v);
    }
}

/// What Starlette leaves alone when it quotes a redirect's `Location`.
const LOCATION_SAFE: &str = ":/%#?=@[]!$&'()*+,;";

/// `urllib.parse.quote(s, safe=safe)`.
fn quote(s: &str, safe: &str) -> String {
    s.bytes().fold(String::with_capacity(s.len()), |mut out, b| {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || safe.as_bytes().contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
        out
    })
}

/// `http.cookies.SimpleCookie`'s value quoting.
fn cookie_value(v: &str) -> String {
    const LEGAL: &str = "!#$%&'*+-.^_`|~:";
    if !v.is_empty() && v.chars().all(|c| c.is_ascii_alphanumeric() || LEGAL.contains(c)) {
        return v.to_string();
    }
    let mut out = String::from('"');
    for c in v.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if c.is_ascii_alphanumeric() || LEGAL.contains(c) || " ()/<=>?@[]{}".contains(c) => out.push(c),
            c if (c as u32) < 256 => out.push_str(&format!("\\{:03o}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `Response.set_cookie` with the attributes in the order `SimpleCookie` writes them.
pub fn set_cookie(name: &str, value: &str, max_age: Option<i64>, path: &str, http_only: bool, same_site: &str) -> String {
    let http_only = if http_only { "HttpOnly; " } else { "" };
    let max_age = max_age.map_or(String::new(), |age| format!("Max-Age={age}; "));
    format!("{name}={}; {http_only}{max_age}Path={path}; SameSite={same_site}; Secure", cookie_value(value))
}

fn delete_cookie(name: &str) -> String {
    format!(r#"{name}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/; SameSite=lax"#)
}
