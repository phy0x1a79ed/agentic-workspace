//! Warns before the kernel picks an OOM victim on this node: names the biggest processes, and the
//! sessions that own them, while there is time to act.
//!
//! An OOM kill once stopped the scope the Claude Code daemon lived in, and every session went with
//! it. The daemon now sits outside such scopes, but a box that runs out still kills something.

use std::collections::HashMap;
use std::future::Future;
use std::time::Duration;

use awm_harness::claude::records::session_by_pid;
use awm_plugin_sdk::ids::{Platform, SessionId};
use awm_harness::claude::ClaudeHome;
use serde_json::{json, Value};

use crate::gateway::{Call, Gateway};

/// Available memory below this fraction of the total counts as low, unless `AWM_KEEP_MEMORY_LOW`
/// says otherwise.
pub const LOW_FRACTION: f64 = 0.12;
/// Memory must stay low this long before a warning, so a brief spike stays quiet.
pub const SUSTAIN_S: f64 = 120.0;
/// The operator, and each session, hears about low memory at most this often.
pub const ALERT_EVERY_S: f64 = 3600.0;
/// How many of the biggest processes a notice names.
pub const TOP_N: usize = 3;
/// The memory watch's period, as a sleep takes it.
pub const TICK: Duration = Duration::from_secs(30);

/// One of the user's biggest processes, and the session above it if any.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hog {
    pub pid: u32,
    pub rss_kb: u64,
    pub command: String,
    /// The name of that session.
    pub session: Option<String>,
    /// The canonical id of that session.
    pub session_id: Option<String>,
}

/// When memory went low, when the operator was last told, and when each session was last warned.
/// A restart forgets it, so a notice can repeat inside the hour.
#[derive(Debug, Clone, PartialEq)]
pub struct MemWatch {
    pub low_fraction: f64,
    pub low_since: Option<f64>,
    pub last_alert: Option<f64>,
    pub warned: HashMap<String, f64>,
}

impl Default for MemWatch {
    fn default() -> Self {
        Self::new(LOW_FRACTION)
    }
}

impl MemWatch {
    /// A watch that counts memory as low below `low_fraction` of the total available.
    pub fn new(low_fraction: f64) -> Self {
        Self { low_fraction, low_since: None, last_alert: None, warned: HashMap::new() }
    }

    /// True when the operator should hear about low memory now.
    pub fn tick(&mut self, now: f64, total_kb: u64, available_kb: u64) -> bool {
        if total_kb == 0 || available_kb as f64 / total_kb as f64 >= self.low_fraction {
            self.low_since = None;
            return false;
        }
        let since = *self.low_since.get_or_insert(now);
        if now - since < SUSTAIN_S {
            return false;
        }
        if self.last_alert.is_some_and(|t| now - t < ALERT_EVERY_S) {
            return false;
        }
        self.last_alert = Some(now);
        true
    }

    /// The hogs whose session has not been warned within the hour, marked warned at `now`.
    pub fn sessions_to_warn(&mut self, now: f64, hogs: &[Hog]) -> Vec<Hog> {
        let mut out = Vec::new();
        for hog in hogs {
            let Some(id) = &hog.session_id else { continue };
            if now - self.warned.get(id).copied().unwrap_or(-ALERT_EVERY_S) >= ALERT_EVERY_S {
                self.warned.insert(id.clone(), now);
                out.push(hog.clone());
            }
        }
        out
    }
}

/// `(MemTotal, MemAvailable)` in kB, 0 for a field that is missing.
pub fn parse_meminfo(text: &str) -> (u64, u64) {
    let mut fields = HashMap::new();
    for line in text.lines() {
        let (key, rest) = line.split_once(':').unwrap_or((line, ""));
        if let Some(n) = rest.split_whitespace().next().filter(|n| n.bytes().all(|b| b.is_ascii_digit())) {
            if let Ok(n) = n.parse::<u64>() {
                fields.insert(key, n);
            }
        }
    }
    (fields.get("MemTotal").copied().unwrap_or(0), fields.get("MemAvailable").copied().unwrap_or(0))
}

/// This node's `(MemTotal, MemAvailable)` in kB, or `(0, 0)` when `/proc/meminfo` cannot be read.
pub async fn read_meminfo() -> (u64, u64) {
    tokio::fs::read_to_string("/proc/meminfo").await.map_or((0, 0), |t| parse_meminfo(&t))
}

fn rss_kb(pid: u32) -> u64 {
    let Ok(text) = std::fs::read_to_string(format!("/proc/{pid}/status")) else { return 0 };
    text.lines()
        .find_map(|l| l.strip_prefix("VmRSS:"))
        .and_then(|rest| rest.split_whitespace().next()?.parse().ok())
        .unwrap_or(0)
}

fn command(pid: u32) -> String {
    let Ok(raw) = std::fs::read(format!("/proc/{pid}/cmdline")) else { return "?".into() };
    let text = String::from_utf8_lossy(&raw).replace('\0', " ");
    let text: String = text.trim().chars().take(120).collect();
    if text.is_empty() {
        "?".into()
    } else {
        text
    }
}

fn text_of(v: Option<&Value>) -> Option<String> {
    match v? {
        Value::Null | Value::Bool(false) => None,
        Value::String(s) => Some(s.clone()).filter(|s| !s.is_empty()),
        other => Some(other.to_string()),
    }
}

/// The name and canonical id of the Claude Code session at or above `pid`.
pub fn owner(home: &ClaudeHome, mut pid: u32) -> (Option<String>, Option<String>) {
    let mut seen = 0;
    while pid > 1 && seen < 64 {
        if let Some(rec) = session_by_pid(home, pid) {
            let id = text_of(rec.get("sessionId")).and_then(|raw| SessionId::canonical_of_native(Platform::Claude, &raw));
            return (text_of(rec.get("name")), id);
        }
        pid = awm_plugin_sdk::procfs::ppid(pid).unwrap_or(0);
        seen += 1;
    }
    (None, None)
}

/// The `n` processes of this user with the most resident memory. Blocking reads of `/proc`.
pub fn top_hogs(home: &ClaudeHome, n: usize) -> Vec<Hog> {
    use std::os::unix::fs::MetadataExt;
    // SAFETY: getuid cannot fail.
    let uid = unsafe { libc::getuid() };
    let mut sizes: Vec<(u64, u32)> = std::fs::read_dir("/proc")
        .map(|rd| {
            rd.flatten()
                .filter_map(|e| {
                    let pid: u32 = e.file_name().to_str()?.parse().ok()?;
                    (std::fs::metadata(e.path()).ok()?.uid() == uid).then(|| (rss_kb(pid), pid))
                })
                .collect()
        })
        .unwrap_or_default();
    sizes.sort_unstable_by(|a, b| b.cmp(a));
    sizes
        .into_iter()
        .take(n)
        .map(|(rss_kb, pid)| {
            let (session, session_id) = owner(home, pid);
            Hog { pid, rss_kb, command: command(pid), session, session_id }
        })
        .collect()
}

fn gb(kb: u64) -> String {
    format!("{:.1} GB", kb as f64 / 1024.0 / 1024.0)
}

/// The operator's notice: how low memory is, and the biggest processes with their sessions.
pub fn render(node: &str, total_kb: u64, available_kb: u64, hogs: &[Hog]) -> String {
    let mut lines = vec![format!(
        "keep on {node} (an automatic notice): memory is low, {} of {} available for 2 min or more. \
         When it runs out, the kernel kills a process. The biggest of this user's:",
        gb(available_kb),
        gb(total_kb)
    )];
    for hog in hogs {
        let who = match &hog.session_id {
            Some(id) => format!(", in session {id} ({})", hog.session.as_deref().unwrap_or("None")),
            None => String::new(),
        };
        lines.push(format!("- pid {}, {}: {}{who}", hog.pid, gb(hog.rss_kb), hog.command));
    }
    lines.join("\n")
}

/// The warning sent into the session that owns `hog`.
pub fn session_warning(node: &str, hog: &Hog, available_kb: u64) -> String {
    format!(
        "keep on {node} (an automatic notice): your process {} holds {}, and the node has {} left. \
         When it runs out, the kernel kills a process, possibly another session. Stop it, cap it \
         (`systemd-run --user --scope -p MemoryMax=<n>G ...`), or move the work to an HPC host.",
        hog.pid,
        gb(hog.rss_kb),
        gb(available_kb)
    )
}

/// The node's memory and processes, so a test can fake both.
pub trait Node: Send + Sync {
    /// `(MemTotal, MemAvailable)` in kB.
    fn meminfo(&self) -> impl Future<Output = (u64, u64)> + Send;
    /// The [`TOP_N`] biggest processes of this user.
    fn top_hogs(&self) -> impl Future<Output = Vec<Hog>> + Send;
}

/// The real node, its sessions read through `home`.
#[derive(Debug, Clone)]
pub struct SystemNode {
    pub home: ClaudeHome,
}

impl Node for SystemNode {
    async fn meminfo(&self) -> (u64, u64) {
        read_meminfo().await
    }

    async fn top_hogs(&self) -> Vec<Hog> {
        let home = self.home.clone();
        tokio::task::spawn_blocking(move || top_hogs(&home, TOP_N)).await.unwrap_or_default()
    }
}

/// Who hears a low-memory notice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notify {
    /// This node's name, as the notices give it.
    pub node: String,
    /// The social account and channel that reach the operator; None logs the notice instead.
    pub operator: Option<(String, String)>,
    /// `AWM_SOCIAL_PEER`: the node whose social plugin sends the notice; None for local.
    pub social_peer: Option<String>,
}

async fn tell_operator(gateway: &impl Gateway, notify: &Notify, text: &str) -> Result<(), String> {
    let Some((account, channel)) = &notify.operator else {
        tracing::warn!("keep: no operator DM on this node; {text}");
        return Ok(());
    };
    let args = json!({"account": account, "channel": channel, "text": text});
    gateway.call(Call::new("social", "send", args).peer(notify.social_peer.clone())).await.map(drop)
}

/// One tick of the memory watch at `now`: once memory has stayed low, tell the operator, then each
/// hog's session. Failures are logged, never raised.
pub async fn memory_tick(watch: &mut MemWatch, node: &impl Node, gateway: &impl Gateway, notify: &Notify, now: f64) {
    let (total, available) = node.meminfo().await;
    if !watch.tick(now, total, available) {
        return;
    }
    let hogs = node.top_hogs().await;
    let text = render(&notify.node, total, available, &hogs);
    tracing::warn!("keep: {text}");
    if let Err(e) = tell_operator(gateway, notify, &text).await {
        tracing::warn!("keep: could not tell the operator: {e}");
    }
    for hog in watch.sessions_to_warn(now, &hogs) {
        let Some(id) = hog.session_id.clone() else { continue };
        let args = json!({"to": {"id": id}, "text": session_warning(&notify.node, &hog, available)});
        if let Err(e) = gateway.call(Call::new("reflection", "message", args)).await {
            tracing::warn!("keep: could not warn {id}: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway::testing::FakeGateway;

    const GB: u64 = 1024 * 1024;
    const T0: f64 = 1_792_000_000.0;

    fn hog(pid: u32, rss_kb: u64, command: &str, session: Option<&str>) -> Hog {
        Hog { pid, rss_kb, command: command.into(), session: session.map(String::from), session_id: session.map(|_| format!("cc{}", "a".repeat(32))) }
    }

    #[test]
    fn test_low_memory_that_stays_low_warns_once_an_hour() {
        let mut w = MemWatch::default();
        w.tick(T0, 60 * GB, 2 * GB);
        assert!(w.tick(T0 + SUSTAIN_S, 60 * GB, 2 * GB));
        assert!(!w.tick(T0 + SUSTAIN_S + 30.0, 60 * GB, 2 * GB));
        assert!(w.tick(T0 + SUSTAIN_S + ALERT_EVERY_S, 60 * GB, 2 * GB));
    }

    struct FakeNode(u64, u64, Vec<Hog>);

    impl Node for FakeNode {
        async fn meminfo(&self) -> (u64, u64) {
            (self.0, self.1)
        }

        async fn top_hogs(&self) -> Vec<Hog> {
            self.2.clone()
        }
    }

    #[tokio::test]
    async fn test_a_tick_tells_the_operator_and_the_sessions() {
        let node = FakeNode(60 * GB, 2 * GB, vec![hog(7, 25 * GB, "python big.py", Some("test run"))]);
        let gateway = FakeGateway::default();
        let notify = Notify { node: "node-a".into(), operator: Some(("acct".into(), "C1".into())), social_peer: None };
        let mut watch = MemWatch::default();
        for now in [T0, T0 + SUSTAIN_S, T0 + SUSTAIN_S + 30.0] {
            memory_tick(&mut watch, &node, &gateway, &notify, now).await;
        }
        let calls = gateway.calls.lock().unwrap().clone();
        let sent: Vec<_> = calls.iter().filter(|c| c.tool == "social").collect();
        let messaged: Vec<_> = calls.iter().filter(|c| c.tool != "social").collect();
        assert_eq!(sent.len(), 1);
        assert_eq!((sent[0].verb.as_str(), &sent[0].args["channel"]), ("send", &json!("C1")));
        assert!(sent[0].args["text"].as_str().unwrap().contains("big.py"));
        assert_eq!(messaged.len(), 1);
        assert_eq!((messaged[0].tool.as_str(), messaged[0].verb.as_str()), ("reflection", "message"));
        assert_eq!(messaged[0].args["to"], json!({"id": format!("cc{}", "a".repeat(32))}));
    }

}
