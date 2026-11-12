//! OneDrive and SharePoint through a signed-in tab in alcor's shared browser, with no Graph
//! app. `onedrive.js` speaks the SharePoint REST API from inside the page.

use std::path::Path;

use async_trait::async_trait;
use awm_plugin_sdk::{Hub, VerbError};
use serde_json::{json, Value};

use super::{exists, is_a_dir, not_a_dir, not_found, Bucket, Entry, Got, Result};
use crate::config::BucketConfig;
use crate::tab::{self, Tab};

const JS: &str = include_str!("onedrive.js");
/// A whole file crosses the browser hop as base64 in one reply.
const MAX_FILE_BYTES: u64 = 40 << 20;

pub struct OneDrive {
    name: String,
    /// The server-relative folder the bucket's paths hang off, such as `/personal/<x>/Documents`.
    /// Unset in the config, it is the signed-in person's OneDrive, looked up on first use.
    root: tokio::sync::OnceCell<String>,
    tab: Tab,
}

impl OneDrive {
    pub fn create(cfg: &BucketConfig, hub: Hub) -> anyhow::Result<std::sync::Arc<dyn Bucket>> {
        let root = match cfg.root.trim_matches('/') {
            "" => tokio::sync::OnceCell::new(),
            r => tokio::sync::OnceCell::new_with(Some(format!("/{r}"))),
        };
        let site = if cfg.site.is_empty() { "onedrive" } else { &cfg.site };
        Ok(std::sync::Arc::new(OneDrive { name: cfg.name.clone(), root, tab: Tab::new(hub, site, &["sharepoint.com"]) }))
    }

    async fn root(&self) -> Result<&str> {
        let r = self.root.get_or_try_init(|| async { self.tab.eval(JS, "window.__awmDrive.home", &[]).await.map(|v| v.as_str().unwrap_or_default().to_string()) });
        r.await.map(String::as_str)
    }

    async fn abs(&self, path: &str) -> Result<String> {
        let root = self.root().await?;
        Ok(match path.trim_matches('/') {
            "" => root.to_string(),
            p => format!("{root}/{p}"),
        })
    }

    async fn run(&self, f: &str, args: &[Value]) -> Result<Value> {
        self.tab.eval(JS, &format!("window.__awmDrive.{f}"), args).await.map_err(|e| {
            let code = e.message.split_once(": ").and_then(|(_, rest)| rest.split(':').next()).unwrap_or_default();
            let kinds: [(&str, fn(String) -> VerbError); 4] = [("ENOENT", not_found), ("EISDIR", is_a_dir), ("ENOTDIR", not_a_dir), ("EINVAL", VerbError::value)];
            match kinds.iter().find(|(c, _)| *c == code) {
                Some((_, kind)) => kind(e.message),
                None => e,
            }
        })
    }

    /// A page entry, its server-relative path made relative to the root.
    fn entry(&self, v: Value) -> Entry {
        let s = |k: &str| v[k].as_str().unwrap_or_default().to_string();
        let path = s("path");
        let rel = path.strip_prefix(self.root.get().map(String::as_str).unwrap_or_default()).map(|p| p.trim_start_matches('/').to_string()).unwrap_or(path);
        Entry { name: s("name"), path: rel, is_dir: v["is_dir"].as_bool().unwrap_or(false), size: v["size"].as_i64().unwrap_or(0), mime: s("mime"), modified: s("modified"), id: s("id"), web_url: s("web_url") }
    }

    fn entries(&self, v: Value) -> Vec<Entry> {
        v.as_array().cloned().unwrap_or_default().into_iter().map(|e| self.entry(e)).collect()
    }

    async fn transfer(&self, src: &str, dst: &str, overwrite: bool, parents: bool, copy: bool) -> Result<Entry> {
        let from = self.stat(src).await?;
        match self.stat(dst).await {
            Ok(_) if !overwrite => return Err(exists(format!("bucket {:?}: {dst:?} exists (pass overwrite)", self.name))),
            Ok(_) => self.rm(dst).await?,
            Err(e) if e.error_class == "FileNotFoundError" => {}
            Err(e) => return Err(e),
        }
        if parents {
            self.mkdir(dst.rsplit_once('/').map(|(p, _)| p).unwrap_or("")).await?;
        }
        self.run("transfer", &[json!(self.abs(src).await?), json!(self.abs(dst).await?), json!(from.is_dir), json!(copy)]).await?;
        self.stat(dst).await
    }
}

#[async_trait]
impl Bucket for OneDrive {
    fn name(&self) -> &str {
        &self.name
    }

    async fn ls(&self, path: &str) -> Result<Vec<Entry>> {
        Ok(self.entries(self.run("ls", &[json!(self.abs(path).await?)]).await?))
    }

    async fn stat(&self, path: &str) -> Result<Entry> {
        Ok(self.entry(self.run("stat", &[json!(self.abs(path).await?)]).await?))
    }

    async fn get(&self, path: &str, dest: &Path) -> Result<Got> {
        let f = tab::file(&self.run("get", &[json!(self.abs(path).await?), json!(MAX_FILE_BYTES)]).await?)?;
        let local = dest.join(super::local_segment(&f.filename));
        tokio::fs::write(&local, &f.data).await.map_err(|x| VerbError::runtime(format!("{}: {x}", local.display())))?;
        Ok(Got { size: f.data.len() as i64, filename: f.filename, mime: f.mime, path: local.display().to_string() })
    }

    async fn put(&self, path: &str, src: &Path, _mime: Option<&str>, parents: bool) -> Result<Entry> {
        let data = tokio::fs::read(src).await.map_err(|x| not_found(format!("{}: {x}", src.display())))?;
        if data.len() as u64 > MAX_FILE_BYTES {
            return Err(VerbError::value(format!("{} is {} bytes; the browser path carries {MAX_FILE_BYTES} at most", src.display(), data.len())));
        }
        Ok(self.entry(self.run("put", &[json!(self.abs(path).await?), json!(tab::b64(&data)), json!(parents)]).await?))
    }

    async fn rm(&self, path: &str) -> Result<()> {
        self.run("rm", &[json!(self.abs(path).await?)]).await.map(|_| ())
    }

    async fn mkdir(&self, path: &str) -> Result<Entry> {
        Ok(self.entry(self.run("mkdir", &[json!(self.abs(path).await?)]).await?))
    }

    async fn mv(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry> {
        self.transfer(src, dst, overwrite, parents, false).await
    }

    async fn cp(&self, src: &str, dst: &str, overwrite: bool, parents: bool) -> Result<Entry> {
        self.transfer(src, dst, overwrite, parents, true).await
    }

    async fn search(&self, query: &str, limit: usize, path: &str) -> Result<Vec<Entry>> {
        Ok(self.entries(self.run("search", &[json!(query), json!(limit), json!(self.abs(path).await?)]).await?))
    }
}
