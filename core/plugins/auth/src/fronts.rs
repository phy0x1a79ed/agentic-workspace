//! The `[[service.front]]` listeners (ports/auth.md C2): one [`Edge`] per declaration, in front of
//! a service's loopback upstream, opened and closed as the loaded services change.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use awm_plugin_sdk::{Hub, SubscriptionOpts, SupervisedSubscription};
use awm_proto::manifest::FrontDecl;
use hyper::header::HeaderValue;
use hyper::Uri;
use rustls::ServerConfig;
use serde_json::{json, Value};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;

use crate::certs;
use crate::edge::{self, not_found, reply, with_header, re_segments, refuse, BoxFut, Dispatch, Edge, EdgeConfig, Forward, Req, Response};
use crate::gate::AuthGate;
use crate::portal::Grants;

/// One upstream a front relays to, with the `Origin` that upstream expects, if it checks one.
struct Door {
    prefix: String,
    up: String,
    origin: Option<HeaderValue>,
}

/// What a front does after the edge's own routes: gate the request, then relay it.
pub struct Relay {
    /// Longest prefix first.
    doors: Vec<Door>,
    wake: Option<Wake>,
}

/// A front's on-demand start: the URL that starts the service and when it was last sent.
struct Wake {
    url: String,
    client: reqwest::Client,
    last: std::sync::Mutex<Option<Instant>>,
}

/// The longest gap between wakes while traffic flows.
const WAKE_EVERY: Duration = Duration::from_secs(60);

impl Wake {
    fn new(url: &str) -> Wake {
        let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(5)).build().expect("reqwest client builds");
        Wake { url: url.to_string(), client, last: std::sync::Mutex::new(None) }
    }

    /// Send the wake for `sub` unless one went out in the last minute, or `now` is set.
    fn send(&self, sub: &str, now: bool) {
        {
            let mut last = self.last.lock().unwrap_or_else(|e| e.into_inner());
            if !now && last.is_some_and(|t| t.elapsed() < WAKE_EVERY) {
                return;
            }
            *last = Some(Instant::now());
        }
        let (client, url, sub) = (self.client.clone(), self.url.clone(), edge::as_header(sub));
        tokio::spawn(async move {
            if let Err(e) = client.get(&url).header("x-awm-as", sub.as_bytes()).send().await {
                tracing::debug!("auth: wake {url} failed: {e}");
            }
        });
    }
}

fn starting_response(html: bool) -> Response {
    let busy = |r: Response| with_header(with_header(r, "retry-after", "3"), "cache-control", "no-store");
    if html {
        const PAGE: &str = "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><title>Starting</title><body style=\"font-family:sans-serif;margin:4em\"><p>Starting the workbench&hellip;</p>";
        return busy(reply(503, "text/html; charset=utf-8", PAGE));
    }
    busy(edge::plain_text(503, "the workbench is starting"))
}

fn origin_of(up: &str) -> Option<HeaderValue> {
    let uri = up.parse::<Uri>().ok()?;
    HeaderValue::from_str(&format!("http://{}", uri.authority()?)).ok()
}

impl Relay {
    pub fn new(decl: &FrontDecl) -> Relay {
        let door = |prefix: &str, up: &str| {
            let up = up.trim_end_matches('/').to_string();
            Door { prefix: prefix.trim_end_matches('/').to_string(), origin: if decl.rewrite_origin { origin_of(&up) } else { None }, up }
        };
        let mut doors: Vec<Door> = decl.paths.iter().map(|(p, up)| door(p, up)).collect();
        doors.push(door("/", &decl.upstream));
        doors.sort_by_key(|d| std::cmp::Reverse(d.prefix.len()));
        Relay { doors, wake: decl.wake.as_deref().map(Wake::new) }
    }

    fn door(&self, path: &str) -> &Door {
        let inside = |d: &&Door| path.strip_prefix(d.prefix.as_str()).is_some_and(|rest| rest.is_empty() || rest.starts_with('/'));
        self.doors.iter().find(inside).unwrap_or(&self.doors[self.doors.len() - 1])
    }

    async fn http(&self, edge: &Edge, req: Req) -> Response {
        if re_segments(&req.raw, &req.path) {
            return not_found();
        }
        let decision = edge.authenticate(&req, Some(&req.shape(false)));
        let Some(sub) = decision.sub().map(str::to_string) else {
            return edge.deny(&req);
        };
        let door = self.door(&req.path);
        let raw = req.raw.clone();
        if let Some(wake) = &self.wake {
            wake.send(&sub, false);
            let html = req.parts.method == hyper::Method::GET && req.header("accept").is_some_and(|a| a.contains("text/html"));
            let starting = || {
                wake.send(&sub, true);
                starting_response(html)
            };
            let opts = Forward { origin: door.origin.clone(), unreachable: Some(&starting), ..Forward::default() };
            return edge.forward(req, &door.up, &raw, &sub, decision.refreshed(), opts).await;
        }
        edge.forward(req, &door.up, &raw, &sub, decision.refreshed(), Forward { origin: door.origin.clone(), ..Forward::default() }).await
    }

    async fn ws(&self, edge: &Edge, req: Req) -> Response {
        if re_segments(&req.raw, &req.path) {
            return refuse(403);
        }
        let Some(sub) = edge.authenticate(&req, Some(&req.shape(true))).sub().map(str::to_string) else {
            return refuse(403);
        };
        if let Some(wake) = &self.wake {
            wake.send(&sub, false);
        }
        let door = self.door(&req.path);
        let mut fwd = edge::ws_headers(&req);
        edge::replace_origin(&mut fwd, door.origin.clone());
        fwd.insert("x-awm-as", edge::as_header(&sub));
        if let Ok(ip) = HeaderValue::from_str(&edge.client_ip(&req)) {
            fwd.insert("x-forwarded-for", ip);
        }
        let raw = req.raw.clone();
        edge.bridge(req, &door.up, &raw, fwd).await
    }
}

impl Dispatch for Relay {
    fn http<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response> {
        Box::pin(Relay::http(self, edge, req))
    }

    fn ws<'a>(&'a self, edge: &'a Edge, req: Req) -> BoxFut<'a, Response> {
        Box::pin(Relay::ws(self, edge, req))
    }
}

/// What every front shares with the main edge.
pub struct Base {
    pub gate: Arc<AuthGate>,
    pub grants: Arc<Grants>,
    /// The main edge's settings. A front replaces only the upstream.
    pub config: EdgeConfig,
    /// `None` is plain HTTP on loopback, behind a TLS-terminating proxy.
    pub tls: Option<Arc<ServerConfig>>,
    pub san: Option<String>,
    pub sans_file: PathBuf,
}

struct Open {
    decl: FrontDecl,
    listening: Result<JoinHandle<()>, String>,
}

type Key = (String, String);

/// The listeners open now, keyed by service and front name.
pub struct Fronts {
    base: Base,
    open: Mutex<BTreeMap<Key, Open>>,
}

/// The fronts of every enabled service in an `awm services` table.
pub fn declared(services: &Value) -> BTreeMap<Key, FrontDecl> {
    let rows = services.as_array().map(Vec::as_slice).unwrap_or_default();
    rows.iter()
        .filter(|row| row["enabled"].as_bool() == Some(true))
        .flat_map(|row| {
            let service = row["name"].as_str().unwrap_or_default().to_string();
            let decls: Vec<FrontDecl> = serde_json::from_value(row["front"].clone()).unwrap_or_default();
            decls.into_iter().map(move |d| ((service.clone(), d.name.clone()), d))
        })
        .collect()
}

impl Fronts {
    pub fn new(base: Base) -> Fronts {
        Fronts { base, open: Mutex::default() }
    }

    fn edge(&self, decl: &FrontDecl) -> Arc<Edge> {
        let b = &self.base;
        let config = EdgeConfig { upstream: decl.upstream.trim_end_matches('/').to_string(), identity_cookies: &[], ..b.config.clone() };
        Arc::new(Edge::new(b.gate.clone(), b.grants.clone(), config, Box::new(Relay::new(decl))))
    }

    /// Close what is no longer declared, then open what is not listening. A port that will not
    /// bind stays reported in [`Fronts::state`] and is tried again at the next change.
    pub async fn reconcile(&self, declared: BTreeMap<Key, FrontDecl>) {
        let mut open = self.open.lock().await;
        let stale: Vec<Key> = open.iter().filter(|(k, o)| declared.get(*k) != Some(&o.decl)).map(|(k, _)| k.clone()).collect();
        for key in stale {
            if let Some(Open { listening: Ok(task), .. }) = open.remove(&key) {
                task.abort();
                let _ = task.await;
                tracing::info!("auth: front {}/{} closed", key.0, key.1);
            }
        }
        for (key, decl) in declared {
            if open.get(&key).is_some_and(|o| o.listening.is_ok()) {
                continue;
            }
            let listening = edge::start(self.edge(&decl), decl.port, self.base.tls.clone()).await.map_err(|e| format!("could not listen on :{}: {e}", decl.port));
            match &listening {
                Ok(_) => tracing::info!("auth: front {}/{} on :{} -> {}", key.0, key.1, decl.port, decl.upstream),
                Err(why) => tracing::warn!("auth: front {}/{} {why}", key.0, key.1),
            }
            open.insert(key, Open { decl, listening });
        }
    }

    /// `auth front`: the `front` block of a service's status.
    pub async fn state(&self, service: &str, name: &str) -> Value {
        let open = self.open.lock().await;
        match open.get(&(service.to_string(), name.to_string())) {
            Some(o) => json!({
                "listener_port": o.decl.port, "upstream": o.decl.upstream, "tls": self.base.tls.is_some(), "san": self.base.san,
                "serving": o.listening.is_ok(), "error": o.listening.as_ref().err(), "url": certs::front_url(o.decl.port, &[self.base.sans_file.as_path()]),
            }),
            None => json!({"listener_port": null, "upstream": null, "tls": false, "san": null, "serving": false,
                           "error": format!("{service} has no front named {name} on this node"), "url": null}),
        }
    }

    async fn refresh(&self, hub: &Hub) {
        match hub.call("awm", "services", json!({}), None).await {
            Ok(table) => self.reconcile(declared(&table)).await,
            Err(e) => tracing::warn!("auth: could not read the service table: {e}"),
        }
    }
}

/// Keep the listeners in step with the gateway: a service that registers or disconnects, and so
/// a reload that adds or removes one, is an event on `awm/services`.
pub fn watch(fronts: Arc<Fronts>, hub: Hub) -> SupervisedSubscription {
    let (connect_fronts, connect_hub) = (fronts.clone(), hub.clone());
    SupervisedSubscription::spawn(
        "awm/services",
        SubscriptionOpts::default(),
        move || {
            let (fronts, hub) = (connect_fronts.clone(), connect_hub.clone());
            async move {
                let stream = hub.subscribe("awm", "services", None, None).await?;
                fronts.refresh(&hub).await;
                Ok(stream)
            }
        },
        move |_event: Value| {
            let (fronts, hub) = (fronts.clone(), hub.clone());
            async move {
                fronts.refresh(&hub).await;
                Ok(())
            }
        },
    )
}
