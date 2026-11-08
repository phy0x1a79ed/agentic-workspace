//! The auth service: the `auth` and `httpsfront` tools on one `Service`, and the browser edge on
//! `AWM_HTTPS_PORT`, which reads the credential store in process.

use std::sync::Arc;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use crate::edge::{self, Edge, EdgeConfig};
use crate::front::{Front, Upstreams};
use crate::fronts::{self, Fronts};
use crate::gate::AuthGate;
use crate::portal::{Grants, GRANT_KEY_ENV};
use crate::store::{AuthStore, INIT_ENV};
use crate::verbs::{self, Caller};
use crate::landing::{self, Landing};
use crate::{certs, policy, snapshot, tls};

const PUBLIC_PROFILE: &str = "public";

const STATUS: &str = "Report the HTTPS front's listener port, TLS state, the SAN set of the leaf cert, the loopback gateway it fronts, and the /ca.crt URL shape for installing the root CA on a device.";

const FRONT: &str = "Report one service's front: the TLS port auth opened for its [[service.front]] declaration, the loopback upstream, the leaf's SAN set, whether it is serving and the URL to open. name defaults to gui.";

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
    let sans_file = certs::sans_file(&state);
    let (tls_config, ca_path, san) = if tls_on {
        let cert_dir = certs::cert_dir(&state, &certs::legacy_service_dir(&awm_plugin_sdk::config::workspace_root()));
        let sans = certs::resolve_sans(None, Some(&sans_file))?;
        let info = certs::ensure_certs(&cert_dir, None, Some(&sans))?;
        tracing::info!("auth: certs ready (SAN={})", info.san);
        (Some(tls::server_config(&info.cert, &info.key)?), info.ca, Some(info.san))
    } else {
        tracing::info!("auth: {}=0: plain HTTP on loopback, no certs", tls::EDGE_TLS_ENV);
        (None, Default::default(), None)
    };

    let hub = service.context().hub().clone();
    let upstreams = Upstreams::from_env();
    let front = Front::new(public, upstreams.clone(), hub, Landing::open(&state.join("landing.db"))?);

    let grant_key = std::env::var(GRANT_KEY_ENV).ok();
    let gate = Arc::new(AuthGate::new(Arc::new(store.clone())));
    let grants = Arc::new(Grants::new(grant_key, env.node.clone()));
    let same_site = if public { "strict" } else { "lax" };
    let fronts = Arc::new(Fronts::new(fronts::Base {
        gate: gate.clone(),
        grants: grants.clone(),
        ca_path: ca_path.clone(),
        ca_download: !public,
        same_site,
        tls: tls_config.clone(),
        san: san.clone(),
        sans_file,
    }));
    let edge = Arc::new(Edge::new(
        gate,
        grants,
        EdgeConfig {
            upstream: env.hub_url.clone(),
            ca_path,
            ca_download: !public,
            same_site,
            behind_proxy: !tls_on,
            identity_cookies: &["trilium.sid", "trilium-csrf"],
        },
        Box::new(front),
    ));
    edge::start(edge, port, tls_config).await.map_err(|e| anyhow::anyhow!("the edge could not listen on :{port}: {e}"))?;
    tracing::info!("auth: edge on :{port} -> {} (tls {}, profile {})", env.hub_url, if tls_on { "on" } else { "off" }, profile.as_deref().unwrap_or("default"));

    let status = json!({
        "listener_port": port, "tls": tls_on, "san": san, "upstream": env.hub_url, "serving": true,
        "profile": profile.as_deref().unwrap_or("default"),
        "vault_upstream": upstreams.vault, "relay_upstream": upstreams.tether, "board_upstream": upstreams.board,
        "vault_mount": upstreams.vault.as_ref().map(|_| policy::vault::SHELL),
        "relay_mounts": upstreams.tether.as_ref().map(|_| [policy::tether::PREFIX, policy::tether::ROOMS]),
        "board_mount": upstreams.board.as_ref().map(|_| policy::board::PREFIX),
    });
    let _watch = fronts::watch(fronts.clone(), service.context().hub().clone());
    register(service, Arc::new(store), status, fronts).run().await
}

fn register(mut service: Service, store: Arc<AuthStore>, status: Value, fronts: Arc<Fronts>) -> Service {
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
    service = service.verb(verbs::TOOL, "front", FRONT, json!([{"name": "service", "type": "string", "required": true}, {"name": "name", "type": "string"}]), move |call: Call| {
        let fronts = fronts.clone();
        async move {
            let args = call.declared_args();
            let text = |key: &str| args.get(key).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
            let service = text("service").ok_or_else(|| VerbError::value("service is required"))?;
            Ok::<Value, VerbError>(fronts.state(&service, &text("name").unwrap_or_else(|| "gui".into())).await)
        }
    });
    service
        .hidden()
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
