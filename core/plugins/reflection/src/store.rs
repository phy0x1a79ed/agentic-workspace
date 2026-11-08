//! reflection's files in `<AWM_DIR>/services/reflection`, in the formats the Python wrote: JSON
//! read with a default and written whole through `<stem>.tmp` and a rename, JSON lines
//! appended, and one file per open ticket.
//!
//! One async mutex guards each file. A caller holds it across one read-modify-write and never
//! across a gateway call, because scopes calls back into reflection during a claim.

use std::io;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use tokio::io::AsyncWriteExt;
use tokio::sync::{Mutex, MutexGuard};

/// One of reflection's state files.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum File {
    /// `{<R9 id>: {level, origin, at_ms, job?, hinted?}}`.
    Trust,
    /// `[{session_id, until, retired, tool, job, name, since_ms, project, scope}]`.
    Waits,
    /// `{<R9 id>: at_ms}`, the newest kept.
    Retired,
    /// One line per `harness_event`: the open ticket merged with the event.
    Events,
    /// One line per lost follow-up.
    Lost,
    /// The `tickets/` folder: `<ticket>.json` per open ticket, `<ticket>.closed` per early event.
    Tickets,
}

impl File {
    const ALL: [File; 6] = [File::Trust, File::Waits, File::Retired, File::Events, File::Lost, File::Tickets];

    pub fn name(self) -> &'static str {
        match self {
            File::Trust => "trust.json",
            File::Waits => "waits.json",
            File::Retired => "retired.json",
            File::Events => "events.jsonl",
            File::Lost => "lost.jsonl",
            File::Tickets => "tickets",
        }
    }

    fn index(self) -> usize {
        File::ALL.iter().position(|f| *f == self).unwrap_or(0)
    }
}

/// The state dir and its locks.
pub struct Store {
    dir: PathBuf,
    locks: [Mutex<()>; 6],
}

/// `json.dumps(data, indent=1)`, as the Python wrote the whole-file stores.
pub fn pretty(v: &Value) -> String {
    let mut out = Vec::new();
    let fmt = serde_json::ser::PrettyFormatter::with_indent(b" ");
    let mut ser = serde_json::Serializer::with_formatter(&mut out, fmt);
    serde::Serialize::serialize(v, &mut ser).expect("a JSON value serializes");
    String::from_utf8(out).expect("serde_json writes UTF-8")
}

impl Store {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into(), locks: Default::default() }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn path(&self, f: File) -> PathBuf {
        self.dir.join(f.name())
    }

    /// The file's lock. Hold it across one read-modify-write, never across a gateway call.
    pub async fn lock(&self, f: File) -> MutexGuard<'_, ()> {
        self.locks[f.index()].lock().await
    }

    async fn read_value(&self, f: File) -> Option<Value> {
        let text = tokio::fs::read_to_string(self.path(f)).await.ok()?;
        serde_json::from_str(&text).ok()
    }

    /// A whole-file object store, or `{}` when it is missing, unreadable or not an object.
    pub async fn read_object(&self, f: File) -> Map<String, Value> {
        match self.read_value(f).await {
            Some(Value::Object(m)) => m,
            _ => Map::new(),
        }
    }

    /// A whole-file list store, or `[]` when it is missing, unreadable or not a list.
    pub async fn read_array(&self, f: File) -> Vec<Value> {
        match self.read_value(f).await {
            Some(Value::Array(a)) => a,
            _ => vec![],
        }
    }

    /// Replace a whole-file store through `<stem>.tmp` and a rename.
    pub async fn write(&self, f: File, data: &Value) -> io::Result<()> {
        tokio::fs::create_dir_all(&self.dir).await?;
        let path = self.path(f);
        let tmp = path.with_extension("tmp");
        tokio::fs::write(&tmp, pretty(data)).await?;
        tokio::fs::rename(&tmp, &path).await
    }

    /// Append one JSON line to a lines store.
    pub async fn append(&self, f: File, line: &Value) -> io::Result<()> {
        tokio::fs::create_dir_all(&self.dir).await?;
        let mut file = tokio::fs::OpenOptions::new().create(true).append(true).open(self.path(f)).await?;
        file.write_all(format!("{line}\n").as_bytes()).await?;
        file.flush().await
    }

    /// A lines store's text, or empty when it is missing.
    pub async fn read_text(&self, f: File) -> String {
        tokio::fs::read_to_string(self.path(f)).await.unwrap_or_default()
    }

    pub fn tickets(&self) -> PathBuf {
        self.path(File::Tickets)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn pretty_matches_pythons_indent_of_one() {
        let v = json!({"a": {"level": "trusted", "at_ms": 1}, "b": []});
        assert_eq!(pretty(&v), "{\n \"a\": {\n  \"level\": \"trusted\",\n  \"at_ms\": 1\n },\n \"b\": []\n}");
    }

    #[tokio::test]
    async fn a_store_reads_its_default_and_writes_in_place() {
        let tmp = tempfile::tempdir().unwrap();
        let s = Store::new(tmp.path().join("state"));
        assert!(s.read_object(File::Trust).await.is_empty() && s.read_array(File::Waits).await.is_empty());
        s.write(File::Waits, &json!({"not": "a list"})).await.unwrap();
        assert!(s.read_array(File::Waits).await.is_empty(), "the wrong type reads as the default");
        s.write(File::Waits, &json!([{"session_id": "cc1"}])).await.unwrap();
        assert_eq!(s.read_array(File::Waits).await, vec![json!({"session_id": "cc1"})]);
        assert!(!tmp.path().join("state/waits.tmp").exists());
        s.append(File::Events, &json!({"kind": "done"})).await.unwrap();
        s.append(File::Events, &json!({"kind": "lost"})).await.unwrap();
        assert_eq!(s.read_text(File::Events).await.lines().count(), 2);
    }
}
