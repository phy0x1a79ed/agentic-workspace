//! Google Drive bucket: Drive v3 REST over reqwest, authenticated by an OAuth refresh token.
//!
//! Drive addresses files by id, so a path resolves by walking `'<parent>' in parents` from the
//! bucket root (a folder id, default `root`). A name held twice in one folder resolves to the
//! first match. Google-native docs have no bytes and are exported instead.
//!
//! The Google base URLs come from `AWM_GDRIVE_API`, `AWM_GDRIVE_UPLOAD`,
//! `AWM_GOOGLE_TOKEN_URL` and `AWM_GOOGLE_AUTH_URL` when set.

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use awm_plugin_sdk::VerbError;
use futures_util::StreamExt;
use reqwest::header::{CONTENT_RANGE, LOCATION, RANGE};
use reqwest::{RequestBuilder, Response, StatusCode};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};
use tokio::sync::Mutex;

use super::{exists, is_a_dir, not_a_dir, not_found, Bucket, Entry, Got, Result};
use crate::config::BucketConfig;

mod auth;
pub use auth::gdrive_auth;

#[cfg(test)]
mod tests;

const FOLDER: &str = "application/vnd.google-apps.folder";
const FIELDS: &str = "id, name, mimeType, size, modifiedTime, webViewLink";
const SCOPE: &str = "https://www.googleapis.com/auth/drive";
/// Bytes per resumable-upload request. Drive wants a multiple of 256 KiB.
const CHUNK: usize = 8 << 20;
/// Refresh an access token this long before Google says it expires.
const TOKEN_MARGIN: Duration = Duration::from_secs(60);

/// Google-native docs export to a portable format, with this suffix on the local name.
fn export_for(mime: &str) -> Option<(&'static str, &'static str)> {
    match mime {
        "application/vnd.google-apps.document" => Some(("application/pdf", ".pdf")),
        "application/vnd.google-apps.spreadsheet" => Some(("text/csv", ".csv")),
        "application/vnd.google-apps.presentation" => Some(("application/pdf", ".pdf")),
        "application/vnd.google-apps.drawing" => Some(("image/png", ".png")),
        _ => None,
    }
}

#[derive(Debug, Clone)]
struct Urls {
    api: String,
    upload: String,
    token: String,
    auth: String,
}

impl Urls {
    fn from_env() -> Self {
        let var = |k: &str, d: &str| match std::env::var(k) {
            Ok(v) if !v.trim().is_empty() => v.trim().trim_end_matches('/').to_string(),
            _ => d.to_string(),
        };
        Self {
            api: var("AWM_GDRIVE_API", "https://www.googleapis.com/drive/v3"),
            upload: var("AWM_GDRIVE_UPLOAD", "https://www.googleapis.com/upload/drive/v3"),
            token: var("AWM_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            auth: var("AWM_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/auth"),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct Meta {
    id: String,
    name: String,
    mime_type: String,
    size: Option<Value>,
    modified_time: String,
    web_view_link: String,
    parents: Vec<String>,
}

impl Meta {
    fn is_dir(&self) -> bool {
        self.mime_type == FOLDER
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct Listing {
    files: Vec<Meta>,
    next_page_token: Option<String>,
}

/// One `[bucket.<name>]` of kind `google_drive`.
pub struct GoogleDrive {
    cfg: BucketConfig,
    root: String,
    urls: Urls,
    http: reqwest::Client,
    /// Resumable uploads answer 308 with no Location, which must reach us unfollowed.
    upload_http: reqwest::Client,
    token: Mutex<Option<(String, Instant)>>,
    chunk: usize,
}

fn split(path: &str) -> Vec<&str> {
    path.trim_matches('/').split('/').filter(|s| !s.is_empty()).collect()
}

/// A filesystem-safe, non-empty basename, as `attachments.safe_name` makes it.
fn safe_name(name: &str) -> String {
    let base = name.rsplit('/').next().unwrap_or("").trim();
    let (mut out, mut bad) = (String::new(), false);
    for c in base.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
            if bad {
                out.push('_');
                bad = false;
            }
            out.push(c);
        } else {
            bad = true;
        }
    }
    if bad {
        out.push('_');
    }
    let t = out.trim_matches(|c| c == '.' || c == '_');
    if t.is_empty() { "attachment-0".into() } else { t.into() }
}

fn io_err(e: std::io::Error) -> VerbError {
    if e.kind() == std::io::ErrorKind::NotFound {
        not_found(e.to_string())
    } else {
        VerbError::runtime(e.to_string())
    }
}

async fn http_error(resp: Response) -> VerbError {
    let status = resp.status();
    let url = resp.url().to_string();
    let body: Value = resp.json().await.unwrap_or(Value::Null);
    let reason = body["error"]["message"]
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(|| status.canonical_reason().unwrap_or("").to_string());
    VerbError::new("HttpError", format!("<HttpError {} when requesting {url} returned \"{reason}\">", status.as_u16()))
}

impl GoogleDrive {
    /// Build the bucket. Credentials are checked on first use, so a bad one shows in `probe`.
    pub fn new(cfg: BucketConfig) -> anyhow::Result<Arc<dyn Bucket>> {
        Ok(Arc::new(Self::with_urls(cfg, Urls::from_env())?))
    }

    fn with_urls(cfg: BucketConfig, urls: Urls) -> anyhow::Result<Self> {
        let root = if cfg.root.trim().is_empty() { "root".to_string() } else { cfg.root.trim().to_string() };
        Ok(Self {
            cfg,
            root,
            urls,
            http: reqwest::Client::builder().build()?,
            upload_http: reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).build()?,
            token: Mutex::new(None),
            chunk: CHUNK,
        })
    }

    /// A live access token. `stale` is one the API just refused, so it is never handed back.
    async fn token(&self, stale: Option<&str>) -> Result<String> {
        let mut slot = self.token.lock().await;
        if let Some((tok, until)) = slot.as_ref() {
            if Some(tok.as_str()) != stale && Instant::now() < *until {
                return Ok(tok.clone());
            }
        }
        let c = &self.cfg;
        if c.client_id.is_empty() || c.client_secret.is_empty() || c.refresh_token.is_empty() {
            return Err(VerbError::runtime(format!(
                "bucket {:?} is missing client_id/client_secret/refresh_token for Google Drive",
                c.name
            )));
        }
        let resp = self
            .http
            .post(&self.urls.token)
            .form(&[
                ("grant_type", "refresh_token"),
                ("client_id", &c.client_id),
                ("client_secret", &c.client_secret),
                ("refresh_token", &c.refresh_token),
            ])
            .send()
            .await?;
        let status = resp.status();
        let body: Value = resp.json().await.unwrap_or(Value::Null);
        if !status.is_success() {
            let err = body["error"].as_str().unwrap_or("refresh failed");
            let msg = match body["error_description"].as_str() {
                Some(d) => format!("{err}: {d}"),
                None => err.to_string(),
            };
            return Err(VerbError::new("RefreshError", msg));
        }
        let Some(tok) = body["access_token"].as_str() else {
            return Err(VerbError::new("RefreshError", "No access token in response."));
        };
        let ttl = Duration::from_secs(body["expires_in"].as_u64().unwrap_or(3600));
        *slot = Some((tok.to_string(), Instant::now() + ttl.saturating_sub(TOKEN_MARGIN)));
        Ok(tok.to_string())
    }

    /// Send with a bearer token, refreshing it once on a 401. A non-2xx (but 308) is `HttpError`.
    async fn send(&self, build: impl Fn() -> RequestBuilder) -> Result<Response> {
        let tok = self.token(None).await?;
        let mut resp = build().bearer_auth(&tok).send().await?;
        if resp.status() == StatusCode::UNAUTHORIZED {
            let fresh = self.token(Some(&tok)).await?;
            resp = build().bearer_auth(&fresh).send().await?;
        }
        if resp.status().is_success() || resp.status() == StatusCode::PERMANENT_REDIRECT {
            Ok(resp)
        } else {
            Err(http_error(resp).await)
        }
    }

    async fn meta(&self, build: impl Fn() -> RequestBuilder) -> Result<Meta> {
        Ok(self.send(build).await?.json().await?)
    }

    fn api(&self, tail: &str) -> String {
        format!("{}/files{tail}", self.urls.api)
    }

    /// Every non-trashed child of `parent`, across pages.
    async fn children(&self, parent: &str) -> Result<Vec<Meta>> {
        let q = format!("'{parent}' in parents and trashed=false");
        let fields = format!("nextPageToken, files({FIELDS})");
        let (mut out, mut page) = (vec![], None::<String>);
        loop {
            let list: Listing = self
                .send(|| {
                    let r = self.http.get(self.api("")).query(&[
                        ("q", q.as_str()),
                        ("fields", &fields),
                        ("pageSize", "1000"),
                        ("supportsAllDrives", "true"),
                        ("includeItemsFromAllDrives", "true"),
                    ]);
                    match &page {
                        Some(p) => r.query(&[("pageToken", p)]),
                        None => r,
                    }
                })
                .await?
                .json()
                .await?;
            out.extend(list.files);
            match list.next_page_token.filter(|p| !p.is_empty()) {
                Some(p) => page = Some(p),
                None => return Ok(out),
            }
        }
    }

    async fn resolve(&self, path: &str) -> Result<Meta> {
        let segs = split(path);
        let mut meta = Meta { id: self.root.clone(), mime_type: FOLDER.into(), ..Default::default() };
        for seg in segs {
            let parent = meta.id.clone();
            let mut matches: Vec<Meta> = self.children(&parent).await?.into_iter().filter(|c| c.name == seg).collect();
            if matches.is_empty() {
                return Err(not_found(format!(
                    "bucket {:?}: no such path {path:?} (missing segment {seg:?})",
                    self.cfg.name
                )));
            }
            if matches.len() > 1 {
                tracing::warn!(bucket = %self.cfg.name, count = matches.len(), parent = %parent, "several entries share one name; using the first");
            }
            meta = matches.swap_remove(0);
        }
        Ok(meta)
    }

    fn entry(meta: &Meta, path: &str) -> Entry {
        let size = match &meta.size {
            Some(Value::String(s)) => s.parse().unwrap_or(0),
            Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
            _ => 0,
        };
        Entry {
            name: meta.name.clone(),
            path: path.to_string(),
            is_dir: meta.is_dir(),
            size,
            mime: meta.mime_type.clone(),
            modified: meta.modified_time.clone(),
            id: meta.id.clone(),
            web_url: meta.web_view_link.clone(),
        }
    }

    /// The folder id a new `path` lands in: made with `parents`, else it must exist.
    async fn parent_of(&self, parent_path: &str, parents: bool, must_be_dir: bool) -> Result<String> {
        if parent_path.is_empty() {
            return Ok(self.root.clone());
        }
        if parents {
            return Ok(self.mkdir(parent_path).await?.id);
        }
        let p = self.resolve(parent_path).await?;
        if must_be_dir && !p.is_dir() {
            return Err(not_a_dir(format!("bucket {:?}: {parent_path:?} is a file", self.cfg.name)));
        }
        Ok(p.id)
    }

    /// Send the local file `src` to the resumable session `session`, one chunk per request.
    async fn upload(&self, session: &str, src: &Path, total: u64, path: &str) -> Result<Meta> {
        let mut file = tokio::fs::File::open(src).await.map_err(io_err)?;
        let mut offset = 0u64;
        loop {
            let n = (total - offset).min(self.chunk as u64) as usize;
            let mut buf = vec![0u8; n];
            file.seek(std::io::SeekFrom::Start(offset)).await.map_err(io_err)?;
            file.read_exact(&mut buf).await.map_err(io_err)?;
            let range = if total == 0 {
                "bytes */0".to_string()
            } else {
                format!("bytes {offset}-{}/{total}", offset + n as u64 - 1)
            };
            let resp = self
                .send(|| self.upload_http.put(session).header(CONTENT_RANGE, &range).body(buf.clone()))
                .await?;
            if resp.status() != StatusCode::PERMANENT_REDIRECT {
                return Ok(resp.json().await?);
            }
            let next = resp
                .headers()
                .get(RANGE)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.rsplit('-').next())
                .and_then(|v| v.trim().parse::<u64>().ok())
                .map_or(0, |end| end + 1);
            if next <= offset || next > total {
                return Err(VerbError::runtime(format!(
                    "bucket {:?}: upload of {path:?} stalled at byte {offset}",
                    self.cfg.name
                )));
            }
            offset = next;
        }
    }

    /// Resolve `src` and the folder `dst` lands in, trashing a taken `dst` when `overwrite`.
    async fn prepare_target(&self, op: &str, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<(Meta, String, String)> {
        let (segs, src_segs) = (split(dst), split(src));
        if segs.is_empty() || src_segs.is_empty() {
            return Err(VerbError::value(format!("{op} needs a source and a target path")));
        }
        if segs.join("/").starts_with(&format!("{}/", src_segs.join("/"))) {
            return Err(VerbError::value(format!("{op}: cannot {op} {src:?} into itself")));
        }
        let meta = self.resolve(src).await?;
        let name = segs[segs.len() - 1].to_string();
        let parent = self.parent_of(&segs[..segs.len() - 1].join("/"), parents, true).await?;
        let taken: Vec<Meta> = self
            .children(&parent)
            .await?
            .into_iter()
            .filter(|c| c.name == name && c.id != meta.id)
            .collect();
        if !taken.is_empty() && !overwrite {
            return Err(exists(format!(
                "bucket {:?}: {dst:?} exists (pass overwrite:true to replace it)",
                self.cfg.name
            )));
        }
        for c in taken {
            self.send(|| {
                self.http
                    .patch(self.api(&format!("/{}", c.id)))
                    .query(&[("supportsAllDrives", "true")])
                    .json(&json!({"trashed": true}))
            })
            .await?;
        }
        Ok((meta, parent, name))
    }

    async fn create_folder(&self, name: &str, parent: &str, fields: &str) -> Result<Meta> {
        self.meta(|| {
            self.http
                .post(self.api(""))
                .query(&[("fields", fields), ("supportsAllDrives", "true")])
                .json(&json!({"name": name, "parents": [parent], "mimeType": FOLDER}))
        })
        .await
    }

    /// Drive copies files only, so a folder is rebuilt level by level.
    fn copy_tree<'a>(&'a self, meta: Meta, parent: String, name: String) -> futures_util::future::BoxFuture<'a, Result<Meta>> {
        Box::pin(async move {
            if !meta.is_dir() {
                return self
                    .meta(|| {
                        self.http
                            .post(self.api(&format!("/{}/copy", meta.id)))
                            .query(&[("fields", FIELDS), ("supportsAllDrives", "true")])
                            .json(&json!({"name": name, "parents": [parent]}))
                    })
                    .await;
            }
            let made = self.create_folder(&name, &parent, FIELDS).await?;
            for child in self.children(&meta.id).await? {
                let n = child.name.clone();
                self.copy_tree(child, made.id.clone(), n).await?;
            }
            Ok(made)
        })
    }
}

#[async_trait]
impl Bucket for GoogleDrive {
    fn name(&self) -> &str {
        &self.cfg.name
    }

    fn kind(&self) -> &str {
        "google_drive"
    }

    async fn ls(&self, path: &str) -> Result<Vec<Entry>> {
        let folder = self.resolve(path).await?;
        if !folder.is_dir() {
            return Err(not_a_dir(format!("bucket {:?}: {path:?} is not a folder", self.cfg.name)));
        }
        let base = path.trim_matches('/');
        let mut kids = self.children(&folder.id).await?;
        kids.sort_by(|a, b| (!a.is_dir(), &a.name).cmp(&(!b.is_dir(), &b.name)));
        Ok(kids
            .iter()
            .map(|c| Self::entry(c, &if base.is_empty() { c.name.clone() } else { format!("{base}/{}", c.name) }))
            .collect())
    }

    async fn stat(&self, path: &str) -> Result<Entry> {
        Ok(Self::entry(&self.resolve(path).await?, path.trim_matches('/')))
    }

    async fn get(&self, path: &str, dest: &Path) -> Result<Got> {
        let meta = self.resolve(path).await?;
        if meta.is_dir() {
            return Err(is_a_dir(format!("bucket {:?}: {path:?} is a folder", self.cfg.name)));
        }
        let mut name = if meta.name.is_empty() { split(path).last().unwrap_or(&"").to_string() } else { meta.name.clone() };
        let (url, query, mime): (String, Vec<(&str, &str)>, String) = match export_for(&meta.mime_type) {
            Some((out, ext)) => {
                if !name.to_lowercase().ends_with(ext) {
                    name.push_str(ext);
                }
                (self.api(&format!("/{}/export", meta.id)), vec![("mimeType", out)], out.to_string())
            }
            None => (
                self.api(&format!("/{}", meta.id)),
                vec![("alt", "media"), ("supportsAllDrives", "true")],
                meta.mime_type.clone(),
            ),
        };
        let resp = self.send(|| self.http.get(&url).query(&query)).await?;
        let local = dest.join(safe_name(&name));
        let written: Result<i64> = async {
            let mut file = tokio::fs::File::create(&local).await.map_err(io_err)?;
            let (mut stream, mut size) = (resp.bytes_stream(), 0i64);
            while let Some(chunk) = stream.next().await {
                let chunk = chunk?;
                file.write_all(&chunk).await.map_err(io_err)?;
                size += chunk.len() as i64;
            }
            file.flush().await.map_err(io_err)?;
            Ok(size)
        }
        .await;
        let size = match written {
            Ok(s) => s,
            Err(e) => {
                let _ = tokio::fs::remove_file(&local).await;
                return Err(e);
            }
        };
        Ok(Got {
            filename: name,
            mime: if mime.is_empty() { "application/octet-stream".into() } else { mime },
            size,
            path: local.display().to_string(),
        })
    }

    async fn put(&self, path: &str, src: &Path, mime: Option<&str>, parents: bool) -> Result<Entry> {
        let segs = split(path);
        let Some(name) = segs.last().map(|s| s.to_string()) else {
            return Err(VerbError::value("put requires a file path"));
        };
        let parent = self.parent_of(&segs[..segs.len() - 1].join("/"), parents, false).await?;
        let total = tokio::fs::metadata(src).await.map_err(io_err)?.len();
        let mime = mime.filter(|m| !m.is_empty()).unwrap_or("application/octet-stream");
        let existing = self.children(&parent).await?.into_iter().find(|c| c.name == name && !c.is_dir());
        let query = [("uploadType", "resumable"), ("fields", FIELDS), ("supportsAllDrives", "true")];
        let total_s = total.to_string();
        let start = self
            .send(|| {
                let r = match &existing {
                    Some(e) => self.upload_http.patch(format!("{}/files/{}", self.urls.upload, e.id)).json(&json!({})),
                    None => self
                        .upload_http
                        .post(format!("{}/files", self.urls.upload))
                        .json(&json!({"name": name, "parents": [parent]})),
                };
                r.query(&query).header("X-Upload-Content-Type", mime).header("X-Upload-Content-Length", &total_s)
            })
            .await?;
        let Some(session) = start.headers().get(LOCATION).and_then(|v| v.to_str().ok()).map(str::to_string) else {
            return Err(VerbError::runtime(format!(
                "bucket {:?}: Drive gave no upload session for {path:?}",
                self.cfg.name
            )));
        };
        let meta = self.upload(&session, src, total, path).await?;
        Ok(Self::entry(&meta, path.trim_matches('/')))
    }

    async fn rm(&self, path: &str) -> Result<()> {
        let meta = self.resolve(path).await?;
        self.send(|| self.http.delete(self.api(&format!("/{}", meta.id))).query(&[("supportsAllDrives", "true")]))
            .await?;
        Ok(())
    }

    async fn mkdir(&self, path: &str) -> Result<Entry> {
        let segs = split(path);
        if segs.is_empty() {
            return Err(VerbError::value("mkdir requires a path"));
        }
        let (mut parent, mut meta) = (self.root.clone(), Meta::default());
        for seg in &segs {
            meta = match self.children(&parent).await?.into_iter().find(|c| c.name == *seg && c.is_dir()) {
                Some(m) => m,
                None => self.create_folder(seg, &parent, "id, name, mimeType, modifiedTime, webViewLink").await?,
            };
            parent = meta.id.clone();
        }
        Ok(Self::entry(&meta, &segs.join("/")))
    }

    async fn mv(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry> {
        let (meta, parent, name) = self.prepare_target("mv", src, dst, overwrite, parents).await?;
        let file = self.api(&format!("/{}", meta.id));
        let cur = self
            .meta(|| self.http.get(&file).query(&[("fields", "parents"), ("supportsAllDrives", "true")]))
            .await?;
        let remove = cur.parents.join(",");
        let moved = self
            .meta(|| {
                self.http
                    .patch(&file)
                    .query(&[
                        ("addParents", parent.as_str()),
                        ("removeParents", &remove),
                        ("fields", FIELDS),
                        ("supportsAllDrives", "true"),
                    ])
                    .json(&json!({"name": name}))
            })
            .await?;
        Ok(Self::entry(&moved, dst.trim_matches('/')))
    }

    async fn cp(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry> {
        let (meta, parent, name) = self.prepare_target("cp", src, dst, overwrite, parents).await?;
        Ok(Self::entry(&self.copy_tree(meta, parent, name).await?, dst.trim_matches('/')))
    }

    async fn search(&self, query: &str, limit: usize, path: &str) -> Result<Vec<Entry>> {
        // Drive cannot query "below this folder", so search spans the whole drive.
        if !path.trim_matches('/').is_empty() {
            return Err(VerbError::new("NotImplementedError", "google_drive search cannot scope to a folder"));
        }
        let q = format!("name contains '{}' and trashed=false", query.replace('\'', "\\'"));
        let fields = format!("files({FIELDS})");
        let size = limit.clamp(1, 1000).to_string();
        let list: Listing = self
            .send(|| {
                self.http.get(self.api("")).query(&[
                    ("q", q.as_str()),
                    ("fields", &fields),
                    ("pageSize", &size),
                    ("supportsAllDrives", "true"),
                    ("includeItemsFromAllDrives", "true"),
                ])
            })
            .await?
            .json()
            .await?;
        Ok(list.files.iter().map(|c| Self::entry(c, &c.name)).collect())
    }

    async fn probe(&self) -> Result<()> {
        self.send(|| {
            self.http
                .get(self.api(&format!("/{}", self.root)))
                .query(&[("fields", "id"), ("supportsAllDrives", "true")])
        })
        .await?;
        Ok(())
    }
}
