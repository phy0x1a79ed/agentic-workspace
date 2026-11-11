//! The verbs behind [`crate::dispatch`], each already admitted.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use awm_chat::slack::api::Client;
use awm_chat::{chan, msg, value, MAX_INLINE_BYTES};
use awm_plugin_sdk::{Call, VerbError};
use base64::Engine;
use serde_json::{json, Value};

use crate::buckets::{self, Bucket};
use crate::config::{BucketConfig, Loaded};
use crate::gmail::{Draft, DraftFile, Endpoints, GmailConnector};
use crate::slack::SlackUser;
use crate::Account;

const PROBE_TIMEOUT_S: u64 = 20;

type R = Result<Value, VerbError>;

pub struct Productivity {
    pub here: String,
    pub callers: Vec<String>,
    accounts: BTreeMap<String, Arc<dyn Account>>,
    buckets: Vec<(BucketConfig, Option<Arc<dyn Bucket>>)>,
}

fn s(v: &Value, key: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::String(x)) => x.clone(),
        Some(other) => other.to_string(),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(x)) => !x.is_empty() && x != "false" && x != "0",
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    }
}

fn int(v: Option<&Value>, default: i64) -> i64 {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(default),
        Some(Value::String(x)) if !x.is_empty() => x.parse().unwrap_or(default),
        _ => default,
    }
}

fn idx(args: &Value) -> Option<i64> {
    match args.get("idx") {
        None | Some(Value::Null) => None,
        Some(Value::String(x)) if x.is_empty() => None,
        v => Some(int(v, 0)),
    }
}

fn rows(msgs: &[awm_chat::Inbound]) -> Value {
    let rows: Vec<Value> = msgs.iter().map(|m| msg(m, "")).collect();
    json!({"count": rows.len(), "messages": rows})
}

/// A probe's error text, or empty when it answered in time.
async fn probed(probe: impl std::future::Future<Output = Result<(), VerbError>>) -> String {
    match tokio::time::timeout(Duration::from_secs(PROBE_TIMEOUT_S), probe).await {
        Err(_) => format!("no answer within {PROBE_TIMEOUT_S}s"),
        Ok(Err(e)) => format!("{}: {}", e.error_class, e.message),
        Ok(Ok(())) => String::new(),
    }
}

/// The files a verb was handed: `{filename, b64}` entries the caller's gateway inlined, or
/// `{filename, path}` on this node.
fn handed_files(args: &Value) -> Result<Vec<(String, Vec<u8>)>, VerbError> {
    let mut out = vec![];
    for f in args.get("files").and_then(Value::as_array).into_iter().flatten() {
        let name = s(f, "filename");
        let data = if let Some(b) = f.get("b64").and_then(Value::as_str) {
            base64::engine::general_purpose::STANDARD.decode(b.trim()).map_err(|e| value(format!("bad base64 for {name:?}: {e}")))?
        } else if let Some(p) = f.get("path").and_then(Value::as_str) {
            std::fs::read(p).map_err(|e| VerbError::new("FileNotFoundError", format!("{p}: {e}")))?
        } else {
            continue;
        };
        if name.is_empty() {
            return Err(value(format!("a file needs a filename: {f}")));
        }
        out.push((name, data));
    }
    Ok(out)
}

impl Productivity {
    pub fn new(here: String, loaded: Loaded) -> Self {
        let mut accounts: BTreeMap<String, Arc<dyn Account>> = BTreeMap::new();
        for a in loaded.accounts.into_iter().filter(|a| a.enabled) {
            let acct: Arc<dyn Account> = match a.platform.as_str() {
                "gmail" => Arc::new(GmailConnector::new(a.clone(), Endpoints::from_env())),
                _ => Arc::new(SlackUser::new(a.clone(), &Client::base_from_env())),
            };
            accounts.insert(a.name, acct);
        }
        let buckets = loaded
            .buckets
            .into_iter()
            .map(|c| {
                let b = buckets::build(&c).map_err(|e| tracing::warn!("bucket {} failed to build: {e}", c.name)).ok();
                (c, b)
            })
            .collect();
        Self { here, callers: loaded.callers, accounts, buckets }
    }

    fn account(&self, call: &Call) -> Result<&Arc<dyn Account>, VerbError> {
        let name = s(&call.args, "account");
        self.accounts.get(&name).ok_or_else(|| value(format!("no account {name:?}; `productivity accounts` lists them")))
    }

    // -- mail and chat ---------------------------------------------------------------

    pub async fn accounts(&self) -> R {
        let probes = self.accounts.iter().map(|(name, a)| async move {
            let reason = probed(a.probe()).await;
            let mut row = json!({"name": name, "platform": a.platform(), "live": reason.is_empty()});
            if !reason.is_empty() {
                row["error"] = json!(reason);
            }
            row
        });
        Ok(json!({"accounts": futures_util::future::join_all(probes).await}))
    }

    pub async fn channels(&self, call: Call) -> R {
        let c = self.account(&call)?.list_channels(truthy(call.args.get("include_dms"))).await?;
        Ok(json!({"channels": c.iter().map(chan).collect::<Vec<_>>()}))
    }

    pub async fn fetch(&self, call: Call) -> R {
        let before = Some(s(&call.args, "before")).filter(|b| !b.is_empty());
        let msgs = self.account(&call)?.fetch(&s(&call.args, "channel"), int(call.args.get("limit"), 50) as usize, before.as_deref()).await?;
        Ok(rows(&msgs))
    }

    pub async fn search(&self, call: Call) -> R {
        let ch = Some(s(&call.args, "channel")).filter(|c| !c.is_empty());
        let msgs = self.account(&call)?.search(&s(&call.args, "query"), int(call.args.get("limit"), 50) as usize, ch.as_deref()).await?;
        Ok(rows(&msgs))
    }

    /// The attachments as `b64` files, which the caller's gateway writes to disk on its node.
    pub async fn download_attachments(&self, call: Call) -> R {
        let files = self.account(&call)?.download_attachments(&s(&call.args, "channel"), &s(&call.args, "message_id"), idx(&call.args)).await?;
        awm_chat::inline_files(&files, &self.here)
    }

    pub async fn draft(&self, call: Call) -> R {
        let Some(gmail) = self.account(&call)?.as_gmail() else {
            return Err(value("draft takes a gmail account; a chat file is staged through the browser"));
        };
        let mut files: Vec<DraftFile> = handed_files(&call.args)?
            .into_iter()
            .map(|(filename, data)| DraftFile { mime: mime_guess::from_path(&filename).first_or_octet_stream().to_string(), filename, data })
            .collect();
        for f in call.args.get("bucket_files").and_then(Value::as_array).into_iter().flatten() {
            files.push(self.bucket_file(&s(f, "bucket"), &s(f, "path")).await?);
        }
        let size: usize = files.iter().map(|f| f.data.len()).sum();
        if size > MAX_INLINE_BYTES {
            return Err(value(format!("the files come to {size} bytes; Gmail takes {MAX_INLINE_BYTES} at most")));
        }
        let d = Draft {
            to: s(&call.args, "to"),
            cc: s(&call.args, "cc"),
            subject: s(&call.args, "subject"),
            body: s(&call.args, "body"),
            files,
            reply_to: Some(s(&call.args, "reply_to")).filter(|r| !r.is_empty()),
        };
        let count = d.files.len();
        let id = gmail.draft(d).await?;
        Ok(json!({"ok": true, "message_id": id, "files": count, "note": "saved to Gmail Drafts; Tony sends it"}))
    }

    // -- buckets ---------------------------------------------------------------------

    fn bucket(&self, name: &str) -> Result<Arc<dyn Bucket>, VerbError> {
        self.buckets.iter().find(|(c, _)| c.name == name).and_then(|(_, b)| b.clone()).ok_or_else(|| VerbError::runtime(format!("bucket {name:?} is not configured or failed to build")))
    }

    /// One bucket file's bytes, for a draft.
    async fn bucket_file(&self, bucket: &str, path: &str) -> Result<DraftFile, VerbError> {
        let dir = tempfile::Builder::new().prefix("awm-productivity-").tempdir().map_err(|e| VerbError::runtime(e.to_string()))?;
        let got = self.bucket(bucket)?.get(path, dir.path()).await?;
        let data = std::fs::read(&got.path).map_err(|e| VerbError::runtime(format!("{}: {e}", got.path)))?;
        Ok(DraftFile { filename: got.filename, mime: got.mime, data })
    }

    pub async fn buckets_verb(&self) -> R {
        let probes = self.buckets.iter().map(|(cfg, b)| async move {
            let reason = match b {
                None => "failed to build (see the productivity service log)".to_string(),
                Some(b) => probed(b.probe()).await,
            };
            let mut row = json!({"name": cfg.name, "kind": cfg.kind, "root": cfg.root, "live": reason.is_empty()});
            if !reason.is_empty() {
                row["error"] = json!(reason);
            }
            row
        });
        Ok(json!({"buckets": futures_util::future::join_all(probes).await}))
    }

    pub async fn bucket_ls(&self, call: Call) -> R {
        let b = self.bucket(&s(&call.args, "bucket"))?;
        let path = s(&call.args, "path");
        if !truthy(call.args.get("recursive")) {
            let e = b.ls(&path).await?;
            return Ok(json!({"entries": e, "count": e.len()}));
        }
        let (e, truncated) = buckets::walk(b.as_ref(), &path, int(call.args.get("limit"), 1000).max(1) as usize).await?;
        Ok(json!({"entries": e, "count": e.len(), "truncated": truncated}))
    }

    pub async fn bucket_stat(&self, call: Call) -> R {
        Ok(json!({"entry": self.bucket(&s(&call.args, "bucket"))?.stat(&s(&call.args, "path")).await?}))
    }

    pub async fn bucket_mkdir(&self, call: Call) -> R {
        Ok(json!({"entry": self.bucket(&s(&call.args, "bucket"))?.mkdir(&s(&call.args, "path")).await?}))
    }

    /// A file that fits one reply comes back inline, and the caller's gateway writes it on the
    /// caller's node. A bigger file or a folder stays in a temp dir here, on alcor.
    pub async fn bucket_get(&self, call: Call) -> R {
        let b = self.bucket(&s(&call.args, "bucket"))?;
        let dir = tempfile::Builder::new().prefix("awm-productivity-").tempdir().map_err(|e| VerbError::runtime(e.to_string()))?.keep();
        let path = s(&call.args, "path");
        let got = match b.get(&path, &dir).await {
            Ok(got) if got.size >= 0 && (got.size as usize) <= MAX_INLINE_BYTES => {
                let data = std::fs::read(&got.path).map_err(|e| VerbError::runtime(e.to_string()));
                let _ = std::fs::remove_dir_all(&dir);
                let file = awm_chat::Downloaded { filename: got.filename, mime: got.mime, data: data? };
                return awm_chat::inline_files(&[file], &self.here);
            }
            Ok(got) => serde_json::to_value(&got).unwrap(),
            Err(e) if e.error_class == "IsADirectoryError" => {
                let tree = buckets::get_tree(b.as_ref(), &path, &dir, int(call.args.get("limit"), 10000).max(1) as usize).await;
                match tree {
                    Ok(t) => t,
                    Err(e) => {
                        let _ = std::fs::remove_dir_all(&dir);
                        return Err(e);
                    }
                }
            }
            Err(e) => {
                let _ = std::fs::remove_dir_all(&dir);
                return Err(e);
            }
        };
        Ok(json!({"files": [got], "count": 1, "dir": dir.display().to_string(), "node": self.here}))
    }

    /// `files` (a file the caller's gateway inlined from `file`), else `src`, a path on alcor.
    pub async fn bucket_put(&self, call: Call) -> R {
        let b = self.bucket(&s(&call.args, "bucket"))?;
        let dest = s(&call.args, "path");
        let parents = truthy(call.args.get("parents"));
        let mime = Some(s(&call.args, "mime")).filter(|m| !m.is_empty());
        let handed = handed_files(&call.args)?;
        if let Some((name, data)) = handed.into_iter().next() {
            let dir = tempfile::Builder::new().prefix("awm-productivity-").tempdir().map_err(|e| VerbError::runtime(e.to_string()))?;
            let local = dir.path().join(awm_chat::safe_name(&name, 0));
            std::fs::write(&local, data).map_err(|e| VerbError::runtime(e.to_string()))?;
            return Ok(json!({"entry": b.put(&dest, &local, mime.as_deref(), parents).await?}));
        }
        let src = s(&call.args, "src");
        let p = Path::new(&src);
        if p.is_dir() {
            let res = buckets::put_tree(b.as_ref(), &dest, p, parents).await?;
            let mut out = json!({"entry": res["entry"], "count": res["count"]});
            if res["failures"].as_array().is_some_and(|f| !f.is_empty()) {
                out["failures"] = res["failures"].clone();
            }
            return Ok(out);
        }
        if !p.is_file() {
            return Err(VerbError::new("FileNotFoundError", format!("bucket_put: pass `file` (a path on your node) or `src` (a path on alcor); no local file or folder {src:?}")));
        }
        Ok(json!({"entry": b.put(&dest, p, mime.as_deref(), parents).await?}))
    }

    pub async fn bucket_rm(&self, call: Call) -> R {
        let path = s(&call.args, "path");
        if path.trim_matches('/').is_empty() {
            return Err(value("bucket_rm: name the file or folder to delete; the bucket root is never deleted"));
        }
        self.bucket(&s(&call.args, "bucket"))?.rm(&path).await?;
        Ok(json!({"ok": true, "path": path}))
    }

    pub async fn bucket_mv(&self, call: Call) -> R {
        let b = self.bucket(&s(&call.args, "bucket"))?;
        Ok(json!({"entry": b.mv(&s(&call.args, "src"), &s(&call.args, "dst"), truthy(call.args.get("overwrite")), truthy(call.args.get("parents"))).await?}))
    }

    pub async fn bucket_cp(&self, call: Call) -> R {
        let b = self.bucket(&s(&call.args, "bucket"))?;
        Ok(json!({"entry": b.cp(&s(&call.args, "src"), &s(&call.args, "dst"), truthy(call.args.get("overwrite")), truthy(call.args.get("parents"))).await?}))
    }

    pub async fn bucket_search(&self, call: Call) -> R {
        let e = self.bucket(&s(&call.args, "bucket"))?.search(&s(&call.args, "query"), int(call.args.get("limit"), 50) as usize, &s(&call.args, "path")).await?;
        Ok(json!({"entries": e, "count": e.len()}))
    }
}
