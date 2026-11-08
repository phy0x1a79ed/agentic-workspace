//! The part on each node: follows the node's emit topics and reports them to the roster.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    awm_plugin_telemetry::run_node().await
}
