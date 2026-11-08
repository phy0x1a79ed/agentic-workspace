//! The operator daemon's events, kept three ways: in a bounded ring that `drain` and the
//! channel's follow read by cursor, in one log file per session, and in a mark on disk.
//!
//! A cursor belongs to one daemon: the daemon stamps its stream with the epoch it minted at
//! start, and a new epoch resets this side. The daemon outlives the plugin, so a restarted
//! plugin asks it for its whole ring again under the same epoch. The mark keeps how far the
//! session logs got. It is saved once a second, so a crash can re-append that second's events,
//! which keep their `seq`.
//!
//! A slot is not an identity: the relay hands a number out again once its session ends. Every
//! mint gets a session id `<slot>-<seq>-<epoch & 0xFFFF as 4 hex>`, and each event is stamped
//! with the id of the mint it belongs to. `claims` holds an invite's floor seq until its mint
//! shows on the stream, since the reply and the event race each other.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

pub const MAX_EVENTS: usize = 4096;
pub const MAX_BYTES: usize = 8 * 1024 * 1024;
/// The daemon sends its own idle line well inside this, so silence this long is a dead socket.
const IDLE_DEADLINE: Duration = Duration::from_secs(90);
/// How often the mark is saved while events flow.
const MARK_INTERVAL: Duration = Duration::from_secs(1);
const MAX_LOG_BYTES: u64 = 64 * 1024 * 1024;
const GENS_PER_SLOT: usize = 8;

fn int(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// Whether `event` is one a reader limited to `sessions` may see: facts of no slot always are.
pub fn in_sessions(event: &Value, sessions: Option<&HashSet<String>>) -> bool {
    match (sessions, int(event, "slot")) {
        (None, _) | (_, None) => true,
        (Some(s), Some(_)) => event.get("session").and_then(Value::as_str).is_some_and(|id| s.contains(id)),
    }
}

/// What `Journal::since` filters on.
#[derive(Default)]
pub struct Filter<'a> {
    pub limit: Option<usize>,
    pub slot: Option<i64>,
    pub task: Option<i64>,
    pub types: &'a [&'a str],
    pub sessions: Option<&'a HashSet<String>>,
}

#[derive(Default)]
struct Ring {
    events: Vec<Value>,
    weights: Vec<usize>,
    bytes: usize,
    first_kept: i64,
    next: i64,
    evicted: u64,
    epoch: Option<i64>,
    connected: bool,
    since: Option<Instant>,
}

/// A bounded, cursor-addressed backlog of what the daemon reported.
pub struct Journal {
    ring: Mutex<Ring>,
    woken: tokio::sync::watch::Sender<u64>,
    max_events: usize,
    max_bytes: usize,
}

impl Default for Journal {
    fn default() -> Self {
        Self::new(MAX_EVENTS, MAX_BYTES)
    }
}

impl Journal {
    pub fn new(max_events: usize, max_bytes: usize) -> Self {
        Self { ring: Default::default(), woken: tokio::sync::watch::channel(0).0, max_events, max_bytes }
    }

    fn ring(&self) -> std::sync::MutexGuard<'_, Ring> {
        self.ring.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn append(&self, event: Value) {
        let Some(seq) = int(&event, "seq") else { return };
        {
            let mut r = self.ring();
            let weight = event.get("data").and_then(Value::as_str).map_or(0, str::len) + 256;
            r.events.push(event);
            r.weights.push(weight);
            r.bytes += weight;
            r.next = r.next.max(seq + 1);
            while !r.events.is_empty() && (r.events.len() > self.max_events || r.bytes > self.max_bytes) {
                let dropped = r.events.remove(0);
                let w = r.weights.remove(0);
                r.bytes -= w;
                r.first_kept = int(&dropped, "seq").unwrap_or(0) + 1;
                r.evicted += 1;
            }
            if let Some(first) = r.events.first().and_then(|e| int(e, "seq")) {
                r.first_kept = r.first_kept.max(first);
            }
        }
        self.woken.send_modify(|n| *n += 1);
    }

    /// Start again, because this is a different daemon than last time.
    pub fn reset(&self, epoch: i64) {
        let mut r = self.ring();
        if r.epoch.is_some_and(|e| e != epoch) {
            tracing::info!("tether: the daemon restarted; the event cursor starts again");
        }
        r.epoch = Some(epoch);
        r.events.clear();
        r.weights.clear();
        (r.bytes, r.first_kept, r.next) = (0, 0, 0);
    }

    pub fn connected(&self, yes: bool) {
        let mut r = self.ring();
        r.connected = yes;
        r.since = yes.then(Instant::now);
    }

    /// Events at or after `cursor`, where to ask next, and any gap. A caller that asked from
    /// before the ring's start is told so, never left with a hole that reads as quiet.
    pub fn since(&self, cursor: Option<i64>, f: &Filter) -> (Vec<Value>, i64, Option<Value>) {
        let r = self.ring();
        let mut start = cursor.unwrap_or(r.first_kept);
        let mut gap = None;
        if start < r.first_kept {
            gap = Some(json!({
                "type": "gap", "seq": r.first_kept, "from": start, "to": r.first_kept, "lost": r.first_kept - start,
                "note": "the buffer moved past this cursor; these events are gone",
            }));
            start = r.first_kept;
        }
        let limit = f.limit.unwrap_or(500);
        let mut out = vec![];
        let mut nxt = if r.events.is_empty() { start.max(r.next) } else { start };
        for event in &r.events {
            let seq = int(event, "seq").unwrap_or(0);
            if seq < start {
                continue;
            }
            if out.len() >= limit {
                break;
            }
            nxt = seq + 1;
            if f.slot.is_some() && int(event, "slot") != f.slot {
                continue;
            }
            if !in_sessions(event, f.sessions) {
                continue;
            }
            if f.task.is_some() && int(event, "task") != f.task {
                continue;
            }
            let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
            if !f.types.is_empty() && !f.types.iter().any(|t| kind.starts_with(t)) {
                continue;
            }
            out.push(event.clone());
        }
        if out.is_empty() {
            nxt = r.next.max(start);
        }
        (out, nxt, gap)
    }

    /// Block until something lands, the task `until` has exited, or `timeout` runs out.
    pub async fn wait(&self, timeout: Duration, until: Option<i64>, sessions: Option<&HashSet<String>>) {
        let deadline = tokio::time::Instant::now() + timeout;
        let mut rx = self.woken.subscribe();
        loop {
            if tokio::time::timeout_at(deadline, rx.changed()).await.is_err() {
                return;
            }
            let Some(task) = until else { return };
            let exited = self.ring().events.iter().rev().any(|e| {
                e.get("type").and_then(Value::as_str) == Some("task.exited") && int(e, "task") == Some(task) && in_sessions(e, sessions)
            });
            if exited {
                return;
            }
        }
    }

    pub fn epoch(&self) -> Option<i64> {
        self.ring().epoch
    }

    pub fn next(&self) -> i64 {
        self.ring().next
    }

    pub fn is_connected(&self) -> bool {
        self.ring().connected
    }

    pub fn head(&self) -> Value {
        let r = self.ring();
        json!({
            "epoch": r.epoch, "next": r.next, "first_kept": r.first_kept, "evicted": r.evicted,
            "held": r.events.len(), "connected": r.connected, "since_s": r.since.map(|s| s.elapsed().as_secs()),
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Gen {
    pub sid: String,
    pub from: i64,
}

/// How far this host recorded the stream, kept across restarts of the plugin in `stream.json`.
/// Everything belongs to one epoch except `declared`, which is kept by slot.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Mark {
    #[serde(default)]
    pub epoch: Option<i64>,
    #[serde(default)]
    pub next: i64,
    #[serde(default)]
    pub open: BTreeMap<i64, String>,
    #[serde(default)]
    pub cursors: BTreeMap<String, i64>,
    #[serde(default)]
    pub declared: BTreeMap<i64, Value>,
    #[serde(default)]
    pub gens: BTreeMap<i64, Vec<Gen>>,
    #[serde(default)]
    pub claims: BTreeMap<i64, i64>,
    #[serde(skip)]
    path: PathBuf,
}

impl Mark {
    /// The mark saved at `path`, or an empty one.
    pub fn load(path: &Path) -> Self {
        let mut mark: Mark = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        mark.path = path.to_path_buf();
        mark
    }

    /// Whether this event was recorded before a restart.
    pub fn seen(&self, epoch: i64, seq: i64) -> bool {
        self.epoch == Some(epoch) && seq < self.next
    }

    /// Adopt the daemon's epoch. A new one makes every number here meaningless.
    pub fn begin(&mut self, epoch: i64) {
        if self.epoch == Some(epoch) {
            return;
        }
        self.epoch = Some(epoch);
        self.next = 0;
        self.open.clear();
        self.cursors.clear();
        self.gens.clear();
        self.claims.clear();
        self.save();
    }

    /// Set `event["session"]` to the id of the mint its slot belongs to. Idempotent, because a
    /// restarted plugin is handed the daemon's ring again. A slot first seen with no mint starts
    /// one at its first event, so its id is fixed once chosen.
    pub fn stamp(&mut self, epoch: i64, event: &mut Value) -> Option<String> {
        let (slot, seq) = (int(event, "slot")?, int(event, "seq")?);
        let starts = event.get("type").and_then(Value::as_str) == Some("session.minted");
        let gens = self.gens.entry(slot).or_default();
        let fresh = starts || gens.is_empty() || seq < gens[0].from;
        if fresh && gens.iter().all(|g| g.from != seq) {
            gens.push(Gen { sid: format!("{slot}-{seq}-{:04x}", epoch & 0xFFFF), from: seq });
            gens.sort_by_key(|g| g.from);
            let excess = gens.len().saturating_sub(GENS_PER_SLOT);
            gens.drain(..excess);
            self.settle(slot);
            self.save();
        }
        let found = self.gens.get(&slot)?.iter().rfind(|g| g.from <= seq).map(|g| g.sid.clone());
        if let Some(sid) = &found {
            event["session"] = json!(sid);
        }
        found
    }

    /// The id of the slot's latest mint, if this host has seen it.
    pub fn current(&self, slot: i64) -> Option<String> {
        self.gens.get(&slot).and_then(|g| g.last()).map(|g| g.sid.clone())
    }

    /// Wait for the mint on `slot` at or after seq `floor`: its id when it is on the stream
    /// already, else None while the claim waits.
    pub fn claim(&mut self, slot: i64, floor: i64) -> Option<String> {
        self.claims.insert(slot, floor);
        let sid = self.settle(slot);
        self.save();
        sid
    }

    pub fn claimed(&self, slot: i64) -> bool {
        self.claims.contains_key(&slot)
    }

    fn settle(&mut self, slot: i64) -> Option<String> {
        let floor = *self.claims.get(&slot)?;
        let sid = self.gens.get(&slot)?.iter().rev().find(|g| g.from >= floor).map(|g| g.sid.clone())?;
        self.claims.remove(&slot);
        Some(sid)
    }

    pub fn save(&self) {
        if self.path.as_os_str().is_empty() {
            return;
        }
        let write = || -> std::io::Result<()> {
            let dir = self.path.parent().unwrap_or(Path::new("."));
            std::fs::create_dir_all(dir)?;
            let mut tmp = tempfile::Builder::new().prefix(".stream-").tempfile_in(dir)?;
            tmp.write_all(serde_json::to_string(self).unwrap_or_default().as_bytes())?;
            tmp.persist(&self.path).map_err(|e| e.error)?;
            Ok(())
        };
        if let Err(e) = write() {
            tracing::warn!("tether: could not save the stream mark to {}: {e}", self.path.display());
        }
    }
}

/// One file per session, opened on the first event that names its slot. Both people agreed to
/// it when the owner answered the prompt.
#[derive(Default)]
pub struct SessionLogs {
    dir: PathBuf,
    open: HashMap<i64, (File, PathBuf)>,
    written: HashMap<i64, u64>,
}

impl SessionLogs {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir, ..Self::default() }
    }

    pub fn write(&mut self, mark: &mut Mark, event: &Value) {
        let Some(slot) = int(event, "slot") else { return };
        if !self.open.contains_key(&slot) && !self.start(mark, slot) {
            return;
        }
        if self.written.get(&slot).copied().unwrap_or(0) >= MAX_LOG_BYTES {
            return;
        }
        let line = event.to_string() + "\n";
        let ok = self.open.get_mut(&slot).is_some_and(|(f, _)| f.write_all(line.as_bytes()).and_then(|_| f.flush()).is_ok());
        if !ok {
            tracing::warn!("tether: could not write the log for slot {slot}");
            self.close_slot(mark, slot);
            return;
        }
        *self.written.entry(slot).or_default() += line.len() as u64;
        if event.get("type").and_then(Value::as_str) == Some("session.phase") && event.get("phase").and_then(Value::as_str) == Some("ended") {
            self.close_slot(mark, slot);
        }
    }

    fn start(&mut self, mark: &mut Mark, slot: i64) -> bool {
        if std::fs::create_dir_all(&self.dir).and_then(|_| std::fs::set_permissions(&self.dir, std::fs::Permissions::from_mode(0o700))).is_err() {
            tracing::warn!("tether: no session log directory at {}", self.dir.display());
            return false;
        }
        let known = mark.open.get(&slot).map(PathBuf::from).filter(|p| p.parent() == Some(self.dir.as_path()) && p.is_file());
        let path = known.unwrap_or_else(|| {
            let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%SZ");
            let mut path = self.dir.join(format!("{stamp}-slot{slot}.jsonl"));
            let mut n = 1;
            while path.exists() {
                n += 1;
                path = self.dir.join(format!("{stamp}-slot{slot}-{n}.jsonl"));
            }
            path
        });
        let Ok(file) = std::fs::OpenOptions::new().create(true).append(true).open(&path) else {
            tracing::warn!("tether: could not open {}", path.display());
            return false;
        };
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        self.written.insert(slot, file.metadata().map(|m| m.len()).unwrap_or(0));
        mark.open.insert(slot, path.display().to_string());
        tracing::info!("tether: recording slot {slot} in {}", path.display());
        self.open.insert(slot, (file, path));
        true
    }

    fn close_slot(&mut self, mark: &mut Mark, slot: i64) {
        self.open.remove(&slot);
        self.written.remove(&slot);
        mark.open.remove(&slot);
    }

    /// Close every file without marking its session finished.
    pub fn close(&mut self) {
        self.open.clear();
        self.written.clear();
    }
}

/// The ring, the mark and the logs one watch connection feeds.
pub struct Stream {
    pub journal: Journal,
    pub book: Mutex<(Mark, SessionLogs)>,
    saved: Mutex<Option<Instant>>,
}

impl Stream {
    pub fn new(mark: Mark, logs: SessionLogs) -> Arc<Self> {
        Arc::new(Self { journal: Journal::default(), book: Mutex::new((mark, logs)), saved: Mutex::new(None) })
    }

    pub fn book(&self) -> std::sync::MutexGuard<'_, (Mark, SessionLogs)> {
        self.book.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Take one event from the wire: stamp it, ring it, and log it unless it was logged before.
    pub fn take(&self, epoch: Option<i64>, event: Value) {
        self.take_at(Instant::now(), epoch, event);
    }

    /// `take` at a given moment. The mark is saved by the event that crosses `MARK_INTERVAL`
    /// and by `flush`, so a crash replays at most that interval's events into the logs.
    fn take_at(&self, now: Instant, epoch: Option<i64>, mut event: Value) {
        if event.get("type").and_then(Value::as_str) == Some("watch.idle") {
            return;
        }
        let mut book = self.book();
        let (mark, logs) = &mut *book;
        if let Some(e) = epoch {
            mark.stamp(e, &mut event);
        }
        // Recorded before anything reacts, so a reader that asks finds it there.
        self.journal.append(event.clone());
        let seq = int(&event, "seq");
        if let (Some(e), Some(s)) = (epoch, seq) {
            if mark.seen(e, s) {
                return;
            }
        }
        logs.write(mark, &event);
        let Some(s) = seq else { return };
        mark.next = s + 1;
        let mut saved = self.saved.lock().unwrap_or_else(|p| p.into_inner());
        if saved.is_some_and(|t| now.duration_since(t) < MARK_INTERVAL) {
            return;
        }
        *saved = Some(now);
        let snapshot = mark.clone();
        drop(saved);
        drop(book);
        snapshot.save();
    }

    /// Save the mark now, off the book lock.
    pub fn flush(&self) {
        let snapshot = self.book().0.clone();
        *self.saved.lock().unwrap_or_else(|p| p.into_inner()) = Some(Instant::now());
        snapshot.save();
    }

    /// The opening line of a watch: adopt its epoch.
    pub fn begin(&self, epoch: i64) {
        if self.journal.epoch() != Some(epoch) {
            self.journal.reset(epoch);
        }
        let mut book = self.book();
        let (mark, logs) = &mut *book;
        if mark.epoch.is_some_and(|e| e != epoch) {
            logs.close();
        }
        mark.begin(epoch);
    }

    /// One connection's worth of events. Returns when the socket closes or goes quiet.
    pub async fn watch_once(&self, socket: &Path) -> std::io::Result<()> {
        let mut stream = tokio::net::UnixStream::connect(socket).await?;
        self.journal.connected(true);
        let out = async {
            let request = json!({"verb": "watch", "args": {"since": self.journal.next()}}).to_string() + "\n";
            stream.write_all(request.as_bytes()).await?;
            let mut lines = BufReader::new(stream).lines();
            let quiet = || std::io::Error::new(std::io::ErrorKind::TimedOut, "the event stream went quiet");
            let Some(opening) = tokio::time::timeout(IDLE_DEADLINE, lines.next_line()).await.map_err(|_| quiet())?? else { return Ok(()) };
            let epoch = serde_json::from_str::<Value>(&opening).ok().and_then(|h| int(&h, "epoch"));
            if let Some(e) = epoch {
                self.begin(e);
            }
            loop {
                let Some(raw) = tokio::time::timeout(IDLE_DEADLINE, lines.next_line()).await.map_err(|_| quiet())?? else { return Ok(()) };
                if let Ok(event) = serde_json::from_str::<Value>(&raw) {
                    self.take(epoch, event);
                }
            }
        }
        .await;
        self.journal.connected(false);
        self.flush();
        out
    }

    /// Hold the watch open for the life of the plugin. Losing it is ordinary: the daemon can be
    /// replaced, and the backoff keeps a daemon that will not start from being asked constantly.
    pub async fn watch_forever(self: Arc<Self>, socket: PathBuf) {
        let mut delay = awm_plugin_sdk::Backoff::doubling(1.0, 10.0);
        loop {
            match self.watch_once(&socket).await {
                Ok(()) => delay.reset(),
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => tracing::info!("tether: the event stream went quiet; reconnecting"),
                Err(_) => {}
            }
            tokio::time::sleep(delay.wait()).await;
        }
    }

    pub fn shutdown(&self) {
        self.book().1.close();
        self.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(seq: i64, slot: i64, kind: &str) -> Value {
        json!({"seq": seq, "slot": slot, "type": kind})
    }

    #[test]
    fn a_cursor_before_the_ring_is_told_it_lost_events() {
        let j = Journal::new(3, MAX_BYTES);
        for s in 0..5 {
            j.append(ev(s, 1, "task.output"));
        }
        let (events, next, gap) = j.since(Some(0), &Filter::default());
        assert_eq!(events.len(), 3);
        assert_eq!(next, 5);
        assert_eq!(gap.unwrap()["lost"], 2);
        let (events, next, gap) = j.since(Some(5), &Filter::default());
        assert!(events.is_empty() && gap.is_none());
        assert_eq!(next, 5, "a cursor at the end stays there");
    }

    #[test]
    fn the_mark_reads_the_python_file_in_place() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("stream.json");
        std::fs::write(&path, r#"{"epoch":7,"next":12,"open":{"3":"/x/a.jsonl"},"cursors":{"j":4},"declared":{"3":{"os":"unix","word":"acre"}},"gens":{"3":[{"sid":"3-5-0007","from":5}]},"claims":{}}"#).unwrap();
        let m = Mark::load(&path);
        assert_eq!((m.epoch, m.next, m.current(3).as_deref()), (Some(7), 12, Some("3-5-0007")));
        assert_eq!(m.declared[&3]["os"], "unix");
        m.save();
        let back: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(back["gens"]["3"][0]["sid"], "3-5-0007");
        assert_eq!(back["declared"]["3"]["word"], "acre");
    }

    #[test]
    fn a_restarted_plugin_refills_without_writing_twice_and_a_new_epoch_resets() {
        let tmp = tempfile::tempdir().unwrap();
        let st = tmp.path().join("stream.json");
        let s = Stream::new(Mark::load(&st), SessionLogs::new(tmp.path().join("sessions")));
        s.begin(9);
        for e in [ev(0, 1, "session.minted"), ev(1, 1, "owner.said")] {
            s.take(Some(9), e);
        }
        s.shutdown();
        let again = Stream::new(Mark::load(&st), SessionLogs::new(tmp.path().join("sessions")));
        again.begin(9);
        for e in [ev(0, 1, "session.minted"), ev(1, 1, "owner.said"), ev(2, 1, "owner.said")] {
            again.take(Some(9), e);
        }
        let files: Vec<_> = std::fs::read_dir(tmp.path().join("sessions")).unwrap().collect();
        assert_eq!(files.len(), 1, "the session goes on in its file");
        let text = std::fs::read_to_string(files[0].as_ref().unwrap().path()).unwrap();
        assert_eq!(text.lines().count(), 3, "each event once");
        assert_eq!(again.journal.since(Some(0), &Filter::default()).0.len(), 3, "the ring refills whole");
        again.begin(10);
        assert_eq!(again.journal.next(), 0);
        assert!(again.book().0.gens.is_empty());
        let mode = std::fs::metadata(tmp.path().join("sessions")).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700);
    }

    #[test]
    fn the_mark_is_saved_once_an_interval_and_on_close() {
        let tmp = tempfile::tempdir().unwrap();
        let st = tmp.path().join("stream.json");
        let s = Stream::new(Mark::load(&st), SessionLogs::new(tmp.path().join("sessions")));
        s.begin(9);
        let next = || Mark::load(&st).next;
        let t0 = Instant::now();
        for i in 0..50 {
            s.take_at(t0 + Duration::from_millis(i * 10), Some(9), ev(i as i64, 1, "owner.said"));
        }
        assert_eq!(next(), 1, "fifty events inside one interval wrote once");
        s.take_at(t0 + MARK_INTERVAL, Some(9), ev(50, 1, "owner.said"));
        assert_eq!(next(), 51, "the event that crosses the interval saves");
        s.take_at(t0 + MARK_INTERVAL + Duration::from_millis(5), Some(9), ev(51, 1, "owner.said"));
        assert_eq!(next(), 51);
        s.shutdown();
        assert_eq!(next(), 52, "close writes the final mark");
    }

}
