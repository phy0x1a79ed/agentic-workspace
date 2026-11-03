//! The gateway's HTTP surface on loopback.

use std::sync::Arc;

use awm_proto::{CallContext, Caller, Lane, Surface, UsageEvent};
use axum::body::Body;
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::gateway::{ApiError, Gateway};
use crate::hub::Claim;
use crate::supervisor;

type Gw = State<Arc<Gateway>>;

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = StatusCode::from_u16(self.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        (status, Json(json!({"detail": self.detail}))).into_response()
    }
}

pub fn router(gw: Arc<Gateway>) -> Router {
    Router::new()
        .route("/status", get(status))
        .route("/tools", get(tools))
        .route("/invoke", post(invoke))
        .route("/usage", get(usage_get).post(usage_post))
        .route("/hub/service/register", post(register))
        .route("/hub/service/control/{id}", get(control))
        .route("/hub/uuids", get(uuids))
        .route("/hub/resolve/{uuid}", get(resolve))
        .route("/hub/plugins", get(plugins))
        .route("/hub/plugins/{plugin}/resolved", get(resolved_tools))
        .route("/svc/{service}/fn/{function}", post(svc_fn))
        .route("/p/{plugin}/{page}", get(page_root))
        .route("/p/{plugin}/{page}/", get(page_root))
        .route("/p/{plugin}/{page}/{*path}", get(page_file))
        .with_state(gw)
}

/// Bind, start the services, serve until SIGINT or SIGTERM, then stand everything down.
pub async fn serve(gw: Arc<Gateway>) -> anyhow::Result<()> {
    let listener = tokio::net::TcpListener::bind(&gw.cfg.listen).await?;
    let addr = listener.local_addr()?;
    let url = format!("http://{addr}");
    let _ = gw.url.set(url.clone());
    let token = uuid::Uuid::new_v4().simple().to_string();
    write_private(&gw.state.join("edge.token"), &token)?;
    std::fs::write(
        gw.state.join("gateway.json"),
        serde_json::to_string_pretty(&json!({"url": url, "pid": std::process::id(), "node": gw.node}))?,
    )?;
    tracing::info!("gateway on {url}, node {}", gw.node);
    supervisor::start_all(&gw);
    let app = router(gw.clone());
    axum::serve(listener, app).with_graceful_shutdown(shutdown_signal(gw.clone())).await?;
    let _ = std::fs::remove_file(gw.state.join("gateway.json"));
    Ok(())
}

async fn shutdown_signal(gw: Arc<Gateway>) {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
    tracing::info!("stopping: standing services down");
    supervisor::stop_all(&gw).await;
}

fn write_private(path: &std::path::Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let _ = std::fs::remove_file(path);
    let mut f = std::fs::OpenOptions::new().create_new(true).write(true).mode(0o600).open(path)?;
    f.write_all(text.as_bytes())
}

fn header<'a>(h: &'a HeaderMap, name: &str) -> Option<&'a str> {
    h.get(name).and_then(|v| v.to_str().ok()).map(str::trim).filter(|s| !s.is_empty())
}

/// The caller context of an inbound request. `X-Awm-Trust` and `X-Awm-Lane` are never read.
/// `X-Awm-Caller` counts only with the edge token, which only `awm edge-serve` can read.
fn context(gw: &Gateway, h: &HeaderMap, default: Surface) -> CallContext {
    let plugin = header(h, "x-awm-plugin").map(str::to_string);
    let surface = if plugin.is_some() {
        Surface::Plugin
    } else {
        header(h, "x-awm-surface").and_then(Surface::parse).unwrap_or(default)
    };
    let mut caller = Caller { node: gw.node.clone(), lane: Lane::Local, session: None, trust: None };
    if let (Some(raw), Some(token)) = (header(h, "x-awm-caller"), header(h, "x-awm-edge-token")) {
        let expected = std::fs::read_to_string(gw.state.join("edge.token")).unwrap_or_default();
        if !expected.is_empty() && expected.trim() == token {
            if let Ok(c) = serde_json::from_str::<Caller>(raw) {
                caller = c;
            }
        } else {
            tracing::warn!("X-Awm-Caller with a wrong edge token; ignored");
        }
    }
    CallContext { caller, as_: header(h, "x-awm-as").map(str::to_string), surface, plugin }
}

async fn status(State(gw): Gw) -> Json<Value> {
    Json(gw.status())
}

#[derive(Deserialize)]
struct ToolsQuery {
    view: Option<String>,
}

async fn tools(State(gw): Gw, Query(q): Query<ToolsQuery>) -> Json<Value> {
    let tools = if q.view.as_deref() == Some("domains") { gw.domain_tools() } else { gw.flat_tools() };
    Json(json!({"tools": tools}))
}

async fn invoke(State(gw): Gw, h: HeaderMap, body: Option<Json<Value>>) -> Result<Json<Value>, ApiError> {
    let Some(Json(body)) = body else { return Err(ApiError::new(422, "body must be a JSON object")) };
    let Some(name) = body.get("name").and_then(Value::as_str) else {
        return Err(ApiError::new(400, "missing 'name' in payload"));
    };
    let args = body.get("args").cloned().filter(|a| !a.is_null()).unwrap_or_else(|| json!({}));
    let ctx = context(&gw, &h, Surface::Http);
    let result = gw.invoke(name, args, &ctx).await?;
    Ok(Json(json!({"result": result})))
}

#[derive(Deserialize)]
struct UsageQuery {
    days: Option<i64>,
}

async fn usage_get(State(gw): Gw, Query(q): Query<UsageQuery>) -> Json<Value> {
    Json(json!(gw.usage.tally(q.days.unwrap_or(crate::usage::WINDOW_DAYS))))
}

async fn usage_post(State(gw): Gw, Json(ev): Json<UsageEvent>) -> Result<Json<Value>, ApiError> {
    if ev.plugin.trim().is_empty() {
        return Err(ApiError::new(400, "usage event needs a plugin"));
    }
    gw.usage.record(ev);
    Ok(Json(json!({"ok": true})))
}

#[derive(Deserialize)]
struct RegisterReq {
    name: String,
    #[serde(default)]
    prefix: Option<String>,
    #[serde(default)]
    pid: Option<i64>,
    #[serde(default)]
    overlay: bool,
}

async fn register(State(gw): Gw, h: HeaderMap, Json(req): Json<RegisterReq>) -> Result<Json<Value>, ApiError> {
    if req.overlay {
        return Err(ApiError::new(409, "the Rust gateway does not run shadow overlays"));
    }
    let token_file = match gw.cfg.external(&req.name) {
        Some(external) => external.token_file.clone(),
        None => Some(supervisor::service_token_path(&gw.state, &req.name)).filter(|p| p.exists()),
    };
    if let Some(path) = token_file {
        let expected = std::fs::read_to_string(&path).unwrap_or_default();
        let sent = header(&h, "x-awm-service-token").unwrap_or_default();
        if expected.trim().is_empty() || expected.trim() != sent {
            tracing::warn!("service {}: register without its service token; refused", req.name);
            return Err(ApiError::new(403, format!("service {:?} needs its service token", req.name)));
        }
    }
    let plugin = gw.plugin_of_service(&req.name);
    let Ok(id) = gw.hub.register(&req.name, &plugin, req.pid) else {
        return Err(ApiError::new(409, format!("service {:?} already has a live instance connected", req.name)));
    };
    tracing::info!("registered service {} (plugin {plugin}) pid {:?}", req.name, req.pid);
    let workspace = gw.cfg.workspace.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
    Ok(Json(json!({
        "service_id": id,
        "name": req.name,
        "prefix": req.prefix.unwrap_or_else(|| format!("/svc/{}", req.name)),
        "control_ws_path": format!("/hub/service/control/{id}"),
        "bridge_ws_base": format!("/hub/service/bridge/{id}"),
        "canonical_workspace": workspace,
    })))
}

async fn control(State(gw): Gw, Path(id): Path<String>, ws: WebSocketUpgrade) -> Response {
    // Refuse before the upgrade, as today: the Python adapter reads 403 and 409 as "stand down".
    if !gw.hub.services().iter().any(|s| s.id == id) {
        return (StatusCode::FORBIDDEN, "unknown service").into_response();
    }
    ws.max_message_size(64 << 20).on_upgrade(move |socket| control_socket(gw, id, socket))
}

async fn control_socket(gw: Arc<Gateway>, id: String, socket: WebSocket) {
    let (mut sink, mut stream) = socket.split();
    let (name, mut rx) = match gw.hub.claim(&id) {
        Claim::Claimed { name, rx } => (name, rx),
        Claim::Held => {
            let _ = sink.send(Message::Close(Some(CloseFrame { code: 4409, reason: "lease already held".into() }))).await;
            return;
        }
        Claim::Unknown => {
            let _ = sink.send(Message::Close(Some(CloseFrame { code: 4404, reason: "unknown service".into() }))).await;
            return;
        }
    };
    tracing::info!("control WS open for {name}");
    let writer = tokio::spawn(async move {
        while let Some(text) = rx.recv().await {
            if sink.send(Message::Text(text.into())).await.is_err() {
                break;
            }
        }
        let _ = sink.close().await;
    });
    let bindings = gw.bindings(&name);
    while let Some(Ok(msg)) = stream.next().await {
        let text = match msg {
            Message::Text(t) => t.to_string(),
            Message::Close(_) => break,
            _ => continue,
        };
        let Ok(frame) = serde_json::from_str::<Value>(&text) else { continue };
        match frame.get("kind").and_then(Value::as_str) {
            Some("ready") => gw.hub.ready(&name, frame.get("api").unwrap_or(&Value::Null), &bindings),
            Some("reply") => gw.hub.reply(&name, &frame),
            Some("usage") => {
                let mut f = frame.clone();
                f["plugin"] = json!(gw.plugin_of_service(&name));
                match serde_json::from_value::<UsageEvent>(f) {
                    Ok(ev) => gw.usage.record(ev),
                    Err(e) => tracing::warn!("{name}: bad usage frame: {e}"),
                }
            }
            Some(other) => tracing::debug!("{name}: ignored frame kind {other}"),
            None => {}
        }
    }
    gw.hub.disconnected(&name);
    writer.abort();
    tracing::info!("control WS closed for {name}");
}

async fn uuids(State(gw): Gw) -> Json<Value> {
    Json(json!(gw.local_uuids()))
}

async fn resolve(State(gw): Gw, Path(uuid): Path<String>) -> Result<Json<Value>, ApiError> {
    let uuid: uuid::Uuid = uuid.parse().map_err(|_| ApiError::new(400, "not a uuid"))?;
    gw.resolve_uuid(&uuid).map(Json).ok_or_else(|| ApiError::new(404, format!("no plugin serves tool {uuid}")))
}

async fn plugins(State(gw): Gw) -> Json<Value> {
    Json(gw.load_snapshot())
}

/// A plugin's resolved tools, for a service that cannot read `AWM_RESOLVED_TOOLS` in the gateway's state.
async fn resolved_tools(State(gw): Gw, Path(plugin): Path<String>) -> Result<Json<Value>, ApiError> {
    if !gw.read_load().plugins.iter().any(|p| p.name == plugin) {
        return Err(ApiError::new(404, format!("no plugin {plugin:?} loaded")));
    }
    let text = std::fs::read_to_string(crate::gateway::resolved_path(&gw.state, &plugin))
        .map_err(|_| ApiError::new(404, format!("no plugin {plugin:?} loaded")))?;
    Ok(Json(serde_json::from_str(&text).map_err(|e| ApiError::new(500, e.to_string()))?))
}

async fn svc_fn(
    State(gw): Gw,
    Path((service, function)): Path<(String, String)>,
    h: HeaderMap,
    body: Option<Json<Value>>,
) -> Result<Json<Value>, ApiError> {
    let args = body.map(|Json(v)| v).unwrap_or_else(|| json!({}));
    let ctx = context(&gw, &h, Surface::Plugin);
    gw.call_function(&service, &function, args, &ctx).await.map(Json)
}

async fn page_root(State(gw): Gw, Path((plugin, page)): Path<(String, String)>) -> Response {
    serve_page(&gw, &plugin, &page, "")
}

async fn page_file(State(gw): Gw, Path((plugin, page, path)): Path<(String, String, String)>) -> Response {
    serve_page(&gw, &plugin, &page, &path)
}

fn serve_page(gw: &Gateway, plugin: &str, page: &str, path: &str) -> Response {
    let Some((dir, entry)) = gw.page_dir(plugin, page) else {
        return (StatusCode::NOT_FOUND, "no such page").into_response();
    };
    if path.split('/').any(|seg| seg == "..") {
        return (StatusCode::BAD_REQUEST, "bad path").into_response();
    }
    let mut file = dir.join(path);
    if path.is_empty() || file.is_dir() {
        file = file.join(&entry);
    }
    // A client-side route has no extension: answer with the entry document.
    if !file.is_file() && std::path::Path::new(path).extension().is_none() {
        file = dir.join(&entry);
    }
    let Ok(bytes) = std::fs::read(&file) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let mime = mime_guess::from_path(&file).first_or_octet_stream();
    if mime.essence_str() == "text/html" {
        let mut ev = UsageEvent::new(plugin, Surface::Page);
        ev.name = Some(page.to_string());
        gw.usage.record(ev);
    }
    ([(header::CONTENT_TYPE, mime.to_string())], Body::from(bytes)).into_response()
}
