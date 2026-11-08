//! The `stream` session: a browser's s16le PCM into a `pacat` playing into the sink.
//!
//! The page opens `/hub/session/mic/stream` with its capture format, and the gateway relays the
//! socket here. Binary frames are PCM; text frames are JSON control. Back go `ready`, `error`,
//! `superseded` and `pong`, written by Python's `json.dumps` rules, as the page has always read
//! them. A new stream supersedes the old one, so two phones never mix into the sink, and the
//! loser is told.

use std::ffi::OsString;
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::{Bridge, WsMsg};
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;
use tokio::process::ChildStdin;
use tokio::sync::watch;

use crate::audio::{self, Audio};

/// An abandoned session would otherwise hold a live `pacat` on the sink forever.
pub const FIRST_FRAME_TIMEOUT: Duration = Duration::from_secs(30);

/// Every browser's native rate, and nothing `pacat` would pitch-shift silently.
pub const MIN_RATE: i64 = 8000;
pub const MAX_RATE: i64 = 192000;

/// One frame from the browser.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Frame {
    Text(String),
    Binary(Vec<u8>),
}

/// The service's end of a session. [`Bridge`] is the real one.
pub trait Wire: Send {
    /// The next frame, or None once the socket closed.
    fn recv(&mut self) -> impl Future<Output = Option<Frame>> + Send;
    fn send_text(&mut self, text: String) -> impl Future<Output = Result<(), String>> + Send;
    fn close(self) -> impl Future<Output = ()> + Send;
}

impl Wire for Bridge {
    async fn recv(&mut self) -> Option<Frame> {
        match Bridge::recv(self).await? {
            WsMsg::Text(t) => Some(Frame::Text(t)),
            WsMsg::Binary(b) => Some(Frame::Binary(b)),
            WsMsg::Close(_) => None,
        }
    }

    async fn send_text(&mut self, text: String) -> Result<(), String> {
        self.send(WsMsg::Text(text)).await.map_err(|e| e.to_string())
    }

    async fn close(self) {
        Bridge::close(self).await
    }
}

/// The argv the PCM plays into the sink with. **CAUTION** It is the audio path itself:
/// `--latency-msec=40` is tuned against the sink.
pub fn pacat_argv(sink: &str, rate: u32, channels: u32) -> Vec<String> {
    [
        "pacat".to_string(),
        "--playback".into(),
        "-d".into(),
        sink.into(),
        "--raw".into(),
        "--format=s16le".into(),
        format!("--rate={rate}"),
        format!("--channels={channels}"),
        "--client-name=awm-mic".into(),
        "--stream-name=browser-mic".into(),
        "--latency-msec=40".into(),
    ]
    .into()
}

/// The environment `pacat` runs with: this process's, plus a runtime dir when none is set.
pub fn pacat_env() -> Vec<(OsString, OsString)> {
    let mut env: Vec<(OsString, OsString)> = std::env::vars_os().collect();
    if !env.iter().any(|(k, _)| k == "XDG_RUNTIME_DIR") {
        env.push(("XDG_RUNTIME_DIR".into(), audio::default_runtime_dir().into()));
    }
    env
}

/// Python's `int()` of a JSON value: an integer, a truncated float, a bool, or a decimal string.
fn py_int(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(*b as i64),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite() && f.abs() < 9.2e18).map(|f| f.trunc() as i64)),
        Value::String(s) => {
            let t = awm_pycompat::pystr::strip(s);
            let (sign, digits) = match t.strip_prefix('-') {
                Some(rest) => (-1, rest),
                None => (1, t.strip_prefix('+').unwrap_or(t)),
            };
            let ok = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c == '_' || c.is_ascii_digit());
            if !ok {
                return None;
            }
            digits.replace('_', "").parse::<i64>().ok().map(|n| sign * n)
        }
        _ => None,
    }
}

/// Check the capture format the browser opened the session with. A wrong rate pitch-shifts
/// everything recorded with no error anywhere, so it never reaches `pacat`.
pub fn parse_init(init: &Value) -> Result<(u32, u32), String> {
    let get = |k: &str| init.get(k);
    match get("format") {
        None => {}
        Some(Value::String(s)) if s == "s16le" => {}
        Some(other) => return Err(format!("unsupported format {}; mic accepts s16le only", awm_pycompat::repr::repr(other))),
    }
    let rate = get("sampleRate").map_or(Some(48000), py_int);
    let channels = get("channels").map_or(Some(1), py_int);
    let (Some(rate), Some(channels)) = (rate, channels) else {
        return Err("sampleRate and channels must be integers".into());
    };
    if !(MIN_RATE..=MAX_RATE).contains(&rate) {
        return Err(format!("sampleRate {rate} outside [{MIN_RATE}, {MAX_RATE}]"));
    }
    if channels != 1 && channels != 2 {
        return Err(format!("channels {channels} must be 1 or 2"));
    }
    Ok((rate as u32, channels as u32))
}

/// One browser's audio and the `pacat` it feeds.
pub struct Stream {
    pub rate: u32,
    pub channels: u32,
    pid: Option<u32>,
    stdin: tokio::sync::Mutex<Option<ChildStdin>>,
    exited: watch::Receiver<bool>,
    superseded: watch::Sender<bool>,
}

impl Stream {
    pub fn exited(&self) -> bool {
        *self.exited.borrow()
    }

    /// Wait until `pacat` has exited and been reaped.
    pub async fn wait_exit(&self) {
        let mut rx = self.exited.clone();
        let _ = rx.wait_for(|e| *e).await;
    }

    /// Another device took the microphone: this stream ends.
    fn supersede(&self) {
        self.superseded.send_replace(true);
        self.kill();
    }

    /// Close `pacat`'s stdin and terminate it. Safe to call twice.
    fn kill(&self) {
        if let Ok(mut stdin) = self.stdin.try_lock() {
            stdin.take();
        }
        if let (Some(pid), false) = (self.pid, self.exited()) {
            // SAFETY: a signal to our own child, which the reaper has not reaped yet.
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        }
    }
}

#[derive(Default)]
struct Live {
    active_streams: usize,
    last_pcm_ts: f64,
    current: Option<Arc<Stream>>,
}

/// mic's audio path: the sink upkeep and the live stream.
pub struct Mic {
    pub audio: Arc<Audio>,
    pub sink: String,
    /// The program run as `pacat`; tests point it at a fake.
    pub pacat: OsString,
    pub first_frame_timeout: Duration,
    live: Mutex<Live>,
}

impl Mic {
    pub fn new(audio: Arc<Audio>, sink: &str) -> Self {
        Self { audio, sink: sink.into(), pacat: "pacat".into(), first_frame_timeout: FIRST_FRAME_TIMEOUT, live: Mutex::new(Live::default()) }
    }

    fn live(&self) -> std::sync::MutexGuard<'_, Live> {
        self.live.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn current(&self) -> Option<Arc<Stream>> {
        self.live().current.clone()
    }

    /// The stream half of `mic_status`.
    pub fn status(&self) -> Value {
        let live = self.live();
        let cur = live.current.as_ref();
        json!({
            "sink": self.sink,
            "active_streams": live.active_streams,
            "last_pcm_ts": (live.last_pcm_ts != 0.0).then_some(live.last_pcm_ts),
            "stream_rate": cur.map(|c| c.rate),
            "stream_channels": cur.map(|c| c.channels),
        })
    }

    /// Spawn this stream's `pacat`, superseding whatever fed the sink. The sink is ensured
    /// first, best effort: it may well exist, and refusing audio would be the worse failure.
    pub async fn start_stream(&self, rate: u32, channels: u32) -> std::io::Result<Arc<Stream>> {
        if let Err(e) = self.audio.ensure_once(&self.sink).await {
            tracing::warn!("sink ensure failed ({e}); starting pacat anyway");
        }
        let argv = pacat_argv(&self.sink, rate, channels);
        let mut child = tokio::process::Command::new(&self.pacat)
            .args(&argv[1..])
            .env_clear()
            .envs(pacat_env())
            .stdin(std::process::Stdio::piped())
            .spawn()?;
        let stdin = child.stdin.take();
        let pid = child.id();
        let (done, exited) = watch::channel(false);
        tokio::spawn(async move {
            let _ = child.wait().await;
            done.send_replace(true);
        });
        let stream = Arc::new(Stream { rate, channels, pid, stdin: tokio::sync::Mutex::new(stdin), exited, superseded: watch::channel(false).0 });
        let prev = self.live().current.replace(stream.clone());
        if let Some(p) = prev {
            p.supersede();
        }
        tracing::info!("pacat started rate={rate} ch={channels} sink={}", self.sink);
        Ok(stream)
    }

    /// Drive one session: browser PCM in, control frames out. The caller opens the bridge
    /// first, because the gateway drops the browser's socket after 5 s unattached, and a sink
    /// repair can take longer.
    pub async fn run_session(&self, init: &Value, mut wire: impl Wire) {
        self.session(init, &mut wire).await;
        wire.close().await;
        tracing::info!("mic session closed");
    }

    async fn session(&self, init: &Value, wire: &mut impl Wire) {
        let (rate, channels) = match parse_init(init) {
            Ok(v) => v,
            Err(message) => return send(wire, json!({"type": "error", "message": message})).await,
        };
        let stream = match self.start_stream(rate, channels).await {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("pacat spawn failed: {e}");
                let message = format!("pacat: {}", os_error(&e, &self.pacat));
                return send(wire, json!({"type": "error", "message": message})).await;
            }
        };
        self.live().active_streams += 1;
        // `ready`, not an open socket, turns the page's badge purple: it means pacat runs.
        send(wire, json!({"type": "ready", "sampleRate": rate, "channels": channels, "sink": self.sink})).await;
        self.pump(wire, &stream).await;
        let mut live = self.live();
        live.active_streams = live.active_streams.saturating_sub(1);
        stream.kill();
        // A fast reconnect's successor may already hold the slot.
        if live.current.as_ref().is_some_and(|c| Arc::ptr_eq(c, &stream)) {
            live.current = None;
        }
    }

    async fn pump(&self, wire: &mut impl Wire, stream: &Stream) {
        let mut superseded = stream.superseded.subscribe();
        tokio::select! {
            biased;
            _ = superseded.wait_for(|s| *s) => {}
            _ = self.recv_loop(wire, stream) => return,
        }
        send(wire, json!({"type": "superseded", "message": "another device took the microphone"})).await;
    }

    async fn recv_loop(&self, wire: &mut impl Wire, stream: &Stream) {
        let first = match tokio::time::timeout(self.first_frame_timeout, wire.recv()).await {
            Ok(f) => f,
            Err(_) => {
                tracing::info!("mic session opened but no audio arrived in {:.0}s; closing", self.first_frame_timeout.as_secs_f64());
                return;
            }
        };
        let mut next = first;
        loop {
            let Some(frame) = next else { return };
            match frame {
                Frame::Binary(pcm) => {
                    if stream.exited() {
                        return send(wire, json!({"type": "error", "message": "pacat exited"})).await;
                    }
                    let mut stdin = stream.stdin.lock().await;
                    let Some(pipe) = stdin.as_mut() else {
                        tracing::info!("pacat pipe broke (superseded, or pacat died)");
                        return;
                    };
                    if pipe.write_all(&pcm).await.is_err() || pipe.flush().await.is_err() {
                        tracing::info!("pacat pipe broke (superseded, or pacat died)");
                        return;
                    }
                    drop(stdin);
                    self.live().last_pcm_ts = audio::now();
                }
                Frame::Text(text) => {
                    if let Ok(Value::Object(msg)) = serde_json::from_str::<Value>(&text) {
                        if msg.get("type").and_then(Value::as_str) == Some("ping") {
                            // Echoed so the page shows a real round-trip time.
                            send(wire, json!({"type": "pong", "t": msg.get("t").cloned().unwrap_or(Value::Null)})).await;
                        }
                    }
                }
            }
            next = wire.recv().await;
        }
    }
}

async fn send(wire: &mut impl Wire, obj: Value) {
    if let Err(e) = wire.send_text(awm_pycompat::json::dumps(&obj)).await {
        tracing::debug!("mic control send failed: {e}");
    }
}

/// An exec failure as Python words it: `[Errno 2] No such file or directory: 'pacat'`.
fn os_error(e: &std::io::Error, program: &OsString) -> String {
    let text = e.to_string();
    let text = text.split(" (os error").next().unwrap_or(&text).to_string();
    match e.raw_os_error() {
        Some(n) => format!("[Errno {n}] {text}: {}", awm_pycompat::repr::str_repr(&program.to_string_lossy())),
        None => text,
    }
}
