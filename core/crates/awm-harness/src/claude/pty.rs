//! Inject into a background Claude Code session over the daemon's PTY socket.
//!
//! A background session's pty belongs to `claude bg-pty-host`, which publishes its master side on
//! a unix socket. A pty has exactly one master, so nothing tmux can do reaches a background
//! session, and nothing here reaches an interactive one.
//!
//! Wire format, per frame: a four-byte big-endian payload length, then one type byte, then the
//! payload. The length covers the payload only, not the type byte. Type `0x01` is a JSON control
//! frame, `0x00` is raw pty bytes (the app's output, and keystrokes going the other way). Reading
//! is unauthenticated; writing is not, so a control frame carrying the session's `ptyAuth` must be
//! sent before any raw frame, or the input is dropped and an `auth-required` frame comes back.
//!
//! The client speaks first. Since Claude Code 2.1.316 a host that holds an input token greets
//! nobody until the client has sent a frame, and drops a client that stays silent for five
//! seconds. The opening frame is a `pong`: the host treats it as a heartbeat reply, and it gives
//! nothing away to a socket that has not yet said whose it is.
//!
//! This is a private protocol, read out of the Claude Code bundle. The greeting is checked on
//! connect, so a CLI update that moves it degrades to a clear refusal instead of misfiring.
//!
//! Injection can confirm that the frames went to a socket whose host named the expected REPL, and
//! that the host did not reject them. It cannot confirm that the text is on screen (the TUI
//! repaints the composer only when it chooses to), nor that a command ran (a slash command runs at
//! end of turn, after the caller's own turn has to return). What the session did with the
//! keystrokes lives in its own status record, which is the caller's to read. Deciding which
//! session a caller may reach, retrying and waiting for completion are the caller's too.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::time::{sleep, timeout, Instant};

/// Raw pty bytes.
pub const RAW: u8 = 0x00;
/// A JSON control frame.
pub const CTL: u8 = 0x01;

/// The PTY protocol version the roster advertises and this module speaks.
pub const SUPPORTED_PROTO: u32 = 1;

// Bracketed paste (DEC private mode 2004) makes a leading `/` arrive as literal text instead of
// opening the TUI's slash menu.
const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";
const BRACKETED_PASTE_MODE: u32 = 2004;
const ENTER: &[u8] = b"\r";
// Ctrl-U: kill the prompt line, so a retried paste cannot concatenate onto a failed one.
const CLEAR_KEY: &[u8] = b"\x15";

/// The beat between a paste or key landing and the next write.
pub const SETTLE: Duration = Duration::from_millis(350);
/// How long the handshake waits for the host's replay to finish and the session to go live.
pub const HELLO_WAIT: Duration = Duration::from_secs(5);
/// A read stops once the stream has been quiet this long.
pub const QUIET: Duration = Duration::from_millis(600);
/// How long [`Connection::check_not_rejected`] listens after the last write.
pub const ACCEPT_CHECK: Duration = Duration::from_secs(1);
/// The quiet period of that listen. A gap is only noticed when a read times out, so it cannot
/// usefully go below [`RECV_TIMEOUT`].
pub const ACCEPT_QUIET: Duration = Duration::from_millis(300);
const RECV_TIMEOUT: Duration = Duration::from_millis(300);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
// Gives the host a beat to register the auth before the first raw frame.
const AUTH_BEAT: Duration = Duration::from_millis(200);

/// The daemon PTY socket could not be reached, or spoke an unexpected shape.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct DaemonError(pub String);

impl DaemonError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// A background session whose PTY the Claude Code daemon hosts: the protocol's input.
///
/// Producing one means deciding that this caller may reach that session, which is the caller's
/// judgement and never this module's.
#[derive(Clone, PartialEq, Eq)]
pub struct DaemonLane {
    pub sock: PathBuf,
    pub auth: String,
    pub session_id: String,
    pub repl_pid: u32,
    pub name: Option<String>,
    pub cli_version: Option<String>,
    pub dec_modes: Vec<u32>,
    /// (cols, rows) the session was dispatched at, when the roster records it.
    pub geometry: Option<(u16, u16)>,
}

impl DaemonLane {
    /// A lane from the raw roster fields: the PTY socket path, its auth token, the session id,
    /// the REPL pid the greeting must name, and the DEC modes the session reports.
    pub fn from_parts(
        sock: impl Into<PathBuf>,
        auth: impl Into<String>,
        session_id: impl Into<String>,
        repl_pid: u32,
        dec_modes: Vec<u32>,
    ) -> Self {
        Self {
            sock: sock.into(),
            auth: auth.into(),
            session_id: session_id.into(),
            repl_pid,
            name: None,
            cli_version: None,
            dec_modes,
            geometry: None,
        }
    }

    /// The lane of a roster job, or None when the job has no PTY socket, auth token or REPL pid.
    pub fn from_job(job: &super::roster::Job) -> Option<Self> {
        let sock = job.pty_sock.as_deref().filter(|s| !s.is_empty())?;
        let auth = job.pty_auth.as_deref().filter(|a| !a.is_empty())?;
        let repl_pid = job.repl_pid?;
        let dec_modes = job.dec_modes.iter().filter_map(|m| u32::try_from(*m).ok()).collect();
        let lane = Self::from_parts(sock, auth, job.session_id.clone().unwrap_or_default(), repl_pid, dec_modes);
        Some(lane.with_name(Some(job.name.clone())).with_cli_version(job.cli_version.clone()))
    }

    pub fn with_name(mut self, name: Option<String>) -> Self {
        self.name = name;
        self
    }

    pub fn with_cli_version(mut self, cli_version: Option<String>) -> Self {
        self.cli_version = cli_version;
        self
    }

    pub fn with_geometry(mut self, geometry: Option<(u16, u16)>) -> Self {
        self.geometry = geometry;
        self
    }

    pub fn kind(&self) -> &'static str {
        "daemon"
    }

    pub fn hosting(&self) -> &'static str {
        "background"
    }
}

impl std::fmt::Debug for DaemonLane {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DaemonLane")
            .field("sock", &self.sock)
            .field("auth", &"<redacted>")
            .field("session_id", &self.session_id)
            .field("repl_pid", &self.repl_pid)
            .field("name", &self.name)
            .field("cli_version", &self.cli_version)
            .field("dec_modes", &self.dec_modes)
            .field("geometry", &self.geometry)
            .finish()
    }
}

/// Encode one frame. The length prefix excludes the type byte.
pub fn frame(kind: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(5 + payload.len());
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.push(kind);
    out.extend_from_slice(payload);
    out
}

/// An incremental frame decoder over a byte stream that may split or coalesce frames.
#[derive(Debug, Default)]
pub struct FrameReader {
    buf: Vec<u8>,
}

impl FrameReader {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append `data` and return every frame it completed, as (type, payload).
    pub fn feed(&mut self, data: &[u8]) -> Vec<(u8, Vec<u8>)> {
        self.buf.extend_from_slice(data);
        let mut out = Vec::new();
        let mut at = 0;
        while self.buf.len() - at >= 5 {
            let n = u32::from_be_bytes(self.buf[at..at + 4].try_into().unwrap()) as usize;
            if self.buf.len() - at < 5 + n {
                break;
            }
            out.push((self.buf[at + 4], self.buf[at + 5..at + 5 + n].to_vec()));
            at += 5 + n;
        }
        self.buf.drain(..at);
        out
    }
}

fn ctl(value: Value) -> Vec<u8> {
    value.to_string().into_bytes()
}

fn py_repr(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => format!("'{s}'"),
        Some(v) => v.to_string(),
    }
}

fn py_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
    }
}

/// A short-lived, authenticated connection to one session's PTY.
///
/// Short-lived on purpose: the host pings periodically and drops a client that misses three, and
/// the completion signal lives in a file. Pings that arrive while connected are answered.
pub struct Connection<S> {
    target: DaemonLane,
    stream: S,
    reader: FrameReader,
    io_timeout: Duration,
    authed: bool,
    hello: Map<String, Value>,
    output: Vec<u8>,
}

/// Open the lane's socket, then handshake and authenticate on it.
pub async fn connect(lane: &DaemonLane) -> Result<Connection<UnixStream>, DaemonError> {
    let stream = match timeout(CONNECT_TIMEOUT, UnixStream::connect(&lane.sock)).await {
        Ok(Ok(stream)) => stream,
        Ok(Err(exc)) => return Err(unreachable_sock(lane, exc)),
        Err(_) => return Err(unreachable_sock(lane, "timed out")),
    };
    connect_with(lane, stream).await
}

fn unreachable_sock(lane: &DaemonLane, exc: impl std::fmt::Display) -> DaemonError {
    DaemonError(format!(
        "cannot reach the background session's PTY socket at {}: {exc}",
        lane.sock.display()
    ))
}

/// Handshake and authenticate on an already open stream, and hand the connection back open.
///
/// Open one per attempt: the daemon hands out pre-warmed spare sockets that greet healthily before
/// any job claims them, so the handshake's identity check belongs to each attempt.
pub async fn connect_with<S>(lane: &DaemonLane, stream: S) -> Result<Connection<S>, DaemonError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut conn = Connection::new(lane.clone(), stream);
    let opened = match conn.handshake().await {
        Ok(()) => conn.authenticate().await,
        Err(e) => Err(e),
    };
    match opened {
        Ok(()) => Ok(conn),
        Err(e) => {
            conn.close().await;
            Err(e)
        }
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> Connection<S> {
    /// A connection on `stream` that has neither handshaken nor authenticated.
    pub fn new(target: DaemonLane, stream: S) -> Self {
        Self {
            target,
            stream,
            reader: FrameReader::new(),
            io_timeout: CONNECT_TIMEOUT,
            authed: false,
            hello: Map::new(),
            output: Vec::new(),
        }
    }

    /// Shut the stream down. Dropping the connection closes it too.
    pub async fn close(mut self) {
        let _ = self.stream.shutdown().await;
    }

    /// The host's greeting, empty until one arrived.
    pub fn hello(&self) -> &Map<String, Value> {
        &self.hello
    }

    /// The raw pty bytes read so far.
    pub fn output(&self) -> &[u8] {
        &self.output
    }

    pub fn clear_output(&mut self) {
        self.output.clear();
    }

    /// The lane this connection writes to.
    pub fn lane(&self) -> &DaemonLane {
        &self.target
    }

    // Read frames until the stream goes quiet, the cap passes, or (with `until_live`) the session
    // reports live. A batch is always finished: frames already decoded are not recoverable.
    async fn pump(&mut self, until_live: bool, quiet_for: Duration, cap: Duration) -> Result<(), DaemonError> {
        self.io_timeout = RECV_TIMEOUT;
        let start = Instant::now();
        let mut last = start;
        let mut buf = vec![0u8; 65536];
        while start.elapsed() < cap {
            let n = match timeout(RECV_TIMEOUT, self.stream.read(&mut buf)).await {
                Err(_) => {
                    if last.elapsed() > quiet_for {
                        return Ok(());
                    }
                    continue;
                }
                Ok(Err(_)) | Ok(Ok(0)) => return Ok(()),
                Ok(Ok(n)) => n,
            };
            last = Instant::now();
            let mut done = false;
            for (kind, payload) in self.reader.feed(&buf[..n]) {
                if kind == RAW {
                    self.output.extend_from_slice(&payload);
                    continue;
                }
                let Ok(Value::Object(ctl_frame)) = serde_json::from_str::<Value>(&String::from_utf8_lossy(&payload))
                else {
                    continue;
                };
                match ctl_frame.get("t").and_then(Value::as_str) {
                    Some("hello") => self.hello = ctl_frame,
                    Some("ping") => self.send(CTL, &ctl(json!({"t": "pong"}))).await?,
                    Some("auth-required") => {
                        return Err(DaemonError::new(
                            "the background session rejected our input token; it has probably \
                             respawned since the roster was read",
                        ))
                    }
                    Some("live") if until_live => done = true,
                    _ => {}
                }
            }
            if done {
                return Ok(());
            }
        }
        Ok(())
    }

    async fn send(&mut self, kind: u8, payload: &[u8]) -> Result<(), DaemonError> {
        let bytes = frame(kind, payload);
        let write = async {
            self.stream.write_all(&bytes).await?;
            self.stream.flush().await
        };
        match timeout(self.io_timeout, write).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(exc)) => Err(DaemonError(format!("writing to the PTY socket failed: {exc}"))),
            Err(_) => Err(DaemonError::new("writing to the PTY socket failed: timed out")),
        }
    }

    fn require_auth(&self) -> Result<(), DaemonError> {
        if self.authed {
            Ok(())
        } else {
            Err(DaemonError::new("refusing to send input before authenticating"))
        }
    }

    /// Speak first, read the host's greeting, and check it names the lane's REPL.
    ///
    /// No greeting, a greeting without `replPid`, and a greeting naming another REPL each refuse:
    /// a recycled spare socket can belong to another job by the time it is dialled.
    pub async fn handshake(&mut self) -> Result<(), DaemonError> {
        self.send(CTL, &ctl(json!({"t": "pong"}))).await?;
        self.pump(true, QUIET, HELLO_WAIT).await?;
        let sock = self.target.sock.display();
        if self.hello.is_empty() {
            return Err(DaemonError(format!(
                "the background session's PTY host never greeted us on {sock}, even after we \
                 spoke first; the socket is not being served, or this Claude Code build changed \
                 the PTY protocol — refusing to type into it blind"
            )));
        }
        let Some(greeted) = self.hello.get("replPid") else {
            return Err(DaemonError(format!(
                "the background session's PTY host greeted us with an unrecognised message \
                 (version {}); reflection cannot safely drive it — background reflection is \
                 unavailable until this is re-verified against the current Claude Code build",
                py_repr(self.hello.get("version"))
            )));
        };
        if greeted.as_f64() != Some(f64::from(self.target.repl_pid)) {
            return Err(DaemonError(format!(
                "the PTY socket {sock} is hosting REPL {}, not the calling session's REPL {}; the \
                 roster entry is stale (the session was re-hosted, or a pre-warmed socket has \
                 since been handed to another job) — refusing to type into a different session",
                py_str(Some(greeted)),
                self.target.repl_pid
            )));
        }
        Ok(())
    }

    /// Send the lane's auth token, then give the host a beat to register it.
    pub async fn authenticate(&mut self) -> Result<(), DaemonError> {
        let auth = ctl(json!({"t": "auth", "token": self.target.auth}));
        self.send(CTL, &auth).await?;
        self.authed = true;
        sleep(AUTH_BEAT).await;
        Ok(())
    }

    /// Deliver `text` as a bracketed paste, or raw when the session reports DEC modes without 2004.
    pub async fn type_text(&mut self, text: &str) -> Result<(), DaemonError> {
        self.require_auth()?;
        let modes = &self.target.dec_modes;
        let payload = if modes.is_empty() || modes.contains(&BRACKETED_PASTE_MODE) {
            [PASTE_START, text.as_bytes(), PASTE_END].concat()
        } else {
            let head: String = text.chars().take(20).collect();
            tracing::warn!(
                "reflection: session {} does not report bracketed paste; sending {head:?} raw",
                self.target.session_id
            );
            text.as_bytes().to_vec()
        };
        self.send(RAW, &payload).await
    }

    pub async fn press_enter(&mut self) -> Result<(), DaemonError> {
        self.require_auth()?;
        self.send(RAW, ENTER).await
    }

    /// Fail if the host says it discarded what was just written, listening for [`ACCEPT_CHECK`].
    ///
    /// A negative check only: the protocol has no positive acknowledgement.
    pub async fn check_not_rejected(&mut self) -> Result<(), DaemonError> {
        self.check_not_rejected_within(ACCEPT_CHECK).await
    }

    /// [`Connection::check_not_rejected`], listening for at most `cap`.
    pub async fn check_not_rejected_within(&mut self, cap: Duration) -> Result<(), DaemonError> {
        self.pump(false, ACCEPT_QUIET, cap).await
    }

    /// Send raw key bytes, such as a Shift+Tab escape sequence.
    pub async fn send_keys(&mut self, data: &[u8]) -> Result<(), DaemonError> {
        self.require_auth()?;
        self.send(RAW, data).await
    }

    /// Make the session redraw its whole screen at `cols` x `rows`.
    ///
    /// A resize to the current size is ignored, so the screen shrinks by a row and comes back.
    pub async fn repaint(&mut self, cols: u16, rows: u16) -> Result<(), DaemonError> {
        for r in [i64::from(rows) - 1, i64::from(rows)] {
            self.send(CTL, &ctl(json!({"t": "resize", "cols": cols, "rows": r}))).await?;
            sleep(SETTLE).await;
        }
        Ok(())
    }

    /// Everything the host has sent so far, as text with its escape sequences, after reading
    /// until [`QUIET`] or [`HELLO_WAIT`].
    pub async fn screen(&mut self) -> Result<String, DaemonError> {
        self.screen_within(QUIET, HELLO_WAIT).await
    }

    /// [`Connection::screen`] with its own quiet period and cap.
    pub async fn screen_within(&mut self, quiet_for: Duration, cap: Duration) -> Result<String, DaemonError> {
        self.pump(false, quiet_for, cap).await?;
        Ok(String::from_utf8_lossy(&self.output).into_owned())
    }

    /// How a caller names this lane to a person.
    pub fn label(&self) -> String {
        let who = self.target.name.as_deref().filter(|n| !n.is_empty()).unwrap_or(&self.target.session_id);
        format!("background session {who}")
    }

    /// Wipe the prompt line.
    pub async fn clear(&mut self) -> Result<(), DaemonError> {
        self.send_keys(CLEAR_KEY).await?;
        sleep(SETTLE).await;
        Ok(())
    }

    /// Paste `text` and let it settle.
    pub async fn write(&mut self, text: &str) -> Result<(), DaemonError> {
        self.type_text(text).await?;
        sleep(SETTLE).await;
        Ok(())
    }

    /// Press Enter and check the host did not discard it.
    pub async fn commit(&mut self) -> Result<(), DaemonError> {
        self.press_enter().await?;
        self.check_not_rejected().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::task::JoinHandle;

    fn ctl_frame(v: Value) -> Vec<u8> {
        frame(CTL, v.to_string().as_bytes())
    }

    fn raw(text: &str) -> Vec<u8> {
        frame(RAW, text.as_bytes())
    }

    fn hello() -> Vec<u8> {
        [ctl_frame(json!({"t": "hello", "replPid": 4242, "version": "9.0.1"})), ctl_frame(json!({"t": "live"}))]
            .concat()
    }

    fn target() -> DaemonLane {
        DaemonLane::from_parts("/tmp/fake.sock", "tok", "sid-1", 4242, vec![2004])
            .with_name(Some("test".into()))
            .with_cli_version(Some("9.0.1".into()))
    }

    #[derive(Default)]
    struct Script {
        inbound: Vec<u8>,
        // Hold `inbound` back until the client has sent a frame.
        speak_first: bool,
        // Answer every raw frame with `auth-required`.
        reject_raw: bool,
    }

    // A scripted host: serves its inbound bytes and keeps the socket open until the client closes
    // it, then returns every frame the client sent.
    fn peer(stream: UnixStream, script: Script) -> JoinHandle<Vec<(u8, Vec<u8>)>> {
        tokio::spawn(async move {
            let mut stream = stream;
            let mut held = script.inbound;
            if !script.speak_first {
                let _ = stream.write_all(&std::mem::take(&mut held)).await;
            }
            let mut reader = FrameReader::new();
            let mut sent = Vec::new();
            let mut buf = vec![0u8; 65536];
            loop {
                let n = match stream.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => n,
                };
                let frames = reader.feed(&buf[..n]);
                if !held.is_empty() {
                    let _ = stream.write_all(&std::mem::take(&mut held)).await;
                }
                if script.reject_raw && frames.iter().any(|(k, _)| *k == RAW) {
                    let _ = stream.write_all(&ctl_frame(json!({"t": "auth-required"}))).await;
                }
                sent.extend(frames);
            }
            sent
        })
    }

    // Write `text` down the lane the way a sender does, minus the retrying.
    async fn paste_and_submit(
        text: &str,
        lane: DaemonLane,
        enter: bool,
        script: Script,
    ) -> (Result<(), DaemonError>, Vec<(u8, Vec<u8>)>) {
        let (client, server) = UnixStream::pair().unwrap();
        let host = peer(server, script);
        let result = async move {
            let mut conn = connect_with(&lane, client).await?;
            conn.write(text).await?;
            conn.check_not_rejected().await?;
            if enter {
                conn.commit().await?;
            }
            conn.close().await;
            Ok(())
        }
        .await;
        (result, host.await.unwrap())
    }

    fn raws(frames: &[(u8, Vec<u8>)]) -> Vec<Vec<u8>> {
        frames.iter().filter(|(k, _)| *k == RAW).map(|(_, p)| p.clone()).collect()
    }

    fn ctls(frames: &[(u8, Vec<u8>)]) -> Vec<Value> {
        frames.iter().filter(|(k, _)| *k == CTL).map(|(_, p)| serde_json::from_slice(p).unwrap()).collect()
    }

    fn script(inbound: Vec<u8>) -> Script {
        Script { inbound, ..Script::default() }
    }

    #[test]
    fn frame_length_excludes_the_type_byte() {
        let encoded = frame(0x00, b"abcd");
        assert_eq!(encoded, b"\x00\x00\x00\x04\x00abcd");
        assert_eq!(u32::from_be_bytes(encoded[..4].try_into().unwrap()), 4);
    }

    #[tokio::test]
    async fn handshake_keeps_frames_that_share_a_read_with_live() {
        let inbound = [
            ctl_frame(json!({"t": "hello", "replPid": 4242})),
            ctl_frame(json!({"t": "live"})),
            raw("⏵⏵ bypass permissions on"),
        ]
        .concat();
        let (client, server) = UnixStream::pair().unwrap();
        let _host = peer(server, script(inbound));
        let mut conn = connect_with(&target(), client).await.unwrap();
        assert!(conn.screen().await.unwrap().contains("bypass permissions on"));
    }

    #[test]
    fn frame_reader_splits_a_coalesced_stream() {
        let stream =
            [ctl_frame(json!({"t": "hello", "replPid": 4242})), raw("xy"), ctl_frame(json!({"t": "live"}))].concat();
        let mut reader = FrameReader::new();
        let mut got = Vec::new();
        for i in 0..stream.len() {
            got.extend(reader.feed(&stream[i..i + 1]));
        }
        assert_eq!(got.iter().map(|(k, _)| *k).collect::<Vec<_>>(), [0x01, 0x00, 0x01]);
        assert_eq!(got[1].1, b"xy");
    }

    #[tokio::test]
    async fn authenticates_before_sending_any_input() {
        let (res, frames) = paste_and_submit("/compact", target(), true, script(hello())).await;
        res.unwrap();
        assert_eq!(frames[0].0, CTL);
        assert_eq!(frames[1].0, CTL);
        assert_eq!(serde_json::from_slice::<Value>(&frames[0].1).unwrap(), json!({"t": "pong"}));
        assert_eq!(serde_json::from_slice::<Value>(&frames[1].1).unwrap()["t"], "auth");
    }

    #[tokio::test]
    async fn text_is_delivered_as_a_bracketed_paste() {
        let (res, frames) = paste_and_submit("/compact", target(), true, script(hello())).await;
        res.unwrap();
        assert_eq!(raws(&frames)[0], b"\x1b[200~/compact\x1b[201~");
    }

    #[tokio::test]
    async fn enter_is_a_separate_write_after_the_paste() {
        let (res, frames) = paste_and_submit("/compact", target(), true, script(hello())).await;
        res.unwrap();
        assert_eq!(raws(&frames), [b"\x1b[200~/compact\x1b[201~".to_vec(), b"\r".to_vec()]);
    }

    #[tokio::test]
    async fn no_enter_when_not_submitting() {
        let (res, frames) = paste_and_submit("draft", target(), false, script(hello())).await;
        res.unwrap();
        assert_eq!(raws(&frames), [b"\x1b[200~draft\x1b[201~".to_vec()]);
    }

    #[tokio::test]
    async fn unbracketed_when_the_session_does_not_report_the_mode() {
        let mut lane = target();
        lane.dec_modes = vec![1000];
        let (res, frames) = paste_and_submit("hello", lane, false, script(hello())).await;
        res.unwrap();
        assert_eq!(raws(&frames), [b"hello".to_vec()]);
    }

    #[tokio::test]
    async fn ping_is_answered_with_a_pong() {
        let inbound = [
            ctl_frame(json!({"t": "hello", "replPid": 4242})),
            ctl_frame(json!({"t": "ping"})),
            ctl_frame(json!({"t": "live"})),
        ]
        .concat();
        let (res, frames) = paste_and_submit("hi", target(), false, script(inbound)).await;
        res.unwrap();
        let pongs = ctls(&frames).into_iter().filter(|v| *v == json!({"t": "pong"})).count();
        assert_eq!(pongs, 2, "the opener, then the answer to the ping");
    }

    #[tokio::test]
    async fn auth_required_is_surfaced_not_swallowed() {
        let inbound =
            [ctl_frame(json!({"t": "hello", "replPid": 4242})), ctl_frame(json!({"t": "auth-required"}))].concat();
        let (res, _) = paste_and_submit("hi", target(), false, script(inbound)).await;
        assert!(res.unwrap_err().0.contains("input token"));
    }

    #[tokio::test]
    async fn unrecognised_greeting_refuses() {
        let inbound = ctl_frame(json!({"t": "hello", "somethingElse": true, "version": "9.9.9"}));
        let (res, _) = paste_and_submit("hi", target(), false, script(inbound)).await;
        let err = res.unwrap_err().0;
        assert!(err.contains("unrecognised"), "{err}");
        assert!(err.contains("(version '9.9.9')"), "{err}");
    }

    #[tokio::test]
    async fn a_socket_hosting_another_repl_refuses() {
        let inbound = [
            ctl_frame(json!({"t": "hello", "replPid": 999, "version": "9.0.1"})),
            ctl_frame(json!({"t": "live"})),
        ]
        .concat();
        let (res, frames) = paste_and_submit("hi", target(), false, script(inbound)).await;
        assert!(res.unwrap_err().0.contains("hosting REPL 999"));
        assert!(raws(&frames).is_empty());
    }

    #[tokio::test]
    async fn a_host_that_never_greets_refuses() {
        let (res, _) = paste_and_submit("hi", target(), false, script(Vec::new())).await;
        assert!(res.unwrap_err().0.contains("never greeted"));
    }

    #[tokio::test]
    async fn enter_refuses_before_authentication() {
        let (client, server) = UnixStream::pair().unwrap();
        let _host = peer(server, script(hello()));
        let mut conn = Connection::new(target(), client);
        assert!(conn.press_enter().await.unwrap_err().0.contains("before authenticating"));
    }

    #[tokio::test]
    async fn input_the_host_discarded_is_not_reported_as_sent() {
        let rejecting = Script { inbound: hello(), reject_raw: true, ..Script::default() };
        let (res, _) = paste_and_submit("hi", target(), false, rejecting).await;
        assert!(res.unwrap_err().0.contains("input token"));
    }

    #[tokio::test]
    async fn a_host_that_waits_for_the_client_to_speak_is_reached() {
        let speak_first = Script { inbound: hello(), speak_first: true, ..Script::default() };
        let (res, frames) = paste_and_submit("/compact", target(), true, speak_first).await;
        res.unwrap();
        assert_eq!(raws(&frames), [b"\x1b[200~/compact\x1b[201~".to_vec(), b"\r".to_vec()]);
    }

    #[tokio::test]
    async fn the_opening_frame_carries_no_token() {
        let inbound = [ctl_frame(json!({"t": "hello", "replPid": 999})), ctl_frame(json!({"t": "live"}))].concat();
        let speak_first = Script { inbound, speak_first: true, ..Script::default() };
        let (res, frames) = paste_and_submit("hi", target(), false, speak_first).await;
        assert!(res.unwrap_err().0.contains("hosting REPL 999"));
        assert_eq!(ctls(&frames), [json!({"t": "pong"})]);
    }

    #[tokio::test]
    async fn repaint_resizes_one_row_short_then_back() {
        let (client, server) = UnixStream::pair().unwrap();
        let host = peer(server, script(hello()));
        let mut conn = connect_with(&target(), client).await.unwrap();
        let before = Instant::now();
        conn.repaint(120, 40).await.unwrap();
        assert!(before.elapsed() >= SETTLE * 2);
        assert_eq!(conn.label(), "background session test");
        conn.close().await;
        let sent = ctls(&host.await.unwrap());
        assert_eq!(
            sent[2..],
            [json!({"t": "resize", "cols": 120, "rows": 39}), json!({"t": "resize", "cols": 120, "rows": 40})]
        );
    }
}
