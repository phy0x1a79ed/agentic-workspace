//! The subcommands `sandbox/run.sh` runs against its own gateway's `POST /invoke`: seed it, and
//! list the services it runs.

use std::path::Path;
use std::time::Duration;

use serde_json::{json, Value};

pub const SCOPES: [(&str, &str, &str); 3] = [
    ("demo", "alpha", "First scope. Use the UI to browse history/artifacts."),
    ("demo", "beta", "Second scope under demo."),
    ("playground", "experiment", "Sandbox scope for UI experiments."),
];

const POSTS: [&str; 2] =
    ["hello from the seeder — this is a fake channel post", "subscribe to demo/alpha in the UI to see this"];

pub struct Gateway {
    base: String,
    http: reqwest::Client,
}

impl Gateway {
    pub fn new(port: u16, timeout: Duration) -> Self {
        let http = reqwest::Client::builder().timeout(timeout).build().expect("an http client");
        Self { base: format!("http://127.0.0.1:{port}"), http }
    }

    /// `(status, body)`, the body parsed as JSON when it is JSON.
    async fn invoke(&self, name: &str, args: Value) -> Result<(u16, Value), String> {
        let resp = self
            .http
            .post(format!("{}/invoke", self.base))
            .json(&json!({"name": name, "args": args}))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let text = resp.text().await.map_err(|e| e.to_string())?;
        let body = if text.is_empty() { json!("") } else { serde_json::from_str(&text).unwrap_or(Value::String(text)) };
        Ok((status, body))
    }

    /// The verb's `result`, which arrives as JSON text.
    async fn result(&self, name: &str, args: Value) -> Result<Value, String> {
        let (code, body) = self.invoke(name, args).await?;
        if code != 200 {
            return Err(format!("[seed] invoke {name}: {code} {body}"));
        }
        Ok(match body.get("result") {
            Some(Value::String(s)) => serde_json::from_str(s).unwrap_or_else(|_| json!({"_raw": s})),
            Some(Value::Null) | None => json!({}),
            Some(other) => other.clone(),
        })
    }
}

/// Refuse to seed anything but the sandbox: `AWM_WORKSPACE` must be the sandbox folder.
pub fn check_sandbox(workspace: Option<&str>, sandbox: &Path) -> Result<(), String> {
    let same = workspace.filter(|w| !w.is_empty()).and_then(|w| Path::new(w).canonicalize().ok())
        == sandbox.canonicalize().ok();
    if workspace.is_some_and(|w| !w.is_empty()) && same {
        return Ok(());
    }
    Err(format!(
        "refusing to seed: AWM_WORKSPACE must point at {} (got {}). Run via sandbox/run.sh seed.",
        sandbox.display(),
        workspace.map(crate::lifecycle::py_repr).unwrap_or_else(|| "None".into())
    ))
}

/// Create the demo scopes and seed demo/alpha's channel. Safe to re-run: what exists is skipped.
pub async fn seed(gw: &Gateway, out: &mut impl std::io::Write) -> Result<(), String> {
    for (project, scope, context) in SCOPES {
        let label = format!("scope {project}/{scope}");
        let args = json!({"project": project, "scope": scope, "context": context, "new_project": true});
        let (code, body) = gw.invoke("scope_create", args).await.map_err(|e| format!("[seed] {label}: {e}"))?;
        let blob = body.to_string();
        let already = blob.contains("already exists") || blob.contains("already has an active session");
        if code == 200 {
            let _ = writeln!(out, "[seed] created {label}");
        } else if code == 409 || already {
            let _ = writeln!(out, "[seed] {label} already exists, skipping");
        } else {
            return Err(format!("[seed] {label}: {code} {body}"));
        }
    }
    let existing = gw.result("journal_fetch", json!({"project": "demo", "scope": "alpha", "limit": 1})).await?;
    if existing.get("total").and_then(Value::as_i64).unwrap_or(0) == 0 {
        for body in POSTS {
            gw.result("journal_post", json!({"project": "demo", "scope": "alpha", "author": "user:dev", "body": body}))
                .await?;
        }
        let _ = writeln!(out, "[seed] seeded demo/alpha channel");
    } else {
        let _ = writeln!(out, "[seed] demo/alpha channel already has posts, skipping");
    }
    let _ = writeln!(out, "[seed] done.");
    Ok(())
}

/// One `<name> <status> <pid>` line per service the gateway runs, or none when it does not answer.
pub async fn services(gw: &Gateway) -> Vec<String> {
    let Ok(rows) = gw.result("awm_services", json!({})).await else { return vec![] };
    let field = |r: &Value, k: &str, dflt: &str| match r.get(k) {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(Value::Number(n)) if n.as_f64() != Some(0.0) => n.to_string(),
        _ => dflt.to_string(),
    };
    rows.as_array()
        .map(|a| a.iter().map(|r| format!("{} {} {}", field(r, "name", "?"), field(r, "status", "?"), field(r, "pid", "-"))).collect())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{extract::State, routing::post, Json, Router};
    use std::sync::{Arc, Mutex};

    #[derive(Clone, Default)]
    struct Fake {
        calls: Arc<Mutex<Vec<Value>>>,
        create_status: u16,
        create_body: Value,
        total: i64,
    }

    async fn invoke(State(f): State<Fake>, Json(body): Json<Value>) -> (axum::http::StatusCode, Json<Value>) {
        f.calls.lock().unwrap().push(body.clone());
        let ok = axum::http::StatusCode::OK;
        match body["name"].as_str().unwrap() {
            "scope_create" => (axum::http::StatusCode::from_u16(f.create_status).unwrap(), Json(f.create_body.clone())),
            "journal_fetch" => (ok, Json(json!({"result": json!({"total": f.total}).to_string()}))),
            "journal_post" => (ok, Json(json!({"result": "{\"ok\": true}"}))),
            "awm_services" => (
                ok,
                Json(json!({"result": json!([{"name": "scopes", "status": "ready", "pid": 41}, {"name": "kb", "status": "down", "pid": null}]).to_string()})),
            ),
            _ => (axum::http::StatusCode::NOT_FOUND, Json(json!({}))),
        }
    }

    async fn serve(f: Fake) -> u16 {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let app = Router::new().route("/invoke", post(invoke)).with_state(f);
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        port
    }

    fn names(f: &Fake) -> Vec<String> {
        f.calls.lock().unwrap().iter().map(|c| c["name"].as_str().unwrap().to_string()).collect()
    }

    #[tokio::test]
    async fn seeds_three_scopes_then_two_posts_on_an_empty_channel() {
        let f = Fake { create_status: 200, create_body: json!({"ok": true}), ..Default::default() };
        let gw = Gateway::new(serve(f.clone()).await, Duration::from_secs(5));
        let mut out = Vec::new();
        seed(&gw, &mut out).await.unwrap();
        assert_eq!(names(&f), ["scope_create", "scope_create", "scope_create", "journal_fetch", "journal_post", "journal_post"]);
        let text = String::from_utf8(out).unwrap();
        assert!(text.contains("[seed] created scope playground/experiment"));
        assert!(text.ends_with("[seed] done.\n"));
    }

    #[tokio::test]
    async fn skips_what_exists() {
        for (status, body) in [(409, json!({})), (500, json!({"detail": "scope demo/alpha already exists"}))] {
            let f = Fake { create_status: status, create_body: body, total: 3, ..Default::default() };
            let gw = Gateway::new(serve(f.clone()).await, Duration::from_secs(5));
            let mut out = Vec::new();
            seed(&gw, &mut out).await.unwrap();
            assert_eq!(names(&f), ["scope_create", "scope_create", "scope_create", "journal_fetch"]);
            assert!(String::from_utf8(out).unwrap().contains("already has posts, skipping"));
        }
    }

    #[test]
    fn refuses_outside_the_sandbox() {
        let tmp = tempfile::tempdir().unwrap();
        let sandbox = tmp.path().join("sandbox");
        std::fs::create_dir_all(&sandbox).unwrap();
        assert!(check_sandbox(Some(sandbox.to_str().unwrap()), &sandbox).is_ok());
        assert!(check_sandbox(Some(tmp.path().to_str().unwrap()), &sandbox).unwrap_err().starts_with("refusing to seed"));
        assert!(check_sandbox(None, &sandbox).unwrap_err().contains("(got None)"));
    }

}
