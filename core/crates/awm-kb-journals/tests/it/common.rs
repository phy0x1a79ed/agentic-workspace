//! A journal in a temp state dir, a fake gateway, the private marks and Python-era databases.

use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use awm_kb_journals::channel::NewPost;
use awm_kb_journals::posts::Post;
use awm_kb_journals::{Fail, Journals, Opts, Outside, Who};
use awm_plugin_sdk::privacy;
use futures_util::future::BoxFuture;
use serde_json::{json, Value};

/// What the journal sent outside itself, and what the stand-in answers.
#[derive(Default)]
pub struct Fake {
    pub peers: Mutex<Vec<String>>,
    pub sent: Mutex<Vec<(String, Vec<Value>)>>,
    pub started: Mutex<Vec<Value>>,
    pub lists: Mutex<std::collections::HashMap<String, Value>>,
}

impl Outside for Fake {
    fn resolve_scope<'a>(&'a self, _: &'a str, _: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async { Ok(json!({"exists": true})) })
    }
    fn peers(&self) -> Vec<String> {
        self.peers.lock().unwrap().clone()
    }
    fn tag_list<'a>(&'a self, peer: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async move { self.lists.lock().unwrap().get(peer).cloned().ok_or_else(|| format!("{peer} is down")) })
    }
    fn tag_receive(&self, peer: &str, tags: Vec<Value>) {
        self.sent.lock().unwrap().push((peer.into(), tags));
    }
    fn start_agent<'a>(&'a self, args: Value) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async move {
            self.started.lock().unwrap().push(args);
            Ok(json!({"job": "job-1"}))
        })
    }
}

pub struct Rig {
    pub dir: tempfile::TempDir,
    pub j: Arc<Journals>,
    pub out: Arc<Fake>,
}

/// One process-wide AWM_DIR, so the SDK reads one marks file. Set before any journal opens.
fn state() -> &'static Path {
    static STATE: OnceLock<tempfile::TempDir> = OnceLock::new();
    STATE
        .get_or_init(|| {
            let d = tempfile::tempdir().unwrap();
            std::env::set_var("AWM_DIR", d.path());
            d
        })
        .path()
}

pub async fn open_at(dir: &Path, node: &str) -> Result<(Arc<Journals>, Arc<Fake>), Fail> {
    state();
    let j = Journals::open(dir, None, Opts { node: node.into(), other_dbs: vec![] }).await?;
    let out = Arc::new(Fake::default());
    j.set_outside(out.clone());
    Ok((j, out))
}

pub async fn rig() -> Rig {
    rig_on("home").await
}

/// A journal with an embedder loaded, so its posts have chunks to search.
pub async fn rig_with_model() -> Rig {
    state();
    let dir = tempfile::tempdir().unwrap();
    let j = Journals::open(dir.path(), Some(Arc::new(awm_embed::Stub::default())), Opts { node: "home".into(), other_dbs: vec![] }).await.expect("open");
    let out = Arc::new(Fake::default());
    j.set_outside(out.clone());
    Rig { dir, j, out }
}

pub async fn rig_on(node: &str) -> Rig {
    from_dir(tempfile::tempdir().unwrap(), node).await
}

pub async fn from_dir(dir: tempfile::TempDir, node: &str) -> Rig {
    let (j, out) = open_at(dir.path(), node).await.expect("open");
    Rig { dir, j, out }
}

pub fn peer(node: &str) -> Who {
    Who::trusted().with_as(Some(&format!("peer:{node}")))
}

impl Rig {
    pub async fn call(&self, verb: &str, args: Value) -> Result<Value, Fail> {
        self.j.call_verb(verb, &args, &Who::trusted()).await
    }

    pub async fn call_as(&self, verb: &str, args: Value, who: &Who) -> Result<Value, Fail> {
        self.j.call_verb(verb, &args, who).await
    }

    /// Close the journal and open the same state dir again, as a restart does.
    pub async fn reopen(self) -> Rig {
        let node = self.j.node.clone();
        let Rig { dir, j, out } = self;
        drop((j, out));
        from_dir(dir, &node).await
    }

    pub async fn post_at(&self, project: &str, scope: &str, author: &str, body: &str, tags: Value, ts: Option<i64>) -> Post {
        self.j
            .post(NewPost {
                project: project.into(),
                scope: scope.into(),
                author: author.into(),
                body: body.into(),
                tags: Some(tags),
                ts,
                ..NewPost::default()
            })
            .await
            .expect("post")
    }

    pub async fn post(&self, project: &str, scope: &str, author: &str, body: &str, tags: Value) -> Post {
        self.post_at(project, scope, author, body, tags, None).await
    }

    pub fn sql(&self) -> awm_store::rusqlite::Connection {
        awm_store::rusqlite::Connection::open(Journals::db_path(self.dir.path())).unwrap()
    }

    /// `(project, scope)` of every post a fetch returns for `args`.
    pub async fn owners(&self, args: Value, who: &Who) -> Vec<(String, String)> {
        let got = self.j.call_verb("fetch", &args, who).await.expect("fetch");
        let mut owners: Vec<(String, String)> =
            got["posts"].as_array().unwrap().iter().map(|p| (str_of(&p["project"]), str_of(&p["scope"]))).collect();
        owners.sort();
        owners.dedup();
        owners
    }
}

pub fn str_of(v: &Value) -> String {
    v.as_str().unwrap().to_string()
}

pub fn keys(v: &Value) -> Vec<&str> {
    let mut k: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
    k.sort();
    k
}

pub fn names_of(tags: &Value) -> Vec<String> {
    let mut n: Vec<String> = tags["tags"].as_array().unwrap().iter().map(|t| str_of(&t["name"])).collect();
    n.sort();
    n
}

pub fn remote(id: &str, name: &str, created_ms: i64, version: i64, origin: &str) -> Value {
    json!({"id": id, "name": name, "description": format!("{name} from {origin}"), "origin": origin,
           "version": version, "created_ms": created_ms})
}

// ---- private marks -------------------------------------------------------------------

static MARKS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
static WRITES: AtomicUsize = AtomicUsize::new(0);

/// Own the process's marks file and write `marks` to it. Hold the guard for the whole test: a
/// peer read or a tag send reads the file. The padding changes the file's size on every write,
/// so the SDK's mtime-and-size cache never serves a stale read.
pub async fn marks(marks: &[(&str, Option<&str>)]) -> tokio::sync::MutexGuard<'static, ()> {
    let guard = MARKS.lock().await;
    state();
    let pad = "x".repeat(WRITES.fetch_add(1, Ordering::SeqCst) + 1);
    let list: Vec<Value> =
        marks.iter().map(|(p, s)| json!({"project": p, "scope": s, "path": format!("/marks/{pad}/{p}")})).collect();
    privacy::write_marks(&list).unwrap();
    guard
}

/// A marks file that does not parse: every test answers "hidden".
pub fn break_marks() {
    let pad = "x".repeat(WRITES.fetch_add(1, Ordering::SeqCst) + 1);
    std::fs::write(privacy::marks_path(), format!("{{not json {pad}")).unwrap();
}

// ---- Python-era databases ------------------------------------------------------------

/// `dao.SCHEMA_SQL` at v6, as the Python wrote it, without the search-feed triggers.
const PY_V6: &str = "
CREATE TABLE scope_posts (id TEXT PRIMARY KEY, owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
  author TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL,
  reply_to TEXT, thread_id TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE scope_cursors (owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL, through_ts INTEGER NOT NULL,
  through_id TEXT NOT NULL, acked_at INTEGER NOT NULL, acked_by TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner_project, owner_scope));
CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE tag_versions (tag_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
  edited_at INTEGER NOT NULL, edited_by TEXT NOT NULL DEFAULT '', PRIMARY KEY (tag_id, version));
CREATE TABLE post_tags (post_id TEXT NOT NULL, tag_id TEXT NOT NULL, PRIMARY KEY (post_id, tag_id));
CREATE TABLE tag_clashes (name TEXT NOT NULL, older_id TEXT NOT NULL, newer_id TEXT NOT NULL, older_node TEXT NOT NULL DEFAULT '',
  newer_node TEXT NOT NULL DEFAULT '', detected_at INTEGER NOT NULL, dispatched_at INTEGER, dispatch TEXT NOT NULL DEFAULT '',
  resolved_at INTEGER, PRIMARY KEY (older_id, newer_id));
CREATE TABLE session_reads (session TEXT NOT NULL, post_id TEXT NOT NULL, first_read INTEGER NOT NULL, last_read INTEGER NOT NULL,
  times INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (session, post_id));
CREATE TABLE post_cites (post_id TEXT NOT NULL, cited_id TEXT NOT NULL, ord INTEGER NOT NULL, PRIMARY KEY (post_id, cited_id));
CREATE TABLE session_parents (session TEXT PRIMARY KEY, parent TEXT NOT NULL, how TEXT NOT NULL, node TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
CREATE TABLE post_versions (post_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, diff TEXT NOT NULL DEFAULT '',
  editor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL, PRIMARY KEY (post_id, version));
CREATE TABLE search_feed (root_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE search_feed_epoch (epoch TEXT NOT NULL);
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (6);
";

pub const P1: &str = "00000000-0000-0000-0000-000000000001";
pub const P2: &str = "00000000-0000-0000-0000-000000000002";
pub const P3: &str = "00000000-0000-0000-0000-000000000003";
pub const P4: &str = "00000000-0000-0000-0000-000000000004";

/// Four posts of awm/dev as the Python left them: a tagged journal, a message, its comment, a
/// journal citing the first; and the scope's cursor at the message.
fn py_v6_rows() -> String {
    format!(
        "INSERT INTO tags VALUES ('{a}', 'alpha', 'a custom tag', 'altair', 1, 5, 5);
         INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, ts) VALUES
           ('{P1}', 'awm', 'dev', 'agent:awm/dev', 'first debrief', 1000),
           ('{P2}', 'awm', 'dev', 'user:tony', 'ping', 2000);
         INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, ts, reply_to, thread_id) VALUES
           ('{P3}', 'awm', 'dev', 'agent:awm/dev', 'pong', 3000, '{P2}', '{P2}');
         INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, ts) VALUES
           ('{P4}', 'awm', 'dev', 'agent:awm/dev', 'second debrief', 4000);
         INSERT INTO post_tags VALUES ('{P1}', 'builtin:journal'), ('{P1}', '{a}'), ('{P2}', 'builtin:message'),
           ('{P3}', 'builtin:message'), ('{P4}', 'builtin:journal');
         INSERT INTO post_cites VALUES ('{P4}', '{P1}', 0);
         INSERT INTO scope_cursors VALUES ('awm', 'dev', 2000, '{P2}', 2500, 'tony');",
        a = "a".repeat(32)
    )
}

/// A v4 database: the post kind is still a column.
const PY_V4: &str = "
CREATE TABLE scope_posts (id TEXT PRIMARY KEY, owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL, author TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL,
  version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE scope_cursors (owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL, through_ts INTEGER NOT NULL,
  through_id TEXT NOT NULL, acked_at INTEGER NOT NULL, acked_by TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner_project, owner_scope));
CREATE TABLE session_reads (session TEXT NOT NULL, post_id TEXT NOT NULL, first_read INTEGER NOT NULL, last_read INTEGER NOT NULL,
  times INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (session, post_id));
CREATE TABLE post_versions (post_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, diff TEXT NOT NULL DEFAULT '',
  editor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL, PRIMARY KEY (post_id, version));
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (4);
INSERT INTO scope_posts (id, owner_project, owner_scope, author, kind, body, ts) VALUES
  ('k1', 'awm', 'dev', 'agent:awm/dev', 'journal', 'a journal', 1000),
  ('k2', 'awm', 'dev', 'agent:awm/dev', 'debrief', 'a debrief', 2000),
  ('k3', 'awm', 'dev', 'user:tony', 'note', 'a note', 3000),
  ('k4', 'awm', 'dev', 'user:tony', 'weird', 'an odd kind', 4000),
  ('k5', 'awm', 'dev', 'user:tony', '', 'no kind', 5000);
";

pub enum Python {
    V6,
    V4,
}

/// A state dir whose journal.db the Python left behind.
pub fn python_dir(kind: Python) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let db = Journals::db_path(dir.path());
    std::fs::create_dir_all(db.parent().unwrap()).unwrap();
    let c = awm_store::rusqlite::Connection::open(&db).unwrap();
    match kind {
        Python::V6 => c.execute_batch(&format!("{PY_V6}{}", py_v6_rows())).unwrap(),
        Python::V4 => c.execute_batch(PY_V4).unwrap(),
    }
    dir
}
