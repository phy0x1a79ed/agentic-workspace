//! The `outsource` binary. With the `awm` feature, `outsource awm-plugin` hosts the market as an awm
//! service: agents reach the caller verbs through the gateway and promise events go out on the
//! `promise` emit topic. outsource's own crates never import awm.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cmd = outsource::cli::command();
    #[cfg(feature = "awm")]
    let cmd = cmd.subcommand(clap::Command::new("awm-plugin").about("Run as the awm outsource service."));
    let m = cmd.get_matches();
    #[cfg(feature = "awm")]
    if m.subcommand_name() == Some("awm-plugin") {
        return awm::run().await;
    }
    outsource::cli::run(m).await
}

#[cfg(feature = "awm")]
mod awm {
    use awm_plugin_sdk::{Call, Service, VerbError};
    use outsource::{offer, roles, surfaces};
    use outsource_proto::{Msg, WatchArgs};
    use serde_json::{json, Value};
    use std::sync::Arc;

    /// The catalog's JSON schema as the plugin contract's param list.
    fn params(schema: &str) -> Value {
        let schema: Value = serde_json::from_str(schema).expect("catalog schema is JSON");
        let required: Vec<&str> = schema["required"].as_array().map(|r| r.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
        let list: Vec<Value> = schema["properties"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(name, p)| {
                json!({"name": name, "type": p["type"], "required": required.contains(&name.as_str()),
                       "description": p.get("description").and_then(Value::as_str).unwrap_or("")})
            })
            .collect();
        Value::Array(list)
    }

    /// The caller's args without the gateway's private `_` keys, which outsource's strict args refuse.
    fn outsource_args(call: &Call) -> Value {
        let mut args = call.args.clone();
        if let Some(obj) = args.as_object_mut() {
            obj.retain(|k, _| !k.starts_with('_'));
        }
        args
    }

    pub async fn run() -> anyhow::Result<()> {
        awm_plugin_sdk::telemetry::init();
        let market = outsource_market::Market::new("market");
        let client = Arc::new(roles::local_client(&market, "awm"));
        let mut svc = Service::from_env()?.emitter("promise", Some("A outsource promise changed phase."));
        for v in surfaces::remote_verbs() {
            let (client, verb) = (client.clone(), v.name);
            svc = svc.verb("outsource", v.name, v.summary, params(v.schema), move |call: Call| {
                let client = client.clone();
                async move {
                    let site = offer::Site::of_caller(call.peer_node());
                    surfaces::invoke(&client, verb, outsource_args(&call), &site).await.map_err(|e| {
                        let name = e["args"]["name"].as_str().unwrap_or("doesNotUnderstand").to_string();
                        VerbError::new(name, e.to_string())
                    })
                }
            });
        }
        let ctx = svc.context();
        let events = roles::local_client(&market, "awm-events");
        let mut rx = events.events().expect("fresh client");
        tokio::spawn(async move {
            let _keep = &events;
            if events.ask(Msg::Watch(WatchArgs { promises: vec![] })).await.is_ok() {
                while let Some(Msg::Event(ev)) = rx.recv().await {
                    ctx.emit("promise", &ev);
                }
            }
        });
        let cfg = roles::MarketConfig::load()?;
        tokio::spawn(async move {
            if let Err(e) = roles::serve(market, cfg).await {
                tracing::error!("outsource market: {e:#}");
            }
        });
        svc.run().await
    }
}
