//! The mount the gateway serves at `/s/dsh/`: a reverse proxy to the fork on loopback.
//!
//! The fork fences every `/api` call: its `Host` must be loopback and a present `Origin` must
//! equal it. The proxy lets the client set `Host` from the upstream URL and rewrites a present
//! `Origin` to the fork's own origin, on HTTP and on the WebSocket upgrade alike. A rewrite on
//! HTTP alone gives a GUI that loads and never streams. Bodies stream both ways, so the fork's
//! server-sent events pass.

use axum::body::Body;
use axum::extract::ws::{CloseFrame as AxumClose, Message as AxumMsg, WebSocket, WebSocketUpgrade};
use axum::extract::{FromRequestParts, Request, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

const HOP: [&str; 10] =
    ["host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer", "content-length", "proxy-authorization"];

#[derive(Clone)]
struct Upstream {
    origin: String,
    client: reqwest::Client,
}

/// The router that proxies every path to the fork at `origin`, such as `http://127.0.0.1:12311`.
pub fn router(origin: String) -> axum::Router {
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("an HTTP client builds");
    axum::Router::new().fallback(proxy).with_state(Upstream { origin, client })
}

/// The headers to pass upstream: no hop-by-hop ones, no handshake keys, `Origin` rewritten.
fn forwarded(h: &HeaderMap, origin: &str) -> Vec<(String, String)> {
    h.iter()
        .filter(|(k, _)| !HOP.contains(&k.as_str()) && !k.as_str().starts_with("sec-websocket"))
        .filter_map(|(k, v)| {
            let v = if k == "origin" { origin.to_string() } else { v.to_str().ok()?.to_string() };
            Some((k.as_str().to_string(), v))
        })
        .collect()
}

async fn proxy(State(up): State<Upstream>, req: Request) -> Response {
    let (mut parts, body) = req.into_parts();
    let path_q = parts.uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or_else(|| "/".into());
    let target = format!("{}{path_q}", up.origin);
    let headers = forwarded(&parts.headers, &up.origin);
    let upgrade = parts.headers.get("upgrade").and_then(|v| v.to_str().ok()).is_some_and(|v| v.eq_ignore_ascii_case("websocket"));
    if upgrade {
        let Ok(ws) = WebSocketUpgrade::from_request_parts(&mut parts, &()).await else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        let url = target.replacen("http", "ws", 1);
        // Open the fork's side first, so a refusal reaches the client as a status, not a closed socket.
        let pipe = match connect(&url, &headers).await {
            Ok(pipe) => pipe,
            Err(e) => return (StatusCode::BAD_GATEWAY, format!("the DeepSeek harness refused the socket: {e}")).into_response(),
        };
        return ws.on_upgrade(move |socket| bridge(socket, pipe));
    }
    let Ok(method) = reqwest::Method::from_bytes(parts.method.as_str().as_bytes()) else {
        return StatusCode::METHOD_NOT_ALLOWED.into_response();
    };
    let mut out = up.client.request(method, &target).body(reqwest::Body::wrap_stream(body.into_data_stream()));
    for (k, v) in headers {
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
        Err(e) => (StatusCode::BAD_GATEWAY, format!("the DeepSeek harness did not answer: {e}")).into_response(),
    }
}

type Pipe = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn connect(url: &str, headers: &[(String, String)]) -> Result<Pipe, String> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
    let mut req = url.into_client_request().map_err(|e| e.to_string())?;
    for (k, v) in headers {
        let name = HeaderName::from_bytes(k.as_bytes()).map_err(|e| e.to_string())?;
        req.headers_mut().insert(name, HeaderValue::from_str(v).map_err(|e| e.to_string())?);
    }
    let (ws, _) = tokio_tungstenite::connect_async(req).await.map_err(|e| e.to_string())?;
    Ok(ws)
}

fn to_upstream(m: AxumMsg) -> Message {
    match m {
        AxumMsg::Text(t) => Message::Text(t.as_str().into()),
        AxumMsg::Binary(b) => Message::Binary(b.to_vec().into()),
        AxumMsg::Ping(b) => Message::Ping(b.to_vec().into()),
        AxumMsg::Pong(b) => Message::Pong(b.to_vec().into()),
        AxumMsg::Close(f) => Message::Close(f.map(|f| CloseFrame { code: CloseCode::from(f.code), reason: f.reason.as_str().into() })),
    }
}

fn to_client(m: Message) -> Option<AxumMsg> {
    Some(match m {
        Message::Text(t) => AxumMsg::Text(t.as_str().into()),
        Message::Binary(b) => AxumMsg::Binary(b.to_vec().into()),
        Message::Ping(b) => AxumMsg::Ping(b.to_vec().into()),
        Message::Pong(b) => AxumMsg::Pong(b.to_vec().into()),
        Message::Close(f) => AxumMsg::Close(f.map(|f| AxumClose { code: u16::from(f.code), reason: f.reason.as_str().into() })),
        Message::Frame(_) => return None,
    })
}

/// Relay frames both ways until either side closes.
async fn bridge(client: WebSocket, upstream: Pipe) {
    let (mut c_tx, mut c_rx) = client.split();
    let (mut u_tx, mut u_rx) = upstream.split();
    let up = async {
        while let Some(Ok(m)) = c_rx.next().await {
            let close = matches!(m, AxumMsg::Close(_));
            if u_tx.send(to_upstream(m)).await.is_err() || close {
                break;
            }
        }
        let _ = u_tx.close().await;
    };
    let down = async {
        while let Some(Ok(m)) = u_rx.next().await {
            let close = matches!(m, Message::Close(_));
            if let Some(m) = to_client(m) {
                if c_tx.send(m).await.is_err() || close {
                    break;
                }
            }
        }
        let _ = c_tx.close().await;
    };
    tokio::select! {
        _ = up => {},
        _ = down => {},
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::extract::ws::WebSocketUpgrade as Wsu;
    use axum::routing::{any, get};
    use std::time::Duration;
    use tokio::sync::oneshot;

    fn client() -> reqwest::Client {
        reqwest::Client::builder().no_proxy().build().unwrap()
    }

    async fn serve(app: axum::Router) -> u16 {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = l.local_addr().unwrap().port();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        port
    }

    fn origin_of(h: &HeaderMap) -> String {
        h.get("origin").and_then(|v| v.to_str().ok()).unwrap_or("-").to_string()
    }

    /// A fake fork: echoes Origin, Host and the body; streams an event, then a second once told to.
    async fn fake_fork(release: oneshot::Receiver<()>) -> u16 {
        let release = std::sync::Arc::new(tokio::sync::Mutex::new(Some(release)));
        let app = axum::Router::new()
            .route(
                "/api/{*m}",
                any(|h: HeaderMap, uri: axum::http::Uri, body: String| async move {
                    format!("origin={} host={} uri={uri} body={body}", origin_of(&h), h.get("host").unwrap().to_str().unwrap())
                }),
            )
            .route(
                "/plugins/events",
                get(move || {
                    let release = release.clone();
                    async move {
                        let rx = release.lock().await.take().unwrap();
                        let first = futures_util::stream::once(async { Ok::<_, std::io::Error>("data: one\n\n".to_string()) });
                        let second = futures_util::stream::once(async move {
                            let _ = rx.await;
                            Ok::<_, std::io::Error>("data: two\n\n".to_string())
                        });
                        Body::from_stream(first.chain(second))
                    }
                }),
            )
            .route(
                "/api/events.mux",
                get(|h: HeaderMap, ws: Wsu| async move {
                    let origin = origin_of(&h);
                    ws.on_upgrade(move |mut s| async move {
                        while let Some(Ok(AxumMsg::Text(t))) = s.recv().await {
                            let _ = s.send(AxumMsg::Text(format!("{t}|origin={origin}").into())).await;
                        }
                    })
                }),
            );
        serve(app).await
    }

    async fn rig() -> (u16, u16, oneshot::Sender<()>) {
        let (tx, rx) = oneshot::channel();
        let fork = fake_fork(rx).await;
        let mount = serve(router(format!("http://127.0.0.1:{fork}"))).await;
        (fork, mount, tx)
    }

    #[tokio::test]
    async fn http_is_proxied_with_the_origin_rewritten() {
        let (fork, mount, _tx) = rig().await;
        let text = client()
            .post(format!("http://127.0.0.1:{mount}/api/session.list?x=1"))
            .header("origin", "https://mesh.example:12130")
            .body("{\"a\":1}")
            .send()
            .await
            .unwrap()
            .text()
            .await
            .unwrap();
        assert_eq!(text, format!("origin=http://127.0.0.1:{fork} host=127.0.0.1:{fork} uri=/api/session.list?x=1 body={{\"a\":1}}"));
        let plain = client().get(format!("http://127.0.0.1:{mount}/api/x")).send().await.unwrap().text().await.unwrap();
        assert!(plain.starts_with("origin=- "), "no Origin stays no Origin: {plain}");
    }

    #[tokio::test]
    async fn a_server_sent_event_streams_before_the_body_ends() {
        let (_fork, mount, tx) = rig().await;
        let resp = client().get(format!("http://127.0.0.1:{mount}/plugins/events")).send().await.unwrap();
        let mut body = resp.bytes_stream();
        let first = tokio::time::timeout(Duration::from_secs(5), body.next()).await.expect("the first event arrives alone").unwrap().unwrap();
        assert_eq!(&first[..], b"data: one\n\n");
        tx.send(()).unwrap();
        let second = tokio::time::timeout(Duration::from_secs(5), body.next()).await.unwrap().unwrap().unwrap();
        assert_eq!(&second[..], b"data: two\n\n");
    }

    #[tokio::test]
    async fn a_websocket_round_trips_with_the_origin_rewritten() {
        use tokio_tungstenite::tungstenite::client::IntoClientRequest;
        let (fork, mount, _tx) = rig().await;
        let mut req = format!("ws://127.0.0.1:{mount}/api/events.mux").into_client_request().unwrap();
        req.headers_mut().insert("origin", "https://mesh.example:12130".parse().unwrap());
        let (mut ws, _) = tokio_tungstenite::connect_async(req).await.unwrap();
        ws.send(Message::Text("hi".into())).await.unwrap();
        let reply = tokio::time::timeout(Duration::from_secs(5), ws.next()).await.unwrap().unwrap().unwrap();
        assert_eq!(reply.into_text().unwrap().as_str(), format!("hi|origin=http://127.0.0.1:{fork}"));
        ws.close(None).await.unwrap();
    }

}
