//! `/s/<service>/...`: a service's own HTTP server, behind the gateway.
//!
//! A service names a loopback URL as `mount` when it registers. The gateway forwards each
//! request under `/s/<service>/` to it, with the method, the query, the body and the headers
//! but the hop-by-hop ones, and relays a WebSocket upgrade both ways. The response streams
//! through chunk by chunk, so server-sent events reach the client as they are sent. A mount
//! answers only while its service is ready.

use std::sync::Arc;

use axum::body::{to_bytes, Body};
use axum::extract::{FromRequestParts, Request, State};
use axum::extract::ws::WebSocketUpgrade;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::gateway::Gateway;

const MAX_BODY: usize = 64 << 20;
const HOP: [&str; 9] =
    ["host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer", "content-length"];

/// Only a loopback URL may be a mount: the gateway is not an open proxy.
pub fn valid_mount(url: &str) -> bool {
    ["http://127.0.0.1:", "http://localhost:", "http://[::1]:"].iter().any(|p| url.starts_with(p))
}

fn split(path: &str) -> Option<(&str, &str)> {
    let rest = path.strip_prefix("/s/")?;
    let (service, tail) = rest.split_once('/').unwrap_or((rest, ""));
    (!service.is_empty()).then_some((service, tail))
}

pub async fn handle(State(gw): State<Arc<Gateway>>, req: Request) -> Response {
    let (mut parts, body) = req.into_parts();
    let path = parts.uri.path().to_string();
    let Some((service, tail)) = split(&path) else { return StatusCode::NOT_FOUND.into_response() };
    let Some(base) = gw.hub.mount_of(service) else {
        return (StatusCode::NOT_FOUND, format!("service {service:?} serves no mount")).into_response();
    };
    let query = parts.uri.query().map(|q| format!("?{q}")).unwrap_or_default();
    let target = format!("{}/{tail}{query}", base.trim_end_matches('/'));
    let upgrade = parts.headers.get("upgrade").and_then(|v| v.to_str().ok()).is_some_and(|v| v.eq_ignore_ascii_case("websocket"));
    if upgrade {
        let headers = forwarded(&parts.headers);
        let Ok(ws) = WebSocketUpgrade::from_request_parts(&mut parts, &()).await else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        let url = target.replacen("http", "ws", 1);
        return ws.on_upgrade(move |socket| async move {
            let hdrs: Vec<(&str, String)> = headers.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
            match crate::peer::connect_ws(&url, &hdrs).await {
                Ok(pipe) => crate::streams::relay(socket, pipe).await,
                Err(e) => tracing::warn!("mount {url}: {e}"),
            }
        });
    }
    let Ok(bytes) = to_bytes(body, MAX_BODY).await else { return StatusCode::PAYLOAD_TOO_LARGE.into_response() };
    let client = reqwest::Client::new();
    let Ok(method) = reqwest::Method::from_bytes(parts.method.as_str().as_bytes()) else {
        return StatusCode::METHOD_NOT_ALLOWED.into_response();
    };
    let mut out = client.request(method, &target).body(bytes.to_vec());
    for (k, v) in forwarded(&parts.headers) {
        out = out.header(k, v);
    }
    match out.send().await {
        Ok(resp) => {
            let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            let mut builder = Response::builder().status(status);
            for (k, v) in resp.headers() {
                if !HOP.contains(&k.as_str()) {
                    builder = builder.header(k.as_str(), v.as_bytes());
                }
            }
            builder.body(Body::from_stream(resp.bytes_stream())).unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
        }
        Err(e) => (StatusCode::BAD_GATEWAY, format!("mount of {service:?}: {e}")).into_response(),
    }
}

fn forwarded(h: &HeaderMap) -> Vec<(String, String)> {
    h.iter()
        .filter(|(k, _)| !HOP.contains(&k.as_str()) && !k.as_str().starts_with("sec-websocket"))
        .filter_map(|(k, v)| v.to_str().ok().map(|v| (k.as_str().to_string(), v.to_string())))
        .collect()
}
