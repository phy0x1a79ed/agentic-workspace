//! What the node's edge does with a request before the gateway leg (the port of
//! `awm.httpsfront.proxy`): the landing page, the vault, slice and tether mounts, and the
//! public profile's allow-list.
//!
//! The slice and tether mounts are answered before authentication. Each decides by the
//! shape of the path alone and is claimed on every node, wired or not, so a path under one never
//! reaches the gateway.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use awm_plugin_sdk::{CallOpts, Hub};
use bytes::{Bytes, BytesMut};
use http_body_util::BodyExt;
use hyper::body::Incoming;
use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use hyper::Method;
use serde_json::{json, Map, Value};

use crate::edge::{
    self, add_cookie, cookie_of, json_reply, not_found, query_param, redirect, refuse, reply, set_cookie, target, text, with_header, BoxFut,
    Dispatch, Edge, Forward, Req, Response, CALLER_HEADERS, HOP,
};
use crate::gate::is_machine_sub;
use crate::landing::Landing;
use crate::pages::{self, LandingView};
use crate::policy::{self, slices, tether, vault, Verdict};
use crate::tokens::{py_str, truthy};

/// How long a resolved slice token is reused. A page load is hundreds of asset requests, and a
/// revoked link must still stop working while somebody watches it.
const SLICE_RESOLVE_TTL: Duration = Duration::from_secs(5);
const SLICE_CACHE_SWEEP: usize = 4096;

const PLUGINS_TIMEOUT: Duration = Duration::from_secs(10);

/// The loopback origins of the two upstreams other than the gateway, each `None` when off.
#[derive(Debug, Clone, Default)]
pub struct Upstreams {
    pub vault: Option<String>,
    pub tether: Option<String>,
}

impl Upstreams {
    pub fn from_env() -> Upstreams {
        let env = |name: &str, default: &str| std::env::var(name).unwrap_or_else(|_| default.into()).trim().to_lowercase();
        let enabled = |name: &str, default: &str| !matches!(env(name, default).as_str(), "0" | "false" | "no");
        let port = |name: &str, default: u16| std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default);
        let origin = |port: u16| format!("http://127.0.0.1:{port}");
        Upstreams {
            vault: enabled("AWM_EDGE_VAULT", "1").then(|| origin(port("AWM_VAULT_PORT", 12511))),
            tether: enabled("AWM_EDGE_TETHER", "0").then(|| origin(port("AWM_TETHER_PORT", 12520))),
        }
    }
}

/// A slice token's meaning, as the trilium service resolved it.
struct Slice {
    note_id: String,
    /// The visitor the token was minted for. An open token has none.
    user: Option<String>,
    write: bool,
}

/// Token -> (expires, resolved or nothing).
type ResolveCache = HashMap<String, (Instant, Option<Arc<Slice>>)>;

pub struct Front {
    public: bool,
    vault: Option<String>,
    tether: Option<String>,
    hub: Hub,
    plugins: reqwest::Client,
    landing: Landing,
    peer_name: String,
    slices: Mutex<ResolveCache>,
}

enum Route {
    Root,
    PublicHome,
    Landing(Op),
    VaultBare,
    VaultManifest,
    VaultLogout,
}

#[derive(Clone, Copy)]
enum Op {
    AddTag,
    RemoveTag,
    SelectFilter,
    DeselectFilter,
    SetName,
}

impl Front {
    pub fn new(public: bool, upstreams: Upstreams, hub: Hub, landing: Landing) -> Front {
        Front {
            public,
            vault: upstreams.vault,
            tether: upstreams.tether,
            hub,
            plugins: reqwest::Client::builder().no_proxy().timeout(PLUGINS_TIMEOUT).build().expect("reqwest client builds"),
            landing,
            peer_name: title(hostname().split('.').next().unwrap_or_default()),
            slices: Mutex::new(HashMap::new()),
        }
    }

    /// The edge's own answers on paths ahead of the catch-all. A method a route does not take
    /// falls through to it.
    fn route(&self, req: &Req) -> Option<Route> {
        let method = &req.parts.method;
        let get = matches!(*method, Method::GET | Method::HEAD);
        let vault = self.vault.is_some();
        Some(match req.path.as_str() {
            "/" if get => if self.public { Route::PublicHome } else { Route::Root },
            "/__landing/tags" if !self.public => Route::Landing(match *method {
                Method::POST => Op::AddTag,
                Method::DELETE => Op::RemoveTag,
                _ => return None,
            }),
            "/__landing/filter" if !self.public => Route::Landing(match *method {
                Method::POST => Op::SelectFilter,
                Method::DELETE => Op::DeselectFilter,
                _ => return None,
            }),
            "/__landing/name" if !self.public && *method == Method::POST => Route::Landing(Op::SetName),
            vault::SHELL_BARE if vault && get => Route::VaultBare,
            vault::MANIFEST if vault && get => Route::VaultManifest,
            vault::LOGOUT if vault && (get || *method == Method::POST) => Route::VaultLogout,
            _ => return None,
        })
    }

    async fn handle_http(&self, edge: &Edge, req: Req) -> Response {
        match self.route(&req) {
            Some(Route::Root) => self.root(edge, req).await,
            Some(Route::PublicHome) => match edge.authenticate(&req, None).sub() {
                Some(_) => redirect(302, vault::SHELL, &[]),
                None => edge.deny(&req),
            },
            Some(Route::Landing(op)) => self.landing_post(edge, req, op).await,
            Some(Route::VaultBare) => redirect(308, vault::SHELL, &[]),
            Some(Route::VaultManifest) => {
                let refreshed = match gate(edge, &req) {
                    Ok(refreshed) => refreshed,
                    Err(denied) => return denied,
                };
                let mut resp = reply(200, "application/manifest+json", vault::manifest().to_string());
                edge.refresh_cookie(&mut resp, refreshed.as_deref());
                resp
            }
            Some(Route::VaultLogout) => redirect(302, "/__auth/logout", &[]),
            None => self.proxy(edge, req).await,
        }
    }

    // -- the landing page ----------------------------------------------------------------

    async fn root(&self, edge: &Edge, req: Req) -> Response {
        let refreshed = match gate(edge, &req) {
            Ok(refreshed) => refreshed,
            Err(denied) => return denied,
        };
        let entries = self.page_entries(edge).await;
        let names: Vec<String> = entries.iter().map(|e| e.name.clone()).collect();
        let view = (|| -> rusqlite::Result<LandingView> {
            Ok(LandingView {
                tags_by_page: self.landing.tags_by_page(&names)?,
                tag_counts: self.landing.all_tag_counts()?,
                selected_tags: self.landing.selected_tags()?,
                display_names: self.landing.display_names(&names)?,
            })
        })();
        let Ok(view) = view else { return internal() };
        let mut resp = reply(200, "text/html; charset=utf-8", pages::landing_page(&entries, &view, &self.peer_name));
        edge.refresh_cookie(&mut resp, refreshed.as_deref());
        resp
    }

    /// The pages the gateway's loaded plugins serve. An unreachable gateway gives an empty index.
    async fn page_entries(&self, edge: &Edge) -> Vec<pages::PageEntry> {
        let fetched = async {
            let resp = self.plugins.get(format!("{}/hub/plugins", edge.config.upstream)).send().await.ok()?;
            if resp.status() != 200 {
                return None;
            }
            serde_json::from_slice::<Value>(&resp.bytes().await.ok()?).ok()
        };
        match fetched.await {
            Some(snapshot) => pages::page_entries(&snapshot),
            None => {
                tracing::debug!("landing: could not fetch the plugin snapshot");
                Vec::new()
            }
        }
    }

    /// `/__landing/*`: the tag, filter and name edits behind the session.
    async fn landing_post(&self, edge: &Edge, req: Req, op: Op) -> Response {
        let refreshed = match gate(edge, &req) {
            Ok(refreshed) => refreshed,
            Err(denied) => return denied,
        };
        let Ok(body) = req.body.collect().await else { return json_reply(400, &json!({"error": "invalid json"})) };
        let Ok(data) = serde_json::from_slice::<Value>(&body.to_bytes()) else { return json_reply(400, &json!({"error": "invalid json"})) };
        let mut resp = match self.landing_edit(&data, op) {
            Ok(Ok(answer)) => json_reply(200, &answer),
            Ok(Err(missing)) => json_reply(400, &json!({"error": missing})),
            Err(()) => internal(),
        };
        edge.refresh_cookie(&mut resp, refreshed.as_deref());
        resp
    }

    /// The answer to one edit, or what the body lacks. `Err` where the store or the body shape
    /// fails, which Python answers with a 500.
    fn landing_edit(&self, data: &Value, op: Op) -> Result<Result<Value, &'static str>, ()> {
        let field = |key: &str| -> Result<String, ()> {
            match data {
                Value::Object(m) => Ok(awm_pycompat::pystr::strip(&m.get(key).filter(|v| truthy(v)).map(py_str).unwrap_or_default()).to_string()),
                v if !truthy(v) => Ok(String::new()),
                _ => Err(()),
            }
        };
        let db = |r: rusqlite::Result<()>| r.map_err(|e| tracing::warn!("landing: {e}"));
        let counts = || -> Result<Value, ()> {
            let rows = self.landing.all_tag_counts().map_err(|_| ())?;
            Ok(Value::Object(rows.into_iter().map(|(tag, n)| (tag, json!(n))).collect::<Map<_, _>>()))
        };
        Ok(Ok(match op {
            Op::AddTag | Op::RemoveTag => {
                let (page, tag) = (field("page")?, field("tag")?);
                if page.is_empty() || tag.is_empty() {
                    return Ok(Err("page and tag are required"));
                }
                db(if matches!(op, Op::AddTag) { self.landing.add_tag(&page, &tag) } else { self.landing.remove_tag(&page, &tag) })?;
                json!({"tags": self.landing.tags_for_page(&page).map_err(|_| ())?, "tag_counts": counts()?})
            }
            Op::SelectFilter | Op::DeselectFilter => {
                let tag = field("tag")?;
                if tag.is_empty() {
                    return Ok(Err("tag is required"));
                }
                db(if matches!(op, Op::SelectFilter) { self.landing.select_tag(&tag) } else { self.landing.deselect_tag(&tag) })?;
                json!({"selected_tags": self.landing.selected_tags().map_err(|_| ())?})
            }
            Op::SetName => {
                let (page, name) = (field("page")?, field("name")?);
                if page.is_empty() {
                    return Ok(Err("page is required"));
                }
                db(if name.is_empty() { self.landing.clear_display_name(&page) } else { self.landing.set_display_name(&page, &name) })?;
                json!({"display_name": self.landing.display_name(&page).map_err(|_| ())?})
            }
        }))
    }

    // -- the catch-all -------------------------------------------------------------------

    async fn proxy(&self, edge: &Edge, mut req: Req) -> Response {
        let raw = req.raw.clone();
        // Before any upstream branch, or the gateway leg keeps the hole.
        if edge::re_segments(&raw, &req.path) {
            return not_found();
        }
        let verdict = self.public.then(|| policy::classify(&req.path));
        if verdict == Some(Verdict::Deny) {
            return not_found();
        }
        // `/invoke` is called with POST and nothing else, signed in or not.
        if verdict == Some(Verdict::Invoke) && req.parts.method != Method::POST {
            return not_found();
        }
        if let Some(vault_up) = &self.vault {
            // Not left to the policy door: this node may run no profile.
            if slices::refused(&req.path) {
                return not_found();
            }
            if slices::owns(&req.path) {
                return self.slice_http(edge, req, vault_up).await;
            }
        }
        if tether::refused(&req.path) {
            return not_found();
        }
        if tether::owns(&req.path) {
            return match &self.tether {
                Some(up) => self.tether_http(edge, req, up).await,
                None => not_found(),
            };
        }

        let decision = edge.authenticate(&req, Some(&req.shape(false)));
        let Some(sub) = decision.sub().map(str::to_string) else {
            return edge.deny(&req);
        };
        let refreshed = decision.refreshed().map(str::to_string);
        if self.public && !policy::allows(&req.path, Some(&sub)) {
            return not_found();
        }
        if verdict == Some(Verdict::Invoke) {
            // The body is read under a cap and checked before anything is forwarded, then sent as
            // read, so what was checked is what the gateway runs.
            let Some(content) = capped_body(&req.parts.headers, &mut req.body, policy::INVOKE_BODY_CAP).await else { return not_found() };
            if !policy::invoke_allows(&content) {
                return not_found();
            }
            return edge.forward(req, &edge.config.upstream, &raw, &sub, refreshed.as_deref(), Forward { body: Some(content), ..Forward::default() }).await;
        }
        if let Some(vault_up) = self.vault.as_deref().filter(|_| vault::owns(&req.path)) {
            // The mesh edge runs no allow-list, so the check the public profile makes in
            // `policy::allows` is made here instead.
            if !self.public && is_machine_sub(Some(&sub)) {
                return not_found();
            }
            // The mount is in the decoded path but not in the bytes.
            let Some(inner) = vault::upstream_raw_path(&raw) else { return not_found() };
            let html = wants_html(&req);
            let unavailable = || vault_unavailable(html, "not listening yet");
            return edge.forward(req, vault_up, &inner, &sub, refreshed.as_deref(), Forward { unreachable: Some(&unavailable), ..Forward::default() }).await;
        }
        edge.forward(req, &edge.config.upstream, &raw, &sub, refreshed.as_deref(), Forward::default()).await
    }

    // -- the slice mount -----------------------------------------------------------------

    /// A public slice of the vault: the same child, a narrower door. Everything is decided from
    /// the token. No session is read or minted, and the one cookie read names an open slice's
    /// visitor.
    async fn slice_http(&self, edge: &Edge, req: Req, vault_up: &str) -> Response {
        let Some(token) = slices::token_of(&req.path).map(str::to_string) else { return not_found() };
        // 404 rather than 403: a revoked, expired or unknown slice looks like a URL that never existed.
        let Some(info) = self.resolve(&token).await else { return not_found() };
        if req.path == slices::shell_bare(&token) {
            return redirect(308, &slices::shell(&token), &[]);
        }
        let Some(inner) = slices::upstream_raw_path(&req.raw) else { return not_found() };
        let Ok(visitor) = slice_visitor(&req, &info, &token) else { return text(400, "this link names a different visitor") };
        let Some(uri) = target(vault_up, &inner, &req.query) else { return not_found() };

        let mut headers = HeaderMap::new();
        for (k, v) in &req.parts.headers {
            let low = k.as_str();
            if !HOP.contains(&low) && !CALLER_HEADERS.contains(&low) && !low.starts_with("x-awm-slice-") {
                headers.append(k.clone(), v.clone());
            }
        }
        headers.insert("x-forwarded-proto", HeaderValue::from_static("https"));
        if let Some(host) = req.parts.headers.get("host") {
            headers.insert("x-forwarded-host", host.clone());
        }
        if let Ok(ip) = HeaderValue::from_str(&edge.client_ip(&req)) {
            headers.insert("x-forwarded-for", ip);
        }
        slice_identity(&mut headers, &info, visitor.as_deref());

        let (method, html) = (req.parts.method.clone(), wants_html(&req));
        let remember = remember_visitor(&req, &info, &token, visitor.as_deref(), edge.config.same_site);
        let body = match req.relayed_body().await {
            Ok(body) => body,
            Err(refusal) => return refusal,
        };
        match edge.send(&method, uri, headers, body).await {
            Ok(mut resp) => {
                if let Some(cookie) = remember {
                    add_cookie(&mut resp, &cookie);
                }
                resp
            }
            Err(_) => vault_unavailable(html, "not listening yet"),
        }
    }

    /// What `token` opens, or nothing. Asked of the trilium service over the gateway as this
    /// service, with no identity: the gateway runs it on the service lane, which that service
    /// admits. A transport failure is not cached.
    async fn resolve(&self, token: &str) -> Option<Arc<Slice>> {
        if let Some((expires, info)) = self.slices.lock().unwrap_or_else(|e| e.into_inner()).get(token) {
            if *expires > Instant::now() {
                return info.clone();
            }
        }
        let call = json!({"name": "trilium", "args": {"verb": "slice_resolve", "args": {"token": token}}});
        let opts = CallOpts { timeout: Duration::from_secs(5), ..CallOpts::default() };
        let (status, body) = match self.hub.invoke_raw(&call, &opts).await {
            Ok(answer) => answer,
            Err(e) => {
                tracing::debug!("slice resolve failed: {e}");
                return None;
            }
        };
        let info = (status == 200).then(|| slice_of(body.get("result")?)).flatten().map(Arc::new);
        let mut cache = self.slices.lock().unwrap_or_else(|e| e.into_inner());
        let now = Instant::now();
        if cache.len() >= SLICE_CACHE_SWEEP {
            cache.retain(|_, (expires, _)| *expires > now);
        }
        cache.insert(token.to_string(), (now + SLICE_RESOLVE_TTL, info.clone()));
        info
    }

    // -- the tether mount ----------------------------------------------------------------

    /// Forward one request to the tether relay: no session, and no opinion. `Authorization` rides
    /// untouched, because it is the relay's credential, not awm's. `X-Awm-As` is left off rather
    /// than defaulted: this leg has no verified identity to state. The client's address still
    /// arrives in the forwarded headers, which the relay counts its per-address budget on.
    async fn tether_http(&self, edge: &Edge, req: Req, up: &str) -> Response {
        let Some(inner) = tether::upstream_raw_path(&req.raw) else { return not_found() };
        let Some(uri) = target(up, &inner, &req.query) else { return not_found() };
        let (method, headers) = (req.parts.method.clone(), edge.upstream_headers(&req, None));
        let body = match req.relayed_body().await {
            Ok(body) => body,
            Err(refusal) => return refusal,
        };
        // The relay answers 404 to everything it declines, so a relay that is down and a slot
        // that never existed are one reply.
        edge.send(&method, uri, headers, body).await.unwrap_or_else(|_| not_found())
    }

    // -- sockets -------------------------------------------------------------------------

    async fn handle_ws(&self, edge: &Edge, req: Req) -> Response {
        let (path, raw) = (req.path.clone(), req.raw.clone());
        if edge::re_segments(&raw, &path) {
            return refuse(403);
        }
        let is_vault = self.vault.is_some() && vault::owns(&path);
        let is_slice = self.vault.is_some() && slices::owns(&path);
        let is_tether = tether::owns(&path);
        // The tether name is claimed whether or not a relay is wired.
        if tether::refused(&path) || (is_tether && self.tether.is_none()) {
            return refuse(403);
        }
        let mut up = edge.config.upstream.as_str();
        let mut target_raw = raw.clone();
        if is_vault || is_slice || is_tether {
            // The client derives its socket URL from the page's own pathname, so a shell served at
            // /trilium/ opens its socket there. The HTTP leg strips the same mount.
            let inner = if is_vault { vault::upstream_raw_path(&raw) } else if is_slice { slices::upstream_raw_path(&raw) } else { tether::upstream_raw_path(&raw) };
            let Some(inner) = inner else { return refuse(403) };
            up = if is_tether { self.tether.as_deref() } else { self.vault.as_deref() }.unwrap_or_default();
            target_raw = inner;
        }
        // `/invoke` is a POST; on the public door it has no socket form.
        if self.public && matches!(policy::classify(&path), Verdict::Deny | Verdict::Invoke) {
            return refuse(403);
        }

        let mut fwd = edge::ws_headers(&req);
        if is_slice {
            // The token is the credential. The socket outlives the resolve that admitted it, so a
            // revoked slice keeps an open socket until the client reconnects; the vault's mask
            // bounds what reaches it either way.
            let Some(token) = slices::token_of(&path) else { return refuse(403) };
            let Some(info) = self.resolve(token).await else { return refuse(403) };
            let Ok(visitor) = slice_visitor(&req, &info, token) else { return refuse(403) };
            fwd.remove("authorization");
            slice_identity(&mut fwd, &info, visitor.as_deref());
        } else if is_tether {
            // The path's grammar was the check: the relay refuses a slot and ticket it did not
            // issue before the socket opens. It keys its miss budget on the client address.
            if let Some(cf) = req.parts.headers.get("cf-connecting-ip") {
                fwd.insert("cf-connecting-ip", cf.clone());
            }
            if let Ok(ip) = HeaderValue::from_str(&edge.client_ip(&req)) {
                fwd.insert("x-forwarded-for", ip);
            }
        } else {
            let Some(sub) = edge.authenticate(&req, Some(&req.shape(true))).sub().map(str::to_string) else { return refuse(403) };
            // The operator session has the gateway's socket, never the vault's.
            if (self.public && !policy::allows(&path, Some(&sub))) || (is_vault && is_machine_sub(Some(&sub))) {
                return refuse(403);
            }
            fwd.insert("x-awm-as", edge::as_header(&sub));
        }
        edge.bridge(req, up, &target_raw, fwd).await
    }
}

impl Dispatch for Front {
    fn http<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response> {
        Box::pin(self.handle_http(edge, req))
    }

    fn ws<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response> {
        Box::pin(self.handle_ws(edge, req))
    }
}

// -- helpers --------------------------------------------------------------------------------

/// The slid-cookie token when the session holds, else the sign-in answer.
fn gate(edge: &Edge, req: &Req) -> Result<Option<String>, Response> {
    let decision = edge.authenticate(req, None);
    match decision.sub() {
        Some(_) => Ok(decision.refreshed().map(str::to_string)),
        None => Err(edge.deny(req)),
    }
}

fn internal() -> Response {
    edge::plain_text(500, "Internal Server Error")
}

fn wants_html(req: &Req) -> bool {
    req.parts.method == Method::GET && req.header("accept").is_some_and(|a| a.contains("text/html"))
}

/// Why the vault is not answering, as a page rather than a bare 502. The usual answer is that it
/// is still starting, and a cold child takes up to two minutes to bind.
fn vault_unavailable(html: bool, reason: &str) -> Response {
    let retry = |r: Response| with_header(r, "retry-after", "5");
    if html {
        let page = reply(503, "text/html; charset=utf-8", pages::vault_unavailable_page(reason));
        return with_header(retry(page), "cache-control", "no-store");
    }
    retry(json_reply(503, &json!({"error": "vault unavailable", "reason": reason})))
}

fn declared_length(headers: &HeaderMap) -> Option<u128> {
    let v = headers.get("content-length")?.to_str().ok()?;
    (!v.is_empty() && v.bytes().all(|b| b.is_ascii_digit())).then(|| v.parse().unwrap_or(u128::MAX))
}

/// The request body, or `None` once it exceeds `cap` bytes. Checked against the declared length
/// first, then while reading, so a chunked body is stopped as well, and never held past the cap.
async fn capped_body(headers: &HeaderMap, body: &mut Incoming, cap: usize) -> Option<Bytes> {
    if declared_length(headers).is_some_and(|n| n > cap as u128) {
        return None;
    }
    let mut out = BytesMut::new();
    while let Some(frame) = body.frame().await {
        if let Ok(data) = frame.ok()?.into_data() {
            if out.len() + data.len() > cap {
                return None;
            }
            out.extend_from_slice(&data);
        }
    }
    Some(out.freeze())
}

fn slice_of(result: &Value) -> Option<Slice> {
    let parsed;
    let result = match result {
        Value::String(s) => {
            parsed = serde_json::from_str::<Value>(s).ok()?;
            &parsed
        }
        other => other,
    };
    let result = result.as_object()?;
    let note_id = result.get("note_id").filter(|v| truthy(v)).map(py_str)?;
    if !result.get("found").is_some_and(truthy) {
        return None;
    }
    Some(Slice { note_id, user: result.get("user").filter(|v| truthy(v)).map(py_str), write: result.get("write").is_some_and(truthy) })
}

/// Who a slice request is attributed to, or `Err` when the link's `?user=` contradicts the name
/// the token was minted with. An open token learns the name from that parameter on first arrival
/// and from its own cookie afterwards, so one link can be handed to a group.
fn slice_visitor(req: &Req, info: &Slice, token: &str) -> Result<Option<String>, ()> {
    let asked = query_param(&req.query, slices::USER_PARAM).filter(|a| !a.is_empty());
    if let Some(bound) = &info.user {
        return if asked.as_ref().is_some_and(|a| a != bound) { Err(()) } else { Ok(Some(bound.clone())) };
    }
    Ok(asked.or_else(|| cookie_of(&req.parts.headers, &slices::cookie_name(token)).filter(|c| !c.is_empty())))
}

/// The slice's three headers, in place of any the browser sent. Trilium trusts them as it trusts
/// loopback, so a browser must not be able to state any of them.
fn slice_identity(headers: &mut HeaderMap, info: &Slice, visitor: Option<&str>) {
    let set = |headers: &mut HeaderMap, name: &str, value: &str| {
        if let (Ok(name), Ok(value)) = (HeaderName::from_bytes(name.as_bytes()), HeaderValue::from_str(value)) {
            headers.insert(name, value);
        }
    };
    set(headers, slices::HEADER_ROOT, &info.note_id);
    set(headers, slices::HEADER_WRITE, if info.write { "1" } else { "0" });
    if let Some(visitor) = visitor {
        set(headers, slices::HEADER_USER, visitor);
    }
}

/// The cookie that keeps an open slice's visitor name across the visit: only when the name
/// arrived in this request's query, scoped to this slice's own path so the edge's sign-in and
/// sign-out cannot clear it.
fn remember_visitor(req: &Req, info: &Slice, token: &str, visitor: Option<&str>, same_site: &str) -> Option<String> {
    let visitor = visitor.filter(|v| !v.is_empty() && info.user.is_none())?;
    (query_param(&req.query, slices::USER_PARAM).as_deref() == Some(visitor)).then(|| set_cookie(&slices::cookie_name(token), visitor, None, &slices::shell(token), true, same_site))
}

fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_default()
}

/// `str.title()`.
fn title(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_word = false;
    for c in s.chars() {
        if c.is_alphabetic() {
            if in_word { out.extend(c.to_lowercase()) } else { out.extend(c.to_uppercase()) }
        } else {
            out.push(c);
        }
        in_word = c.is_alphabetic();
    }
    out
}
