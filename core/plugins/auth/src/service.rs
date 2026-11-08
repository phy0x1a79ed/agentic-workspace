//! The auth service: the `auth` and `httpsfront` tools on one `Service`, and the browser edge on
//! `AWM_HTTPS_PORT`, which reads the credential store in process.

use std::sync::Arc;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use crate::edge::{self, Edge, EdgeConfig, Private};
use crate::gate::AuthGate;
use crate::portal::{Grants, GRANT_KEY_ENV};
use crate::store::{AuthStore, INIT_ENV};
use crate::verbs::{self, Caller};
use crate::{certs, landing, snapshot, tls};

const PUBLIC_PROFILE: &str = "public";

const STATUS: &str = "Report the HTTPS front's listener port, TLS state, the SAN set of the leaf cert, the loopback gateway it fronts, and the /ca.crt URL shape for installing the root CA on a device.";

pub async fn serve() -> anyhow::Result<()> {
    let service = Service::from_env()?;
    let env = service.context().env().clone();
    snapshot::take(&env.awm_dir)?;
    let state = env.state_dir();
    std::fs::create_dir_all(&state)?;

    let create = std::env::var(INIT_ENV).is_ok_and(|v| v == "1");
    let store = AuthStore::open_for_start(AuthStore::path_in(&state), create)?;
    if landing::copy_once(&env.awm_dir.join("services/httpsfront/httpsfront.db"), &state.join("landing.db"))? {
        tracing::info!("auth: copied httpsfront.db to landing.db");
    }

    let port = tls::https_port().map_err(anyhow::Error::msg)?;
    let profile = std::env::var("AWM_EDGE_PROFILE").ok().map(|p| p.trim().to_lowercase()).filter(|p| !p.is_empty());
    let public = profile.as_deref() == Some(PUBLIC_PROFILE);
    let tls_on = tls::tls_enabled();
    let (tls_config, ca_path, san) = if tls_on {
        let cert_dir = certs::cert_dir(&state, &certs::legacy_service_dir(&awm_plugin_sdk::config::workspace_root()));
        let sans = certs::resolve_sans(None, Some(&certs::sans_file(&state)))?;
        let info = certs::ensure_certs(&cert_dir, None, Some(&sans))?;
        tracing::info!("auth: certs ready (SAN={})", info.san);
        (Some(tls::server_config(&info.cert, &info.key)?), info.ca, Some(info.san))
    } else {
        tracing::info!("auth: {}=0: plain HTTP on loopback, no certs", tls::EDGE_TLS_ENV);
        (None, Default::default(), None)
    };

    let grant_key = std::env::var(GRANT_KEY_ENV).ok();
    let edge = Arc::new(Edge::new(
        Arc::new(AuthGate::new(Arc::new(store.clone()))),
        Arc::new(Grants::new(grant_key, env.node.clone())),
        EdgeConfig {
            upstream: env.hub_url.clone(),
            ca_path,
            ca_download: !public,
            same_site: if public { "strict" } else { "lax" },
            behind_proxy: !tls_on,
            identity_cookies: &[],
        },
        Box::new(Private),
    ));
    edge::start(edge, port, tls_config).await.map_err(|e| anyhow::anyhow!("the edge could not listen on :{port}: {e}"))?;
    tracing::info!("auth: edge on :{port} -> {} (tls {}, profile {})", env.hub_url, if tls_on { "on" } else { "off" }, profile.as_deref().unwrap_or("default"));

    let status = json!({
        "listener_port": port, "tls": tls_on, "san": san, "upstream": env.hub_url, "serving": true,
        "profile": profile.as_deref().unwrap_or("default"),
    });
    register(service, Arc::new(store), status).run().await
}

fn register(mut service: Service, store: Arc<AuthStore>, status: Value) -> Service {
    let specs = verbs::verbs().into_iter().map(|v| (v.name, v.description, v.params)).chain([
        ("config_get", "The settings contract and its live values.", json!([])),
        ("config_set", "Merge values into the settings and answer as config_get.", json!([{"name": "values", "type": "object"}])),
    ]);
    for (name, description, params) in specs {
        let store = store.clone();
        service = service.verb(verbs::TOOL, name, description, params, move |call: Call| {
            let store = store.clone();
            async move {
                let (args, as_, plugin) = (call.declared_args(), call.as_.clone(), call.plugin.clone());
                let run = move || verbs::call(&store, name, &args, Caller { as_: as_.as_deref(), plugin: plugin.as_deref() }, crate::now());
                match tokio::task::spawn_blocking(run).await {
                    Ok(out) => out,
                    Err(e) => Err(VerbError::runtime(e.to_string())),
                }
            }
        });
        service = service.hidden();
    }
    service
        .api("config", verbs::config_fragment(&store))
        .verb("httpsfront", "status", STATUS, json!([]), move |_: Call| {
            let st = status_with_urls(&status);
            async move { Ok::<Value, VerbError>(st) }
        })
}

/// The status, with the URL a person reads out over a phone.
fn status_with_urls(status: &Value) -> Value {
    let host = certs::default_sans().into_iter().find_map(|t| t.strip_prefix("IP:").filter(|ip| *ip != "127.0.0.1").map(str::to_string));
    let base = format!("https://{}:{}", host.as_deref().unwrap_or("<host-ip>"), status["listener_port"]);
    let mut out = status.clone();
    out["ca_url"] = json!(format!("{base}/ca.crt"));
    out["url_shape"] = json!(format!("{base}/"));
    out
}
