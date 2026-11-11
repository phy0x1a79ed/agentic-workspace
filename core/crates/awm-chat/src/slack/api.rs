//! The one Slack Web API client both kinds of account share: form POSTs with a Bearer token,
//! the 429 `Retry-After` wait, and for a session account the `d` cookie and the `creds_cmd`
//! refresh on a stale-credential error.

use std::collections::HashMap;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::{Mutex, RwLock};

use awm_plugin_sdk::VerbError;

use crate::runtime;

/// Where the Web API lives, unless `AWM_SLACK_API` says otherwise.
pub const DEFAULT_API: &str = "https://slack.com/api";

/// Errors that mean the session's credentials went stale: refetch them and retry once.
const REFRESHABLE: [&str; 4] = ["invalid_auth", "not_authed", "token_revoked", "token_expired"];

/// Slack answered `ok: false`. `error` is Slack's code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiError {
    pub method: String,
    pub error: String,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "slack {}: {}", self.method, self.error)
    }
}

impl std::error::Error for ApiError {}

impl ApiError {
    /// The verb error Python raised: `SlackApiError`.
    pub fn verb(self) -> VerbError {
        VerbError::new("SlackApiError", self.to_string())
    }
}

#[derive(Clone, Default)]
struct Creds {
    token: String,
    cookie: String,
}

/// What a sender looks like to the lab: a display name, and whether they are outside it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Person {
    pub name: String,
    pub outside: bool,
}

pub struct Client {
    http: reqwest::Client,
    base: String,
    creds: RwLock<Creds>,
    creds_cmd: Option<String>,
    refresh: Mutex<()>,
    people: Mutex<HashMap<String, Person>>,
    /// This account's own team, for `outside`.
    pub team_id: std::sync::Mutex<String>,
}

pub type ApiResult = std::result::Result<Value, ApiError>;

impl Client {
    pub fn new(base: &str, token: &str, cookie: &str, creds_cmd: Option<String>) -> Self {
        Self {
            http: reqwest::Client::builder().connect_timeout(Duration::from_secs(20)).timeout(Duration::from_secs(120)).build().unwrap_or_default(),
            base: base.trim_end_matches('/').to_string(),
            creds: RwLock::new(Creds { token: token.into(), cookie: cookie.into() }),
            creds_cmd: creds_cmd.filter(|c| !c.trim().is_empty()),
            refresh: Mutex::new(()),
            people: Mutex::new(HashMap::new()),
            team_id: std::sync::Mutex::new(String::new()),
        }
    }

    /// The API base: `AWM_SLACK_API`, else slack.com.
    pub fn base_from_env() -> String {
        std::env::var("AWM_SLACK_API").ok().filter(|b| !b.trim().is_empty()).unwrap_or_else(|| DEFAULT_API.to_string())
    }

    pub fn http(&self) -> &reqwest::Client {
        &self.http
    }

    async fn auth_headers(&self, token: Option<&str>) -> Vec<(&'static str, String)> {
        let c = self.creds.read().await.clone();
        let mut h = vec![("Authorization", format!("Bearer {}", token.unwrap_or(&c.token)))];
        if token.is_none() && !c.cookie.is_empty() {
            h.push(("Cookie", format!("d={}", c.cookie)));
        }
        h
    }

    /// Run `creds_cmd` through `sh -c` and take the `{token, cookie}` it prints.
    pub async fn fetch_creds(&self) -> crate::Result<()> {
        let Some(cmd) = &self.creds_cmd else {
            return Err(runtime("no creds_cmd to refresh session credentials"));
        };
        let out = tokio::process::Command::new("sh")
            .arg("-c")
            .arg(cmd)
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .output()
            .await
            .map_err(|e| runtime(format!("creds_cmd did not run: {e}")))?;
        if !out.status.success() {
            let msg: String = String::from_utf8_lossy(&out.stderr).trim().chars().take(200).collect();
            let code = out.status.code().map(|c| c.to_string()).unwrap_or_else(|| "on a signal".into());
            return Err(runtime(format!("creds_cmd exited {code}: {msg}")));
        }
        let data: Value = serde_json::from_slice(&out.stdout).map_err(|e| runtime(format!("creds_cmd output was not JSON: {e}")))?;
        let field = |k: &str| data.get(k).and_then(Value::as_str).unwrap_or("").trim().to_string();
        let (token, cookie) = (field("token"), field("cookie"));
        if token.is_empty() || cookie.is_empty() {
            return Err(runtime("creds_cmd JSON missing token/cookie"));
        }
        *self.creds.write().await = Creds { token, cookie };
        Ok(())
    }

    /// POST one method form-encoded, as this account or with `token` (the app token).
    pub async fn call_as(&self, method: &str, token: Option<&str>, params: &[(&str, String)]) -> ApiResult {
        match self.call_once(method, token, params).await {
            Err(e) if token.is_none() && self.creds_cmd.is_some() && REFRESHABLE.contains(&e.error.as_str()) => {
                tracing::info!("slack auth error {:?} on {method}; refreshing creds", e.error);
                {
                    let _one = self.refresh.lock().await;
                    self.fetch_creds().await.map_err(|r| ApiError { method: method.into(), error: r.message })?;
                }
                self.call_once(method, token, params).await
            }
            other => other,
        }
    }

    pub async fn call(&self, method: &str, params: &[(&str, String)]) -> ApiResult {
        self.call_as(method, None, params).await
    }

    async fn call_once(&self, method: &str, token: Option<&str>, params: &[(&str, String)]) -> ApiResult {
        let url = format!("{}/{method}", self.base);
        let fail = |error: String| ApiError { method: method.into(), error };
        loop {
            let mut req = self.http.post(&url).form(params);
            for (k, v) in self.auth_headers(token).await {
                req = req.header(k, v);
            }
            let r = req.send().await.map_err(|e| fail(format!("http: {}", e.without_url())))?;
            let status = r.status();
            if status.as_u16() == 429 {
                let wait = r.headers().get("Retry-After").and_then(|v| v.to_str().ok()).and_then(|v| v.trim().parse::<f64>().ok()).unwrap_or(1.0);
                tokio::time::sleep(Duration::from_secs_f64(wait.clamp(0.0, 600.0))).await;
                continue;
            }
            let body: Value = r.json().await.unwrap_or(Value::Null);
            if body.get("ok").and_then(Value::as_bool) != Some(true) {
                return Err(fail(body.get("error").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| format!("http {}", status.as_u16()))));
            }
            return Ok(body);
        }
    }

    /// Every page of a cursor-paged method's `key`, at most `cap` items.
    pub async fn paged(&self, method: &str, key: &str, cap: Option<usize>, params: &[(&str, String)]) -> std::result::Result<Vec<Value>, ApiError> {
        let mut out: Vec<Value> = vec![];
        let mut cursor = String::new();
        loop {
            let mut p: Vec<(&str, String)> = vec![("limit", "200".into())];
            if !cursor.is_empty() {
                p.push(("cursor", cursor.clone()));
            }
            p.extend(params.iter().cloned());
            let r = self.call(method, &p).await?;
            out.extend(r.get(key).and_then(Value::as_array).cloned().unwrap_or_default());
            cursor = r.pointer("/response_metadata/next_cursor").and_then(Value::as_str).unwrap_or("").to_string();
            if cursor.is_empty() || cap.is_some_and(|c| out.len() >= c) {
                if let Some(c) = cap {
                    out.truncate(c);
                }
                return Ok(out);
            }
        }
    }

    /// The sender behind a user id, from `users.info`, cached. A user Slack will not describe is
    /// named by their id and counted as outside.
    pub async fn person(&self, user_id: &str) -> Person {
        if user_id.is_empty() {
            return Person::default();
        }
        if let Some(p) = self.people.lock().await.get(user_id) {
            return p.clone();
        }
        let p = match self.call("users.info", &[("user", user_id.to_string())]).await {
            Ok(r) => {
                let u = r.get("user").cloned().unwrap_or(json!({}));
                let s = |v: Option<&Value>| v.and_then(Value::as_str).unwrap_or("").to_string();
                let name = [s(u.pointer("/profile/display_name")), s(u.get("real_name")), s(u.get("name"))].into_iter().find(|n| !n.is_empty()).unwrap_or_else(|| user_id.to_string());
                let flag = |k: &str| u.get(k).and_then(Value::as_bool).unwrap_or(false);
                let team = s(u.get("team_id"));
                let own = self.team_id.lock().map(|t| t.clone()).unwrap_or_default();
                Person { name, outside: flag("is_restricted") || flag("is_ultra_restricted") || (!team.is_empty() && !own.is_empty() && team != own) }
            }
            Err(_) => Person { name: user_id.to_string(), outside: true },
        };
        self.people.lock().await.insert(user_id.to_string(), p.clone());
        p
    }

    /// GET an auth-gated `url_private` with this account's own credentials.
    pub async fn get_file(&self, url: &str) -> std::result::Result<reqwest::Response, reqwest::Error> {
        let mut req = self.http.get(url);
        for (k, v) in self.auth_headers(None).await {
            req = req.header(k, v);
        }
        req.send().await
    }

    /// The workspace name: `team.info`, else the `team` that `auth.test` gave.
    pub async fn team_name(&self, fallback: &str) -> String {
        match self.call("team.info", &[]).await {
            Ok(r) => r.pointer("/team/name").and_then(Value::as_str).filter(|n| !n.is_empty()).unwrap_or(fallback).to_string(),
            Err(_) => fallback.to_string(),
        }
    }
}

/// Every param that has a value, for the methods whose optional params Python dropped when None.
pub fn params<'a>(items: &[(&'a str, Option<String>)]) -> Vec<(&'a str, String)> {
    items.iter().filter_map(|(k, v)| v.clone().map(|v| (*k, v))).collect()
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use axum::extract::State;
    use axum::http::HeaderMap;
    use axum::routing::post;
    use axum::{Json, Router};
    use serde_json::{json, Value};

    use super::Client;

    type Seen = Arc<Mutex<Vec<(String, String)>>>;

    /// auth.test that takes only `xoxc-NEW`, recording each call's token and cookie.
    async fn fake() -> (String, Seen) {
        async fn auth(State(seen): State<Seen>, headers: HeaderMap) -> Json<Value> {
            let h = |k: &str| headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            let (auth, cookie) = (h("authorization"), h("cookie"));
            seen.lock().unwrap().push((auth.clone(), cookie));
            Json(if auth == "Bearer xoxc-NEW" { json!({"ok": true, "user_id": "UME"}) } else { json!({"ok": false, "error": "invalid_auth"}) })
        }
        let seen = Seen::default();
        let app = Router::new().route("/auth.test", post(auth)).with_state(seen.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (base, seen)
    }

    #[tokio::test]
    async fn stale_session_creds_are_refetched_once() {
        let (base, seen) = fake().await;
        let cmd = r#"printf '{"token":"xoxc-NEW","cookie":"xoxd-NEW"}'"#;
        let c = Client::new(&base, "xoxc-OLD", "xoxd-OLD", Some(cmd.into()));
        assert_eq!(c.call("auth.test", &[]).await.unwrap()["user_id"], "UME");
        let tokens: Vec<_> = seen.lock().unwrap().iter().map(|(a, _)| a.clone()).collect();
        assert_eq!(tokens, ["Bearer xoxc-OLD", "Bearer xoxc-NEW"]);
        assert_eq!(seen.lock().unwrap().last().unwrap().1, "d=xoxd-NEW");

        // With no creds_cmd there is nothing to refresh.
        let c = Client::new(&base, "xoxc-OLD", "xoxd-OLD", None);
        assert_eq!(c.call("auth.test", &[]).await.unwrap_err().to_string(), "slack auth.test: invalid_auth");
    }
}
