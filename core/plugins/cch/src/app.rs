//! The live objects behind the verb table: the [`Backend`] every verb acts through, [`Live`] over
//! this host's sessions, and the boot that re-arms the follow-ups an earlier process still owed.

use std::future::Future;
use std::time::Duration;

use awm_harness::claude::ClaudeHome;
use awm_harness::contract::Reporter;
use awm_plugin_sdk::config::{self, EdgeList};
use awm_plugin_sdk::{CallOpts, Hub};
use serde_json::Value;

use crate::control::{self, ControlError, RemoveJob, Target};
use crate::lane::{Host, LaneError};
use crate::lineage::{self, Mode, Procfs};
use crate::paths::Paths;
use crate::permission_mode::{self, ModeResult};
use crate::promises::{Pending, Promises};
use crate::resume::{self, LiveLiveness, LiveWatch, LiveWorld, Resumer};
use crate::sender::{SendError, SendRequest, Sender};
use crate::transcript::Tail;
use crate::verbs::py_repr;
use crate::watcher::{self, LiveProbe};

/// One control verb, as the verb table hands it to [`Backend::control`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Message { text: String, confirm: bool },
    Interrupt,
    Model(String),
    Effort(String),
    Rename(String),
    Cd { path: String, brief: String },
    RemoteControl { on: bool, name: Option<String> },
    Mcp { action: String, server: String },
    Link,
    /// Stop the session and remove its job.
    Stop { confirm: bool },
}

/// A verb called on another node's cch.
#[derive(Debug, Clone, PartialEq)]
pub struct PeerCall {
    pub node: String,
    pub tool: &'static str,
    pub verb: &'static str,
    pub args: Value,
    pub timeout: Duration,
}

/// Everything the verbs do beyond reading Claude Code's records under [`Backend::home`]. [`Live`]
/// is the real one; the tests fake it.
pub trait Backend: Send + Sync + 'static {
    fn home(&self) -> &ClaudeHome;
    /// What cch takes the session at `pid` to be: `session`, `session_id`, `pid`, `hosting`, `pane`.
    fn describe_caller(&self, pid: Option<u32>) -> impl Future<Output = Result<Value, LaneError>> + Send;
    /// The mode the session at `pid` was started with, for the gateway's gate.
    fn mode_of(&self, pid: Option<i64>) -> impl Future<Output = Mode> + Send;
    /// The session `job` names, or the caller's own ([`control::resolve`]).
    fn resolve(&self, job: Option<String>, pid: Option<u32>) -> impl Future<Output = Result<Target, ControlError>> + Send;
    fn control(&self, op: Op, job: Option<String>, pid: Option<u32>) -> impl Future<Output = Result<Value, ControlError>> + Send;
    /// Type into the caller's own session, arming the resume a slash command is owed.
    fn send(&self, req: SendRequest) -> impl Future<Output = Result<Value, SendError>> + Send;
    fn ensure_bypass(&self, pid: Option<u32>, expect: Option<String>) -> impl Future<Output = Result<ModeResult, LaneError>> + Send;
    /// Every promise still worth keeping, clearing the ones that are not.
    fn pending(&self) -> impl Future<Output = Vec<Pending>> + Send;
    /// Why the caller stamped `as_` may not act on this node's sessions, or None if it may.
    fn caller_refusal(&self, as_: Option<String>) -> impl Future<Output = Option<String>> + Send;
    fn node_name(&self) -> String;
    fn call_peer(&self, call: PeerCall) -> impl Future<Output = Result<Value, String>> + Send;
}

/// Why a call stamped `as_` may not start or stop sessions here, or None if it may. A peer must
/// be a node that holds an edge to this one; the bare legacy `peer` names no node and is refused.
pub fn caller_refusal(as_: Option<&str>, edges: impl FnOnce() -> anyhow::Result<EdgeList>) -> Option<String> {
    let stamp = as_?;
    if !(stamp == "peer" || stamp.starts_with("peer:")) {
        return None;
    }
    let Some(node) = config::caller_peer(as_) else {
        return Some("an unverified peer call cannot start or stop sessions".into());
    };
    let admitted = match edges() {
        Ok(edges) => config::admits(&edges, as_),
        Err(e) => return Some(format!("the fleet edge list is invalid: {e}")),
    };
    (!admitted).then(|| {
        format!(
            "peer {} holds no edge to this node; only a peer that reaches this node may start or stop sessions",
            py_repr(node)
        )
    })
}

/// The live resume watcher: one detached task per promise.
pub type Watch = LiveWatch<Host, Host, LiveProbe, LiveWorld>;

/// The real [`Backend`]: this host's records, tmux server and daemon, the promise store, and the hub.
pub struct Live {
    pub paths: Paths,
    pub host: Host,
    pub promises: Promises,
    pub reporter: Reporter,
    pub watch: Watch,
    pub hub: Hub,
    fx: control::Live<Watch>,
}

impl Live {
    /// The live objects over `paths`, reporting to reflection through `hub`.
    pub fn new(paths: Paths, hub: Hub) -> Self {
        let home = paths.home.clone();
        let host = Host::live(home.clone(), None);
        let promises = Promises::new(paths.promises_dir());
        let reporter = resume::reporter(hub.clone(), &promises);
        let sender = Sender { detect: host.clone(), opener: host.clone(), probe: LiveProbe { home: home.clone() } };
        let resumer = Resumer::new(sender, LiveWorld { home }, promises.clone(), reporter.clone());
        let watch = LiveWatch { resumer };
        let fx = control::Live::new(paths.clone(), host.clone(), promises.clone(), watch.clone());
        Self { paths, host, promises, reporter, watch, hub, fx }
    }

    fn sender(&self) -> Sender<Host, Host, LiveProbe> {
        Sender { detect: self.host.clone(), opener: self.host.clone(), probe: LiveProbe { home: self.paths.home.clone() } }
    }

    fn liveness(&self) -> LiveLiveness {
        LiveLiveness { home: self.paths.home.clone(), reporter: self.reporter.clone() }
    }

    /// Re-arm the follow-ups the process this one replaced still owed, then resend the events
    /// reflection did not take. A restart takes the in-flight watchers with it.
    pub async fn boot(&self) {
        let home = self.paths.home.clone();
        let mut watch = self.watch.clone();
        let out = resume::replay_pending(
            &self.promises,
            &self.liveness(),
            watcher::now_ms(),
            |pid| Tail::new(home.clone(), pid),
            &mut watch,
        )
        .await;
        if out["pending"].as_u64().unwrap_or(0) > 0 {
            tracing::info!("cch: {} pending resume(s) found on boot, {} re-armed", out["pending"], out["resumed"]);
        }
        let reporter = self.reporter.clone();
        tokio::spawn(async move {
            let sent = reporter.resend().await;
            if sent > 0 {
                tracing::info!("cch: resent {sent} spooled harness events");
            }
        });
    }
}

impl Backend for Live {
    fn home(&self) -> &ClaudeHome {
        &self.paths.home
    }

    async fn describe_caller(&self, pid: Option<u32>) -> Result<Value, LaneError> {
        self.sender().describe_caller(pid).await
    }

    async fn mode_of(&self, pid: Option<i64>) -> Mode {
        let Some(pid) = pid else { return Mode::Unknown };
        let paths = self.paths.clone();
        match tokio::task::spawn_blocking(move || lineage::mode_of(&paths, &Procfs, pid)).await {
            Ok(mode) => mode,
            Err(e) => {
                // A mode that cannot be read restricts hardest.
                tracing::warn!("cch: could not read the mode of pid {pid}: {e}");
                Mode::Unknown
            }
        }
    }

    async fn resolve(&self, job: Option<String>, pid: Option<u32>) -> Result<Target, ControlError> {
        control::resolve(&self.fx, job.as_deref(), pid).await
    }

    async fn control(&self, op: Op, job: Option<String>, pid: Option<u32>) -> Result<Value, ControlError> {
        let (fx, job) = (&self.fx, job.as_deref());
        match op {
            Op::Message { text, confirm } => control::message(fx, job, &text, pid, confirm).await,
            Op::Interrupt => control::interrupt(fx, job, pid).await,
            Op::Model(name) => control::model(fx, &name, job, pid).await,
            Op::Effort(level) => control::effort(fx, &level, job, pid).await,
            Op::Rename(name) => control::rename(fx, &name, job, pid).await,
            Op::Cd { path, brief } => control::cd(fx, &path, job, pid, &brief).await,
            Op::RemoteControl { on, name } => control::remote_control(fx, on, name.as_deref(), job, pid).await,
            Op::Mcp { action, server } => control::mcp(fx, &action, &server, job, pid).await,
            Op::Link => control::link(fx, job, pid).await,
            Op::Stop { confirm } => {
                let remove = RemoveJob { home: self.paths.home.clone(), claude: self.paths.claude_bin.clone() };
                control::stop(fx, job, pid, confirm, remove).await
            }
        }
    }

    async fn send(&self, req: SendRequest) -> Result<Value, SendError> {
        let tail = Tail::new(self.paths.home.clone(), req.caller_pid.unwrap_or_default());
        let mut watch = self.watch.clone();
        self.sender().send(&req, &self.promises, tail, &mut watch).await
    }

    async fn ensure_bypass(&self, pid: Option<u32>, expect: Option<String>) -> Result<ModeResult, LaneError> {
        permission_mode::ensure_bypass(&self.host, pid, expect.as_deref()).await
    }

    async fn pending(&self) -> Vec<Pending> {
        self.promises.load_all(watcher::now_ms(), &self.liveness()).await
    }

    async fn caller_refusal(&self, as_: Option<String>) -> Option<String> {
        tokio::task::spawn_blocking(move || caller_refusal(as_.as_deref(), config::edges))
            .await
            .unwrap_or_else(|e| Some(format!("could not check the caller: {e}")))
    }

    fn node_name(&self) -> String {
        config::node_name()
    }

    async fn call_peer(&self, call: PeerCall) -> Result<Value, String> {
        let opts = CallOpts { peer: Some(call.node), timeout: call.timeout, ..CallOpts::default() };
        self.hub.call_with(call.tool, call.verb, call.args, &opts).await.map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn caller_refusal_fails_closed_on_a_peer_it_cannot_place() {
        let empty = || Ok(EdgeList::default());
        assert_eq!(caller_refusal(None, empty), None);
        assert_eq!(caller_refusal(Some("user:someone"), empty), None);
        assert_eq!(caller_refusal(Some("peer"), empty).as_deref(), Some("an unverified peer call cannot start or stop sessions"));
        let far = caller_refusal(Some("peer:far"), empty).unwrap();
        assert!(far.starts_with("peer 'far' holds no edge to this node"), "{far}");
        let broken = caller_refusal(Some("peer:far"), || Err(anyhow::anyhow!("bad yaml"))).unwrap();
        assert!(broken.contains("bad yaml"), "{broken}");
    }
}
