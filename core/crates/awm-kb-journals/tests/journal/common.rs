//! A journal in a temp state dir, the stub embedder, and a stand-in for the gateway.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use awm_embed::Stub;
use awm_kb_journals::channel::NewPost;
use awm_kb_journals::posts::Post;
use awm_kb_journals::{Fail, Journals, Opts, Outside, Who};
use futures_util::future::BoxFuture;
use serde_json::{json, Value};

/// What the journal sent outside itself, and what the stand-in answers.
#[derive(Default)]
pub struct Fake {
    pub peers: Mutex<Vec<String>>,
    pub sent: Mutex<Vec<(String, Vec<Value>)>>,
    pub started: Mutex<Vec<Value>>,
    pub lists: Mutex<HashMap<String, Value>>,
    /// None: every scope exists. Some(Err): the scopes service is down.
    pub resolve: Mutex<Option<Result<Value, String>>>,
    pub resolve_calls: Mutex<Vec<(String, String)>>,
}

impl Outside for Fake {
    fn resolve_scope<'a>(&'a self, project: &'a str, scope: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async move {
            let answer = self.resolve.lock().unwrap().clone();
            match answer {
                None => Ok(json!({"exists": true})),
                Some(a) => {
                    self.resolve_calls.lock().unwrap().push((project.into(), scope.into()));
                    a
                }
            }
        })
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
    pub stub: Arc<Stub>,
}

pub fn opts(node: &str) -> Opts {
    Opts { node: node.into(), other_dbs: vec![] }
}

pub async fn open_at(dir: &std::path::Path, node: &str) -> (Arc<Journals>, Arc<Fake>, Arc<Stub>) {
    let stub = Arc::new(Stub::default());
    let j = Journals::open(dir, Some(stub.clone()), opts(node)).await.expect("open");
    let out = Arc::new(Fake::default());
    j.set_outside(out.clone());
    (j, out, stub)
}

pub async fn rig() -> Rig {
    rig_on("home").await
}

pub async fn rig_on(node: &str) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let (j, out, stub) = open_at(dir.path(), node).await;
    Rig { dir, j, out, stub }
}

pub fn who(trust: &str, scope: &str) -> Who {
    Who { trust: Some(trust.into()), scope: Some(scope.into()).filter(|s: &String| !s.is_empty()), ..Who::default() }
}

pub fn trusted_as(as_: &str) -> Who {
    Who::trusted().with_as(Some(as_))
}

pub fn with_session(session: &str) -> Who {
    Who { session: Some(session.into()), ..Who::trusted() }
}

impl Rig {
    pub async fn call(&self, verb: &str, args: Value) -> Result<Value, Fail> {
        self.j.call_verb(verb, &args, &Who::trusted()).await
    }

    pub async fn call_as(&self, verb: &str, args: Value, who: &Who) -> Result<Value, Fail> {
        self.j.call_verb(verb, &args, who).await
    }

    /// `channel.post(project, scope, author=..., body=..., tags=...)`.
    pub async fn post(&self, project: &str, scope: &str, author: &str, body: &str, tags: Value) -> Post {
        self.j
            .post(NewPost {
                project: project.into(),
                scope: scope.into(),
                author: author.into(),
                body: body.into(),
                tags: Some(tags),
                ..NewPost::default()
            })
            .await
            .expect("post")
    }

    pub async fn post_meta(&self, project: &str, scope: &str, author: &str, body: &str, tags: Value, meta: Value) -> Post {
        self.j
            .post(NewPost {
                project: project.into(),
                scope: scope.into(),
                author: author.into(),
                body: body.into(),
                tags: Some(tags),
                meta,
                ..NewPost::default()
            })
            .await
            .expect("post")
    }

    pub async fn post_at(&self, project: &str, scope: &str, author: &str, body: &str, tags: Value, ms: i64) -> Post {
        self.j
            .post(NewPost {
                project: project.into(),
                scope: scope.into(),
                author: author.into(),
                body: body.into(),
                tags: Some(tags),
                ts: Some(ms),
                ..NewPost::default()
            })
            .await
            .expect("post")
    }

    pub async fn get(&self, id: &str) -> Post {
        self.j.get_post(id).await.unwrap().expect("a post")
    }

    pub fn names(&self) -> awm_kb_journals::posts::Names {
        self.j.tag_names().unwrap()
    }

    pub fn tags_of(&self, p: &Post) -> Vec<String> {
        p.tag_names(&self.names())
    }

    pub fn dict(&self, p: &Post) -> Value {
        p.to_dict(&self.names())
    }

    pub fn sql(&self) -> awm_store::rusqlite::Connection {
        awm_store::rusqlite::Connection::open(Journals::db_path(self.dir.path())).unwrap()
    }
}

pub fn ids(posts: &Value) -> Vec<String> {
    posts.as_array().unwrap().iter().map(|p| p["id"].as_str().unwrap().to_string()).collect()
}

pub fn bodies(posts: &Value) -> Vec<String> {
    posts.as_array().unwrap().iter().map(|p| p["body"].as_str().unwrap().to_string()).collect()
}
