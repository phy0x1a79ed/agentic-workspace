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
use crate::buckets::{get_tree, put_tree, walk};

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
    if id == "root" && q.get("alt").is_none() {
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
async fn ls_root_sorts_folders_first() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let e = b.ls("").await.unwrap();
    let got: Vec<_> = e.iter().map(|e| (e.name.as_str(), e.is_dir, e.path.as_str())).collect();
    assert_eq!(got, [("docs", true, "docs"), ("pic.png", false, "pic.png")]);
    let sub = b.ls("docs").await.unwrap();
    assert_eq!((sub[0].path.as_str(), sub[0].size, sub[0].modified.as_str()), ("docs/note.txt", 5, "2026-01-01"));
    let err = b.ls("pic.png").await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("NotADirectoryError", "bucket \"g\": \"pic.png\" is not a folder"));
}

#[tokio::test]
async fn stat_resolves_nested_paths() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let e = b.stat("/docs/note.txt").await.unwrap();
    assert_eq!((e.name.as_str(), e.is_dir, e.id.as_str(), e.path.as_str()), ("note.txt", false, "f1", "docs/note.txt"));
    assert_eq!(b.stat("pic.png").await.unwrap().web_url, "https://drive.example/f2");
    let err = b.stat("docs/nope.txt").await.unwrap_err();
    assert_eq!(err.error_class, "FileNotFoundError");
    assert_eq!(err.message, "bucket \"g\": no such path \"docs/nope.txt\" (missing segment \"nope.txt\")");
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
async fn ambiguous_name_first_match_wins_and_lists_page() {
    let mut nodes = tree();
    nodes.push(("f1b", node("note.txt", "text/plain", "d1", b"different")));
    let s = stand(nodes).await;
    s.st.lock().unwrap().page = 1;
    let b = s.bucket("");
    assert_eq!(b.stat("docs/note.txt").await.unwrap().id, "f1");
    assert_eq!(b.ls("docs").await.unwrap().len(), 2);
}

#[tokio::test]
async fn get_downloads_bytes() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let got = b.get("docs/note.txt", dir.path()).await.unwrap();
    assert_eq!((got.filename.as_str(), got.mime.as_str(), got.size), ("note.txt", "text/plain", 5));
    assert_eq!(got.path, dir.path().join("note.txt").display().to_string());
    assert_eq!(std::fs::read(&got.path).unwrap(), b"hello");
    let err = b.get("docs", dir.path()).await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("IsADirectoryError", "bucket \"g\": \"docs\" is a folder"));
}

#[tokio::test]
async fn get_exports_google_native_docs() {
    let mut nodes = tree();
    nodes.push(("g1", node("plan", "application/vnd.google-apps.document", "root", b"DOC")));
    nodes.push(("g2", node("sums.CSV", "application/vnd.google-apps.spreadsheet", "root", b"1,2")));
    nodes.push(("g3", node("sketch", "application/vnd.google-apps.drawing", "root", b"PIC")));
    let s = stand(nodes).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let got = b.get("plan", dir.path()).await.unwrap();
    assert_eq!((got.filename.as_str(), got.mime.as_str()), ("plan.pdf", "application/pdf"));
    assert_eq!(std::fs::read(&got.path).unwrap(), b"EXPORTED:DOC");
    let got = b.get("sums.CSV", dir.path()).await.unwrap();
    assert_eq!((got.filename.as_str(), got.mime.as_str()), ("sums.CSV", "text/csv"));
    let got = b.get("sketch", dir.path()).await.unwrap();
    assert_eq!((got.filename.as_str(), got.mime.as_str()), ("sketch.png", "image/png"));
    assert_eq!(s.st.lock().unwrap().exports, ["application/pdf", "text/csv", "image/png"]);
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
async fn put_overwrites_an_existing_file() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let e = b.put("docs/note.txt", &src(dir.path(), b"replaced"), None, false).await.unwrap();
    assert_eq!(e.id, "f1");
    let out = tempfile::tempdir().unwrap();
    assert_eq!(std::fs::read(b.get("docs/note.txt", out.path()).await.unwrap().path).unwrap(), b"replaced");
}

#[tokio::test]
async fn put_into_a_missing_folder_needs_parents() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let f = src(dir.path(), b"x");
    assert_eq!(b.put("new/deep/f.txt", &f, None, false).await.unwrap_err().error_class, "FileNotFoundError");
    assert_eq!(b.put("new/deep/f.txt", &f, None, true).await.unwrap().path, "new/deep/f.txt");
    assert!(b.ls("new/deep").await.unwrap().iter().any(|x| x.name == "f.txt"));
    assert_eq!(b.put("/", &f, None, false).await.unwrap_err().message, "put requires a file path");
    assert_eq!(b.put("x", &dir.path().join("absent"), None, false).await.unwrap_err().error_class, "FileNotFoundError");
}

#[tokio::test]
async fn rm_deletes_and_probe_answers() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    b.rm("pic.png").await.unwrap();
    assert!(!b.ls("").await.unwrap().iter().any(|e| e.name == "pic.png"));
    b.probe().await.unwrap();
    let err = s.bucket("gone").probe().await.unwrap_err();
    assert_eq!(err.error_class, "HttpError");
    assert!(err.message.starts_with("<HttpError 404 when requesting ") && err.message.ends_with("returned \"File not found: gone.\">"), "{}", err.message);
}

#[tokio::test]
async fn mkdir_creates_parents_and_accepts_an_existing_folder() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let e = b.mkdir("a/b").await.unwrap();
    assert!(e.is_dir && e.name == "b" && e.path == "a/b");
    assert!(b.ls("").await.unwrap().iter().any(|x| x.name == "a"));
    assert!(b.ls("a").await.unwrap().iter().any(|x| x.name == "b"));
    assert_eq!(b.mkdir("/a/b/").await.unwrap().id, e.id);
    assert_eq!(b.mkdir("docs").await.unwrap().id, "d1");
    assert_eq!(b.mkdir("").await.unwrap_err().message, "mkdir requires a path");
}

#[tokio::test]
async fn mv_renames_moves_and_overwrites() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let e = b.mv("pic.png", "docs/p2.png", false, false).await.unwrap();
    assert_eq!((e.path.as_str(), e.id.as_str()), ("docs/p2.png", "f2"));
    assert_eq!(s.where_is("f2"), ("d1".into(), "p2.png".into(), false));

    let err = b.mv("docs/p2.png", "docs/note.txt", false, false).await.unwrap_err();
    assert_eq!(err.error_class, "FileExistsError");
    assert_eq!(err.message, "bucket \"g\": \"docs/note.txt\" exists (pass overwrite:true to replace it)");
    b.mv("docs/p2.png", "docs/note.txt", true, false).await.unwrap();
    assert!(s.where_is("f1").2, "the old target is trashed");
    assert_eq!(s.where_is("f2"), ("d1".into(), "note.txt".into(), false));

    assert_eq!(b.mv("docs/note.txt", "new/n.txt", false, false).await.unwrap_err().error_class, "FileNotFoundError");
    assert_eq!(b.mv("docs/note.txt", "new/n.txt", false, true).await.unwrap().path, "new/n.txt");

    let err = b.mv("docs", "docs/inner", false, false).await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("ValueError", "mv: cannot mv \"docs\" into itself"));
}

#[tokio::test]
async fn cp_rebuilds_folders_and_overwrites() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let e = b.cp("docs", "docs2", false, false).await.unwrap();
    assert!(e.is_dir && e.id != "d1");
    let names = |v: Vec<Entry>| v.into_iter().map(|e| e.name).collect::<Vec<_>>();
    assert_eq!(names(b.ls("docs2").await.unwrap()), ["note.txt"]);
    assert_eq!(names(b.ls("docs").await.unwrap()), ["note.txt"]);

    assert_eq!(b.cp("pic.png", "docs/note.txt", false, false).await.unwrap_err().error_class, "FileExistsError");
    let c = b.cp("pic.png", "docs/note.txt", true, false).await.unwrap();
    assert!(s.where_is("f1").2);
    let out = tempfile::tempdir().unwrap();
    let got = b.get("docs/note.txt", out.path()).await.unwrap();
    assert_eq!((std::fs::read(got.path).unwrap(), c.path.as_str()), (b"img".to_vec(), "docs/note.txt"));
    assert_eq!(s.where_is("f2").1, "pic.png", "the source stays");
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

#[tokio::test]
async fn refresh_failures_carry_google_auth_text() {
    let s = stand(tree()).await;
    let mut c = cfg("");
    c.refresh_token = "r-revoked".into();
    let err = GoogleDrive::with_urls(c, s.urls.clone()).unwrap().probe().await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("RefreshError", "invalid_grant: Bad Request"));
    let mut c = cfg("");
    c.client_secret.clear();
    let err = GoogleDrive::with_urls(c, s.urls.clone()).unwrap().probe().await.unwrap_err();
    assert_eq!(err.error_class, "RuntimeError");
    assert_eq!(err.message, "bucket \"g\" is missing client_id/client_secret/refresh_token for Google Drive");
}

#[tokio::test]
async fn search_is_name_contains_over_the_drive() {
    let s = stand(tree()).await;
    let b = s.bucket("");
    let hits = b.search("note", 50, "").await.unwrap();
    assert_eq!(hits.iter().map(|e| (e.name.as_str(), e.path.as_str())).collect::<Vec<_>>(), [("note.txt", "note.txt")]);
    let err = b.search("note", 50, "docs").await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("NotImplementedError", "google_drive search cannot scope to a folder"));
}

fn tree2() -> Vec<(&'static str, Node)> {
    vec![
        ("d1", node("docs", F, "root", b"")),
        ("d2", node("deep dir", F, "d1", b"")),
        ("f1", node("note.txt", "text/plain", "d1", b"hello")),
        ("f2", node("a b.txt", "text/plain", "d2", b"ab")),
        ("f3", node("pic.png", "image/png", "root", b"img")),
    ]
}

#[tokio::test]
async fn walk_is_breadth_first_and_bounded() {
    let s = stand(tree2()).await;
    let b = s.bucket("");
    let (entries, truncated) = walk(&b, "", 100).await.unwrap();
    let paths: Vec<_> = entries.iter().map(|e| e.path.as_str()).collect();
    assert_eq!(paths, ["docs", "pic.png", "docs/deep dir", "docs/note.txt", "docs/deep dir/a b.txt"]);
    assert!(!truncated);
    let (entries, truncated) = walk(&b, "", 2).await.unwrap();
    assert!(entries.len() == 2 && truncated);
}

#[tokio::test]
async fn get_tree_writes_a_faithful_tree() {
    let s = stand(tree2()).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let out = get_tree(&b, "docs", dir.path(), 100).await.unwrap();
    let root = dir.path().join("docs");
    assert_eq!(out["path"], root.display().to_string());
    assert_eq!((out["is_dir"].clone(), out["count"].clone(), out["size"].clone()), (json!(true), json!(2), json!(7)));
    assert_eq!(out["failures"], json!([]));
    assert_eq!(std::fs::read(root.join("note.txt")).unwrap(), b"hello");
    assert_eq!(std::fs::read(root.join("deep dir").join("a b.txt")).unwrap(), b"ab");
    let err = get_tree(&b, "docs", tempfile::tempdir().unwrap().path(), 1).await.unwrap_err();
    assert!(err.error_class == "ValueError" && err.message.contains("larger limit"));
}

#[tokio::test]
async fn put_tree_uploads_a_tree() {
    let s = stand(tree2()).await;
    let b = s.bucket("");
    let dir = tempfile::tempdir().unwrap();
    let up = dir.path().join("up");
    std::fs::create_dir_all(up.join("sub")).unwrap();
    std::fs::write(up.join("top.txt"), b"t").unwrap();
    std::fs::write(up.join("sub").join("low.txt"), b"l").unwrap();
    let out = put_tree(&b, "docs/up", &up, false).await.unwrap();
    assert_eq!((out["count"].clone(), out["failures"].clone(), out["entry"]["is_dir"].clone()), (json!(2), json!([]), json!(true)));
    let paths: Vec<_> = walk(&b, "docs/up", 100).await.unwrap().0.into_iter().map(|e| e.path).collect();
    assert_eq!(paths, ["docs/up/sub", "docs/up/top.txt", "docs/up/sub/low.txt"]);

    let lone = dir.path().join("lone");
    std::fs::create_dir(&lone).unwrap();
    assert_eq!(put_tree(&b, "nope/up", &lone, false).await.unwrap_err().error_class, "FileNotFoundError");
    assert_eq!(put_tree(&b, "nope/up", &lone, true).await.unwrap()["entry"]["path"], "nope/up");
}

#[test]
fn safe_name_matches_the_python() {
    assert_eq!(safe_name("a b?.txt"), "a_b_.txt");
    assert_eq!(safe_name("dir/x.pdf"), "x.pdf");
    assert_eq!(safe_name("..."), "attachment-0");
    assert_eq!(safe_name(" _note_ "), "note");
}
