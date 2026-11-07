//! The tmux lane: paste into an interactive session's pane, read it back and press keys.
//!
//! The paste is `load-buffer` (stdin), then `paste-buffer -d -p` (bracketed, so a leading `/`
//! lands as text instead of opening the slash menu), then `send-keys Enter`. It never sends Escape,
//! which would cancel the in-flight turn: the command queues behind it instead.

use std::collections::HashMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::io::AsyncWriteExt;

use crate::lane::{Key, Lane, LaneError};
use crate::target::{ProcTable, TmuxLane};

/// The beat between the paste landing and the Enter that submits it, so the TUI has focused the
/// pasted input first.
pub const SETTLE: Duration = Duration::from_millis(150);

/// Ctrl-U, which kills the prompt line. Only ever sent before a retry, so a new paste cannot
/// concatenate onto what a failed attempt left in the box.
pub const CLEAR_KEY: &str = "C-u";

/// A tmux invocation failed or the target pane could not be resolved.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TmuxError(pub String);

/// What one tmux invocation printed and whether it exited zero.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunOutput {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
}

/// Runs one tmux command line; tests pass a pane model instead of a real server.
pub trait Runner: Clone + Send + Sync + 'static {
    /// Run `argv` (the binary first), feeding `stdin` when given. An error means it did not start.
    fn run(&self, argv: Vec<String>, stdin: Option<Vec<u8>>)
        -> impl Future<Output = std::io::Result<RunOutput>> + Send;
}

/// The live runner: a child process per command.
#[derive(Debug, Clone, Copy, Default)]
pub struct ProcessRunner;

impl Runner for ProcessRunner {
    async fn run(&self, argv: Vec<String>, stdin: Option<Vec<u8>>) -> std::io::Result<RunOutput> {
        let mut cmd = tokio::process::Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = cmd.spawn()?;
        if let (Some(data), Some(mut pipe)) = (stdin, child.stdin.take()) {
            pipe.write_all(&data).await?;
        }
        let out = child.wait_with_output().await?;
        Ok(RunOutput {
            success: out.status.success(),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        })
    }
}

/// One pane of `list-panes -a`. There is no activity field: tmux has no per-pane activity format,
/// and the caller's identity, never recency, says which pane is theirs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pane {
    pub pane: String,
    pub pid: String,
    pub command: String,
    pub session: String,
}

/// A tmux server, reached through `runner`.
#[derive(Debug, Clone)]
pub struct Tmux<R = ProcessRunner> {
    pub bin: String,
    pub socket: Option<String>,
    pub runner: R,
}

/// `tmux` from PATH, else `/usr/bin/tmux` for systemd's minimal PATH.
pub fn tmux_bin() -> String {
    std::env::var_os("PATH")
        .and_then(|path| std::env::split_paths(&path).map(|d| d.join("tmux")).find(|p| is_executable(p)))
        .unwrap_or_else(|| PathBuf::from("/usr/bin/tmux"))
        .to_string_lossy()
        .into_owned()
}

fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

impl Tmux<ProcessRunner> {
    /// The live server, or the one on `socket` when given.
    pub fn live(socket: Option<String>) -> Self {
        Self { bin: tmux_bin(), socket, runner: ProcessRunner }
    }
}

impl<R: Runner> Tmux<R> {
    /// A server reached through `runner`, with the binary named `tmux`.
    pub fn with_runner(runner: R) -> Self {
        Self { bin: "tmux".into(), socket: None, runner }
    }

    fn argv(&self, rest: &[&str]) -> Vec<String> {
        let mut argv = vec![self.bin.clone()];
        if let Some(sock) = &self.socket {
            argv.extend(["-S".to_string(), sock.clone()]);
        }
        argv.extend(rest.iter().map(|s| s.to_string()));
        argv
    }

    async fn run(&self, rest: &[&str], stdin: Option<Vec<u8>>) -> Result<RunOutput, TmuxError> {
        let argv = self.argv(rest);
        let what = argv[1..argv.len().min(3)].join(" ");
        match self.runner.run(argv, stdin).await {
            Ok(out) if out.success => Ok(out),
            Ok(out) => Err(TmuxError(format!("{what}… failed: {}", out.stderr.trim()))),
            Err(e) => Err(TmuxError(format!("{what}… failed: {e}"))),
        }
    }

    /// Every pane on the server.
    pub async fn list_panes(&self) -> Result<Vec<Pane>, TmuxError> {
        let fmt = "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}";
        let out = self.run(&["list-panes", "-a", "-F", fmt], None).await?;
        Ok(out
            .stdout
            .lines()
            .filter_map(|line| {
                let parts: Vec<&str> = line.split('\t').collect();
                (parts.len() >= 4).then(|| Pane {
                    pane: parts[0].into(),
                    pid: parts[1].into(),
                    command: parts[2].into(),
                    session: parts[3].into(),
                })
            })
            .collect())
    }

    /// The pane whose process subtree contains `pid`. An exact ancestry match against a pid we
    /// were given, so at most one pane can answer and nothing is ranked.
    pub async fn pane_for_pid(&self, pid: u32, procs: &impl ProcTable) -> Result<Option<String>, TmuxError> {
        let kids = procs.children().await;
        Ok(self
            .list_panes()
            .await?
            .into_iter()
            .find(|p| is_pid(&p.pid) && awm_plugin_sdk::procfs::subtree_contains(p.pid.parse().unwrap_or(0), pid, &kids))
            .map(|p| p.pane))
    }

    /// The visible text of `pane`.
    pub async fn capture_pane(&self, pane: &str) -> Result<String, TmuxError> {
        Ok(self.run(&["capture-pane", "-t", pane, "-p"], None).await?.stdout)
    }

    /// Send one key, by tmux's name for it, to `pane`.
    pub async fn send_keys(&self, pane: &str, key: &str) -> Result<(), TmuxError> {
        self.run(&["send-keys", "-t", pane, key], None).await.map(drop)
    }

    async fn assert_pane_exists(&self, pane: &str) -> Result<(), TmuxError> {
        self.run(&["display-message", "-p", "-t", pane, "#{pane_id}"], None).await.map(drop)
    }

    // The pane id can be real and still point at a shell or editor now (a stale id repointed).
    async fn assert_pane_has_agent(&self, pane: &str, procs: &impl ProcTable) -> Result<(), TmuxError> {
        let out = self.run(&["display-message", "-p", "-t", pane, "#{pane_pid}"], None).await?;
        let pid = out.stdout.trim();
        if !is_pid(pid) || !subtree_has_agent(pid.parse().unwrap_or(0), procs).await {
            return Err(TmuxError(format!("pane {pane} is not running an agent (claude); refusing to inject")));
        }
        Ok(())
    }

    /// Open `lane` for writing, re-checking that the pane exists and still runs claude. The
    /// checks belong to this attempt: a pane can be destroyed or repointed between resolving it
    /// and writing to it, so each retry is a fresh transaction.
    pub async fn open_lane(&self, lane: &TmuxLane, procs: &impl ProcTable) -> Result<TmuxSession<R>, TmuxError> {
        self.assert_pane_exists(&lane.pane).await?;
        self.assert_pane_has_agent(&lane.pane, procs).await?;
        Ok(self.session(lane))
    }

    /// `lane` for reading its screen and pressing keys, unchecked.
    pub fn session(&self, lane: &TmuxLane) -> TmuxSession<R> {
        TmuxSession { tmux: self.clone(), pane: lane.pane.clone() }
    }
}

fn is_pid(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

async fn subtree_has_agent(root: u32, procs: &impl ProcTable) -> bool {
    let kids: HashMap<u32, Vec<u32>> = procs.children().await;
    let (mut stack, mut seen) = (vec![root], std::collections::HashSet::new());
    while let Some(pid) = stack.pop() {
        if !seen.insert(pid) {
            continue;
        }
        if procs.cmdline(pid).await.to_lowercase().contains("claude") {
            return true;
        }
        stack.extend(kids.get(&pid).into_iter().flatten().copied());
    }
    false
}

/// One pane, open as a [`Lane`].
#[derive(Debug, Clone)]
pub struct TmuxSession<R = ProcessRunner> {
    tmux: Tmux<R>,
    pane: String,
}

impl<R: Runner> TmuxSession<R> {
    /// The pane id.
    pub fn pane(&self) -> &str {
        &self.pane
    }
}

impl<R: Runner> Lane for TmuxSession<R> {
    fn label(&self) -> String {
        format!("pane {}", self.pane)
    }

    async fn write(&mut self, text: &str) -> Result<(), LaneError> {
        let buf = format!("awm-rfl-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
        self.tmux.run(&["load-buffer", "-b", &buf, "-"], Some(text.as_bytes().to_vec())).await?;
        self.tmux.run(&["paste-buffer", "-d", "-p", "-b", &buf, "-t", &self.pane], None).await?;
        tokio::time::sleep(SETTLE).await;
        Ok(())
    }

    async fn clear(&mut self) -> Result<(), LaneError> {
        self.tmux.send_keys(&self.pane, CLEAR_KEY).await?;
        tokio::time::sleep(SETTLE).await;
        Ok(())
    }

    /// Nothing to check: tmux has no channel on which a paste is refused. A `capture-pane` check
    /// does not belong here either: Claude Code stops painting its composer while it compacts,
    /// which is the window every deferred resume aims at, so it would read a false negative.
    async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
        Ok(())
    }

    async fn commit(&mut self) -> Result<(), LaneError> {
        Ok(self.tmux.send_keys(&self.pane, "Enter").await?)
    }

    async fn read(&mut self) -> Result<String, LaneError> {
        Ok(self.tmux.capture_pane(&self.pane).await?)
    }

    async fn glance(&mut self) -> Result<String, LaneError> {
        self.read().await
    }

    /// `capture-pane` renders the screen as it stands, so nothing is missing.
    async fn repaint(&mut self) -> Result<bool, LaneError> {
        Ok(false)
    }

    /// `capture-pane` renders the current screen, so there is no stale copy to discard.
    fn forget(&mut self) {}

    async fn press(&mut self, key: Key) -> Result<(), LaneError> {
        Ok(self.tmux.send_keys(&self.pane, key.tmux_name()).await?)
    }

    async fn close(self) {}
}

/// An in-memory tmux server holding one pane with a working prompt box, for tests.
pub mod testing {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use super::{RunOutput, Runner, CLEAR_KEY};

    /// What the fake server has seen and holds.
    #[derive(Debug, Clone)]
    pub struct PaneState {
        pub calls: Vec<Vec<String>>,
        pub buffers: HashMap<String, String>,
        pub prompt: String,
        pub submitted: Vec<String>,
        pub scrollback: String,
        pub pane_pid: String,
        pub session: String,
        /// Rows `list-panes` answers: pane, pid, command, session.
        pub list_panes: Vec<[String; 4]>,
        /// Every command exits non-zero, though its effect still lands.
        pub failing: bool,
        /// False models a compacting session: the paste lands but the composer is not painted.
        pub paints: bool,
        /// Verbs that exit non-zero with no effect.
        pub fail_verbs: Vec<String>,
    }

    impl Default for PaneState {
        fn default() -> Self {
            Self {
                calls: Vec::new(),
                buffers: HashMap::new(),
                prompt: String::new(),
                submitted: Vec::new(),
                scrollback: String::new(),
                pane_pid: "4242".into(),
                session: "sess0".into(),
                list_panes: Vec::new(),
                failing: false,
                paints: true,
                fail_verbs: Vec::new(),
            }
        }
    }

    /// A [`Runner`] over one modelled pane. Clones share the pane.
    #[derive(Debug, Clone, Default)]
    pub struct FakePane(pub Arc<Mutex<PaneState>>);

    impl FakePane {
        pub fn new(state: PaneState) -> Self {
            Self(Arc::new(Mutex::new(state)))
        }

        /// A snapshot of the pane's state.
        pub fn state(&self) -> PaneState {
            self.0.lock().unwrap().clone()
        }

        /// The tmux verb of each call, in order.
        pub fn verbs(&self) -> Vec<String> {
            self.state().calls.iter().map(|argv| verb_of(argv).to_string()).collect()
        }

        /// Every token of every call.
        pub fn flat(&self) -> Vec<String> {
            self.state().calls.into_iter().flatten().collect()
        }
    }

    fn rest_of(argv: &[String]) -> &[String] {
        let rest = &argv[1..];
        if rest.first().map(String::as_str) == Some("-S") {
            &rest[2..]
        } else {
            rest
        }
    }

    fn verb_of(argv: &[String]) -> &str {
        rest_of(argv).first().map(String::as_str).unwrap_or("")
    }

    fn after(argv: &[String], flag: &str) -> String {
        argv.iter().position(|a| a == flag).and_then(|i| argv.get(i + 1)).cloned().unwrap_or_default()
    }

    impl Runner for FakePane {
        async fn run(&self, argv: Vec<String>, stdin: Option<Vec<u8>>) -> std::io::Result<RunOutput> {
            let mut s = self.0.lock().unwrap();
            s.calls.push(argv.clone());
            let verb = verb_of(&argv).to_string();
            if s.fail_verbs.contains(&verb) {
                return Ok(RunOutput { success: false, stdout: String::new(), stderr: "boom".into() });
            }
            let mut stdout = String::new();
            match verb.as_str() {
                "load-buffer" => {
                    let text = String::from_utf8_lossy(&stdin.unwrap_or_default()).into_owned();
                    s.buffers.insert(after(&argv, "-b"), text);
                }
                "paste-buffer" => {
                    let text = s.buffers.get(&after(&argv, "-b")).cloned().unwrap_or_default();
                    s.prompt.push_str(&text);
                }
                "send-keys" => {
                    let key = argv.last().cloned().unwrap_or_default();
                    if key == "Enter" {
                        let prompt = std::mem::take(&mut s.prompt);
                        s.scrollback.push_str(&format!("❯ {prompt}\n"));
                        s.submitted.push(prompt);
                    } else if key == CLEAR_KEY {
                        s.prompt.clear();
                    }
                }
                "capture-pane" => {
                    stdout =
                        if s.paints { format!("{}❯ {}", s.scrollback, s.prompt) } else { s.scrollback.clone() };
                }
                "display-message" => {
                    stdout = match argv.last().map(String::as_str) {
                        Some("#{pane_pid}") => s.pane_pid.clone(),
                        Some("#{session_name}") => s.session.clone(),
                        _ => after(&argv, "-t"),
                    };
                }
                "list-panes" => {
                    stdout = s.list_panes.iter().map(|row| row.join("\t")).collect::<Vec<_>>().join("\n");
                }
                _ => {}
            }
            Ok(RunOutput { success: !s.failing, stdout, stderr: "boom".into() })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{FakePane, PaneState};
    use super::*;
    use crate::target::testing::FakeProcs;

    fn lane() -> TmuxLane {
        TmuxLane { pane: "%7".into(), session_id: "sid-1".into(), repl_pid: 4242, name: Some("test".into()) }
    }

    fn pane(state: PaneState) -> FakePane {
        FakePane::new(state)
    }

    // The sender's lane usage, minus its confirmation: up to three attempts, each opening the lane
    // afresh, the later ones wiping the prompt on the way in, and a wipe after giving up.
    async fn send_with(text: &str, pane: &FakePane, procs: &FakeProcs, enter: bool) -> Result<bool, Vec<String>> {
        let tmux = Tmux::with_runner(pane.clone());
        let mut failures = Vec::new();
        for attempt in 1..=3 {
            let tried: Result<bool, LaneError> = async {
                let mut w = tmux.open_lane(&lane(), procs).await?;
                if attempt > 1 {
                    w.clear().await?;
                }
                w.write(text).await?;
                w.check_not_rejected().await?;
                if enter {
                    w.commit().await?;
                }
                Ok(enter)
            }
            .await;
            match tried {
                Ok(submitted) => return Ok(submitted),
                Err(e) => failures.push(e.to_string()),
            }
        }
        if let Ok(mut w) = tmux.open_lane(&lane(), procs).await {
            let _ = w.clear().await;
        }
        Err(failures)
    }

    async fn send(text: &str, pane: &FakePane) -> Result<bool, Vec<String>> {
        send_with(text, pane, &FakeProcs::new(), true).await
    }

    fn call_with(pane: &FakePane, verb: &str) -> Vec<String> {
        pane.state().calls.into_iter().find(|a| a.iter().any(|t| t == verb)).unwrap()
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_command_lands_in_the_prompt_and_is_submitted() {
        let pane = pane(PaneState::default());
        assert_eq!(send("/compact", &pane).await, Ok(true));
        assert_eq!(pane.state().submitted, ["/compact"]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_paste_is_bracketed_and_targets_the_pane() {
        // `-p` is load-bearing: without bracketed paste a leading `/` opens the slash menu.
        let pane = pane(PaneState::default());
        send("/compact", &pane).await.unwrap();
        let paste = call_with(&pane, "paste-buffer");
        assert!(paste.contains(&"-p".to_string()) && paste.contains(&"-d".to_string()));
        assert_eq!(after_flag(&paste, "-t"), "%7");
    }

    fn after_flag(argv: &[String], flag: &str) -> String {
        argv[argv.iter().position(|a| a == flag).unwrap() + 1].clone()
    }

    #[tokio::test(start_paused = true)]
    async fn test_no_escape_is_ever_sent() {
        // Escape would cancel the agent's in-flight turn.
        let pane = pane(PaneState::default());
        send("/compact", &pane).await.unwrap();
        assert!(!pane.flat().contains(&"Escape".to_string()));
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_text_goes_in_on_stdin_not_as_an_argument() {
        // Command lines are visible to every process on the box.
        let pane = pane(PaneState::default());
        send("/model opus", &pane).await.unwrap();
        let load = call_with(&pane, "load-buffer");
        assert_eq!(load.last().unwrap(), "-");
        assert!(!load.join(" ").contains("/model opus"));
        assert_eq!(pane.state().buffers.values().next().unwrap(), "/model opus");
    }

    #[tokio::test(start_paused = true)]
    async fn test_enter_false_leaves_the_text_in_the_prompt_unsubmitted() {
        let pane = pane(PaneState::default());
        let submitted = send_with("half a thought", &pane, &FakeProcs::new(), false).await;
        assert_eq!(submitted, Ok(false));
        assert!(pane.state().submitted.is_empty());
        assert_eq!(pane.state().prompt, "half a thought");
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_paste_the_tui_never_paints_is_still_submitted() {
        // Claude Code stops painting its composer while it compacts; the paste is there anyway.
        let pane = pane(PaneState { paints: false, ..PaneState::default() });
        assert_eq!(send("/compact", &pane).await, Ok(true));
        assert_eq!(pane.state().submitted, ["/compact"]);
        assert_eq!(pane.verbs().iter().filter(|v| *v == "paste-buffer").count(), 1, "and on the first attempt");
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_pane_is_never_captured_during_a_send() {
        let pane = pane(PaneState::default());
        send("/compact", &pane).await.unwrap();
        assert!(!pane.verbs().contains(&"capture-pane".to_string()));
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_retry_wipes_the_prompt_with_ctrl_u() {
        let pane = pane(PaneState { fail_verbs: vec!["paste-buffer".into()], ..PaneState::default() });
        assert!(send("/compact", &pane).await.is_err());
        assert_eq!(
            pane.flat().iter().filter(|t| *t == CLEAR_KEY).count(),
            3,
            "attempts 2 and 3 wipe on the way in; the give-up wipes on the way out"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_pane_running_no_agent_is_refused() {
        let pane = pane(PaneState::default());
        let procs = FakeProcs { agent: false, ..FakeProcs::new() };
        let failures = send_with("/compact", &pane, &procs, true).await.unwrap_err();
        assert!(failures[0].contains("is not running an agent (claude)"), "{failures:?}");
        assert!(pane.state().submitted.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_dead_pane_is_refused() {
        let pane = pane(PaneState { failing: true, ..PaneState::default() });
        let failures = send("/compact", &pane).await.unwrap_err();
        assert_eq!(failures[0], "display-message -p… failed: boom");
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_pane_is_re_checked_on_every_attempt() {
        let pane = pane(PaneState { fail_verbs: vec!["paste-buffer".into()], ..PaneState::default() });
        assert!(send("/compact", &pane).await.is_err());
        assert_eq!(
            pane.verbs().iter().filter(|v| *v == "display-message").count(),
            8,
            "two assertions per attempt, three attempts, plus the give-up wipe"
        );
    }

    fn row(pane: &str, pid: &str, cmd: &str, sess: &str) -> [String; 4] {
        [pane.into(), pid.into(), cmd.into(), sess.into()]
    }

    #[tokio::test]
    async fn test_a_pane_is_found_by_containing_the_caller() {
        let procs = FakeProcs { children: HashMap::from([(100, vec![200]), (200, vec![4242])]), ..FakeProcs::new() };
        let pane = pane(PaneState {
            list_panes: vec![row("%1", "999", "bash", "s"), row("%7", "100", "claude", "s")],
            ..PaneState::default()
        });
        let tmux = Tmux::with_runner(pane);
        assert_eq!(tmux.pane_for_pid(4242, &procs).await.unwrap().as_deref(), Some("%7"));
    }

    #[tokio::test]
    async fn test_a_caller_in_no_pane_resolves_to_nothing() {
        let procs = FakeProcs { children: HashMap::from([(100, vec![200])]), ..FakeProcs::new() };
        let pane = pane(PaneState { list_panes: vec![row("%1", "100", "claude", "s")], ..PaneState::default() });
        assert_eq!(Tmux::with_runner(pane).pane_for_pid(4242, &procs).await.unwrap(), None);
    }

    #[tokio::test]
    async fn test_panes_are_never_ranked_by_recency() {
        // tmux has no per-pane activity format: it expanded to empty, every pane ranked the same,
        // and every deferred resume went to the lowest-numbered agent pane.
        let pane = pane(PaneState { list_panes: vec![row("%1", "100", "claude", "s")], ..PaneState::default() });
        Tmux::with_runner(pane.clone()).list_panes().await.unwrap();
        let fmt = call_with(&pane, "list-panes").last().unwrap().clone();
        assert!(!fmt.contains("activity"));
    }
}
