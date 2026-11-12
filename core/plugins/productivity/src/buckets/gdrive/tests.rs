//! The Drive bucket against a loopback stand-in of Drive v3 that keeps an in-memory tree.

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::{Arc, Mutex};

use axum::body::{Body, Bytes};
use axum::extract::{Path as AxPath, Query, State};
use axum::http::{HeaderMap, StatusCode as Sc};
use axum::response::{IntoResponse, Response as AxResponse};
use axum::routing::{get, patch, post, put};
use axum::{Form, Json, Router};
use serde_json::{json, Value};

use super::*;

const F: &str = FOLDER;

#[derive(Clone, Default)]
struct Node {
    name: String,
    mime: String,
    parent: String,
    content: Vec<u8>,
    trashed: bool,
    modified: String,
}

struct Session {
    target: Option<String>,
    name: String,
    parent: String,
    mime: String,
    total: usize,
    buf: Vec<u8>,
}

#[derive(Default)]
struct Fake {
    base: String,
    /// Insertion order, so "first match" is the order the tree was written in.
    nodes: Vec<(String, Node)>,
    seq: u32,
    issued: u32,
    valid: Option<String>,
    reject_next: bool,
    expires_in: u64,
    page: usize,
    sessions: HashMap<String, Session>,
    chunks: usize,
    exports: Vec<String>,
}

type St = Arc<Mutex<Fake>>;

impl Fake {
    fn node(&mut self, id: &str) -> Option<&mut Node> {
        self.nodes.iter_mut().find(|(i, _)| i == id).map(|(_, n)| n)
    }
    fn meta(&self, id: &str) -> Value {
        let n = &self.nodes.iter().find(|(i, _)| i == id).unwrap().1;
        let mut m = json!({"id": id, "name": n.name, "mimeType": n.mime, "webViewLink": format!("https://drive.example/{id}")});
        if n.mime != F {
            m["size"] = json!(n.content.len().to_string());
        }
        if !n.modified.is_empty() {
            m["modifiedTime"] = json!(n.modified);
        }
        m
    }
    fn add(&mut self, prefix: &str, node: Node) -> String {
        self.seq += 1;
        let id = format!("{prefix}{}", self.seq);
        self.nodes.push((id.clone(), node));
        id
    }
}

fn authed(f: &mut Fake, h: &HeaderMap) -> bool {
    if f.reject_next {
        f.reject_next = false;
        f.valid = None;
        return false;
    }
    let got = h.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("");
    matches!(&f.valid, Some(t) if got == format!("Bearer {t}"))
}

fn denied() -> AxResponse {
    (Sc::UNAUTHORIZED, Json(json!({"error": {"code": 401, "message": "Invalid Credentials"}}))).into_response()
}

fn missing(id: &str) -> AxResponse {
    (Sc::NOT_FOUND, Json(json!({"error": {"code": 404, "message": format!("File not found: {id}.")}}))).into_response()
}

async fn token(State(s): State<St>, Form(f): Form<HashMap<String, String>>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if f.get("grant_type").map(String::as_str) != Some("refresh_token") || f.get("refresh_token").map(String::as_str) != Some("r-test") {
        return (Sc::BAD_REQUEST, Json(json!({"error": "invalid_grant", "error_description": "Bad Request"}))).into_response();
    }
    st.issued += 1;
    let tok = format!("tok-{}", st.issued);
    st.valid = Some(tok.clone());
    Json(json!({"access_token": tok, "expires_in": st.expires_in, "token_type": "Bearer"})).into_response()
}

async fn list(State(s): State<St>, h: HeaderMap, Query(q): Query<HashMap<String, String>>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    let query = q.get("q").cloned().unwrap_or_default();
    let needle = query.split('\'').nth(1).unwrap_or("").to_string();
    let ids: Vec<String> = st
        .nodes
        .iter()
        .filter(|(_, n)| !n.trashed)
        .filter(|(_, n)| if query.contains("in parents") { n.parent == needle } else { n.name.contains(&needle) })
        .map(|(i, _)| i.clone())
        .collect();
    let start: usize = q.get("pageToken").and_then(|p| p.parse().ok()).unwrap_or(0);
    let size = if st.page > 0 { st.page } else { q.get("pageSize").and_then(|p| p.parse().ok()).unwrap_or(100) };
    let files: Vec<Value> = ids.iter().skip(start).take(size).map(|i| st.meta(i)).collect();
    let mut out = json!({"files": files});
    if start + size < ids.len() {
        out["nextPageToken"] = json!((start + size).to_string());
    }
    Json(out).into_response()
}

async fn file_get(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>, Query(q): Query<HashMap<String, String>>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    if id == "root" && !q.contains_key("alt") {
        return Json(json!({"id": "root"})).into_response();
    }
    let Some(n) = st.node(&id).cloned() else { return missing(&id) };
    if q.get("alt").map(String::as_str) == Some("media") {
        let chunks: Vec<std::result::Result<Vec<u8>, Infallible>> = n.content.chunks(64 * 1024).map(|c| Ok(c.to_vec())).collect();
        return Body::from_stream(futures_util::stream::iter(chunks)).into_response();
    }
    let mut m = st.meta(&id);
    m["parents"] = json!([n.parent]);
    Json(m).into_response()
}

async fn export(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>, Query(q): Query<HashMap<String, String>>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    st.exports.push(q.get("mimeType").cloned().unwrap_or_default());
    let Some(n) = st.node(&id).cloned() else { return missing(&id) };
    [b"EXPORTED:".as_slice(), &n.content].concat().into_response()
}

async fn create(State(s): State<St>, h: HeaderMap, Json(b): Json<Value>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    let node = Node {
        name: b["name"].as_str().unwrap().into(),
        mime: b["mimeType"].as_str().unwrap_or("text/plain").into(),
        parent: b["parents"][0].as_str().unwrap().into(),
        ..Default::default()
    };
    let id = st.add("new", node);
    Json(st.meta(&id)).into_response()
}

async fn update(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>, Query(q): Query<HashMap<String, String>>, Json(b): Json<Value>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    let Some(n) = st.node(&id) else { return missing(&id) };
    if b["trashed"].as_bool() == Some(true) {
        n.trashed = true;
    }
    if let Some(name) = b["name"].as_str() {
        n.name = name.into();
    }
    if let Some(add) = q.get("addParents") {
        assert_eq!(q.get("removeParents"), Some(&n.parent));
        n.parent = add.clone();
    }
    Json(st.meta(&id)).into_response()
}

async fn copy(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>, Json(b): Json<Value>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    let Some(mut n) = st.node(&id).cloned() else { return missing(&id) };
    n.name = b["name"].as_str().unwrap().into();
    n.parent = b["parents"][0].as_str().unwrap().into();
    let id = st.add("cp", n);
    Json(st.meta(&id)).into_response()
}

async fn delete(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    st.nodes.retain(|(i, _)| *i != id);
    Sc::NO_CONTENT.into_response()
}

fn start_session(st: &mut Fake, h: &HeaderMap, target: Option<String>, b: &Value) -> AxResponse {
    let hv = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    st.seq += 1;
    let sid = format!("s{}", st.seq);
    st.sessions.insert(
        sid.clone(),
        Session {
            target,
            name: b["name"].as_str().unwrap_or("").into(),
            parent: b["parents"][0].as_str().unwrap_or("").into(),
            mime: hv("x-upload-content-type"),
            total: hv("x-upload-content-length").parse().unwrap(),
            buf: vec![],
        },
    );
    (Sc::OK, [("location", format!("{}/upload/session/{sid}", st.base))]).into_response()
}

async fn upload_new(State(s): State<St>, h: HeaderMap, Query(q): Query<HashMap<String, String>>, Json(b): Json<Value>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    assert_eq!(q.get("uploadType").map(String::as_str), Some("resumable"));
    start_session(&mut st, &h, None, &b)
}

async fn upload_existing(State(s): State<St>, h: HeaderMap, AxPath(id): AxPath<String>, Json(b): Json<Value>) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    start_session(&mut st, &h, Some(id), &b)
}

async fn upload_chunk(State(s): State<St>, h: HeaderMap, AxPath(sid): AxPath<String>, body: Bytes) -> AxResponse {
    let mut st = s.lock().unwrap();
    if !authed(&mut st, &h) {
        return denied();
    }
    st.chunks += 1;
    let range = h.get("content-range").unwrap().to_str().unwrap().to_string();
    let sess = st.sessions.get_mut(&sid).unwrap();
    if range != "bytes */0" {
        let (span, _) = range.strip_prefix("bytes ").unwrap().split_once('/').unwrap();
        let start: usize = span.split_once('-').unwrap().0.parse().unwrap();
        assert_eq!(start, sess.buf.len(), "chunks arrive in order");
        sess.buf.extend_from_slice(&body);
        if sess.buf.len() < sess.total {
            let end = sess.buf.len() - 1;
            return (Sc::PERMANENT_REDIRECT, [("range", format!("bytes=0-{end}"))]).into_response();
        }
    }
    let sess = st.sessions.remove(&sid).unwrap();
    let id = match sess.target {
        Some(id) => {
            st.node(&id).unwrap().content = sess.buf;
            id
        }
        None => st.add("up", Node { name: sess.name, mime: sess.mime, parent: sess.parent, content: sess.buf, ..Default::default() }),
    };
    Json(st.meta(&id)).into_response()
}

struct Stand {
    st: St,
    urls: Urls,
}

async fn stand(nodes: Vec<(&str, Node)>) -> Stand {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let st: St = Arc::new(Mutex::new(Fake {
        base: base.clone(),
        nodes: nodes.into_iter().map(|(i, n)| (i.to_string(), n)).collect(),
        expires_in: 3600,
        ..Default::default()
    }));
    let app = Router::new()
        .route("/token", post(token))
        .route("/drive/files", get(list).post(create))
        .route("/drive/files/{id}", get(file_get).patch(update).delete(delete))
        .route("/drive/files/{id}/export", get(export))
        .route("/drive/files/{id}/copy", post(copy))
        .route("/upload/files", post(upload_new))
        .route("/upload/files/{id}", patch(upload_existing))
        .route("/upload/session/{sid}", put(upload_chunk))
        .with_state(st.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let urls = Urls {
        api: format!("{base}/drive"),
        upload: format!("{base}/upload"),
        token: format!("{base}/token"),
        auth: format!("{base}/auth"),
    };
    Stand { st, urls }
}

fn cfg(root: &str) -> BucketConfig {
    BucketConfig {
        name: "g".into(),
        kind: "google_drive".into(),
        root: root.into(),
        site: String::new(),
        client_id: "c-test".into(),
        client_secret: "s-test".into(),
        refresh_token: "r-test".into(),
    }
}

impl Stand {
    fn bucket(&self, root: &str) -> GoogleDrive {
        GoogleDrive::with_urls(cfg(root), self.urls.clone()).unwrap()
    }
    fn where_is(&self, id: &str) -> (String, String, bool) {
        let mut st = self.st.lock().unwrap();
        let n = st.node(id).unwrap();
        (n.parent.clone(), n.name.clone(), n.trashed)
    }
}

fn node(name: &str, mime: &str, parent: &str, content: &[u8]) -> Node {
    Node { name: name.into(), mime: mime.into(), parent: parent.into(), content: content.to_vec(), ..Default::default() }
}

/// root/ ├─ docs/ │  └─ note.txt  └─ pic.png
fn tree() -> Vec<(&'static str, Node)> {
    let mut note = node("note.txt", "text/plain", "d1", b"hello");
    note.modified = "2026-01-01".into();
    vec![("d1", node("docs", F, "root", b"")), ("f1", note), ("f2", node("pic.png", "image/png", "root", b"img"))]
}

fn src(dir: &Path, data: &[u8]) -> std::path::PathBuf {
    let p = dir.join("src.bin");
    std::fs::write(&p, data).unwrap();
    p
}

#[tokio::test]
async fn paths_resolve_under_the_bucket_root() {
    let mut nodes = vec![("top", node("top", F, "root", b"")), ("out", node("outside.txt", "text/plain", "root", b"x"))];
    nodes.push(("d1", node("docs", F, "top", b"")));
    nodes.push(("f1", node("note.txt", "text/plain", "d1", b"hello")));
    let s = stand(nodes).await;
    let b = s.bucket("top");
    let names: Vec<_> = b.ls("").await.unwrap().into_iter().map(|e| e.name).collect();
    assert_eq!(names, ["docs"]);
    assert_eq!(b.stat("docs/note.txt").await.unwrap().id, "f1");
    assert_eq!(b.stat("outside.txt").await.unwrap_err().error_class, "FileNotFoundError");
    let e = b.mkdir("docs/new").await.unwrap();
    assert_eq!(s.where_is(&e.id).0, "d1");
}

#[tokio::test]
async fn get_streams_a_large_file_and_sanitises_the_name() {
    let data: Vec<u8> = (0..3 * 1024 * 1024 + 17).map(|i| (i % 251) as u8).collect();
    let mut nodes = tree();
    nodes.push(("big", node("big file?.bin", "application/octet-stream", "root", &data)));
    let s = stand(nodes).await;
    let dir = tempfile::tempdir().unwrap();
    let got = s.bucket("").get("big file?.bin", dir.path()).await.unwrap();
    assert_eq!(got.filename, "big file?.bin");
    assert_eq!(got.path, dir.path().join("big_file_.bin").display().to_string());
    assert_eq!(got.size, data.len() as i64);
    assert_eq!(std::fs::read(&got.path).unwrap(), data);
}

#[tokio::test]
async fn put_is_a_chunked_resumable_upload() {
    let s = stand(tree()).await;
    let mut b = s.bucket("");
    b.chunk = 4;
    let dir = tempfile::tempdir().unwrap();
    let e = b.put("docs/fresh.txt", &src(dir.path(), b"0123456789"), Some("text/plain"), false).await.unwrap();
    assert_eq!((e.name.as_str(), e.path.as_str(), e.size, e.mime.as_str()), ("fresh.txt", "docs/fresh.txt", 10, "text/plain"));
    assert_eq!(s.st.lock().unwrap().chunks, 3);
    assert!(b.ls("docs").await.unwrap().iter().any(|x| x.name == "fresh.txt"));
    let out = tempfile::tempdir().unwrap();
    assert_eq!(std::fs::read(b.get("docs/fresh.txt", out.path()).await.unwrap().path).unwrap(), b"0123456789");
    let empty = b.put("empty.txt", &src(dir.path(), b""), None, false).await.unwrap();
    assert_eq!((empty.size, empty.mime.as_str()), (0, "application/octet-stream"));
}

#[tokio::test]
async fn token_is_cached_then_refreshed_once_on_a_401() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    b.stat("docs/note.txt").await.unwrap();
    b.ls("").await.unwrap();
    assert_eq!(s.st.lock().unwrap().issued, 1);
    s.st.lock().unwrap().reject_next = true;
    b.stat("pic.png").await.unwrap();
    assert_eq!(s.st.lock().unwrap().issued, 2);

    s.st.lock().unwrap().expires_in = 30; // inside the refresh margin: stale at once
    let b = s.bucket("");
    b.probe().await.unwrap();
    b.probe().await.unwrap();
    assert_eq!(s.st.lock().unwrap().issued, 4);
}

#[test]
fn safe_name_matches_the_python() {
    assert_eq!(safe_name("a b?.txt"), "a_b_.txt");
    assert_eq!(safe_name("dir/x.pdf"), "x.pdf");
    assert_eq!(safe_name("..."), "attachment-0");
    assert_eq!(safe_name(" _note_ "), "note");
}
