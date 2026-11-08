//! The roster: `telemetry nodes`, `services` and `seats` on alcor.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    awm_plugin_telemetry::run_roster().await
}
