//! `social gdrive-auth`: the installed-app loopback OAuth consent that mints a Drive refresh token.

use anyhow::{bail, Context};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use super::{Urls, SCOPE};

const DONE: &str = "The authentication flow has completed. You may close this window.";

/// Run the consent flow on 127.0.0.1:`port` (0 picks a free port) and return the refresh token.
/// It prints the consent URL and writes nothing to disk.
pub async fn gdrive_auth(client_id: &str, client_secret: &str, port: u16, open_browser: bool) -> anyhow::Result<String> {
    let listener = TcpListener::bind(("127.0.0.1", port))
        .await
        .with_context(|| format!("could not listen on 127.0.0.1:{port}"))?;
    flow(&reqwest::Client::new(), &Urls::from_env(), listener, client_id, client_secret, |url| {
        println!("Open this URL in a browser:\n\n{url}\n");
        if open_browser {
            let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
            let _ = std::process::Command::new(opener)
                .arg(url)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn();
        }
    })
    .await
}

fn random_token() -> String {
    URL_SAFE_NO_PAD.encode(rand::random::<[u8; 32]>())
}

async fn flow(
    http: &reqwest::Client,
    urls: &Urls,
    listener: TcpListener,
    client_id: &str,
    client_secret: &str,
    show: impl FnOnce(&str),
) -> anyhow::Result<String> {
    let redirect = format!("http://localhost:{}/", listener.local_addr()?.port());
    let (state, verifier) = (random_token(), random_token());
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", &redirect),
        ("scope", SCOPE),
        ("state", &state),
        ("access_type", "offline"),
        ("prompt", "consent"),
        ("code_challenge", &challenge),
        ("code_challenge_method", "S256"),
    ];
    let query: Vec<String> = params.iter().map(|(k, v)| format!("{k}={}", urlencoding::encode(v))).collect();
    show(&format!("{}?{}", urls.auth, query.join("&")));

    let code = loop {
        let (mut sock, _) = listener.accept().await?;
        let Some(params) = read_redirect(&mut sock).await else {
            reply(&mut sock, "404 Not Found", "").await;
            continue;
        };
        let get = |k: &str| params.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        if let Some(err) = get("error") {
            reply(&mut sock, "400 Bad Request", &format!("Authorization failed: {err}")).await;
            bail!("authorization was refused: {err}");
        }
        let Some(code) = get("code") else {
            reply(&mut sock, "404 Not Found", "").await;
            continue;
        };
        if get("state").as_deref() != Some(state.as_str()) {
            reply(&mut sock, "400 Bad Request", "State mismatch.").await;
            bail!("mismatching_state: CSRF Warning! State not equal in request and response.");
        }
        reply(&mut sock, "200 OK", DONE).await;
        break code;
    };

    let resp = http
        .post(&urls.token)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("redirect_uri", &redirect),
            ("code_verifier", &verifier),
        ])
        .send()
        .await
        .context("token exchange failed")?;
    let status = resp.status();
    let body: Value = resp.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        let err = body["error"].as_str().unwrap_or("token exchange failed");
        bail!("({err}) {}", body["error_description"].as_str().unwrap_or(status.as_str()));
    }
    match body["refresh_token"].as_str() {
        Some(t) if !t.is_empty() => Ok(t.to_string()),
        _ => bail!(
            "No refresh token was returned — re-run with a fresh consent (revoke prior access at \
             https://myaccount.google.com/permissions then retry)."
        ),
    }
}

/// The query of a `GET /?...` redirect, or `None` for any other request.
async fn read_redirect(sock: &mut TcpStream) -> Option<Vec<(String, String)>> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    while !buf.windows(4).any(|w| w == b"\r\n\r\n") && buf.len() < 16 * 1024 {
        let n = sock.read(&mut chunk).await.ok()?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let text = String::from_utf8_lossy(&buf);
    let target = text.lines().next()?.strip_prefix("GET ")?.split(' ').next()?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != "/" {
        return None;
    }
    let decode = |s: &str| urlencoding::decode(&s.replace('+', " ")).map(|c| c.into_owned()).unwrap_or_default();
    Some(
        query
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                (decode(k), decode(v))
            })
            .collect(),
    )
}

async fn reply(sock: &mut TcpStream, status: &str, body: &str) {
    let msg = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = sock.write_all(msg.as_bytes()).await;
    let _ = sock.shutdown().await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::post;
    use axum::{Form, Json, Router};
    use std::collections::HashMap;

    #[tokio::test]
    async fn consent_flow_returns_the_refresh_token() {
        let token = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let taddr = token.local_addr().unwrap();
        let app = Router::new().route(
            "/token",
            post(|Form(f): Form<HashMap<String, String>>| async move {
                assert_eq!(f["grant_type"], "authorization_code");
                assert_eq!(f["code"], "code-test");
                assert!(!f["code_verifier"].is_empty());
                assert!(f["redirect_uri"].starts_with("http://localhost:"));
                Json(serde_json::json!({"access_token": "a-test", "refresh_token": "r-minted"}))
            }),
        );
        tokio::spawn(async move { axum::serve(token, app).await.unwrap() });
        let urls = Urls {
            api: String::new(),
            upload: String::new(),
            token: format!("http://{taddr}/token"),
            auth: "http://auth.invalid/o/oauth2/auth".into(),
        };
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = tokio::sync::oneshot::channel::<String>();
        let run = tokio::spawn(async move {
            flow(&reqwest::Client::new(), &urls, listener, "cid", "secret", |u| tx.send(u.to_string()).unwrap()).await
        });
        let url = rx.await.unwrap();
        assert!(url.contains("access_type=offline") && url.contains("prompt=consent"));
        assert!(url.contains(&urlencoding::encode(SCOPE).into_owned()));
        let state = url.split("state=").nth(1).unwrap().split('&').next().unwrap().to_string();
        let http = reqwest::Client::new();
        let miss = http.get(format!("http://127.0.0.1:{port}/favicon.ico")).send().await.unwrap();
        assert_eq!(miss.status(), 404);
        let ok = http.get(format!("http://127.0.0.1:{port}/?state={state}&code=code-test")).send().await.unwrap();
        assert_eq!(ok.text().await.unwrap(), DONE);
        assert_eq!(run.await.unwrap().unwrap(), "r-minted");
    }

    #[tokio::test]
    async fn a_wrong_state_is_refused() {
        let urls = Urls { api: String::new(), upload: String::new(), token: String::new(), auth: "http://auth.invalid/".into() };
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let run = tokio::spawn(async move { flow(&reqwest::Client::new(), &urls, listener, "cid", "s", |_| {}).await });
        let r = reqwest::get(format!("http://127.0.0.1:{port}/?state=forged&code=c")).await.unwrap();
        assert_eq!(r.status(), 400);
        let err = run.await.unwrap().unwrap_err().to_string();
        assert!(err.starts_with("mismatching_state"), "{err}");
    }
}
