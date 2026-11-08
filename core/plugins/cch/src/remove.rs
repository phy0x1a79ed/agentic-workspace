//! Deletes the sessions the pool made and nobody took. It plans by default, and before each
//! deletion re-reads the session's own record rather than trusting the plan's snapshot: between
//! a tick and its removals a session can be claimed, renamed or prompted.
//!
//! Removal has its own predicate, [`crate::sessions::Sessions::removable`], rather than being
//! whatever claiming rejected: "delete the extras" would delete the session somebody is attached
//! to and typing into.

use std::time::Duration;

use awm_harness::claude::cli::{RunError, Runner};
use serde::Serialize;

use crate::lineage::Processes;
use crate::pool::{Pool, Terminals};
use crate::sessions::{age_s, Session};

/// How long one `claude rm` may run.
pub const REMOVE_TIMEOUT: Duration = Duration::from_secs(20);

/// One session the plan may delete, and why. `removed` is set once [`apply`] has acted on it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Removal {
    pub session: String,
    pub name: String,
    pub why: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub removed: Option<bool>,
}

/// Every session that may be deleted at epoch second `now` (default: the wall clock), with the
/// reason it may be.
///
/// The terminals are read once for the whole plan: re-reading them per session lets the set
/// change underneath a single plan.
pub fn plan<P: Processes, T: Terminals>(pool: &Pool<P, T>, now: Option<f64>) -> Vec<Removal> {
    let s = pool.sessions();
    let version = s.binary_version();
    let attached = pool.terminals.attached();
    s.load()
        .into_iter()
        .filter(|x| s.removable(x, version.as_deref(), now, Some(&attached)))
        .map(|x| Removal { why: why(pool, &x, version.as_deref(), now), session: x.short, name: x.name, removed: None })
        .collect()
}

fn why<P: Processes, T: Terminals>(pool: &Pool<P, T>, s: &Session, version: Option<&str>, now: Option<f64>) -> String {
    let minutes = age_s(s, now) / 60.0;
    if !pool.sessions().is_alive(s) {
        return "the process is gone".into();
    }
    if s.origin_cwd.is_some() {
        return format!("claimed {minutes:.0} minutes ago and never prompted");
    }
    if let Some(v) = version.filter(|v| s.cli_version.as_deref() != Some(*v)) {
        return format!("seeded by {}, the binary is now {v}", s.cli_version.as_deref().unwrap_or("None"));
    }
    format!("aged out at {minutes:.0} minutes")
}

fn still_removable<P: Processes, T: Terminals>(pool: &Pool<P, T>, short: &str) -> bool {
    let s = pool.sessions();
    let version = s.binary_version();
    let attached = pool.terminals.attached();
    s.load().iter().any(|x| x.short == short && s.removable(x, version.as_deref(), None, Some(&attached)))
}

/// Carry out the plan, re-checking each session as it comes up, with `claude rm` run by `runner`.
pub async fn apply<P, T>(pool: &Pool<P, T>, runner: &impl Runner, now: Option<f64>) -> Vec<Removal>
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    let mut done = Vec::new();
    for item in pool.read(move |p| plan(p, now)).await {
        let short = item.session.clone();
        let check = short.clone();
        if !pool.read(move |p| still_removable(p, &check)).await {
            tracing::info!("cx: {short} stopped being removable between the plan and the removal — left alone");
            done.push(Removal { removed: Some(false), why: "claimed while we looked".into(), ..item });
            continue;
        }
        let removed = rm(pool, runner, &short).await;
        done.push(Removal { removed: Some(removed), ..item });
    }
    done
}

async fn rm<P, T>(pool: &Pool<P, T>, runner: &impl Runner, short: &str) -> bool {
    let argv = [pool.paths.claude_bin.to_string_lossy().into_owned(), "rm".into(), short.into()];
    match runner.run(&argv, REMOVE_TIMEOUT).await {
        Ok(out) if out.success => {
            tracing::info!("cx: removed {short}");
            true
        }
        Ok(out) => {
            let err: String = String::from_utf8_lossy(&out.output).trim().chars().take(200).collect();
            tracing::warn!("cx: removing {short} failed: {err}");
            false
        }
        Err(RunError::TimedOut) => {
            tracing::warn!("cx: removing {short} timed out");
            false
        }
        Err(RunError::CouldNotRun(why)) => {
            tracing::warn!("cx: removing {short} failed: {why}");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pool::testing::TestBox;
    use awm_harness::claude::cli::RunOutput;
    use std::collections::HashSet;
    use std::sync::Mutex;

    #[test]
    fn test_the_removal_plan_holds_only_the_session_nobody_can_reach() {
        // The one case removal exists for, and nothing else on a live box.
        let b = TestBox::new();
        let items: std::collections::HashMap<String, Removal> =
            plan(&b.pool(), Some(b.a_minute_on())).into_iter().map(|i| (i.session.clone(), i)).collect();
        assert_eq!(items.keys().cloned().collect::<HashSet<_>>(), HashSet::from(["strandeddead".into(), "stranded".into()]));
        assert_eq!(items["strandeddead"].why, "the process is gone");
        assert!(items["stranded"].why.contains("never prompted"));
    }

    /// Answers `claude rm` with success, recording each argv.
    #[derive(Default)]
    struct Rm(Mutex<Vec<Vec<String>>>);

    impl Runner for Rm {
        async fn run(&self, argv: &[String], limit: Duration) -> Result<RunOutput, RunError> {
            assert_eq!(limit, REMOVE_TIMEOUT);
            self.0.lock().unwrap().push(argv.to_vec());
            Ok(RunOutput { success: !argv[2].ends_with("dead"), output: b"no such job".to_vec() })
        }
    }

    #[tokio::test]
    async fn apply_runs_claude_rm_for_each_planned_session_and_reports_the_outcome() {
        let b = TestBox::new();
        let rm = Rm::default();
        let done = apply(&b.pool(), &rm, None).await;
        let by: std::collections::HashMap<_, _> = done.iter().map(|d| (d.session.as_str(), d.removed)).collect();
        assert_eq!(by["stranded"], Some(true));
        assert_eq!(by["strandeddead"], Some(false));
        let calls = rm.0.lock().unwrap().clone();
        assert!(calls.iter().all(|c| c[1] == "rm" && c[0].ends_with("claude")));
        assert_eq!(calls.len(), 2);
    }
}
