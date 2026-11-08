use std::sync::Arc;

use awm_plugin_sdk::Service;
use awm_plugin_transcripts::layout::Tree;
use awm_plugin_transcripts::{daily, runs, App, Cch};
use awm_store::Store;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let dir = ctx.env().state_dir();
    std::fs::create_dir_all(&dir)?;
    let store = Store::open(dir.join("transcripts.db"))?;
    let app = Arc::new(App { tree: Tree::home(), store: store.clone(), live: Box::new(Cch(ctx.clone())) });
    let (timer_ctx, timer_app) = (ctx.clone(), app.clone());
    awm_plugin_transcripts::service(svc, app)
        // Only the database here: work on the trees would keep the service unready.
        .on_start(async move { tokio::task::spawn_blocking(move || runs::open(&store)).await?.map_err(Into::into) })
        .on_ready(async move {
            ctx.spawn("daily", async move {
                daily::daily(timer_ctx, timer_app).await;
                Ok(())
            });
        })
        .run()
        .await
}
