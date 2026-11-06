//! The gateway's HTTP surface on loopback.

use std::sync::Arc;

use awm_proto::{CallContext, Caller, Lane, Surface, UsageEvent};
use axum::body::Body;
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::gateway::{ApiError, Gateway};
use crate::hub::Claim;
use crate::supervisor;

const SERVICE_TOKEN_HEADER: &str = "x-awm-service-token";

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
        .route("/mcp", post(mcp).get(mcp_no_stream).delete(mcp_no_stream))
        .route("/usage", get(usage_get).post(usage_post))
        .route("/hub/service/register", post(register))
        .route("/hub/service/control/{id}", get(control))
        .route("/hub/service/bridge/{id}/{bridge}", get(bridge))
        .route("/hub/emit/{service}/{topic}", get(stream))
        .route("/hub/session/{service}/{kind}", get(stream))
        .route("/s/{service}", any(crate::mount::handle))
        .route("/s/{service}/{*rest}", any(crate::mount::handle))
        .route("/hub/table", get(table))
        .route("/hub/table/changed", post(table_changed))
        .route("/hub/tool/{name}", get(tool_owner))
        .route("/hub/contract/{contract}", get(contract_providers))
        .route("/hub/plugins", get(plugins))
        .route("/hub/reload", post(reload))
        .route("/hub/install", post(install))
        .route("/hub/uninstall", post(uninstall))
        .route("/p/{plugin}/{page}", get(page_root))
        .route("/p/{plugin}/{page}/", get(page_root))
        .route("/p/{plugin}/{page}/{*path}", get(page_file))
        // A call can carry a file inline (`files.rs`), and a door host receives it here.
        .layer(DefaultBodyLimit::max(crate::files::MAX_REQUEST_BYTES))
        .with_state(gw)
}

/// The isolated door's own listener: `/mcp` and nothing else, so a container that reaches it
/// cannot reach `/invoke` or the hub.
pub fn door_router(gw: Arc<Gateway>) -> Router {
    Router::new().route("/mcp", post(mcp).get(mcp_no_stream).delete(mcp_no_stream)).with_state(gw)
}

/// Bind, start the services, serve until SIGINT or SIGTERM, then stand everything down.
pub async fn serve(gw: Arc<Gateway>) -> anyhow::Result<()> {
    let listener = tokio::net::TcpListener::bind(&gw.cfg.listen).await?;
    let addr = listener.local_addr()?;
    let url = format!("http://{addr}");
    let _ = gw.url.set(url.clone());
    let token = uuid::Uuid::new_v4().simple().to_string();
    write_private(&gw.state.join("edge.token"), &token)?;
    let mut door_url = Value::Null;
    if let Some(addr) = &gw.cfg.isolated_listen {
        let door = tokio::net::TcpListener::bind(addr).await?;
        door_url = json!(format!("http://{}", door.local_addr()?));
        tracing::info!("isolated door on {door_url}");
        let app = door_router(gw.clone());
        let stop = gw.clone();
        tokio::spawn(async move {
            let until = async move {
                while !stop.stopping() {
                    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                }
            };
            if let Err(e) = axum::serve(door, app).with_graceful_shutdown(until).await {
                tracing::error!("isolated door stopped: {e}");
            }
        });
    }
    std::fs::write(
        gw.state.join("gateway.json"),
        serde_json::to_string_pretty(&json!({"url": url, "isolated_url": door_url, "pid": std::process::id(), "node": gw.node}))?,
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
/// A local request gets the id, trust level, mode and scope of the session its pid header
/// leads to (`caller.rs`).
async fn context(gw: &Gateway, h: &HeaderMap, default: Surface) -> Result<CallContext, ApiError> {
    // A service names itself by the token its spawn was given. A wrong token is refused,
    // since a caller that sends one means to be that service.
    let service = match header(h, SERVICE_TOKEN_HEADER) {
        Some(token) => match gw.service_of_token(token) {
            Some(name) => Some(name),
            None => return Err(ApiError::new(401, "unknown service token")),
        },
        None => None,
    };
    // A page names its plugin by the path it was served from.
    let plugin = match &service {
        Some(name) => Some(gw.plugin_of_service(name)),
        None => header(h, "referer").and_then(page_plugin),
    };
    let surface = if service.is_some() {
        Surface::Plugin
    } else {
        header(h, "x-awm-surface").and_then(Surface::parse).unwrap_or(default)
    };
    let mut caller = Caller { node: gw.node.clone(), lane: Lane::Local, ..Caller::default() };
    let mut edge = false;
    if let (Some(raw), Some(token)) = (header(h, "x-awm-caller"), header(h, "x-awm-edge-token")) {
        let expected = std::fs::read_to_string(gw.state.join("edge.token")).unwrap_or_default();
        if !expected.is_empty() && expected.trim() == token {
            if let Ok(c) = serde_json::from_str::<Caller>(raw) {
                caller = c;
                edge = true;
            }
        } else {
            tracing::warn!("X-Awm-Caller with a wrong edge token; ignored");
        }
    }
    let mut mode = None;
    let mut session_pid = None;
    let pid = header(h, "x-awm-session-pid").or_else(|| header(h, "x-awm-caller-pid"));
    if !edge && service.is_some() && pid.is_none() {
        // A service acting on its own account: trusted, on the service lane.
        caller.lane = Lane::Service;
        caller.trust = Some(crate::caller::TRUSTED.into());
    } else if !edge {
        // A local request with no pid comes from no session: a user's tool on this node.
        // A service that forwards a session's pid acts for that session.
        let lookup = match pid.map(str::parse::<u32>) {
            Some(Ok(pid)) => gw.resolve_caller(pid).await,
            Some(Err(_)) => crate::caller::Lookup::Failed,
            None => crate::caller::Lookup::NoOwner { mode: None },
        };
        if let crate::caller::Lookup::Session(a) = &lookup {
            session_pid = a.get(crate::caller::FOUND_PID).and_then(Value::as_u64).map(|p| p as u32);
        }
        mode = crate::caller::fill(&mut caller, &lookup, &gw.projects_dir());
    }
    let mut as_ = header(h, "x-awm-as").map(str::to_string);
    // A call from another node names that node as the stamp a service reads, since a Python
    // handler sees `as_` and not `caller`, and hides what the private marks cover from it.
    if edge && as_.is_none() && caller.node != gw.node {
        as_ = Some(format!("peer:{}", caller.node));
    }
    Ok(CallContext { caller, as_, surface, plugin, mode, pid: session_pid })
}

/// The plugin whose page sent a request, from its Referer `<origin>/p/<plugin>/<page>/...`.
fn page_plugin(referer: &str) -> Option<String> {
    let path = referer.split_once("://").map_or(referer, |(_, rest)| rest.find('/').map_or("", |i| &rest[i..]));
    let mut parts = path.strip_prefix("/p/")?.splitn(3, '/');
    let plugin = parts.next().filter(|p| awm_proto::valid_tool_name(p))?;
    parts.next().filter(|page| !page.is_empty())?;
    Some(plugin.to_string())
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

/// A caller token belongs on `/mcp`. Anywhere else it would pass as a trusted local caller.
fn refuse_token(h: &HeaderMap) -> Result<(), ApiError> {
    match header(h, crate::isolated::TOKEN_HEADER) {
        Some(_) => Err(ApiError::new(403, "an isolated session calls awm only through /mcp")),
        None => Ok(()),
    }
}

async fn invoke(State(gw): Gw, h: HeaderMap, body: Option<Json<Value>>) -> Result<Json<Value>, ApiError> {
    refuse_token(&h)?;
    let Some(Json(body)) = body else { return Err(ApiError::new(422, "body must be a JSON object")) };
    let Some(name) = body.get("name").and_then(Value::as_str) else {
        return Err(ApiError::new(400, "missing 'name' in payload"));
    };
    let args = body.get("args").cloned().filter(|a| !a.is_null()).unwrap_or_else(|| json!({}));
    let ctx = context(&gw, &h, Surface::Http).await?;
    let session = ctx.caller.lane == Lane::Local && matches!(ctx.surface, Surface::Mcp | Surface::Cli);
    let mut args = args;
    if session {
        crate::files::inline_file_arg(name, &mut args).map_err(|e| ApiError::new(400, e))?;
    }
    let mut result = gw.invoke(name, args, &ctx).await?;
    // The session's node is where inline file content lands; a plugin or a peer takes it as sent.
    if session {
        result = crate::files::localize(&result, &gw.node);
    }
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
    pid: Option<i64>,
    #[serde(default)]
    overlay: bool,
    /// The loopback URL of the service's own HTTP server, served at `/s/<name>/`.
    #[serde(default)]
    mount: Option<String>,
}

async fn register(State(gw): Gw, h: HeaderMap, Json(req): Json<RegisterReq>) -> Result<Json<Value>, ApiError> {
    if req.overlay {
        return Err(ApiError::new(409, "the Rust gateway does not run shadow overlays"));
    }
    if let Some(path) = gw.token_file_of(&req.name) {
        let expected = std::fs::read_to_string(&path).unwrap_or_default();
        let sent = header(&h, SERVICE_TOKEN_HEADER).unwrap_or_default();
        if expected.trim().is_empty() || expected.trim() != sent {
            tracing::warn!("service {}: register without its service token; refused", req.name);
            return Err(ApiError::new(403, format!("service {:?} needs its service token", req.name)));
        }
    }
    if let Some(m) = req.mount.as_deref().filter(|m| !crate::mount::valid_mount(m)) {
        return Err(ApiError::new(400, format!("mount {m:?} is not a loopback http URL")));
    }
    let plugin = gw.plugin_of_service(&req.name);
    let Ok(id) = gw.hub.register(&req.name, &plugin, req.pid, req.mount.clone()) else {
        return Err(ApiError::new(409, format!("service {:?} already has a live instance connected", req.name)));
    };
    tracing::info!("registered service {} (plugin {plugin}) pid {:?}", req.name, req.pid);
    let workspace = gw.cfg.workspace.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
    Ok(Json(json!({
        "service_id": id,
        "name": req.name,
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
            Some("emit") => {
                if let Some(topic) = frame.get("topic").and_then(Value::as_str) {
                    gw.topics.publish(&gw, &name, topic, frame.get("payload").unwrap_or(&Value::Null));
                }
            }
            Some("session.opened") => gw.stream_sessions.opened(&name, &frame),
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
    gw.hub.disconnected(&name, &id);
    writer.abort();
    tracing::info!("control WS closed for {name}");
}

#[derive(Deserialize)]
struct StreamQuery {
    peer: Option<String>,
}

/// `GET /hub/emit/<service>/<topic>` and `GET /hub/session/<service>/<kind>` (`streams.rs`).
async fn stream(
    State(gw): Gw,
    h: HeaderMap,
    uri: axum::http::Uri,
    Query(q): Query<StreamQuery>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    refuse_token(&h)?;
    let ctx = context(&gw, &h, Surface::Http).await?;
    if crate::modes::is_restricted(ctx.mode.as_deref()) {
        return Err(ApiError::new(403, format!("mode {:?} may not open a stream", ctx.mode.as_deref().unwrap_or_default())));
    }
    let path = uri.path().to_string();
    let pipe = match q.peer.filter(|p| p != "local" && *p != gw.node) {
        Some(peer) => {
            if ctx.caller.lane == Lane::Edge {
                return Err(ApiError::new(403, "a stream from another node may not hop on"));
            }
            gw.peers.open_ws(&peer, &path, &ctx).await?
        }
        None => gw.open_local(&path, &ctx)?,
    };
    Ok(ws.max_message_size(64 << 20).on_upgrade(move |socket| crate::streams::relay(socket, pipe)))
}

/// `GET /hub/service/bridge/<service_id>/<bridge_id>`: a service's end of a direct session.
async fn bridge(State(gw): Gw, Path((id, bridge)): Path<(String, String)>, ws: WebSocketUpgrade) -> Response {
    let Some(service) = gw.hub.name_of_id(&id) else { return (StatusCode::FORBIDDEN, "unknown service").into_response() };
    if !gw.stream_sessions.expects_bridge(&service, &bridge) {
        return (StatusCode::NOT_FOUND, "no session waits for this bridge").into_response();
    }
    ws.max_message_size(64 << 20).on_upgrade(move |socket| async move { gw.stream_sessions.bridge(&service, &bridge, socket) })
}

/// This node's tools, for a peer's name table, less the singletons that leave the peer out.
async fn table(State(gw): Gw, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    let ctx = context(&gw, &h, Surface::Http).await?;
    Ok(Json(json!(gw.local_table(&ctx.caller))))
}

#[derive(Deserialize)]
struct TableChanged {
    node: String,
}

/// A peer says its table changed: read it again, then reload, since a need may now resolve.
async fn table_changed(State(gw): Gw, h: HeaderMap, Json(body): Json<TableChanged>) -> Result<Json<Value>, ApiError> {
    refuse_token(&h)?;
    if !gw.refresh_peer(&body.node).await {
        return Err(ApiError::new(404, format!("{:?} is not a peer of this node", body.node)));
    }
    gw.reload().await.map(Json)
}

/// The owner of a tool name, looked up now.
async fn tool_owner(State(gw): Gw, Path(name): Path<String>) -> Result<Json<Value>, ApiError> {
    gw.owner(&name).map(|o| Json(json!(o))).ok_or_else(|| ApiError::new(404, format!("no plugin serves tool {name:?}")))
}

/// Every provider of `<contract>@<major>`, looked up now. A bare name means major 1.
async fn contract_providers(State(gw): Gw, Path(contract): Path<String>) -> Result<Json<Value>, ApiError> {
    let (name, major) = match contract.split_once('@') {
        Some((n, m)) => (n.to_string(), m.parse().map_err(|_| ApiError::new(400, format!("bad major in {contract:?}")))?),
        None => (contract.clone(), 1),
    };
    Ok(Json(json!(gw.providers(&name, major))))
}

async fn plugins(State(gw): Gw) -> Json<Value> {
    Json(gw.load_snapshot())
}

async fn reload(State(gw): Gw, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    refuse_token(&h)?;
    gw.reload().await.map(Json)
}

#[derive(Deserialize)]
struct InstallBody {
    sources: Vec<String>,
    #[serde(default)]
    approval: Option<String>,
}

async fn install(State(gw): Gw, h: HeaderMap, Json(body): Json<InstallBody>) -> Result<Json<Value>, ApiError> {
    refuse_token(&h)?;
    gw.install(&body.sources, body.approval.as_deref()).await.map(Json)
}

#[derive(Deserialize)]
struct UninstallBody {
    name: String,
}

async fn uninstall(State(gw): Gw, h: HeaderMap, Json(body): Json<UninstallBody>) -> Result<Json<Value>, ApiError> {
    refuse_token(&h)?;
    gw.uninstall(&body.name).await.map(Json)
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

async fn mcp_no_stream() -> Response {
    (StatusCode::METHOD_NOT_ALLOWED, "this MCP door answers POST only").into_response()
}

/// `POST /mcp`: MCP over streamable HTTP with JSON replies, for an isolated session.
async fn mcp(State(gw): Gw, h: HeaderMap, body: axum::body::Bytes) -> Response {
    use crate::isolated::rpc_error;
    let Some(token) = header(&h, crate::isolated::TOKEN_HEADER) else {
        return (StatusCode::UNAUTHORIZED, Json(json!({"detail": "X-Awm-Caller-Token is required"}))).into_response();
    };
    let holder = match gw.holder_of(token).await {
        Ok(Some(holder)) => holder,
        Ok(None) => return (StatusCode::UNAUTHORIZED, Json(json!({"detail": "unknown caller token"}))).into_response(),
        Err(e) => {
            tracing::warn!("isolated door: token lookup failed: {}", e.detail);
            return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"detail": format!("cannot check the caller token: {}", e.detail)})))
                .into_response();
        }
    };
    let msg: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(rpc_error(&Value::Null, -32700, e.to_string()))).into_response(),
    };
    let batch = msg.is_array();
    let mut replies = Vec::new();
    for m in msg.as_array().cloned().unwrap_or_else(|| vec![msg]) {
        if let Some(r) = mcp_message(&gw, &holder, &m).await {
            replies.push(r);
        }
    }
    match (batch, replies.len()) {
        (_, 0) => StatusCode::ACCEPTED.into_response(),
        (false, _) => Json(replies.remove(0)).into_response(),
        (true, _) => Json(Value::Array(replies)).into_response(),
    }
}

async fn mcp_message(gw: &Gateway, holder: &crate::isolated::Holder, msg: &Value) -> Option<Value> {
    let id = msg.get("id").cloned()?;
    let method = msg.get("method").and_then(Value::as_str)?;
    let result = match method {
        "initialize" => json!({
            "protocolVersion": msg.pointer("/params/protocolVersion").and_then(Value::as_str).unwrap_or("2025-06-18"),
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {"name": "awm", "version": env!("CARGO_PKG_VERSION")},
        }),
        "ping" => json!({}),
        "tools/list" => json!({"tools": gw.domain_tools_for(Some(holder))}),
        "tools/call" => {
            let params = msg.get("params").cloned().unwrap_or(Value::Null);
            let tool = params.get("name").and_then(Value::as_str).unwrap_or("");
            let envelope = params.get("arguments").cloned().unwrap_or_else(|| json!({}));
            let (text, is_error) = match gw.invoke_isolated(holder, tool, &envelope).await {
                Ok(text) => (text, false),
                Err(e) => {
                    let detail = match e.detail {
                        Value::String(s) => s,
                        other => other.to_string(),
                    };
                    (format!("Error {}: {detail}", e.status), true)
                }
            };
            json!({"content": [{"type": "text", "text": text}], "isError": is_error})
        }
        other => return Some(crate::isolated::rpc_error(&id, -32601, format!("method {other:?} not found"))),
    };
    Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

#[cfg(test)]
mod tests {
    use super::page_plugin;

    #[test]
    fn a_page_is_known_by_its_path() {
        assert_eq!(page_plugin("https://mizar.example:7819/p/dsh/dsh/").as_deref(), Some("dsh"));
        assert_eq!(page_plugin("http://127.0.0.1:7819/p/ops/board/assets/x.js").as_deref(), Some("ops"));
        assert_eq!(page_plugin("/p/mic/mic").as_deref(), Some("mic"));
        assert_eq!(page_plugin("https://host/p/dsh"), None);
        assert_eq!(page_plugin("https://host/p/dsh/"), None);
        assert_eq!(page_plugin("https://host/dashboard/p/dsh/x"), None);
        assert_eq!(page_plugin("https://host/p/a_b/x/"), None);
    }
}
