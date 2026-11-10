//! The pool loop: keeps one session warm on one rule, that a session is replaced when it stops
//! being claimable, and runs the lineage bookkeeping beside it. Within a tick, seed comes before
//! remove: an aged session has already stopped counting, so its replacement exists before removal
//! collects it, with nothing to sequence the two.
//!
//! A second copy of cch must not seed against the same home (a dev sandbox, a stray manual run,
//! a predecessor still dying), so the tick holds an exclusive `flock` on `reconcile.lock`. It is
//! `flock`, not an `fcntl` record lock, so a Python and a Rust copy exclude each other.

use std::fs::File;
use std::future::Future;
use std::os::fd::AsRawFd;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_harness::claude::cli::Runner;
use awm_harness::claude::launch::LaunchError;
use serde_json::{json, Map, Value};
use tokio::sync::Notify;
use tokio::time::Instant;

use crate::lineage::Processes;
use crate::paths::Paths;
use crate::pool::{self, Pool, Terminals};
use crate::remove::{self, Removal};
use crate::seed::{self, Launcher};

/// How long the same seeding refusal is swallowed before it is logged again. A tick is seconds,
/// and a node with no daemon would otherwise write a line a tick forever.
pub const QUIET: Duration = Duration::from_secs(600);

/// The pool's half of a tick. [`LivePool`] is the real one; tests fake the seeding and removal.
pub trait Tend: Send + Sync {
    /// How many sessions could be handed out right now.
    fn warm_count(&self) -> impl Future<Output = usize> + Send;
    /// Seed one warm session ([`seed::seed_one`]).
    fn seed_one(&self) -> impl Future<Output = Result<String, LaunchError>> + Send;
    /// Remove what may be removed ([`remove::apply`]).
    fn remove(&self) -> impl Future<Output = anyhow::Result<Vec<Removal>>> + Send;
}

/// Lifecycle's bookkeeping, run every tick even with the pool switched off: a start that
/// outlives its launch is adopted only here, and without that cx refuses to stop or resume the
/// job. Each answers the jobs it acted on.
pub trait Bookkeeping: Send + Sync {
    /// Adopt the starts that outlived their launch, under lifecycle's start lock.
    fn adopt_pending(&self) -> impl Future<Output = anyhow::Result<Vec<String>>> + Send;
    /// Unpin the jobs that are gone.
    fn prune_pins(&self) -> impl Future<Output = anyhow::Result<Vec<String>>> + Send;
    /// Drop the lineage records of removed jobs.
    fn prune_lineage(&self) -> impl Future<Output = anyhow::Result<Vec<String>>> + Send;
}

/// The real pool half: the records, the launcher and the `claude rm` runner.
#[derive(Clone)]
pub struct LivePool<P, T, L, R> {
    pub pool: Pool<P, T>,
    pub launcher: L,
    pub runner: R,
}

impl<P, T, L, R> Tend for LivePool<P, T, L, R>
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
    L: Launcher,
    R: Runner + Send + Sync,
{
    async fn warm_count(&self) -> usize {
        self.pool
            .read(|p| {
                let s = p.sessions();
                let version = s.binary_version();
                pool::warm(s.load(), &s, version.as_deref(), None).len()
            })
            .await
    }

    async fn seed_one(&self) -> Result<String, LaunchError> {
        seed::seed_one(&self.pool, &self.launcher).await
    }

    async fn remove(&self) -> anyhow::Result<Vec<Removal>> {
        Ok(remove::apply(&self.pool, &self.runner, None).await)
    }
}

/// Why this process must not tend the warm pool, or None if it may.
pub fn enabled(paths: &Paths) -> Option<String> {
    if !paths.loop_enabled {
        return Some("switched off (AWM_CX_LOOP=0)".into());
    }
    if !paths.pool_wanted() {
        return Some("this node does not run the cx profile".into());
    }
    None
}

#[derive(Default)]
struct State {
    lock: Option<File>,
    last_tick: Option<Instant>,
    last_refusal: Option<(String, Instant)>,
}

/// The reconcile loop, and everything its status reports.
pub struct Loop<T, B> {
    paths: Arc<Paths>,
    tend: T,
    books: B,
    state: Mutex<State>,
    ticking: tokio::sync::Mutex<()>,
    kick: Notify,
    running: AtomicBool,
}

fn take_lock(path: &Path) -> Option<File> {
    let opened = path.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|()| {
        use std::os::unix::fs::OpenOptionsExt;
        std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).mode(0o600).open(path)
    });
    let file = match opened {
        Ok(f) => f,
        Err(exc) => {
            tracing::warn!("cx: cannot open the reconcile lock: {exc}");
            return None;
        }
    };
    // SAFETY: flock(2) on a descriptor `file` owns; the kernel drops the lock with the file.
    (unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0).then_some(file)
}

fn logged(step: &str, done: anyhow::Result<Vec<String>>, line: impl Fn(&str) -> String) {
    match done {
        Ok(jobs) => jobs.iter().for_each(|job| tracing::info!("cx: {}", line(job))),
        Err(exc) => tracing::error!("cx: {step} failed: {exc:#}"),
    }
}

impl<T: Tend, B: Bookkeeping> Loop<T, B> {
    pub fn new(paths: Arc<Paths>, tend: T, books: B) -> Self {
        Self {
            paths,
            tend,
            books,
            state: Mutex::default(),
            ticking: tokio::sync::Mutex::new(()),
            kick: Notify::new(),
            running: AtomicBool::new(false),
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Why this process must not tend the warm pool, or None if it may. Switching the pool off
    /// leaves the bookkeeping running.
    pub fn enabled(&self) -> Option<String> {
        enabled(&self.paths)
    }

    /// Sleep a tick, cut short by a kick, then tick; forever. Never returns.
    pub async fn run(&self) {
        self.running.store(true, Ordering::Relaxed);
        loop {
            let _ = tokio::time::timeout(self.paths.tick(), self.kick.notified()).await;
            self.tick().await;
        }
    }

    /// Run the next tick now. Called after a claim, so the replacement is seeded while the caller
    /// is still starting up rather than up to a tick later.
    pub fn kick(&self) {
        self.kick.notify_one();
    }

    /// `loop`, `seeding_loop`, `holds_lock` and `last_tick` (seconds ago), for the pool status.
    pub fn status(&self) -> Map<String, Value> {
        let st = self.state();
        let last_tick = st.last_tick.map(|t| (t.elapsed().as_secs_f64() * 10.0).round() / 10.0);
        let v = json!({
            "loop": if self.running.load(Ordering::Relaxed) { "running" } else { "not started" },
            "seeding_loop": self.enabled().unwrap_or_else(|| "on".into()),
            "holds_lock": st.lock.is_some(),
            "last_tick": last_tick,
        });
        v.as_object().cloned().unwrap_or_default()
    }

    /// One pass: take the lock or do nothing, tend the pool if enabled, then the bookkeeping. A
    /// failure in one step never skips the others. Ticks run one at a time.
    pub async fn tick(&self) {
        let _one = self.ticking.lock().await;
        if !self.take_lock().await {
            return;
        }
        let paths = self.paths.clone();
        if tokio::task::spawn_blocking(move || enabled(&paths)).await.ok().flatten().is_none() {
            self.tend_pool().await;
        }
        logged("adopt_pending", self.books.adopt_pending().await, |j| format!("adopted {j} for a start that outlived its launch"));
        logged("prune_pins", self.books.prune_pins().await, |j| format!("unpinned {j}, whose job is gone"));
        logged("prune_lineage", self.books.prune_lineage().await, |j| {
            format!("dropped the lineage of {j}, which was removed")
        });
        self.state().last_tick = Some(Instant::now());
    }

    async fn tend_pool(&self) {
        if self.tend.warm_count().await < usize::try_from(self.paths.want).unwrap_or(usize::MAX) {
            match self.tend.seed_one().await {
                Ok(_) => {}
                Err(LaunchError::Refused(reason)) => self.note_refusal(reason),
                Err(LaunchError::Timeout(why)) => tracing::warn!("cx: {why}"),
                Err(exc) => tracing::error!("cx: seeding failed: {exc}"),
            }
        }
        // Under the pool lock, so a claim in flight cannot have its session deleted out from
        // under it between the move and the reply.
        let done = {
            let _held = pool::LOCK.lock().await;
            self.tend.remove().await
        };
        match done {
            Ok(done) => {
                for item in done {
                    let verb = if item.removed == Some(true) { "removed" } else { "kept" };
                    tracing::info!("cx: {verb} {} ({})", item.short, item.why);
                }
            }
            Err(exc) => tracing::error!("cx: removal failed: {exc:#}"),
        }
    }

    fn note_refusal(&self, reason: String) {
        let mut st = self.state();
        if st.last_refusal.as_ref().is_some_and(|(last, at)| *last == reason && at.elapsed() < QUIET) {
            return;
        }
        tracing::info!("cx: not seeding — {reason}");
        st.last_refusal = Some((reason, Instant::now()));
    }

    /// Hold the pool's lock, taking it if this process does not have it yet. Held for the life of
    /// the loop rather than per tick: the kernel drops it when the process dies, so a copy killed
    /// mid-tick hands the pool over with no cleanup pass and nothing to time out.
    async fn take_lock(&self) -> bool {
        if self.state().lock.is_some() {
            return true;
        }
        let path = self.paths.cx_state.join("reconcile.lock");
        let Ok(Some(file)) = tokio::task::spawn_blocking(move || take_lock(&path)).await else { return false };
        self.state().lock = Some(file);
        tracing::info!("cx: holding the reconcile lock");
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pool::testing::{NoTerminals, Table, TestBox};

    /// The real warm count over the box; seeding and removal recorded, and failing where told.
    struct FakeTend {
        pool: Pool<Table, NoTerminals>,
        calls: Arc<Mutex<Vec<&'static str>>>,
        seed: fn() -> Result<String, LaunchError>,
        remove_fails: bool,
    }

    impl Tend for FakeTend {
        async fn warm_count(&self) -> usize {
            let p = &self.pool;
            let s = p.sessions();
            let version = s.binary_version();
            pool::warm(s.load(), &s, version.as_deref(), None).len()
        }

        async fn seed_one(&self) -> Result<String, LaunchError> {
            self.calls.lock().unwrap().push("seed");
            (self.seed)()
        }

        async fn remove(&self) -> anyhow::Result<Vec<Removal>> {
            self.calls.lock().unwrap().push("remove");
            if self.remove_fails {
                anyhow::bail!("boom");
            }
            Ok(Vec::new())
        }
    }

    /// Bookkeeping that records adoption and lineage pruning, failing adoption where told.
    struct FakeBooks {
        calls: Arc<Mutex<Vec<&'static str>>>,
        adopt_fails: bool,
    }

    impl Bookkeeping for FakeBooks {
        async fn adopt_pending(&self) -> anyhow::Result<Vec<String>> {
            if self.adopt_fails {
                anyhow::bail!("boom");
            }
            self.calls.lock().unwrap().push("adopt");
            Ok(Vec::new())
        }

        async fn prune_pins(&self) -> anyhow::Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn prune_lineage(&self) -> anyhow::Result<Vec<String>> {
            self.calls.lock().unwrap().push("prune");
            Ok(Vec::new())
        }
    }

    struct Rig {
        lp: Loop<FakeTend, FakeBooks>,
        calls: Arc<Mutex<Vec<&'static str>>>,
    }

    impl Rig {
        fn calls(&self) -> Vec<&'static str> {
            self.calls.lock().unwrap().clone()
        }
    }

    fn seeded() -> Result<String, LaunchError> {
        Ok("new".into())
    }

    fn rig(b: &TestBox, adjust: impl FnOnce(&mut Paths), seed: fn() -> Result<String, LaunchError>) -> Rig {
        let pool = b.pool_with(adjust);
        let calls = Arc::new(Mutex::new(Vec::new()));
        let tend = FakeTend { pool: pool.clone(), calls: calls.clone(), seed, remove_fails: false };
        let books = FakeBooks { calls: calls.clone(), adopt_fails: false };
        Rig { lp: Loop::new(pool.paths.clone(), tend, books), calls }
    }

    fn aged(p: &mut Paths) {
        p.rotate_age_s = 1.0;
    }

    #[tokio::test]
    async fn test_a_tick_seeds_before_it_removes() {
        // What makes "create the new one, then delete the old one" true. Nothing sequences the
        // two: seeding runs first and an aged session has already stopped counting as claimable,
        // so the replacement exists before removal collects what it replaced.
        let b = TestBox::new();
        let r = rig(&b, aged, seeded);
        r.lp.tick().await;
        assert_eq!(r.calls(), ["seed", "remove", "adopt", "prune"]);
    }

    #[tokio::test]
    async fn test_a_refusal_does_not_end_the_loop() {
        let b = TestBox::new();
        let r = rig(&b, aged, || Err(LaunchError::Refused("no daemon".into())));
        r.lp.tick().await;
        assert_ne!(r.lp.status()["last_tick"], Value::Null);
    }

    #[tokio::test]
    async fn test_only_one_copy_holds_the_pool() {
        // One mechanism for the sandbox and a stray manual run.
        let b = TestBox::new();
        let (first, second) = (rig(&b, aged, seeded), rig(&b, aged, seeded));
        first.lp.tick().await;
        second.lp.tick().await;
        assert_eq!(first.lp.status()["holds_lock"], true);
        assert_eq!(second.lp.status()["holds_lock"], false);
        let seeds = first.calls().iter().chain(second.calls().iter()).filter(|c| **c == "seed").count();
        assert_eq!(seeds, 1);
    }

    #[tokio::test]
    async fn test_a_late_start_is_adopted_with_the_pool_switched_off() {
        // Adoption runs only in the tick, and cx refuses to stop or resume a job it holds no
        // record of.
        let b = TestBox::new();
        let r = rig(&b, |p| p.loop_enabled = false, seeded);
        r.lp.tick().await;
        assert_eq!(r.calls(), ["adopt", "prune"]);
    }

}
