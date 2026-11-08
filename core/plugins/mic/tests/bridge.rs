//! The audio path mic hands to `pacat`, the session-open payload, and the `stream` session
//! end to end against a fake `pacat`. Ported from `test_bridge_env.py`, plus the session cases.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_mic::audio::{Audio, BoxFut, Out, Runner};
use awm_plugin_mic::bridge::{pacat_argv, pacat_env, parse_init, Frame, Mic, Wire};
use serde_json::{json, Value};
use tokio::sync::mpsc;

fn env_of(key: &str) -> Option<String> {
    pacat_env().into_iter().find(|(k, _)| k == key).map(|(_, v)| v.to_string_lossy().into_owned())
}

/// The environment cases share the process env, so they run as one test.
#[test]
fn pacat_env_carries_a_runtime_dir_and_inherits_the_rest() {
    std::env::remove_var("XDG_RUNTIME_DIR");
    assert_eq!(env_of("XDG_RUNTIME_DIR"), Some(format!("/run/user/{}", unsafe { libc::getuid() })));
    std::env::set_var("XDG_RUNTIME_DIR", "/run/user/9999");
    assert_eq!(env_of("XDG_RUNTIME_DIR").as_deref(), Some("/run/user/9999"));
    std::env::set_var("AWM_MIC_CANARY", "kept");
    assert_eq!(env_of("AWM_MIC_CANARY").as_deref(), Some("kept"));
}

#[test]
fn the_pacat_argv_is_unchanged() {
    assert_eq!(
        pacat_argv("virtmic", 48000, 1),
        ["pacat", "--playback", "-d", "virtmic", "--raw", "--format=s16le", "--rate=48000", "--channels=1", "--client-name=awm-mic", "--stream-name=browser-mic", "--latency-msec=40"]
    );
}

#[test]
fn a_browsers_native_rate_is_accepted() {
    assert_eq!(parse_init(&json!({"sampleRate": 44100, "channels": 1})), Ok((44100, 1)));
    assert_eq!(parse_init(&json!({"sampleRate": "44100", "channels": 2.0})), Ok((44100, 2)));
}

#[test]
fn the_defaults_match_the_page() {
    assert_eq!(parse_init(&json!({})), Ok((48000, 1)));
}

#[test]
fn nonsense_never_reaches_pacat() {
    for (init, msg) in [
        (json!({"sampleRate": 0}), "sampleRate 0 outside [8000, 192000]"),
        (json!({"sampleRate": 1_000_000_000}), "sampleRate 1000000000 outside [8000, 192000]"),
        (json!({"sampleRate": "many"}), "sampleRate and channels must be integers"),
        (json!({"sampleRate": null}), "sampleRate and channels must be integers"),
        (json!({"channels": 7}), "channels 7 must be 1 or 2"),
        (json!({"format": "f32le"}), "unsupported format 'f32le'; mic accepts s16le only"),
        (json!({"format": null}), "unsupported format None; mic accepts s16le only"),
    ] {
        assert_eq!(parse_init(&init), Err(msg.to_string()), "{init}");
    }
}

// -- the session ---------------------------------------------------------------

/// A PulseAudio that answers every command, so a pass never fails.
struct Quiet;

impl Runner for Quiet {
    fn run<'a>(&'a self, _cmd: &'a [String]) -> BoxFut<'a, Out> {
        Box::pin(async { Out::default() })
    }
}

/// The browser's side: frames in, control text out.
struct TestWire {
    rx: mpsc::UnboundedReceiver<Frame>,
    tx: mpsc::UnboundedSender<String>,
}

impl Wire for TestWire {
    async fn recv(&mut self) -> Option<Frame> {
        self.rx.recv().await
    }

    async fn send_text(&mut self, text: String) -> Result<(), String> {
        self.tx.send(text).map_err(|e| e.to_string())
    }

    async fn close(self) {}
}

struct Phone {
    to: Option<mpsc::UnboundedSender<Frame>>,
    from: mpsc::UnboundedReceiver<String>,
    done: tokio::task::JoinHandle<()>,
}

impl Phone {
    fn open(mic: &Arc<Mic>, init: Value) -> Self {
        let (to, rx) = mpsc::unbounded_channel();
        let (tx, from) = mpsc::unbounded_channel();
        let m = mic.clone();
        let done = tokio::spawn(async move { m.run_session(&init, TestWire { rx, tx }).await });
        Phone { to: Some(to), from, done }
    }

    fn send(&self, f: Frame) {
        self.to.as_ref().unwrap().send(f).unwrap();
    }

    async fn next(&mut self) -> Option<String> {
        tokio::time::timeout(Duration::from_secs(10), self.from.recv()).await.expect("a control frame within 10 s")
    }

    async fn hang_up(mut self) {
        self.to.take();
        tokio::time::timeout(Duration::from_secs(10), self.done).await.unwrap().unwrap();
    }
}

struct Rig {
    _home: tempfile::TempDir,
    dir: tempfile::TempDir,
    mic: Arc<Mic>,
}

/// A mic whose `pacat` is `script`, run with the fake's dir as `$D`.
fn rig(script: &str, first_frame: Duration) -> Rig {
    let (home, dir) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
    let pacat = dir.path().join("pacat");
    std::fs::write(&pacat, format!("#!/bin/sh\nD='{}'\n{script}\n", dir.path().display())).unwrap();
    std::fs::set_permissions(&pacat, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let audio = Arc::new(Audio::new(Arc::new(Quiet), home.path().into(), home.path().into()));
    let mut mic = Mic::new(audio, "virtmic");
    mic.pacat = pacat.into();
    mic.first_frame_timeout = first_frame;
    Rig { _home: home, dir, mic: Arc::new(mic) }
}

/// Records its argv and its stdin per process, ignores SIGTERM, and exits on EOF.
const RECORDER: &str = "trap '' TERM\nprintf '%s\\n' \"$@\" > \"$D/args.$$\"\nexec cat > \"$D/pcm.$$\"";

fn recorded(dir: &Path, prefix: &str) -> Vec<(PathBuf, Vec<u8>)> {
    let mut out: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with(prefix))
        .map(|e| (e.path(), std::fs::read(e.path()).unwrap()))
        .collect();
    out.sort();
    out
}

async fn eventually(what: &str, mut ok: impl FnMut() -> bool) {
    for _ in 0..200 {
        if ok() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("{what} never happened");
}

#[tokio::test]
async fn audio_reaches_pacat_and_pings_are_answered() {
    let r = rig(RECORDER, Duration::from_secs(30));
    let mut phone = Phone::open(&r.mic, json!({"sampleRate": 44100, "channels": 1}));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "ready", "sampleRate": 44100, "channels": 1, "sink": "virtmic"}"#);
    phone.send(Frame::Binary(b"abcd".to_vec()));
    phone.send(Frame::Text(r#"{"type":"ping","t":5}"#.into()));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "pong", "t": 5}"#);
    phone.send(Frame::Text("not json".into()));
    phone.send(Frame::Text(r#"{"type":"ping"}"#.into()));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "pong", "t": null}"#);
    let st = r.mic.status();
    assert_eq!((st["active_streams"].clone(), st["stream_rate"].clone(), st["stream_channels"].clone()), (json!(1), json!(44100), json!(1)));
    assert!(st["last_pcm_ts"].as_f64().unwrap() > 0.0);
    let stream = r.mic.current().unwrap();
    // The fake ignores SIGTERM only once its trap is set.
    eventually("the fake pacat reads", || recorded(r.dir.path(), "pcm.").iter().any(|(_, b)| b == b"abcd")).await;
    phone.hang_up().await;
    stream.wait_exit().await;
    assert_eq!(recorded(r.dir.path(), "pcm.").iter().map(|(_, b)| b.clone()).collect::<Vec<_>>(), [b"abcd".to_vec()]);
    let args = String::from_utf8(recorded(r.dir.path(), "args.")[0].1.clone()).unwrap();
    assert_eq!(args.lines().collect::<Vec<_>>(), pacat_argv("virtmic", 44100, 1)[1..]);
    assert!(r.mic.current().is_none());
    assert_eq!(r.mic.status()["active_streams"], 0);
}

#[tokio::test]
async fn a_new_phone_supersedes_the_old_one_and_tells_it() {
    let r = rig(RECORDER, Duration::from_secs(30));
    let mut a = Phone::open(&r.mic, json!({}));
    assert!(a.next().await.unwrap().contains(r#""type": "ready""#));
    let first = r.mic.current().unwrap();
    let mut b = Phone::open(&r.mic, json!({}));
    assert!(b.next().await.unwrap().contains(r#""type": "ready""#));
    assert_eq!(a.next().await.unwrap(), r#"{"type": "superseded", "message": "another device took the microphone"}"#);
    assert_eq!(a.next().await, None, "the superseded session ends");
    first.wait_exit().await;
    b.send(Frame::Binary(b"B".to_vec()));
    b.send(Frame::Text(r#"{"type":"ping","t":1}"#.into()));
    b.next().await.unwrap();
    eventually("the second pacat reads", || recorded(r.dir.path(), "pcm.").iter().any(|(_, b)| b == b"B")).await;
    assert_eq!(r.mic.status()["active_streams"], 1);
    let second = r.mic.current().unwrap();
    assert!(!Arc::ptr_eq(&first, &second), "the successor holds the slot");
    b.hang_up().await;
    second.wait_exit().await;
    let pcm: Vec<Vec<u8>> = recorded(r.dir.path(), "pcm.").into_iter().map(|(_, b)| b).collect();
    assert!(pcm.contains(&b"B".to_vec()) && pcm.contains(&vec![]), "{pcm:?}");
    a.done.await.unwrap();
}

#[tokio::test]
async fn a_session_with_no_audio_ends_at_the_deadline() {
    let r = rig(RECORDER, Duration::from_millis(200));
    let mut phone = Phone::open(&r.mic, json!({}));
    assert!(phone.next().await.unwrap().contains("ready"));
    let stream = r.mic.current().unwrap();
    assert_eq!(phone.next().await, None, "the session closes with no frame sent");
    stream.wait_exit().await;
    assert!(r.mic.current().is_none());
}

#[tokio::test]
async fn audio_after_pacat_exits_is_refused() {
    let r = rig("exit 3", Duration::from_secs(30));
    let mut phone = Phone::open(&r.mic, json!({}));
    assert!(phone.next().await.unwrap().contains("ready"));
    r.mic.current().unwrap().wait_exit().await;
    phone.send(Frame::Binary(vec![0, 0]));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "error", "message": "pacat exited"}"#);
    assert_eq!(phone.next().await, None);
}

#[tokio::test]
async fn a_bad_format_is_refused_before_pacat_starts() {
    let r = rig(RECORDER, Duration::from_secs(30));
    let mut phone = Phone::open(&r.mic, json!({"format": "f32le"}));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "error", "message": "unsupported format 'f32le'; mic accepts s16le only"}"#);
    assert_eq!(phone.next().await, None);
    assert!(recorded(r.dir.path(), "args.").is_empty());
}

#[tokio::test]
async fn a_missing_pacat_is_reported() {
    let mut r = rig(RECORDER, Duration::from_secs(30));
    Arc::get_mut(&mut r.mic).unwrap().pacat = "/nonexistent/pacat".into();
    let mut phone = Phone::open(&r.mic, json!({}));
    assert_eq!(phone.next().await.unwrap(), r#"{"type": "error", "message": "pacat: [Errno 2] No such file or directory: '/nonexistent/pacat'"}"#);
    eventually("the session ends", || r.mic.current().is_none()).await;
}
