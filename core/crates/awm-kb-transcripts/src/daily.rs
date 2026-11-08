//! The sweep, inside the plugin: it replaces the `claude-archive-sessions` systemd timer.
//!
//! A session's stop on `reflection/sessions` triggers it, and a start catches up, at most once
//! per [`SWEEP_GAP_S`]. A stop never archives that session early: the transcript stays for the
//! retention days, so `resume_other` keeps working. The sweep runs on its own thread at idle IO
//! priority and nice 19, records `trigger = "timer"`, and indexes the sessions it archived.

use std::sync::Arc;
use std::time::Instant;

use awm_plugin_sdk::{Context, SubscriptionOpts, SupervisedSubscription};
use serde_json::Value;

use crate::{runs, sweep, App};

/// The shortest gap between two sweeps. A stop inside it does nothing.
pub const SWEEP_GAP_S: f64 = 12.0 * 3600.0;

/// Whether a sweep runs now: none ran yet, or the last one is older than the gap.
pub fn due(last: Option<f64>, now: f64) -> bool {
    last.is_none_or(|t| now - t > SWEEP_GAP_S)
}

fn lower_priority() {
    // SAFETY: both calls only change this thread's own scheduling.
    unsafe {
        const IOPRIO_WHO_PROCESS: libc::c_long = 1;
        const IOPRIO_CLASS_IDLE: libc::c_long = 3;
        libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0 as libc::c_long, IOPRIO_CLASS_IDLE << 13);
        let tid = libc::syscall(libc::SYS_gettid) as libc::id_t;
        libc::setpriority(libc::PRIO_PROCESS, tid, 19);
    }
}

/// Run `f` on a fresh thread at idle IO priority and nice 19, so it yields the disk.
pub fn idle_thread(f: Box<dyn FnOnce() + Send>) {
    std::thread::spawn(move || {
        lower_priority();
        f();
    });
}

/// Run `f` on a fresh idle thread and wait for its answer.
async fn idle<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    idle_thread(Box::new(move || {
        let _ = tx.send(f());
    }));
    rx.await.ok()
}

/// One timer sweep, counted as a background run of `nightly` with its outcome.
pub async fn run_once(ctx: &Context, app: &App) {
    let started = Instant::now();
    let ok = match app.live.session_ids().await {
        Err(why) => {
            tracing::error!("transcripts: the daily sweep did not run: {why}");
            false
        }
        Ok(live) => {
            let tree = app.tree.clone();
            match idle(move || sweep::archive_sessions(&tree, sweep::DEFAULT_RETENTION_DAYS, false, &live, None)).await {
                None => false,
                Some((result, moved)) => {
                    app.index_moved(moved).await;
                    if let Err(e) = runs::record(&app.store, "sweep", &result, "timer").await {
                        tracing::error!("transcripts: the daily sweep ran but its row was not written: {e}");
                    }
                    let failed = result["failed"].as_array().map_or(0, Vec::len);
                    tracing::info!("transcripts: daily sweep moved {} sessions, {} failed", result["sessions"], failed);
                    failed == 0
                }
            }
        }
    };
    ctx.usage("nightly", ok, started.elapsed().as_millis() as u64);
}

/// Whether the last sweep leaves room for another.
async fn sweep_due(app: &App) -> bool {
    match runs::last_timer(&app.store).await {
        Ok(last) => due(last, sweep::now()),
        Err(e) => {
            tracing::warn!("transcripts: could not read the last sweep: {e}");
            false
        }
    }
}

/// Catch up if due, then sweep when a session stops and the last sweep is past the gap.
pub async fn daily(ctx: Context, app: Arc<App>) {
    if sweep_due(&app).await {
        run_once(&ctx, &app).await;
    }
    let hub = ctx.hub().clone();
    let connect = move || {
        let hub = hub.clone();
        async move { hub.subscribe("reflection", "sessions", None, None).await }
    };
    let handler = move |event: Value| {
        let (ctx, app) = (ctx.clone(), app.clone());
        async move {
            if event.get("event").and_then(Value::as_str) == Some("stopped") && sweep_due(&app).await {
                run_once(&ctx, &app).await;
            }
            Ok(())
        }
    };
    SupervisedSubscription::spawn("transcripts-sweep", SubscriptionOpts::default(), connect, handler);
}
