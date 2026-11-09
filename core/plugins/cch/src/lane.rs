//! One lane abstraction over the two ways a keystroke reaches a session: a tmux pane and a
//! background session's daemon PTY. Senders and screen readers drive both through [`Lane`] and
//! must not know which one they hold.

use std::future::Future;
use std::time::Duration;

use awm_harness::claude::pty::{self, Connection, DaemonError};
use awm_harness::claude::ClaudeHome;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::UnixStream;

use crate::target::{LiveProcs, ProcTable, ResolveError, Target};
use crate::tmux::{ProcessRunner, Runner, Tmux, TmuxError, TmuxSession};

/// Why a lane could not be resolved, opened, written or read.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum LaneError {
    #[error(transparent)]
    Resolve(#[from] ResolveError),
    #[error(transparent)]
    Tmux(#[from] TmuxError),
    #[error(transparent)]
    Daemon(#[from] DaemonError),
}

/// A key a verb may press. Escape alone is the interrupt key; Down and Enter walk a modal's list;
/// Shift+Tab steps the permission-mode cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Key {
    ShiftTab,
    Escape,
    Enter,
    Down,
    Up,
}

impl Key {
    /// The key by the name a verb asks for it (`Escape`, `Enter`, `Down`, `Up`, `BTab`).
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "BTab" => Some(Self::ShiftTab),
            "Escape" => Some(Self::Escape),
            "Enter" => Some(Self::Enter),
            "Down" => Some(Self::Down),
            "Up" => Some(Self::Up),
            _ => None,
        }
    }

    /// tmux's `send-keys` name.
    pub fn tmux_name(self) -> &'static str {
        match self {
            Self::ShiftTab => "BTab",
            Self::Escape => "Escape",
            Self::Enter => "Enter",
            Self::Down => "Down",
            Self::Up => "Up",
        }
    }

    /// The bytes a raw pty receives. Shift+Tab is CSI Z, "cursor back tab".
    pub fn raw_bytes(self) -> &'static [u8] {
        match self {
            Self::ShiftTab => b"\x1b[Z",
            Self::Escape => b"\x1b",
            Self::Enter => b"\r",
            Self::Down => b"\x1b[B",
            Self::Up => b"\x1b[A",
        }
    }
}

/// One open session lane. Open one per attempt (see [`Opener`]) and close it when done.
///
/// The writer half has no read-back: neither lane can tell from the screen whether a write
/// arrived (the TUI repaints its composer when it likes, and not at all while compacting), so
/// whether a submit took is the session record's to say, never the lane's.
pub trait Lane: Send {
    /// How a result names this lane to a person: `pane %7`, `background session <name>`.
    fn label(&self) -> String;

    /// Paste `text` into the prompt as a bracketed paste and let it settle. Never presses Enter.
    fn write(&mut self, text: &str) -> impl Future<Output = Result<(), LaneError>> + Send;

    /// Wipe the prompt line (Ctrl-U) and let it settle. Only before a retry, or after a give-up.
    fn clear(&mut self) -> impl Future<Output = Result<(), LaneError>> + Send;

    /// Fail if the host announced discarding what was just written. A negative check only: Ok
    /// does not mean the text arrived. The tmux lane has no such channel and always answers Ok.
    fn check_not_rejected(&mut self) -> impl Future<Output = Result<(), LaneError>> + Send;

    /// Press Enter to submit what was written. Never retry after this: the command may be running.
    fn commit(&mut self) -> impl Future<Output = Result<(), LaneError>> + Send;

    /// The screen once it has gone quiet (daemon: up to 3 s), with escape sequences on the daemon
    /// lane. Pass it through `guards::current_screen` before classifying it.
    fn read(&mut self) -> impl Future<Output = Result<String, LaneError>> + Send;

    /// The screen after a short listen (daemon: up to 0.3 s), for polling a busy session whose
    /// spinner never lets the stream go quiet.
    fn glance(&mut self) -> impl Future<Output = Result<String, LaneError>> + Send;

    /// Have the session redraw its whole screen. False when the lane cannot (tmux renders the
    /// current screen anyway; a daemon session of unknown size cannot be asked).
    fn repaint(&mut self) -> impl Future<Output = Result<bool, LaneError>> + Send;

    /// Press one key.
    fn press(&mut self, key: Key) -> impl Future<Output = Result<(), LaneError>> + Send;

    /// Close the lane. Dropping it closes it too.
    fn close(self) -> impl Future<Output = ()> + Send
    where
        Self: Sized;
}

/// Opens a resolved [`Target`] as a [`Lane`].
pub trait Opener: Send + Sync {
    type Lane: Lane;

    /// Open `target` for writing, re-checking it is still that session: a tmux pane must exist
    /// and run claude, a daemon socket must greet as the target's REPL. Call once per attempt.
    fn open_writer(&self, target: &Target) -> impl Future<Output = Result<Self::Lane, LaneError>> + Send;

    /// Open `target` to read its screen and press keys.
    fn open_session(&self, target: &Target) -> impl Future<Output = Result<Self::Lane, LaneError>> + Send;
}

/// A daemon `read` returns once the stream has been quiet this long.
pub const READ_QUIET: Duration = Duration::from_millis(300);
/// The longest a daemon `read` listens.
pub const READ_CAP: Duration = Duration::from_secs(3);
/// A daemon `glance` needs only the next paint, not quiet.
pub const GLANCE_QUIET: Duration = Duration::from_millis(100);
/// The longest a daemon `glance` listens.
pub const GLANCE_CAP: Duration = Duration::from_millis(300);

/// A background session's PTY, open as a [`Lane`].
pub struct DaemonSession<S = UnixStream> {
    conn: Connection<S>,
}

impl<S: AsyncRead + AsyncWrite + Unpin + Send> DaemonSession<S> {
    /// Wrap a connection that has handshaken and authenticated.
    pub fn new(conn: Connection<S>) -> Self {
        Self { conn }
    }

    /// The connection, for a protocol verb the lane does not cover.
    pub fn connection(&mut self) -> &mut Connection<S> {
        &mut self.conn
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin + Send> Lane for DaemonSession<S> {
    fn label(&self) -> String {
        self.conn.label()
    }

    async fn write(&mut self, text: &str) -> Result<(), LaneError> {
        Ok(self.conn.write(text).await?)
    }

    async fn clear(&mut self) -> Result<(), LaneError> {
        Ok(self.conn.clear().await?)
    }

    async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
        Ok(self.conn.check_not_rejected().await?)
    }

    async fn commit(&mut self) -> Result<(), LaneError> {
        Ok(self.conn.commit().await?)
    }

    async fn read(&mut self) -> Result<String, LaneError> {
        Ok(self.conn.screen_within(READ_QUIET, READ_CAP).await?)
    }

    async fn glance(&mut self) -> Result<String, LaneError> {
        Ok(self.conn.screen_within(GLANCE_QUIET, GLANCE_CAP).await?)
    }

    async fn repaint(&mut self) -> Result<bool, LaneError> {
        let Some((cols, rows)) = self.conn.lane().geometry else {
            return Ok(false);
        };
        self.conn.repaint(cols, rows).await?;
        Ok(true)
    }

    async fn press(&mut self, key: Key) -> Result<(), LaneError> {
        Ok(self.conn.send_keys(key.raw_bytes()).await?)
    }

    async fn close(self) {
        self.conn.close().await;
    }
}

/// Either live lane, as [`Host`] opens it.
pub enum AnyLane<R = ProcessRunner> {
    Tmux(TmuxSession<R>),
    Daemon(Box<DaemonSession<UnixStream>>),
}

macro_rules! each {
    ($self:ident, $l:ident => $e:expr) => {
        match $self {
            AnyLane::Tmux($l) => $e,
            AnyLane::Daemon($l) => $e,
        }
    };
}

impl<R: Runner> Lane for AnyLane<R> {
    fn label(&self) -> String {
        each!(self, l => l.label())
    }

    async fn write(&mut self, text: &str) -> Result<(), LaneError> {
        each!(self, l => l.write(text).await)
    }

    async fn clear(&mut self) -> Result<(), LaneError> {
        each!(self, l => l.clear().await)
    }

    async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
        each!(self, l => l.check_not_rejected().await)
    }

    async fn commit(&mut self) -> Result<(), LaneError> {
        each!(self, l => l.commit().await)
    }

    async fn read(&mut self) -> Result<String, LaneError> {
        each!(self, l => l.read().await)
    }

    async fn glance(&mut self) -> Result<String, LaneError> {
        each!(self, l => l.glance().await)
    }

    async fn repaint(&mut self) -> Result<bool, LaneError> {
        each!(self, l => l.repaint().await)
    }

    async fn press(&mut self, key: Key) -> Result<(), LaneError> {
        each!(self, l => l.press(key).await)
    }

    async fn close(self) {
        match self {
            AnyLane::Tmux(l) => l.close().await,
            AnyLane::Daemon(l) => (*l).close().await,
        }
    }
}

/// This host's sessions: where Claude Code keeps its records, the tmux server and the process
/// table. It resolves callers ([`crate::target::Resolve`]) and opens their lanes ([`Opener`]).
#[derive(Debug, Clone)]
pub struct Host<R = ProcessRunner, P = LiveProcs> {
    pub home: ClaudeHome,
    pub tmux: Tmux<R>,
    pub procs: P,
}

impl Host {
    /// The live host: `home`, the tmux server on `socket` (the default one when None), `/proc`.
    pub fn live(home: ClaudeHome, socket: Option<String>) -> Self {
        Self { home, tmux: Tmux::live(socket), procs: LiveProcs }
    }
}

impl<R: Runner, P: ProcTable> Opener for Host<R, P> {
    type Lane = AnyLane<R>;

    async fn open_writer(&self, target: &Target) -> Result<AnyLane<R>, LaneError> {
        match target {
            Target::Tmux(lane) => Ok(AnyLane::Tmux(self.tmux.open_lane(lane, &self.procs).await?)),
            Target::Daemon(lane) => Ok(AnyLane::Daemon(Box::new(DaemonSession::new(pty::connect(lane).await?)))),
        }
    }

    async fn open_session(&self, target: &Target) -> Result<AnyLane<R>, LaneError> {
        match target {
            Target::Tmux(lane) => Ok(AnyLane::Tmux(self.tmux.session(lane))),
            Target::Daemon(lane) => Ok(AnyLane::Daemon(Box::new(DaemonSession::new(pty::connect(lane).await?)))),
        }
    }
}

/// A scripted in-memory lane, for the tests of modules that drive lanes.
pub mod testing {
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};

    use super::{Key, Lane, LaneError, Opener};
    use crate::target::Target;

    /// One call made on a [`ScriptedLane`].
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum Op {
        Open,
        Write(String),
        Clear,
        CheckNotRejected,
        Commit,
        Read,
        Glance,
        Repaint,
        Press(Key),
        Close,
    }

    type Failure = (Box<dyn Fn(&Op) -> bool + Send>, LaneError);

    #[derive(Default)]
    struct Script {
        ops: Vec<Op>,
        screens: VecDeque<String>,
        failures: Vec<Failure>,
    }

    /// A lane that records every call, serves queued screens, and fails where told. It is its own
    /// [`Opener`]: opening hands back a clone, and clones share one script.
    #[derive(Clone, Default)]
    pub struct ScriptedLane(Arc<Mutex<Script>>);

    impl ScriptedLane {
        pub fn new() -> Self {
            Self::default()
        }

        /// Queue a screen for `read`/`glance`. The last one queued repeats once the rest are used.
        pub fn push_screen(&self, screen: impl Into<String>) -> &Self {
            self.0.lock().unwrap().screens.push_back(screen.into());
            self
        }

        /// Fail every call that `matches` with `err`, `Op::Open` included.
        pub fn fail_when(&self, matches: impl Fn(&Op) -> bool + Send + 'static, err: LaneError) -> &Self {
            self.0.lock().unwrap().failures.push((Box::new(matches), err));
            self
        }

        /// Every call so far, in order.
        pub fn ops(&self) -> Vec<Op> {
            self.0.lock().unwrap().ops.clone()
        }

        fn record(&self, op: Op) -> Result<(), LaneError> {
            let mut s = self.0.lock().unwrap();
            let failed = s.failures.iter().find(|(m, _)| m(&op)).map(|(_, e)| e.clone());
            s.ops.push(op);
            failed.map_or(Ok(()), Err)
        }

        fn screen(&self, op: Op) -> Result<String, LaneError> {
            self.record(op)?;
            let mut s = self.0.lock().unwrap();
            Ok(if s.screens.len() > 1 { s.screens.pop_front() } else { s.screens.front().cloned() }
                .unwrap_or_default())
        }
    }

    impl Lane for ScriptedLane {
        fn label(&self) -> String {
            "scripted lane".into()
        }

        async fn write(&mut self, text: &str) -> Result<(), LaneError> {
            self.record(Op::Write(text.into()))
        }

        async fn clear(&mut self) -> Result<(), LaneError> {
            self.record(Op::Clear)
        }

        async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
            self.record(Op::CheckNotRejected)
        }

        async fn commit(&mut self) -> Result<(), LaneError> {
            self.record(Op::Commit)
        }

        async fn read(&mut self) -> Result<String, LaneError> {
            self.screen(Op::Read)
        }

        async fn glance(&mut self) -> Result<String, LaneError> {
            self.screen(Op::Glance)
        }

        async fn repaint(&mut self) -> Result<bool, LaneError> {
            self.record(Op::Repaint).map(|()| false)
        }

        async fn press(&mut self, key: Key) -> Result<(), LaneError> {
            self.record(Op::Press(key))
        }

        async fn close(self) {
            let _ = self.record(Op::Close);
        }
    }

    impl Opener for ScriptedLane {
        type Lane = ScriptedLane;

        async fn open_writer(&self, _target: &Target) -> Result<ScriptedLane, LaneError> {
            self.record(Op::Open).map(|()| self.clone())
        }

        async fn open_session(&self, _target: &Target) -> Result<ScriptedLane, LaneError> {
            self.record(Op::Open).map(|()| self.clone())
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::target::TmuxLane;
    use crate::tmux::testing::{FakePane, PaneState};
    use awm_harness::claude::pty::{connect_with, frame, DaemonLane, FrameReader, CTL};
    use serde_json::json;
    use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream};
    use tokio::task::JoinHandle;

    pub(crate) fn daemon_target() -> DaemonLane {
        DaemonLane::from_parts("/tmp/fake.sock", "tok", "sid-1", 4242, vec![2004])
            .with_name(Some("test".into()))
            .with_cli_version(Some("9.0.1".into()))
    }

    pub(crate) type Frames = JoinHandle<Vec<(u8, Vec<u8>)>>;

    // A scripted PTY host: serves `inbound`, then returns every frame the client sent once it closes.
    pub(crate) fn fake_host(inbound: Vec<u8>) -> (DuplexStream, Frames) {
        let (client, mut server) = tokio::io::duplex(1 << 16);
        let host = tokio::spawn(async move {
            let _ = server.write_all(&inbound).await;
            let (mut reader, mut sent, mut buf) = (FrameReader::new(), Vec::new(), vec![0u8; 65536]);
            loop {
                match server.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => sent.extend(reader.feed(&buf[..n])),
                }
            }
            sent
        });
        (client, host)
    }

    pub(crate) async fn daemon_session(lane: DaemonLane, inbound: Vec<u8>) -> (DaemonSession<DuplexStream>, Frames) {
        let (client, host) = fake_host(inbound);
        (DaemonSession::new(connect_with(&lane, client).await.unwrap()), host)
    }

    fn tmux_session(pane: &FakePane) -> TmuxSession<FakePane> {
        let lane = TmuxLane { pane: "%7".into(), session_id: "sid-1".into(), repl_pid: 4242, name: None };
        Tmux::with_runner(pane.clone()).session(&lane)
    }

    #[tokio::test(start_paused = true)]
    async fn test_every_lane_answers_the_rejection_check() {
        // Only the daemon lane has anything to report; the others answer by not failing.
        let only_hello = frame(CTL, json!({"t": "hello", "replPid": 4242}).to_string().as_bytes());
        let (mut daemon, _host) = daemon_session(daemon_target(), only_hello).await;
        daemon.check_not_rejected().await.unwrap();
        let pane = FakePane::new(PaneState::default());
        tmux_session(&pane).check_not_rejected().await.unwrap();
        assert!(pane.state().calls.is_empty());
    }

}
