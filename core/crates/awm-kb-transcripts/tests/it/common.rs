//! A whole fake `~/.claude/projects` tree: the defects transcripts exists to fix are all about
//! which paths a walk reaches, so the tests use real files.
#![allow(dead_code)]

use std::collections::HashSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use awm_kb_transcripts::layout::Tree;
use awm_kb_transcripts::{App, BoxFut, Live};
use awm_store::Store;
use serde_json::{json, Value};

pub struct Fixed(pub Result<HashSet<String>, String>);

impl Live for Fixed {
    fn session_ids(&self) -> BoxFut<'_, Result<HashSet<String>, String>> {
        let out = self.0.clone();
        Box::pin(async move { out })
    }
}

pub struct Rig {
    pub dir: tempfile::TempDir,
    pub app: App,
}

impl Rig {
    pub fn projects(&self) -> PathBuf {
        self.app.tree.projects.clone()
    }
    pub fn archive(&self) -> PathBuf {
        self.app.tree.archive.clone()
    }
}

pub fn rig_with(live: Result<HashSet<String>, String>) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let tree = Tree { projects: dir.path().join("projects"), archive: dir.path().join("archive") };
    std::fs::create_dir_all(&tree.projects).unwrap();
    std::fs::create_dir_all(&tree.archive).unwrap();
    let store = Store::open(dir.path().join("transcripts.db")).unwrap();
    awm_kb_transcripts::runs::open(&store).unwrap();
    let lance = dir.path().join("lance");
    Rig { dir, app: App::new(tree, store, Box::new(Fixed(live)), lance) }
}

impl Rig {
    /// Open the index, as kb's start step does.
    pub async fn start(&self) {
        awm_kb_transcripts::start(&self.app).await.unwrap();
    }

    /// Level the index with the archive, reading on a plain thread.
    pub async fn reconcile(&self) -> Value {
        self.app.reconcile(|f| {
            std::thread::spawn(f);
        })
        .await
        .unwrap()
    }

    /// Open the index and level it.
    pub async fn indexed(&self) -> Value {
        self.start().await;
        self.reconcile().await
    }
}

/// A user message line carrying `text`, as Claude Code writes one.
pub fn msg(text: &str) -> String {
    json!({"type": "user", "message": {"role": "user", "content": text}}).to_string()
}

/// Lines of user messages, newline-terminated.
pub fn msgs(texts: &[&str]) -> String {
    texts.iter().map(|t| msg(t) + "\n").collect()
}

pub fn rig() -> Rig {
    rig_with(Ok(HashSet::new()))
}

pub fn age(path: &Path, days: f64) {
    let t = SystemTime::now() - Duration::from_secs_f64(days * 86400.0);
    std::fs::File::options().write(true).open(path).unwrap().set_times(std::fs::FileTimes::new().set_accessed(t).set_modified(t)).unwrap();
}

pub fn write(path: &Path, body: &str, days: f64) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, body).unwrap();
    age(path, days);
}

pub fn write_gz(path: &Path, body: &str, days: f64) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut enc = flate2::write::GzEncoder::new(std::fs::File::create(path).unwrap(), flate2::Compression::default());
    enc.write_all(body.as_bytes()).unwrap();
    enc.finish().unwrap();
    age(path, days);
}

pub fn gunzip(path: &Path) -> String {
    let mut s = String::new();
    std::io::Read::read_to_string(&mut flate2::read::MultiGzDecoder::new(std::fs::File::open(path).unwrap()), &mut s).unwrap();
    s
}

/// A transcript and a sidecar with tool output and a subagent, all `age_days` old.
pub fn make_session(projects: &Path, project: &str, sid: &str, age_days: f64, sidecar: bool, transcript: bool) -> PathBuf {
    let p = projects.join(project);
    std::fs::create_dir_all(&p).unwrap();
    if transcript {
        write(&p.join(format!("{sid}.jsonl")), "{\"type\":\"user\"}\n", age_days);
    }
    if sidecar {
        write(&p.join(sid).join("tool-results/out.txt"), "tool output", age_days);
        write(&p.join(sid).join("subagents/a.jsonl"), "{\"type\":\"assistant\"}\n", age_days);
    }
    p
}
