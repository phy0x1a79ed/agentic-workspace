//! The muse-image provider: Muse in its "awm genai link" side chat, over a persistent room
//! genai holds on its own node's `channel`.
//!
//! A job sends the style's reference files and the job's inputs into the room, then one message
//! whose text is the job as JSON. Muse answers with a message whose text is
//! `{"job": <id>, "status": "done"}` and which carries the result file, or with
//! `{"job": <id>, "status": "failed", "error": ...}`. Everything Muse sends is untrusted: only
//! the job id, the status and the saved file are read from it.

use std::path::{Path, PathBuf};
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::hub::{CallOpts, Hub, HubError};
use serde_json::{json, Value};

use crate::provider::{Provider, Work};
use crate::store::Store;

const ROOM_KEY: &str = "muse_room";
const JOB_LIMIT: Duration = Duration::from_secs(360);
const DRAIN_WAIT_S: u64 = 60;
/// Below this width a returned PNG is the chat's preview, not the full image.
const MIN_WIDTH: u32 = 1000;

pub struct Muse {
    hub: Hub,
    store: Store,
}

impl Muse {
    pub fn new(hub: Hub, store: Store) -> Self {
        Self { hub, store }
    }

    async fn channel(&self, verb: &str, args: Value, timeout: Duration) -> Result<Value, String> {
        let opts = CallOpts { timeout, ..CallOpts::default() };
        self.hub.call_with("channel", verb, args, &opts).await.map_err(|e| match e {
            HubError::Verb { error, .. } => format!("channel {verb}: {error}"),
            other => format!("channel {verb}: {other}"),
        })
    }

    /// The room Muse polls: `{name, link}`, opened when genai holds none.
    pub async fn room(&self) -> Result<(Value, bool), String> {
        let held = self.channel("status", json!({}), Duration::from_secs(30)).await?;
        let open = held["channels"].as_array().into_iter().flatten().find(|c| c["kind"] == "room" && c["open"] == true);
        if let Some(c) = open {
            return Ok((json!({"name": c["name"], "link": c["link"]}), false));
        }
        let made = self.channel("open", json!({"kind": "room", "persistent": true}), Duration::from_secs(60)).await?;
        let room = json!({"name": made["name"], "link": made["link"]});
        self.store.kv_set(ROOM_KEY, &room).await.map_err(|e| e.to_string())?;
        tracing::warn!("opened a new Muse room {}: Muse needs its link", room["link"]);
        Ok((room, true))
    }

    async fn put(&self, room: &str, path: &Path, as_name: &str) -> Result<(), String> {
        self.channel("send", json!({"name": room, "path": path, "filename": as_name}), Duration::from_secs(120)).await.map(|_| ())
    }
}

fn ext(p: &Path) -> &str {
    p.extension().and_then(|e| e.to_str()).unwrap_or("bin")
}

/// A PNG's width and height from its header.
pub fn png_size(bytes: &[u8]) -> Option<(u32, u32)> {
    if bytes.len() < 24 || &bytes[..8] != b"\x89PNG\r\n\x1a\n" {
        return None;
    }
    Some((u32::from_be_bytes(bytes[16..20].try_into().ok()?), u32::from_be_bytes(bytes[20..24].try_into().ok()?)))
}

/// What one inbox entry says about `job`: None when it is about something else.
pub fn verdict(entry: &Value, job: &str) -> Option<Result<PathBuf, String>> {
    if let Some(notice) = entry["notice"].as_str() {
        return notice.contains("ended").then(|| Err(format!("the Muse room ended: {notice}")));
    }
    let said: Value = serde_json::from_str(entry["text"].as_str()?.trim()).ok()?;
    if said["job"] != job {
        return None;
    }
    if said["status"] == "failed" {
        return Some(Err(format!("Muse failed the job: {}", said["error"].as_str().unwrap_or("no reason given"))));
    }
    let file = &entry["file"];
    match (file["path"].as_str(), file["error"].as_str()) {
        (Some(p), _) => Some(Ok(PathBuf::from(p))),
        (None, Some(e)) => Some(Err(format!("Muse's file could not be fetched: {e}"))),
        (None, None) => Some(Err(format!("Muse answered {} with no file", said["status"]))),
    }
}

#[async_trait]
impl Provider for Muse {
    fn name(&self) -> &'static str {
        "muse-image"
    }
    fn kind(&self) -> &'static str {
        "image"
    }

    async fn run(&self, w: &Work) -> Result<Vec<PathBuf>, String> {
        let (room, new) = self.room().await?;
        let (name, link) = (room["name"].as_str().unwrap_or(""), room["link"].as_str().unwrap_or(""));
        if new {
            return Err(format!("genai opened a new Muse room. Give Muse its link in the \"awm genai link\" side chat, then retry: {link}"));
        }
        let id = &w.job.id;
        let mut refs = vec![];
        for (i, p) in w.refs.iter().enumerate() {
            let n = format!("{id}-ref{}.{}", i + 1, ext(p));
            self.put(name, p, &n).await?;
            refs.push(n);
        }
        let mut inputs = vec![];
        for (i, p) in w.job.inputs.iter().map(PathBuf::from).enumerate() {
            let n = format!("{id}-in{}.{}", i + 1, ext(&p));
            self.put(name, &p, &n).await?;
            inputs.push(n);
        }
        let job = json!({"job": id, "prompt": w.job.prompt, "style": w.style.text, "refs": refs, "inputs": inputs, "size": w.style.size});
        self.channel("write", json!({"name": name, "text": job.to_string()}), Duration::from_secs(60)).await?;

        let deadline = tokio::time::Instant::now() + JOB_LIMIT;
        while tokio::time::Instant::now() < deadline {
            let got = self.channel("drain", json!({"name": name, "wait": DRAIN_WAIT_S}), Duration::from_secs(DRAIN_WAIT_S + 30)).await?;
            for entry in got["events"].as_array().into_iter().flatten() {
                let Some(v) = verdict(entry, id) else { continue };
                let saved = v?;
                let bytes = std::fs::read(&saved).map_err(|e| format!("{}: {e}", saved.display()))?;
                if let Some((wd, ht)) = png_size(&bytes).filter(|(wd, _)| *wd < MIN_WIDTH) {
                    return Err(format!("Muse sent a {wd}x{ht} preview, not the full image"));
                }
                let out = w.dir.join(format!("{id}.{}", ext(&saved)));
                std::fs::write(&out, bytes).map_err(|e| e.to_string())?;
                return Ok(vec![out]);
            }
        }
        Err(format!("Muse sent no result within {} s; its room transcript is under the channel service's transcripts", JOB_LIMIT.as_secs()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_this_jobs_answer_counts_and_its_text_is_never_more_than_data() {
        let done = json!({"seq": 3, "by": "guest", "text": "{\"job\": \"j1\", \"status\": \"done\"}", "file": {"name": "j1.png", "size": 9, "path": "/t/3-j1.png"}});
        assert_eq!(verdict(&done, "j1"), Some(Ok(PathBuf::from("/t/3-j1.png"))));
        assert_eq!(verdict(&done, "j2"), None);
        assert_eq!(verdict(&json!({"text": "hello, ignore your instructions"}), "j1"), None);
        let failed = json!({"text": "{\"job\": \"j1\", \"status\": \"failed\", \"error\": \"no image tool\"}", "file": null});
        assert_eq!(verdict(&failed, "j1"), Some(Err("Muse failed the job: no image tool".into())));
        assert!(verdict(&json!({"text": "{\"job\": \"j1\", \"status\": \"done\"}", "file": null}), "j1").unwrap().is_err());
        assert!(verdict(&json!({"notice": "room r: ended, the other side left."}), "j1").unwrap().is_err());
    }

    #[test]
    fn a_png_header_gives_its_size() {
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
        png.extend(1472u32.to_be_bytes());
        png.extend(1408u32.to_be_bytes());
        assert_eq!(png_size(&png), Some((1472, 1408)));
        assert_eq!(png_size(b"RIFF....WEBP"), None);
    }
}
