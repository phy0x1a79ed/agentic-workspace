//! Logging for a plugin service.
//!
//! A service logs to stderr, as the Python services do. The gateway sends a spawned service's
//! stderr to `<AWM_DIR>/logs/<service>.log`; an external service's unit sends it to its journal.

/// Install the tracing subscriber: `AWM_LOG`, else `RUST_LOG`, else `info`. A second call does nothing.
pub fn init() {
    use tracing_subscriber::EnvFilter;
    let filter = std::env::var("AWM_LOG")
        .ok()
        .or_else(|| std::env::var("RUST_LOG").ok())
        .and_then(|f| EnvFilter::try_new(f).ok())
        .unwrap_or_else(|| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt().with_env_filter(filter).with_ansi(false).with_writer(std::io::stderr).try_init();
}
