//! The productivity plugin: Tony's own mail, chats and drives, read and staged, never sent.
//!
//! `productivity` serves the plugin on alcor. `productivity gdrive-auth` mints a Google Drive
//! refresh token. Slack, Teams and Discord chats go through alcor's shared browser. Every verb
//! runs [`admit`] first: only sessions on the nodes in `callers` are served, and never a seat.

mod buckets;
mod chat;
mod config;
mod gmail;
mod slack;
mod verbs;

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_chat::{permission, unsupported, Channel, Downloaded, Identity, Inbound, Result};
use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::Value;

use verbs::Productivity;

pub const MANIFEST: &str = include_str!("manifest.json");

/// One of Tony's accounts, read on demand. No account has a send.
#[async_trait]
pub trait Account: Send + Sync {
    fn platform(&self) -> &str;
    /// One cheap authenticated round trip, for `accounts`.
    async fn probe(&self) -> Result<()>;
    /// Existing messages, oldest first. `before` pages backwards.
    async fn fetch(&self, channel: &str, limit: usize, before: Option<&str>) -> Result<Vec<Inbound>>;
    async fn search(&self, query: &str, limit: usize, channel: Option<&str>) -> Result<Vec<Inbound>>;
    /// Re-fetch one message and return its attachments' bytes, or only attachment `idx`.
    async fn download_attachments(&self, channel: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>>;
    async fn list_channels(&self, include_dms: bool) -> Result<Vec<Channel>>;
    async fn identity(&self) -> Result<Identity>;
    fn as_gmail(&self) -> Option<&gmail::GmailConnector> {
        None
    }
}

/// Refuse a call unless every node behind it is in `callers` and it comes from no seat.
///
/// The node comes from the door's key and cannot be forged. A seat's name and scope are the
/// calling node's word, so the seat check only backs up the node list.
pub fn admit(callers: &[String], call: &Call) -> std::result::Result<(), VerbError> {
    let c = &call.caller;
    for node in std::iter::once(c.node.as_str()).chain(c.origin.as_deref()) {
        if !callers.iter().any(|n| n == node) {
            return Err(permission(format!("productivity serves sessions on {} only; a call from {node:?} is refused", callers.join(", "))));
        }
    }
    let claim = call.caller_claim();
    let names = [c.name.as_deref(), claim.and_then(|v| v.get("name")).and_then(Value::as_str)];
    let scopes = [c.scope.as_deref(), call.caller_scope()];
    if names.iter().flatten().any(|n| n.starts_with("seat:")) || scopes.iter().flatten().any(|s| s.starts_with("seats/")) {
        return Err(permission("a seat cannot reach Tony's own accounts"));
    }
    Ok(())
}

async fn dispatch(app: Arc<Productivity>, name: &str, call: Call) -> std::result::Result<Value, VerbError> {
    admit(&app.callers, &call)?;
    match name {
        "accounts" => app.accounts().await,
        "channels" => app.channels(call).await,
        "fetch" => app.fetch(call).await,
        "search" => app.search(call).await,
        "download_attachments" => app.download_attachments(call).await,
        "draft" => app.draft(call).await,
        "chat_read" => app.chat_read(call).await,
        "chat_download" => app.chat_download(call).await,
        "chat_stage" => app.chat_stage(call).await,
        "buckets" => app.buckets_verb().await,
        "bucket_ls" => app.bucket_ls(call).await,
        "bucket_stat" => app.bucket_stat(call).await,
        "bucket_mkdir" => app.bucket_mkdir(call).await,
        "bucket_get" => app.bucket_get(call).await,
        "bucket_put" => app.bucket_put(call).await,
        "bucket_rm" => app.bucket_rm(call).await,
        "bucket_mv" => app.bucket_mv(call).await,
        "bucket_cp" => app.bucket_cp(call).await,
        "bucket_search" => app.bucket_search(call).await,
        other => Err(unsupported(format!("no handler for {other}"))),
    }
}

/// Every verb of `manifest.json`, served by [`dispatch`].
pub fn service(mut svc: Service, app: Arc<Productivity>) -> Service {
    let manifest: Value = serde_json::from_str(MANIFEST).expect("manifest.json parses");
    for f in manifest["functions"].as_array().unwrap() {
        let name = f["name"].as_str().unwrap().to_string();
        let (a, n) = (app.clone(), name.clone());
        svc = svc.verb("productivity", &name, f["description"].as_str().unwrap(), f.get("params").cloned().unwrap_or(Value::Null), move |call| {
            let (a, n) = (a.clone(), n.clone());
            async move { dispatch(a, &n, call).await }
        });
        if let Some(t) = f.get("timeout").and_then(Value::as_f64) {
            svc = svc.timeout(Duration::from_secs_f64(t));
        }
    }
    svc
}

fn arg(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned()
}

/// `productivity gdrive-auth --client-id <id> (--client-secret <s> | --client-secret-file <path>) [--port N] [--no-browser]`
async fn gdrive_auth(args: &[String]) -> anyhow::Result<()> {
    let id = arg(args, "--client-id").ok_or_else(|| anyhow::anyhow!("--client-id is required"))?;
    let secret = match (arg(args, "--client-secret"), arg(args, "--client-secret-file")) {
        (Some(s), _) => s,
        (None, Some(f)) => std::fs::read_to_string(f)?.split_whitespace().collect(),
        _ => anyhow::bail!("--client-secret or --client-secret-file is required"),
    };
    let port = arg(args, "--port").map(|p| p.parse()).transpose()?.unwrap_or(0);
    let token = buckets::gdrive::gdrive_auth(&id, &secret, port, !args.iter().any(|a| a == "--no-browser")).await?;
    println!("{token}");
    Ok(())
}

async fn run() -> anyhow::Result<()> {
    // Both ring and aws-lc-rs are linked, so rustls has no default until one is named.
    let _ = tokio_rustls::rustls::crypto::ring::default_provider().install_default();
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let env = svc.context().env().clone();
    let loaded = config::load(&config::config_file(&env.awm_dir)).map_err(|e| anyhow::anyhow!(e))?;
    for n in &loaded.notes {
        tracing::info!("productivity.toml: {n}");
    }
    if loaded.callers.is_empty() {
        tracing::warn!("productivity.toml names no callers: every call is refused");
    }
    let app = Arc::new(Productivity::new(env.node.clone(), loaded));
    service(svc, app).run().await
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = match args.first().map(String::as_str) {
        Some("gdrive-auth") => gdrive_auth(&args[1..]).await,
        _ => run().await,
    };
    if let Err(e) = out {
        eprintln!("productivity: {e:#}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests;
