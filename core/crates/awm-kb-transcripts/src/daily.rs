//! The daily sweep, inside the plugin: it replaces the `claude-archive-sessions` systemd timer.
//!
//! It runs at about 03:30 local time, and once at start when the last timer sweep is more than
//! 26 h old, so a node that was off catches up. The sweep runs on its own thread at idle IO
//! priority and nice 19, and records `trigger = "timer"`.

use std::sync::Arc;
use std::time::{Duration, Instant};

use awm_plugin_sdk::Context;

use crate::{runs, sweep, App};

pub const AT_HOUR: i32 = 3;
pub const AT_MINUTE: i32 = 30;
pub const CATCH_UP_AFTER_S: f64 = 26.0 * 3600.0;

/// The next local `hour:minute` after `now`, as Unix seconds, through the C library's zone rules.
pub fn next_at(now: f64, hour: i32, minute: i32) -> f64 {
    // SAFETY: localtime_r and mktime only read and write the tm passed to them.
    unsafe {
        let t = now.floor() as libc::time_t;
        let mut base: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut base);
        for add_days in 0..3 {
            let mut tm = base;
            tm.tm_mday += add_days;
            tm.tm_hour = hour;
            tm.tm_min = minute;
            tm.tm_sec = 0;
            tm.tm_isdst = -1;
            let at = libc::mktime(&mut tm) as f64;
            if at > now {
                return at;
            }
        }
        now + 86400.0
    }
}

/// Whether the last timer sweep is old enough that this start catches up. A node with no timer
/// sweep yet waits for 03:30.
pub fn due(last: Option<f64>, now: f64) -> bool {
    last.is_some_and(|t| now - t > CATCH_UP_AFTER_S)
}

/// Run `f` on a fresh thread at idle IO priority and nice 19, so the sweep yields the disk.
async fn idle<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    std::thread::spawn(move || {
        // SAFETY: both calls only change this thread's own scheduling.
        unsafe {
            const IOPRIO_WHO_PROCESS: libc::c_long = 1;
            const IOPRIO_CLASS_IDLE: libc::c_long = 3;
            libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0 as libc::c_long, IOPRIO_CLASS_IDLE << 13);
            let tid = libc::syscall(libc::SYS_gettid) as libc::id_t;
            libc::setpriority(libc::PRIO_PROCESS, tid, 19);
        }
        let _ = tx.send(f());
    });
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
            match idle(move || sweep::archive(&tree, sweep::DEFAULT_RETENTION_DAYS, false, &live, None)).await {
                None => false,
                Some(result) => {
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

/// Catch up if due, then sweep each day at 03:30 local time.
pub async fn daily(ctx: Context, app: Arc<App>) {
    let last = runs::last_timer(&app.store).await.unwrap_or_else(|e| {
        tracing::warn!("transcripts: could not read the last timer sweep: {e}");
        None
    });
    if due(last, sweep::now()) {
        run_once(&ctx, &app).await;
    }
    loop {
        let now = sweep::now();
        let wait = (next_at(now, AT_HOUR, AT_MINUTE) - now).max(1.0);
        tokio::time::sleep(Duration::from_secs_f64(wait)).await;
        run_once(&ctx, &app).await;
    }
}
