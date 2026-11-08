//! Long-lived background work that is noticed when it fails (`gatewayclient/subscription.py`):
//! a loop respawned when it dies, and a topic subscription that heals itself.

use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Value};

use crate::backoff::Backoff;
use crate::hub::{HubError, Subscription};

/// The health of one supervised loop, for a status verb.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Health {
    pub name: String,
    pub running: bool,
    /// How many times the loop died and was started again.
    pub restarts: u64,
    pub last_error: Option<String>,
    /// Unix seconds when the current run started.
    pub started_at: Option<u64>,
}

/// The handle of a supervised loop. Dropping it leaves the loop running.
#[derive(Clone)]
pub struct Supervised {
    health: Arc<Mutex<Health>>,
    task: Arc<tokio::task::AbortHandle>,
}

impl Supervised {
    pub fn health(&self) -> Health {
        self.health.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    pub fn abort(&self) {
        self.task.abort();
        self.health.lock().unwrap_or_else(|p| p.into_inner()).running = false;
    }
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Run `factory()` for good, respawning it `delay` after each death.
pub fn spawn_supervised<F, Fut>(name: &str, delay: Duration, factory: F) -> Supervised
where
    F: Fn() -> Fut + Send + Sync + 'static,
    Fut: Future<Output = anyhow::Result<()>> + Send + 'static,
{
    let health = Arc::new(Mutex::new(Health { name: name.into(), ..Health::default() }));
    let h = health.clone();
    let name = name.to_string();
    let task = tokio::spawn(async move {
        loop {
            {
                let mut g = h.lock().unwrap_or_else(|p| p.into_inner());
                (g.running, g.started_at) = (true, Some(now()));
            }
            let why = match tokio::spawn(factory()).await {
                Ok(Ok(())) => "returned unexpectedly".to_string(),
                Ok(Err(e)) => format!("{e:#}"),
                Err(e) => format!("panicked: {e}"),
            };
            tracing::error!("supervised task {name} died ({why}); respawning in {:.0}s", delay.as_secs_f64());
            {
                let mut g = h.lock().unwrap_or_else(|p| p.into_inner());
                (g.running, g.restarts, g.last_error) = (false, g.restarts + 1, Some(why));
            }
            tokio::time::sleep(delay).await;
        }
    });
    Supervised { health, task: Arc::new(task.abort_handle()) }
}

/// How a [`SupervisedSubscription`] paces itself. The defaults are `subscription.py`'s.
#[derive(Debug, Clone)]
pub struct SubscriptionOpts {
    /// With no event for this long, resubscribe: it heals every kind of silent staleness.
    pub idle_timeout: Duration,
    /// The idle deadline varies by this fraction, so a fleet does not resubscribe in step.
    pub idle_jitter: f64,
    /// A connection that lasted this long puts the failures before it behind it.
    pub stability: Duration,
    pub backoff_initial: Duration,
    pub backoff_max: Duration,
    pub backoff_factor: f64,
}

impl Default for SubscriptionOpts {
    fn default() -> Self {
        Self {
            idle_timeout: Duration::from_secs(300),
            idle_jitter: 0.25,
            stability: Duration::from_secs(30),
            backoff_initial: Duration::from_secs(2),
            backoff_max: Duration::from_secs(30),
            backoff_factor: 1.5,
        }
    }
}

/// Consecutive failed connections before a warning becomes an error.
const ERROR_AFTER: u64 = 5;

#[derive(Default)]
struct SubState {
    connected: bool,
    connects: u64,
    reconnects: u64,
    events: u64,
    handler_errors: u64,
    consecutive_failures: u64,
    last_connect_ts: Option<f64>,
    last_event_ts: Option<f64>,
    last_error: Option<String>,
    last_end: Option<&'static str>,
    connected_since: Option<std::time::Instant>,
}

/// A named, self-healing consumer of one emit topic. It reconnects with backoff, resubscribes
/// after an idle deadline (break before make, so no event arrives twice), and survives a
/// handler that fails on one event.
#[derive(Clone)]
pub struct SupervisedSubscription {
    name: String,
    opts: SubscriptionOpts,
    state: Arc<Mutex<SubState>>,
    task: Arc<Mutex<Option<tokio::task::AbortHandle>>>,
}

fn wall() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl SupervisedSubscription {
    /// Start consuming. `connect` opens one fresh subscription, such as
    /// `move || hub.subscribe("social", "command", peer, None)`; read a peer selector inside it so
    /// a reconnect picks up a change. `handler` runs once per event.
    pub fn spawn<C, CF, H, HF>(name: &str, opts: SubscriptionOpts, connect: C, handler: H) -> Self
    where
        C: Fn() -> CF + Send + Sync + 'static,
        CF: Future<Output = Result<Subscription, HubError>> + Send + 'static,
        H: Fn(Value) -> HF + Send + Sync + 'static,
        HF: Future<Output = anyhow::Result<()>> + Send + 'static,
    {
        let sub = Self { name: name.into(), opts, state: Arc::default(), task: Arc::default() };
        let me = sub.clone();
        let task = tokio::spawn(async move { me.run(connect, handler).await });
        *sub.task.lock().unwrap_or_else(|p| p.into_inner()) = Some(task.abort_handle());
        sub
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, SubState> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn abort(&self) {
        if let Some(t) = self.task.lock().unwrap_or_else(|p| p.into_inner()).take() {
            t.abort();
        }
        self.lock().connected = false;
    }

    /// Connected, with no run of failures behind it, or connected past the stability window.
    /// Never a function of the last event: a recovery topic is silent for weeks.
    pub fn healthy(&self) -> bool {
        let s = self.lock();
        s.connected && (s.consecutive_failures == 0 || s.connected_since.is_some_and(|t| t.elapsed() >= self.opts.stability))
    }

    /// The status block of `SupervisedSubscription.health()`, key for key.
    pub fn health(&self) -> Value {
        let healthy = self.healthy();
        let s = self.lock();
        let now = wall();
        let round1 = |x: f64| (x * 10.0).round() / 10.0;
        json!({
            "name": self.name,
            "connected": s.connected,
            "healthy": healthy,
            "connects": s.connects,
            "reconnects": s.reconnects,
            "events": s.events,
            "handler_errors": s.handler_errors,
            "consecutive_failures": s.consecutive_failures,
            "connected_at": s.last_connect_ts,
            "connected_for_s": s.connected_since.map(|t| round1(t.elapsed().as_secs_f64())),
            "last_event_at": s.last_event_ts,
            "last_event_age_s": s.last_event_ts.map(|t| round1(now - t)),
            "last_error": s.last_error,
            "last_end": s.last_end,
            "idle_timeout_s": self.opts.idle_timeout.as_secs_f64(),
        })
    }

    fn idle_deadline(&self) -> Duration {
        let base = self.opts.idle_timeout.as_secs_f64();
        let spread = base * self.opts.idle_jitter;
        // A cheap uniform draw in [-1, 1): the clock's nanoseconds.
        let r = (SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0) as f64 / 1e9) * 2.0 - 1.0;
        Duration::from_secs_f64((base + r * spread).max(base * 0.5))
    }

    async fn run<C, CF, H, HF>(self, connect: C, handler: H)
    where
        C: Fn() -> CF,
        CF: Future<Output = Result<Subscription, HubError>>,
        H: Fn(Value) -> HF,
        HF: Future<Output = anyhow::Result<()>> + Send + 'static,
    {
        tracing::info!("subscription {}: starting", self.name);
        let mut backoff = Backoff::new(self.opts.backoff_initial, self.opts.backoff_max, self.opts.backoff_factor);
        loop {
            let started = std::time::Instant::now();
            let end = match connect().await {
                Err(e) => {
                    self.lock().last_error = Some(e.to_string());
                    "error"
                }
                Ok(mut stream) => {
                    {
                        let mut s = self.lock();
                        s.connects += 1;
                        if s.connects > 1 {
                            s.reconnects += 1;
                        }
                        (s.connected, s.last_connect_ts, s.connected_since) = (true, Some(wall()), Some(std::time::Instant::now()));
                    }
                    tracing::info!("subscription {}: connected", self.name);
                    let end = loop {
                        match tokio::time::timeout(self.idle_deadline(), stream.next()).await {
                            Err(_) => {
                                tracing::info!("subscription {}: no event for {:.0}s; re-subscribing", self.name, self.opts.idle_timeout.as_secs_f64());
                                break "idle";
                            }
                            Ok(None) => {
                                self.lock().last_error = Some("stream closed by peer".into());
                                break "closed";
                            }
                            Ok(Some(ev)) => {
                                {
                                    let mut s = self.lock();
                                    s.events += 1;
                                    s.last_event_ts = Some(wall());
                                }
                                // A handler that fails or panics on one event must not end the stream.
                                match tokio::spawn(handler(ev)).await {
                                    Ok(Ok(())) => {}
                                    Ok(Err(e)) => {
                                        self.lock().handler_errors += 1;
                                        tracing::warn!("subscription {}: handler failed on one event (continuing): {e:#}", self.name);
                                    }
                                    Err(e) => {
                                        self.lock().handler_errors += 1;
                                        tracing::warn!("subscription {}: handler panicked on one event (continuing): {e}", self.name);
                                    }
                                }
                            }
                        }
                    };
                    // Break before make: the old stream closes before the next one opens.
                    drop(stream);
                    end
                }
            };
            let failures = {
                let mut s = self.lock();
                (s.connected, s.connected_since, s.last_end) = (false, None, Some(end));
                if end == "idle" || started.elapsed() >= self.opts.stability {
                    s.consecutive_failures = 0;
                } else {
                    s.consecutive_failures += 1;
                }
                s.consecutive_failures
            };
            if end == "idle" {
                backoff.reset();
                continue;
            }
            let why = self.lock().last_error.clone().unwrap_or_default();
            if failures == 0 {
                backoff.reset();
            }
            let wait = backoff.wait();
            if failures == 0 {
                tracing::warn!("subscription {} dropped after {:.0}s (retrying in {:.0}s): {why}", self.name, started.elapsed().as_secs_f64(), wait.as_secs_f64());
            } else if failures >= ERROR_AFTER {
                tracing::error!("subscription {} failed ({failures} consecutive, retrying in {:.0}s): {why}", self.name, wait.as_secs_f64());
            } else {
                tracing::warn!("subscription {} failed ({failures} consecutive, retrying in {:.0}s): {why}", self.name, wait.as_secs_f64());
            }
            tokio::time::sleep(wait).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    #[tokio::test]
    async fn a_dead_loop_comes_back() {
        let runs = Arc::new(AtomicU32::new(0));
        let r = runs.clone();
        let sup = spawn_supervised("t", Duration::from_millis(10), move || {
            let r = r.clone();
            async move {
                if r.fetch_add(1, Ordering::SeqCst) == 0 {
                    anyhow::bail!("first run fails");
                }
                std::future::pending::<()>().await;
                Ok(())
            }
        });
        for _ in 0..100 {
            if runs.load(Ordering::SeqCst) >= 2 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let h = sup.health();
        assert_eq!((h.restarts, h.running, h.last_error.as_deref()), (1, true, Some("first run fails")));
        sup.abort();
    }

}
