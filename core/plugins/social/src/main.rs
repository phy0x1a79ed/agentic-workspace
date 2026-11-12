//! The social plugin: the bot chat accounts and their seats (`awm/services/social`).

mod app;
mod config;
mod connectors;
mod db;
mod seats;

use std::collections::BTreeMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::Value;

use app::Social;

pub const MANIFEST: &str = include_str!("manifest.json");

async fn dispatch(app: Arc<Social>, name: &str, call: Call) -> Result<Value, VerbError> {
    match name {
        "send" => app.send(call).await,
        "react" => app.react(call).await,
        "fetch" => app.fetch(call).await,
        "history" => app.history(call).await,
        "seats" => app.seats_verb(call).await,
        "mount" => app.mount(call).await,
        "unmount" => app.unmount(call).await,
        "forward" => app.forward(call).await,
        "seat_remote_control" => app.seat_remote_control(call).await,
        "seat_refresh" => app.seat_refresh(call).await,
        "seat_pull" => app.seat_pull(call).await,
        "seat_report" => app.seat_report(call).await,
        "seat_act" => app.seat_act(call).await,
        "download_attachments" => app.download_attachments(call).await,
        "accounts" => app.accounts(call).await,
        "channels" => app.channels(call).await,
        "open_dm" => app.open_dm(call).await,
        "list_operators" => app.list_operators(call).await,
        "add_operator" => app.add_operator(call).await,
        "remove_operator" => app.remove_operator(call).await,
        "lookup" => app.lookup(call).await,
        "emit_probe" => app.emit_probe(call).await,
        other => Err(VerbError::runtime(format!("no handler for {other}"))),
    }
}

/// Every verb of `manifest.json`, served by [`dispatch`].
pub fn service(svc: Service, app: Arc<Social>) -> Service {
    let manifest: Value = serde_json::from_str(MANIFEST).expect("manifest.json parses");
    let mut svc = svc.api("emitters", manifest["emitters"].clone()).api("sessions", manifest["sessions"].clone());
    for f in manifest["functions"].as_array().unwrap() {
        let name = f["name"].as_str().unwrap().to_string();
        let a = app.clone();
        let n = name.clone();
        svc = svc.verb("social", &name, f["description"].as_str().unwrap(), f.get("params").cloned().unwrap_or(Value::Null), move |call| {
            let (a, n) = (a.clone(), n.clone());
            async move { dispatch(a, &n, call).await }
        });
        let tool = f["tool"].as_str().unwrap();
        if tool != format!("social_{name}") {
            svc = svc.published_as(tool);
        }
        if let Some(t) = f.get("timeout").and_then(Value::as_f64) {
            svc = svc.timeout(Duration::from_secs_f64(t));
        }
        if let Some(sf) = f.get("surfaces").and_then(Value::as_array) {
            svc = svc.surfaces(sf.iter().filter_map(Value::as_str).map(str::to_string).collect::<Vec<_>>());
        }
    }
    svc
}

async fn run() -> anyhow::Result<()> {
    // Both ring and aws-lc-rs are linked, so rustls has no default until one is named.
    let _ = tokio_rustls::rustls::crypto::ring::default_provider().install_default();
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    awm_statecheck::cli::state_subcommand("social", include_str!("../state.toml"), &awm_plugin_sdk::config::service_dir("social"), &awm_dir)?;
    let svc = Service::from_env()?;
    let env = svc.context().env().clone();
    let hub = svc.context().hub().clone();
    let loaded = config::load(&config::config_file(&env.awm_dir)).map_err(|e| anyhow::anyhow!(e))?;
    for n in &loaded.notes {
        tracing::info!("social.toml: {n}");
    }
    let db = db::Db::open(&awm_plugin_sdk::config::service_dir("social").join("social.db"))?;
    let conns: app::Conns = Arc::new(RwLock::new(BTreeMap::new()));
    let seats = seats::Seats::new(db.clone(), loaded.seats.clone(), loaded.accounts.clone());
    seats.sync().await;
    let app = Social::new(env.node.clone(), hub.clone(), db.clone(), loaded.accounts.clone(), conns.clone(), seats);
    let _ = app.ctx.set(svc.context());

    for a in loaded.accounts.iter().filter(|a| a.enabled) {
        db.upsert_account(&a.name, &a.platform, "bot", a.display_name.as_deref().unwrap_or(""), a.enabled).await;
        let (m, c) = (app.clone(), app.clone());
        let hooks = connectors::Hooks {
            on_message: Arc::new(move |msg| {
                let m = m.clone();
                Box::pin(async move { m.on_message(msg).await })
            }),
            on_command: Some(Arc::new(move |cmd| {
                let c = c.clone();
                Box::pin(async move { c.on_command(cmd).await })
            })),
            history: db::History::new(db.clone(), &a.name),
            db: db.clone(),
        };
        match connectors::build(a.clone(), hooks) {
            Ok(conn) => {
                conns.write().unwrap().insert(a.name.clone(), conn.clone());
                let name = a.name.clone();
                awm_plugin_sdk::spawn_supervised(&format!("social.{name}"), Duration::from_secs(5), move || conn.clone().start());
            }
            Err(e) => tracing::warn!("account {} failed to build: {e}", a.name),
        }
    }

    let ready = app.clone();
    service(svc, app.clone())
        .on_ready(async move {
            ready.watch_sessions().await;
        })
        .run()
        .await
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("social: {e:#}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests;
