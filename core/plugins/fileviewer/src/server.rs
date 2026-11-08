//! The mount: GET and HEAD of `/<path>` under the root, behind the gateway's `/s/fileviewer/`.

use std::path::PathBuf;
use std::sync::Arc;

use awm_plugin_sdk::axum::body::Body;
use awm_plugin_sdk::axum::extract::{Request, State};
use awm_plugin_sdk::axum::http::{header, Method, StatusCode};
use awm_plugin_sdk::axum::response::Response;
use awm_plugin_sdk::axum::Router;
use futures_util::stream;
use serde_json::{json, Value};
use tokio::io::AsyncReadExt;

use crate::mask::Mask;
use crate::mime::{MimeDb, FALLBACK};
use crate::resolve::{open_regular, realpath, resolve};

pub struct Config {
    /// `/s/<service>`.
    pub prefix: String,
    /// The root as configured, for the status.
    pub root_raw: String,
    pub root: PathBuf,
    pub mask: Mask,
    pub mime: MimeDb,
    pub listen: String,
}

impl Config {
    /// The root as `FILEVIEWER_MOUNT_ROOT` names it (default `/`), with the mask and the
    /// system's mime table.
    pub fn new(service: &str, root_raw: &str, mask_file: Option<&str>, listen: String) -> Self {
        let root = realpath(&absolute(root_raw)).unwrap_or_else(|| absolute(root_raw));
        let mask = Mask::load(mask_file, &root);
        Self { prefix: format!("/s/{service}"), root_raw: root_raw.into(), root, mask, mime: MimeDb::system(), listen }
    }

    pub fn status(&self) -> Value {
        json!({
            "mounted": true,
            "prefix": self.prefix,
            "root": self.root_raw,
            "deny_globs": self.mask.globs.len(),
            "listen": self.listen,
            "url_shape": format!("{}/<absolute-path-under-{}>", self.prefix, self.root_raw),
        })
    }
}

fn absolute(p: &str) -> PathBuf {
    let p = PathBuf::from(p);
    if p.is_absolute() {
        p
    } else {
        std::env::current_dir().map(|d| d.join(&p)).unwrap_or(p)
    }
}

pub fn router(cfg: Arc<Config>) -> Router {
    Router::new().fallback(serve).with_state(cfg)
}

const CHUNK: usize = 64 << 10;

async fn serve(State(cfg): State<Arc<Config>>, req: Request) -> Response {
    let head = req.method() == Method::HEAD;
    if !head && req.method() != Method::GET {
        return plain(StatusCode::NOT_IMPLEMENTED, "not implemented", false);
    }
    let rel = unquote(req.uri().path());
    let cfg2 = cfg.clone();
    let found = tokio::task::spawn_blocking(move || {
        let target = resolve(&rel, &cfg2.root, &cfg2.mask)?;
        let name = target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let (file, size) = open_regular(&target)?;
        Some((file, size, cfg2.mime.guess(&name).unwrap_or_else(|| FALLBACK.into())))
    })
    .await
    .ok()
    .flatten();
    let Some((file, size, mime)) = found else { return plain(StatusCode::NOT_FOUND, "not found", head) };
    let body = if head {
        Body::empty()
    } else {
        let reader = tokio::fs::File::from_std(file).take(size);
        Body::from_stream(stream::unfold(reader, |mut r| async move {
            let mut buf = vec![0u8; CHUNK];
            match r.read(&mut buf).await {
                Ok(0) => None,
                Ok(n) => {
                    buf.truncate(n);
                    Some((Ok::<_, std::io::Error>(buf), r))
                }
                Err(e) => Some((Err(e), r)),
            }
        }))
    };
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, mime)
        .header(header::CONTENT_LENGTH, size)
        .body(body)
        .unwrap_or_else(|_| plain(StatusCode::NOT_FOUND, "not found", head))
}

fn plain(status: StatusCode, text: &'static str, head: bool) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(header::CONTENT_LENGTH, text.len())
        .body(if head { Body::empty() } else { Body::from(text) })
        .expect("static response")
}

/// `urllib.parse.unquote`: `%XX` decoded, invalid UTF-8 replaced, a malformed escape kept.
pub fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let hex = |c: u8| (c as char).to_digit(16);
        match (b[i], b.get(i + 1).and_then(|&c| hex(c)), b.get(i + 2).and_then(|&c| hex(c))) {
            (b'%', Some(h), Some(l)) => {
                out.push((h * 16 + l) as u8);
                i += 3;
            }
            (c, _, _) => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
