//! The telemetry core plugin (fleet.md R147 items 1, 5 and 6): facts about the swarm, gathered
//! from each node's emit topics with no polling. The `telemetry` binary is the roster on alcor;
//! `telemetry-node` is the part on every node that opts in.

pub mod alcor;
pub mod node;
pub mod roster;
pub mod verbs;

use std::sync::Arc;

use awm_plugin_sdk::{config, Service};

/// The swarm this node opted in to: the install config's `[telemetry] swarm`.
pub fn swarm(cfg: &config::InstallConfig) -> Option<String> {
    cfg.get("telemetry")?.get("swarm")?.as_str().map(str::trim).filter(|s| !s.is_empty()).map(str::to_string)
}

/// Serve the roster.
pub async fn run_roster() -> anyhow::Result<()> {
    let svc = Service::from_env()?.state(include_str!("../state.toml"));
    let swarm = swarm(&config::InstallConfig::load()?);
    let part = alcor::RosterPart::load(svc.context().env().state_dir(), swarm.clone(), alcor::edge_singletons())?;
    let ctx = svc.context();
    let follow = part.clone();
    alcor::service(svc, part)
        .on_ready(async move {
            if swarm.is_some() {
                if config::edges().is_ok_and(|e| e.singletons.iter().any(|t| t.tool == "trilium")) {
                    tokio::spawn(follow.clone().push_to_vault(ctx.hub().clone()));
                }
                follow.follow_seats(ctx);
            } else {
                tracing::warn!("no [telemetry] swarm in the install config: this roster refuses every report");
            }
        })
        .run()
        .await
}

/// Run the node part: follow this node's topics and report to the roster.
pub async fn run_node() -> anyhow::Result<()> {
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let Some(swarm) = swarm(&config::InstallConfig::load()?) else {
        tracing::info!("no [telemetry] swarm in the install config: this node reports nothing");
        return svc.run().await;
    };
    let node = config::label(&ctx.env().node);
    svc.on_ready(async move {
        let hub = ctx.hub().clone();
        let probe = Arc::new(node::GatewayProbe::new(hub.clone(), node.clone()));
        let part = node::start(hub.clone(), node, swarm, probe, node::forward_through(hub), node::marks());
        let _ = part.tx.send(node::Msg::Resync);
    })
    .run()
    .await
}
