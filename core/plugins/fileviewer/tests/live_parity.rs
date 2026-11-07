//! Agreement with a running Python fileviewer on this host. Runs only when both are set,
//! and passes as a no-op otherwise:
//! - `AWM_FILEVIEWER_PYTHON`: the Python listener, `127.0.0.1:<port>`, serving root `/`
//!   with the default mask.
//! - `AWM_FILEVIEWER_PATHS`: a file of request paths, one per line, not yet escaped.
//!
//! Each path is requested from both, and the status, type and body are compared in memory.
//! The test prints counts and row numbers only, never a path or a body.

use std::sync::Arc;

use awm_plugin_fileviewer::mask::{Mask, DEFAULT_MASK};
use awm_plugin_fileviewer::server::{router, Config};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

/// The globs the port added. The Python mask is the rest.
const ADDED: [&str; 4] = ["**/.pre-rust/**", "**/.pre-rust.tmp/**", "**/.rust-*/**", "**/globus-tokens.json"];

fn escape(path: &str) -> String {
    path.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' | b'%' | b'?' | b'=' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

async fn fetch(addr: &str, target: &str) -> (u16, Option<String>, Vec<u8>) {
    let mut s = tokio::net::TcpStream::connect(addr).await.unwrap();
    s.write_all(format!("GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).await.unwrap();
    let Some(split) = raw.windows(4).position(|w| w == b"\r\n\r\n") else { return (0, None, raw) };
    let head = String::from_utf8_lossy(&raw[..split]).into_owned();
    let mut lines = head.split("\r\n");
    let status = lines.next().and_then(|l| l.split(' ').nth(1)).and_then(|c| c.parse().ok()).unwrap_or(0);
    let ctype = lines.filter_map(|l| l.split_once(": ")).find(|(k, _)| k.eq_ignore_ascii_case("content-type")).map(|(_, v)| v.to_string());
    (status, ctype, raw[split + 4..].to_vec())
}

async fn serve(globs: Vec<String>) -> String {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let mut cfg = Config::new("fileviewer", "/", None, format!("http://{addr}"));
    cfg.mask = Mask::new(globs);
    tokio::spawn(async move { awm_plugin_sdk::axum::serve(listener, router(Arc::new(cfg))).await.unwrap() });
    addr
}

#[tokio::test]
async fn agrees_with_the_python_service() {
    let (Ok(python), Ok(list)) = (std::env::var("AWM_FILEVIEWER_PYTHON"), std::env::var("AWM_FILEVIEWER_PATHS")) else { return };
    let paths: Vec<String> = std::fs::read_to_string(list).unwrap().lines().filter(|l| !l.is_empty()).map(String::from).collect();
    let old: Vec<String> = DEFAULT_MASK.iter().filter(|g| !ADDED.contains(g)).map(|g| g.to_string()).collect();
    assert_eq!(old.len(), 31, "the Python default mask");
    let same_mask = serve(old).await;
    let full_mask = serve(DEFAULT_MASK.iter().map(|g| g.to_string()).collect()).await;
    let (mut agree, mut differ, mut by_mask, mut served) = (0, vec![], vec![], 0);
    for (i, p) in paths.iter().enumerate() {
        let target = escape(p);
        let want = fetch(&python, &target).await;
        let got = fetch(&same_mask, &target).await;
        if want == got {
            agree += 1;
            served += usize::from(got.0 == 200);
        } else {
            differ.push(format!("row {}: python {} {:?} {}B, rust {} {:?} {}B", i + 1, want.0, want.1, want.2.len(), got.0, got.1, got.2.len()));
        }
        if fetch(&full_mask, &target).await != got {
            by_mask.push(i + 1);
        }
    }
    eprintln!("parity: {agree} agree ({served} served 200), {} differ, {} more hidden by the added globs", differ.len(), by_mask.len());
    for d in &differ {
        eprintln!("  {d}");
    }
    eprintln!("  added globs hide rows {by_mask:?}");
}
