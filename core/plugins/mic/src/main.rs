use std::sync::Arc;
use std::time::Duration;

use awm_plugin_mic::audio::{self, Audio};
use awm_plugin_mic::bridge::Mic;
use awm_plugin_sdk::Service;

fn main() -> anyhow::Result<()> {
    audio::ensure_runtime_env();
    tokio::runtime::Builder::new_multi_thread().enable_all().build()?.block_on(run())
}

async fn run() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let var = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
    let sink = var("MIC_SINK").unwrap_or_else(|| audio::DEFAULT_SINK.into());
    let interval = var("MIC_HEALTH_INTERVAL_S").and_then(|v| v.trim().parse::<f64>().ok()).filter(|s| *s > 0.0).unwrap_or(20.0);
    let mic = Arc::new(Mic::new(Arc::new(Audio::system()), &sink));
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let loop_mic = mic.clone();
    awm_plugin_mic::service(svc, mic)
        .on_ready(async move {
            let c = ctx.clone();
            ctx.spawn("health", async move {
                awm_plugin_mic::health(c, loop_mic, Duration::from_secs_f64(interval)).await;
                Ok(())
            });
        })
        .run()
        .await
}
