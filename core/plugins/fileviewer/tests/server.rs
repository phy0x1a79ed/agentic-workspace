//! The Python service's `test_server`, the port's seven changes, and mime parity with
//! Python 3.12.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;
use std::sync::Arc;

use awm_plugin_fileviewer::mask::{Mask, DEFAULT_MASK};
use awm_plugin_fileviewer::mime::MimeDb;
use awm_plugin_fileviewer::resolve::{open_regular, resolve};
use awm_plugin_fileviewer::server::{router, unquote, Config};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn mask(globs: &[&str]) -> Mask {
    Mask::new(globs.iter().map(|g| g.to_string()).collect())
}

struct Reply {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Reply {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

/// A served root, its listener, and raw requests, so a path reaches the server unnormalised.
struct Served {
    _dir: tempfile::TempDir,
    root: std::path::PathBuf,
    port: u16,
}

impl Served {
    async fn new(globs: &[&str]) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        fs::write(root.join("fig.svg"), "<svg/>").unwrap();
        fs::write(root.join("secret.pem"), "KEY").unwrap();
        fs::create_dir(root.join("site")).unwrap();
        fs::write(root.join("site/index.html"), "<p>hi</p>").unwrap();
        fs::create_dir(root.join("bare")).unwrap();
        symlink(root.join("secret.pem"), root.join("link.txt")).unwrap();
        Self::serve(dir, root, globs).await
    }

    async fn serve(dir: tempfile::TempDir, root: std::path::PathBuf, globs: &[&str]) -> Self {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut cfg = Config::new("fileviewer", root.to_str().unwrap(), None, format!("http://127.0.0.1:{port}"));
        cfg.mask = mask(globs);
        tokio::spawn(async move { awm_plugin_sdk::axum::serve(listener, router(Arc::new(cfg))).await.unwrap() });
        Self { _dir: dir, root, port }
    }

    async fn req(&self, method: &str, path: &str) -> Reply {
        let mut s = tokio::net::TcpStream::connect(("127.0.0.1", self.port)).await.unwrap();
        s.write_all(format!("{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
        let mut raw = Vec::new();
        s.read_to_end(&mut raw).await.unwrap();
        let split = raw.windows(4).position(|w| w == b"\r\n\r\n").expect("a header block");
        let head = String::from_utf8_lossy(&raw[..split]).into_owned();
        let mut lines = head.split("\r\n");
        let status = lines.next().unwrap().split(' ').nth(1).unwrap().parse().unwrap();
        let headers = lines.filter_map(|l| l.split_once(": ")).map(|(k, v)| (k.to_string(), v.to_string())).collect();
        Reply { status, headers, body: raw[split + 4..].to_vec() }
    }

    async fn get(&self, path: &str) -> Reply {
        self.req("GET", path).await
    }
}

#[test]
fn the_default_mask_covers_the_usual_secrets_and_the_port_state() {
    for g in ["**/.ssh/**", "**/*.pem", "**/*.key", "**/.env", "**/*.token", "**/.aws/**", "**/.git/**"] {
        assert!(DEFAULT_MASK.contains(&g), "{g}");
    }
    let m = Mask::load(None, Path::new("/"));
    for p in [
        "var/lib/awm/services/auth/.pre-rust/auth.db",
        "var/lib/awm/services/auth/.pre-rust.tmp/auth.db",
        "home/u/.awm/services/routine/.rust-20261107T120000/routine.db",
        "home/u/.awm/services/dvc/globus-tokens.json",
    ] {
        assert!(m.hides(p), "{p}");
    }
    assert!(!m.hides("home/u/.awm/services/dvc/dvc.toml"));
}

#[test]
fn load_mask_defaults_only_when_no_override() {
    assert_eq!(Mask::load(None, Path::new("/")).globs, DEFAULT_MASK.iter().map(|g| g.to_string()).collect::<Vec<_>>());
}

#[test]
fn load_mask_merges_the_override_and_hides_itself() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let mf = root.join("fileviewer.mask");
    fs::write(&mf, "# a comment\n**/private/**\n\n**/*.secretkey\n**/.ssh\n").unwrap();
    let m = Mask::load(Some(mf.to_str().unwrap()), &root);
    for g in ["**/private/**", "**/*.secretkey", "**/.ssh/**", "**/fileviewer.mask", "fileviewer.mask"] {
        assert!(m.globs.iter().any(|x| x == g), "{g}");
    }
    assert!(!m.globs.iter().any(|g| g.is_empty() || g.starts_with('#')));
    assert_eq!(m.globs.iter().filter(|g| *g == "**/.ssh").count(), 1, "deduplicated");
    assert!(m.hides("fileviewer.mask") && m.hides("a/private/b"));
}

#[test]
fn load_mask_tolerates_an_unreadable_override() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("does-not-exist.mask");
    let m = Mask::load(Some(missing.to_str().unwrap()), dir.path());
    assert!(m.globs.iter().any(|g| g == "**/.ssh/**"));
    assert!(m.globs.iter().any(|g| g == "**/does-not-exist.mask"));
    assert!(!m.is_bad());
}

#[test]
fn the_status_names_the_mount() {
    let cfg = Config::new("fileviewer", "/", None, "http://127.0.0.1:1234".into());
    let s = cfg.status();
    assert_eq!(s["mounted"], true);
    assert_eq!(s["prefix"], "/s/fileviewer");
    assert_eq!(s["root"], "/");
    assert_eq!(s["listen"], "http://127.0.0.1:1234");
    assert_eq!(s["deny_globs"], DEFAULT_MASK.len());
    assert_eq!(s["url_shape"], "/s/fileviewer/<absolute-path-under-/>");
}

#[tokio::test]
async fn serves_a_file_with_its_type() {
    let s = Served::new(&["**/*.pem"]).await;
    let r = s.get("/fig.svg").await;
    assert_eq!((r.status, r.header("content-type"), r.body.as_slice()), (200, Some("image/svg+xml"), b"<svg/>".as_slice()));
    assert_eq!(r.header("content-length"), Some("6"));
}

#[tokio::test]
async fn head_has_the_length_and_no_body() {
    let s = Served::new(&["**/*.pem"]).await;
    let r = s.req("HEAD", "/fig.svg").await;
    assert_eq!((r.status, r.header("content-length"), r.body.len()), (200, Some("6"), 0));
    let r = s.req("HEAD", "/nope").await;
    assert_eq!((r.status, r.header("content-length"), r.body.len()), (404, Some("9"), 0));
}

#[tokio::test]
async fn hides_masked_and_symlinked_secrets() {
    let s = Served::new(&["**/*.pem"]).await;
    assert_eq!(s.get("/secret.pem").await.status, 404);
    assert_eq!(s.get("/link.txt").await.status, 404);
}

#[tokio::test]
async fn refuses_traversal_and_missing_with_one_404() {
    let s = Served::new(&["**/*.pem"]).await;
    let missing = s.get("/nope.txt").await;
    assert_eq!((missing.status, missing.header("content-type"), missing.body.as_slice()), (404, Some("text/plain; charset=utf-8"), b"not found".as_slice()));
    for p in ["/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/a%00b", "/secret.pem", "/bare/"] {
        let r = s.get(p).await;
        assert_eq!((r.status, r.body), (404, missing.body.clone()), "{p}");
    }
}

#[tokio::test]
async fn a_directory_serves_its_index_only() {
    let s = Served::new(&["**/*.pem"]).await;
    assert_eq!(s.get("/site/").await.body, b"<p>hi</p>");
    assert_eq!(s.get("/site").await.body, b"<p>hi</p>");
    assert_eq!(s.get("/bare/").await.status, 404);
}

#[tokio::test]
async fn the_query_is_not_part_of_the_path_and_escapes_decode() {
    let s = Served::new(&["**/*.pem"]).await;
    assert_eq!(s.get("/fig.svg?x=1").await.status, 200);
    assert_eq!(s.get("/fig%2Esvg").await.status, 200);
    assert_eq!(unquote("/a%zz%41%e2%82%ac%ff"), "/a%zzA€\u{fffd}");
}

// Change 1: a symlinked index.html is resolved and checked again.
#[tokio::test]
async fn a_symlinked_index_is_checked_again() {
    let s = Served::new(&["**/*.pem"]).await;
    fs::create_dir(s.root.join("trap")).unwrap();
    symlink(s.root.join("secret.pem"), s.root.join("trap/index.html")).unwrap();
    assert_eq!(s.get("/trap/").await.status, 404);
    let outside = tempfile::NamedTempFile::new().unwrap();
    fs::create_dir(s.root.join("out")).unwrap();
    symlink(outside.path(), s.root.join("out/index.html")).unwrap();
    assert_eq!(s.get("/out/").await.status, 404);
    fs::create_dir(s.root.join("fine")).unwrap();
    symlink(s.root.join("fig.svg"), s.root.join("fine/index.html")).unwrap();
    assert_eq!(s.get("/fine/").await.body, b"<svg/>");
}

// Change 2: the final open refuses a symlink.
#[test]
fn the_final_open_refuses_a_symlink() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("f"), "x").unwrap();
    symlink(dir.path().join("f"), dir.path().join("l")).unwrap();
    fs::create_dir(dir.path().join("d")).unwrap();
    assert_eq!(open_regular(&dir.path().join("f")).unwrap().1, 1);
    assert!(open_regular(&dir.path().join("l")).is_none());
    assert!(open_regular(&dir.path().join("d")).is_none());
}

// Change 3: a path starting with `//` resolves as written.
#[tokio::test]
async fn a_double_slash_path_resolves_as_written() {
    let s = Served::new(&["**/*.pem"]).await;
    fs::create_dir(s.root.join("etc")).unwrap();
    fs::write(s.root.join("etc/f.txt"), "f").unwrap();
    let r = s.get("//etc/f.txt").await;
    assert_eq!((r.status, r.header("content-type")), (200, Some("text/plain")));
}

// Change 4: a symlink loop is a 404.
#[tokio::test]
async fn a_symlink_loop_is_a_404() {
    let s = Served::new(&["**/*.pem"]).await;
    symlink(s.root.join("b"), s.root.join("a")).unwrap();
    symlink(s.root.join("a"), s.root.join("b")).unwrap();
    symlink(s.root.join("self"), s.root.join("self")).unwrap();
    for p in ["/a", "/b/x", "/self"] {
        assert_eq!(s.get(p).await.status, 404, "{p}");
    }
    assert!(resolve("a", &s.root, &mask(&[])).is_none());
}

// Change 5: a bad glob denies everything.
#[tokio::test]
async fn a_bad_glob_denies_everything() {
    let s = Served::new(&["**/*.pem", "**/[z-a]"]).await;
    assert_eq!(s.get("/fig.svg").await.status, 404);
    assert!(mask(&["[!]"]).hides("anything"));
    let dir = tempfile::tempdir().unwrap();
    let mf = dir.path().join("bad.mask");
    fs::write(&mf, [0xffu8, 0xfe]).unwrap();
    assert!(Mask::load(Some(mf.to_str().unwrap()), dir.path()).is_bad());
}

// Change 6: another method is a plain 501, and no Server header names the runtime.
#[tokio::test]
async fn another_method_is_a_501() {
    let s = Served::new(&["**/*.pem"]).await;
    let r = s.req("POST", "/fig.svg").await;
    assert_eq!((r.status, r.body.as_slice()), (501, b"not implemented".as_slice()));
    assert_eq!(s.get("/fig.svg").await.header("server"), None);
}

// Change 7 is the_default_mask_covers_the_usual_secrets_and_the_port_state.

#[tokio::test]
async fn a_negation_carves_a_servable_subtree() {
    let s = Served::new(&["!**/site/**", "**/*.pem"]).await;
    fs::write(s.root.join("site/key.pem"), "K").unwrap();
    assert_eq!(s.get("/site/key.pem").await.status, 404, "the later deny wins");
    let s = Served::new(&["**/site/**", "!**/site/index.html"]).await;
    assert_eq!(s.get("/site/").await.status, 404, "`**/site/**` masks the directory itself");
    assert_eq!(s.get("/site/index.html").await.status, 200);
}

#[test]
fn mime_types_match_python() {
    let v: serde_json::Value = serde_json::from_str(include_str!("mime_parity.json")).unwrap();
    assert!(v["python"].as_str().unwrap().starts_with("3.12."));
    let mut with = MimeDb::defaults();
    with.read(v["fixture"].as_str().unwrap());
    for (key, db) in [("defaults", MimeDb::defaults()), ("with_fixture", with)] {
        let mut bad = vec![];
        for row in v[key].as_array().unwrap() {
            let name = row[0].as_str().unwrap();
            let want = row[1].as_str().map(String::from);
            if db.guess(name) != want {
                bad.push(format!("{name:?}: {:?} != {want:?}", db.guess(name)));
            }
        }
        assert!(bad.is_empty(), "{key}: {bad:#?}");
    }
}
