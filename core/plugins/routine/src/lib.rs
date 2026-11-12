//! The routine plugin: routines a project defines in its repo, installed per node, fired by
//! a systemd user timer, a call or an event, watched to a verdict, and handed to a fixing agent when
//! they fail. Node rollout, `routine rollout`, ships in the same binary.

pub mod definitions;
pub mod escalate;
pub mod events;
pub mod rollout;
pub mod outbound;
pub mod proc;
pub mod runner;
pub mod service;
pub mod snapshot;
pub mod store;
pub mod timers;

/// Now in UTC, as Python's `datetime.now(timezone.utc).isoformat(timespec="seconds")` writes it.
pub fn now() -> String {
    iso(chrono::Utc::now())
}

pub fn iso(t: chrono::DateTime<chrono::Utc>) -> String {
    t.to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
}

/// A short random id: 12 hex digits of a v4 uuid.
pub fn new_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()[..12].to_string()
}

/// A float as Python's `{:g}` writes a step timeout.
pub fn fmt_g(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e16 {
        format!("{}", x as i64)
    } else {
        format!("{x}")
    }
}
