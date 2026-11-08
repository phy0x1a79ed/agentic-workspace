//! Keeps `/effort` and `/model` from changing the user's saved defaults in Claude Code's
//! `settings.json`: snapshot the keys they write before the command, put back what it changed.
//!
//! Both commands set the running session, then save the same value as the default for every new
//! session: `/effort` under `modelSettings.<model>.effortLevel`, `/model` as the top-level `model`.
//! Putting back is safe for the running session, which prefers its own state over the file. Only
//! the keys the commands write are touched. Anything else, including edits made while the verb was
//! waiting, is left as it is, and the file keeps its own formatting.

use std::future::Future;
use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;
use serde_json::{Map, Value};
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

/// Held by every cch reader and writer of `settings.json` while it reads and writes, so the
/// settings guard and the hook reconcile never interleave on the file.
pub static SETTINGS_LOCK: Mutex<()> = Mutex::const_new(());

/// The top-level keys the commands write.
pub const TOP_KEYS: [&str; 2] = ["effortLevel", "model"];
/// How long to look for the CLI's write after the command went in.
pub const WRITE_WAIT: Duration = Duration::from_millis(4000);
/// How often to look for it.
pub const POLL: Duration = Duration::from_millis(250);
/// The beat after the write lands, so a write in two steps is seen whole.
pub const SETTLE: Duration = Duration::from_millis(400);
/// A write can land after the first restore, so the file is checked again after this.
pub const RECHECK: Duration = Duration::from_millis(1000);
pub const MAX_PASSES: usize = 3;

const MODEL_SETTINGS: &str = "modelSettings";
const EFFORT_LEVEL: &str = "effortLevel";

/// The clock and the sleep `restore` waits with, so tests can play the CLI's write in between.
pub trait Pace {
    /// Time since an arbitrary fixed start.
    fn elapsed(&self) -> Duration;
    fn sleep(&self, d: Duration) -> impl Future<Output = ()> + Send;
}

/// [`Pace`] on the tokio clock.
pub struct TokioPace {
    start: tokio::time::Instant,
}

impl Default for TokioPace {
    fn default() -> Self {
        Self { start: tokio::time::Instant::now() }
    }
}

impl Pace for TokioPace {
    fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }

    fn sleep(&self, d: Duration) -> impl Future<Output = ()> + Send {
        tokio::time::sleep(d)
    }
}

/// The keys `/effort` and `/model` write, as they were, and the file's (mtime ns, size) then.
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub path: PathBuf,
    pub keys: Map<String, Value>,
    pub stamp: (i128, u64),
}

/// What `restore` did: the keys it put back, sorted, whether the file was written at all while
/// it waited, and the error that stopped it, if one did.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Restored {
    pub restored: Vec<String>,
    pub changed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

async fn stamp(path: &Path) -> (i128, u64) {
    match tokio::fs::metadata(path).await {
        Ok(m) => (i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec()), m.size()),
        Err(_) => (0, 0),
    }
}

async fn read(path: &Path) -> Option<(String, Map<String, Value>)> {
    let text = tokio::fs::read_to_string(path).await.ok()?;
    match serde_json::from_str(&text).ok()? {
        Value::Object(doc) => Some((text, doc)),
        _ => None,
    }
}

fn keys_of(doc: &Map<String, Value>) -> Map<String, Value> {
    TOP_KEYS
        .iter()
        .chain([&MODEL_SETTINGS])
        .filter_map(|&k| doc.get(k).map(|v| (k.to_string(), v.clone())))
        .collect()
}

/// The saved defaults as they stand now in the file at `path` (`ClaudeHome::settings()`), or
/// `None` when the file is absent or not a JSON object.
pub async fn snapshot(path: &Path) -> Option<Snapshot> {
    let _held = SETTINGS_LOCK.lock().await;
    let Some((_, doc)) = read(path).await else {
        tracing::warn!(
            "reflection: {} is missing or not plain JSON, so the defaults in it cannot be guarded",
            path.display()
        );
        return None;
    };
    Some(Snapshot { path: path.to_path_buf(), keys: keys_of(&doc), stamp: stamp(path).await })
}

fn model_efforts(models: Option<&Value>) -> Map<String, Value> {
    let Some(Value::Object(models)) = models else { return Map::new() };
    models
        .iter()
        .filter_map(|(m, v)| v.as_object()?.get(EFFORT_LEVEL).map(|e| (m.clone(), e.clone())))
        .collect()
}

fn falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

/// The object under `key` in `map`, made an empty one first when it is absent or not an object.
fn object_at<'a>(map: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    let slot = map.entry(key).or_insert(Value::Null);
    if !slot.is_object() {
        *slot = Value::Object(Map::new());
    }
    slot.as_object_mut().expect("made an object above")
}

/// Puts the guarded keys of `doc` back as the snapshot had them. Names what moved.
fn revert(snap: &Snapshot, doc: &mut Map<String, Value>) -> Vec<String> {
    let mut moved = Vec::new();
    for key in TOP_KEYS {
        let before = snap.keys.get(key);
        if before != doc.get(key) {
            match before {
                Some(v) => {
                    doc.insert(key.to_string(), v.clone());
                }
                None => {
                    doc.shift_remove(key);
                }
            }
            moved.push(key.to_string());
        }
    }
    let snap_models = snap.keys.get(MODEL_SETTINGS);
    let was = model_efforts(snap_models);
    let now = model_efforts(doc.get(MODEL_SETTINGS));
    let mut changed: Vec<String> = was.keys().chain(now.keys().filter(|m| !was.contains_key(*m))).cloned().collect();
    changed.retain(|m| was.get(m) != now.get(m));
    for model in changed {
        let models = object_at(doc, MODEL_SETTINGS);
        let entry = object_at(models, &model);
        match was.get(&model) {
            Some(level) => {
                entry.insert(EFFORT_LEVEL.into(), level.clone());
            }
            None => {
                entry.shift_remove(EFFORT_LEVEL);
                let snap_has = snap_models.and_then(Value::as_object).is_some_and(|m| m.contains_key(&model));
                if entry.is_empty() && !snap_has {
                    models.shift_remove(&model);
                }
            }
        }
        moved.push(format!("{MODEL_SETTINGS}.{model}.{EFFORT_LEVEL}"));
    }
    if doc.get(MODEL_SETTINGS).is_some_and(falsy) && !snap.keys.contains_key(MODEL_SETTINGS) {
        doc.shift_remove(MODEL_SETTINGS);
    }
    moved
}

/// Python's `str.splitlines` boundaries; a `\r\n` pair yields an extra empty line, which the
/// indent scan skips anyway.
const LINE_BREAKS: [char; 10] =
    ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];

/// The file's own indent unit: a tab, or as many spaces as the first indented line has.
fn indent(text: &str) -> String {
    for line in text.split(LINE_BREAKS).skip(1) {
        let stripped = line.trim_start_matches([' ', '\t']);
        if !stripped.is_empty() && stripped.len() != line.len() {
            if line.starts_with('\t') {
                return "\t".into();
            }
            return " ".repeat(line.len() - stripped.len());
        }
    }
    "  ".into()
}

/// `doc` as Python's `json.dumps(doc, indent=..., ensure_ascii=False)` writes it.
fn dumps(doc: &Map<String, Value>, indent: &str) -> String {
    let mut out = Vec::new();
    let fmt = serde_json::ser::PrettyFormatter::with_indent(indent.as_bytes());
    let mut ser = serde_json::Serializer::with_formatter(&mut out, fmt);
    doc.serialize(&mut ser).expect("a JSON map always serializes");
    String::from_utf8(out).expect("serde_json writes UTF-8")
}

/// Writes `doc` over `path` in `text`'s formatting, through a temp file renamed into place,
/// keeping the file's mode.
async fn write(path: &Path, text: &str, doc: &Map<String, Value>) -> io::Result<()> {
    let mut out = dumps(doc, &indent(text));
    if text.ends_with('\n') {
        out.push('\n');
    }
    let dir = path.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".settings-guard-{}", uuid::Uuid::new_v4().simple()));
    let result = async {
        let mut fh = tokio::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp).await?;
        fh.write_all(out.as_bytes()).await?;
        fh.flush().await?;
        drop(fh);
        let mode = tokio::fs::metadata(path).await?.permissions().mode() & 0o7777;
        tokio::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode)).await?;
        tokio::fs::rename(&tmp, path).await
    }
    .await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(&tmp).await;
    }
    result
}

async fn restore_once(snap: &Snapshot) -> io::Result<Vec<String>> {
    let _held = SETTINGS_LOCK.lock().await;
    let Some((text, mut doc)) = read(&snap.path).await else { return Ok(Vec::new()) };
    let moved = revert(snap, &mut doc);
    if !moved.is_empty() {
        write(&snap.path, &text, &doc).await?;
    }
    Ok(moved)
}

async fn keys_differ(snap: &Snapshot) -> bool {
    let _held = SETTINGS_LOCK.lock().await;
    match read(&snap.path).await {
        Some((_, mut doc)) => !revert(snap, &mut doc).is_empty(),
        None => false,
    }
}

/// Waits up to `wait` (normally [`WRITE_WAIT`]) for the CLI's write, then puts the guarded keys
/// back. Never fails: an error is logged and returned in [`Restored::error`].
pub async fn restore(snap: Option<&Snapshot>, wait: Duration, pace: &impl Pace) -> Restored {
    let mut out = Restored::default();
    let Some(snap) = snap else { return out };
    if let Err(e) = restore_into(snap, wait, pace, &mut out).await {
        tracing::warn!("reflection: could not guard the defaults in {}: {e}", snap.path.display());
        out.error = Some(e.to_string());
    }
    out
}

async fn restore_into(snap: &Snapshot, wait: Duration, pace: &impl Pace, out: &mut Restored) -> io::Result<()> {
    let deadline = pace.elapsed() + wait;
    while stamp(&snap.path).await == snap.stamp {
        if pace.elapsed() >= deadline {
            return Ok(());
        }
        pace.sleep(POLL).await;
    }
    out.changed = true;
    pace.sleep(SETTLE).await;
    for _ in 0..MAX_PASSES {
        let moved = restore_once(snap).await?;
        out.restored.extend(moved);
        out.restored.sort();
        out.restored.dedup();
        pace.sleep(RECHECK).await;
        if !keys_differ(snap).await {
            break;
        }
    }
    if !out.restored.is_empty() {
        tracing::info!("reflection: put back {} in {}", out.restored.join(", "), snap.path.display());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex as StdMutex;

    /// Python's `json.dumps(BASE, indent=2) + "\n"`, byte for byte.
    const BASE_TEXT: &str = r#"{
  "theme": "dark",
  "model": "opus",
  "effortLevel": "medium",
  "modelSettings": {
    "claude-opus-5-5": {
      "effortLevel": "high",
      "autoCompactWindow": 200000
    },
    "claude-sonnet-5": {
      "effortLevel": "medium"
    }
  },
  "hooks": {
    "Stop": []
  }
}
"#;

    fn base() -> Map<String, Value> {
        serde_json::from_str(BASE_TEXT).unwrap()
    }

    type Hook = Box<dyn Fn(Duration) + Send + Sync>;

    /// A clock that moves only when slept on, with an optional hook that runs on each sleep.
    #[derive(Default)]
    struct Clock {
        now: StdMutex<Duration>,
        on_sleep: Option<Hook>,
    }

    impl Clock {
        fn now(&self) -> Duration {
            *self.now.lock().unwrap()
        }
    }

    impl Pace for Clock {
        fn elapsed(&self) -> Duration {
            self.now()
        }

        fn sleep(&self, d: Duration) -> impl Future<Output = ()> + Send {
            *self.now.lock().unwrap() += d;
            if let Some(hook) = &self.on_sleep {
                hook(d);
            }
            std::future::ready(())
        }
    }

    struct Fixture {
        _dir: tempfile::TempDir,
        path: PathBuf,
    }

    fn fixture_with(text: Option<&str>) -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        if let Some(text) = text {
            std::fs::write(&path, text).unwrap();
        }
        Fixture { _dir: dir, path }
    }

    fn file() -> Fixture {
        fixture_with(Some(BASE_TEXT))
    }

    fn text_of(path: &Path) -> String {
        std::fs::read_to_string(path).unwrap()
    }

    fn doc_of(path: &Path) -> Map<String, Value> {
        serde_json::from_str(&text_of(path)).unwrap()
    }

    /// What the CLI does on save: rewrite the file with `edit` applied.
    fn cli_write_indent(path: &Path, edit: impl FnOnce(&mut Map<String, Value>), indent: &str) {
        let mut doc = doc_of(path);
        edit(&mut doc);
        std::fs::write(path, dumps(&doc, indent) + "\n").unwrap();
    }

    fn cli_write(path: &Path, edit: impl FnOnce(&mut Map<String, Value>)) {
        cli_write_indent(path, edit, "  ");
    }

    async fn run(path: &Path, edit: impl FnOnce(&mut Map<String, Value>)) -> Restored {
        let snap = snapshot(path).await;
        cli_write(path, edit);
        restore(snap.as_ref(), WRITE_WAIT, &Clock::default()).await
    }

    fn set(doc: &mut Map<String, Value>, path: &[&str], value: Value) {
        let (key, parents) = path.split_last().unwrap();
        let mut map = doc;
        for p in parents {
            map = map.get_mut(*p).and_then(Value::as_object_mut).unwrap();
        }
        map.insert(key.to_string(), value);
    }

    #[test]
    fn dumps_matches_python_json_dumps() {
        assert_eq!(dumps(&base(), "  ") + "\n", BASE_TEXT);
    }

    #[tokio::test]
    async fn test_a_changed_model_effort_is_put_back_and_nothing_else_moves() {
        let f = file();
        let before = text_of(&f.path);
        let out = run(&f.path, |d| set(d, &["modelSettings", "claude-opus-5-5", "effortLevel"], json!("low"))).await;
        assert_eq!(out.restored, ["modelSettings.claude-opus-5-5.effortLevel"]);
        assert_eq!(text_of(&f.path), before);
    }

    #[tokio::test]
    async fn test_keys_the_cli_added_are_removed_again() {
        let f = file();
        let before = text_of(&f.path);
        run(&f.path, |d| set(d, &["modelSettings", "claude-fable-5"], json!({"effortLevel": "low"}))).await;
        assert_eq!(text_of(&f.path), before);
    }

    #[tokio::test]
    async fn test_a_key_the_cli_deleted_comes_back() {
        let f = file();
        run(&f.path, |d| {
            d.shift_remove("effortLevel");
        })
        .await;
        assert_eq!(doc_of(&f.path)["effortLevel"], "medium");
    }

    #[tokio::test]
    async fn test_edits_to_other_keys_made_meanwhile_are_merged_not_overwritten() {
        let f = file();
        run(&f.path, |d| {
            set(d, &["modelSettings", "claude-opus-5-5", "effortLevel"], json!("low"));
            d.insert("theme".into(), json!("light"));
            set(d, &["modelSettings", "claude-opus-5-5", "autoCompactWindow"], json!(300000));
            d.insert("added".into(), json!(true));
        })
        .await;
        let doc = doc_of(&f.path);
        assert!(doc["theme"] == "light" && doc["added"] == true);
        assert_eq!(
            doc["modelSettings"]["claude-opus-5-5"],
            json!({"effortLevel": "high", "autoCompactWindow": 300000})
        );
    }

    #[tokio::test]
    async fn test_the_file_mode_is_kept() {
        let f = file();
        std::fs::set_permissions(&f.path, std::fs::Permissions::from_mode(0o600)).unwrap();
        run(&f.path, |d| {
            d.insert("effortLevel".into(), json!("low"));
        }).await;
        assert_eq!(std::fs::metadata(&f.path).unwrap().permissions().mode() & 0o777, 0o600);
    }

    #[tokio::test]
    async fn test_a_missing_or_unreadable_file_is_not_guarded_and_not_touched() {
        for content in [None, Some("{not json"), Some("[1, 2]")] {
            let f = fixture_with(content);
            assert_eq!(snapshot(&f.path).await, None, "{content:?}");
            assert_eq!(restore(None, WRITE_WAIT, &Clock::default()).await, Restored::default());
            assert_eq!(std::fs::read_to_string(&f.path).ok().as_deref(), content);
        }
    }

}
