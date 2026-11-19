//! Styles: one Trilium note per style, read at job time.
//!
//! A style note carries `#genaiStyle=<name>`, `#provider=<provider>` and optionally
//! `#size=<WxH>`. Its body is the style text, and its image attachments are the reference
//! media. The notes live under the note labelled `#genaiStyles`, so Tony edits them in Trilium.
//! Attachments are cached on disk by their blob id, which changes when their bytes change.

use std::path::{Path, PathBuf};
use std::time::Duration;

use awm_plugin_sdk::hub::Hub;
use base64::Engine;
use serde::Serialize;
use serde_json::{json, Value};

/// The note id of the folder `style_add` creates when Trilium has no `#genaiStyles` note.
const FOLDER_ID: &str = "genai_styles";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Ref {
    pub title: String,
    pub mime: String,
    pub blob: String,
    #[serde(skip)]
    pub attachment: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Style {
    pub name: String,
    pub provider: String,
    pub size: Option<String>,
    pub text: String,
    pub refs: Vec<Ref>,
    pub note_id: String,
}

fn label<'a>(note: &'a Value, name: &str) -> Option<&'a str> {
    note["attributes"].as_array()?.iter().find(|a| a["type"] == "label" && a["name"] == name).and_then(|a| a["value"].as_str())
}

/// The visible text of a Trilium HTML body: tags dropped, block ends as line breaks.
pub fn plain(html: &str) -> String {
    let mut out = String::new();
    let mut rest = html;
    while let Some(i) = rest.find('<') {
        out.push_str(&rest[..i]);
        let Some(j) = rest[i..].find('>') else { break };
        let tag = rest[i + 1..i + j].trim_start_matches('/').split([' ', '/']).next().unwrap_or("").to_ascii_lowercase();
        if ["p", "br", "li", "div", "h1", "h2", "h3"].contains(&tag.as_str()) && !out.ends_with('\n') && !out.is_empty() {
            out.push('\n');
        }
        rest = &rest[i + j + 1..];
    }
    out.push_str(rest);
    let out = out.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&#39;", "'").replace("&amp;", "&");
    out.lines().map(str::trim).filter(|l| !l.is_empty()).collect::<Vec<_>>().join("\n")
}

fn html(text: &str) -> String {
    text.lines().filter(|l| !l.trim().is_empty()).map(|l| format!("<p>{}</p>", l.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;"))).collect()
}

fn style_of(note: &Value, body: &str) -> Option<Style> {
    let refs = note["attachments"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|a| a["mime"].as_str().is_some_and(|m| m.starts_with("image/") || m.starts_with("audio/")))
        .map(|a| Ref { title: a["title"].as_str().unwrap_or("").into(), mime: a["mime"].as_str().unwrap_or("").into(), blob: a["blobId"].as_str().unwrap_or("").into(), attachment: a["attachmentId"].as_str().unwrap_or("").into() })
        .collect();
    Some(Style {
        name: label(note, "genaiStyle")?.to_string(),
        provider: label(note, "provider").unwrap_or("").to_string(),
        size: label(note, "size").filter(|s| !s.is_empty()).map(str::to_string),
        text: plain(body),
        refs,
        note_id: note["noteId"].as_str().unwrap_or("").to_string(),
    })
}

pub struct Styles {
    hub: Hub,
    cache: PathBuf,
}

impl Styles {
    pub fn new(hub: Hub, cache: PathBuf) -> Self {
        Self { hub, cache }
    }

    async fn trilium(&self, verb: &str, args: Value) -> Result<Value, String> {
        crate::provider::call(&self.hub, "trilium", verb, args, Duration::from_secs(60)).await
    }

    async fn search(&self, query: &str) -> Result<Vec<Value>, String> {
        Ok(self.trilium("note_search", json!({"query": query, "limit": 200})).await?["results"].as_array().cloned().unwrap_or_default())
    }

    pub async fn list(&self) -> Result<Vec<Style>, String> {
        let mut out = vec![];
        for hit in self.search("#genaiStyle").await? {
            let note = self.trilium("note_get", json!({"note_id": hit["noteId"], "content": false})).await?;
            out.extend(style_of(&note, ""));
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(out)
    }

    pub async fn get(&self, name: &str) -> Result<Style, String> {
        let hits = self.search(&format!("#genaiStyle=\"{}\"", name.replace('"', ""))).await?;
        let [hit] = hits.as_slice() else {
            return Err(if hits.is_empty() { format!("no style named {name:?}; `styles` lists them") } else { format!("{} notes carry #genaiStyle={name}", hits.len()) });
        };
        let note = self.trilium("note_get", json!({"note_id": hit["noteId"]})).await?;
        style_of(&note, note["content"].as_str().unwrap_or("")).ok_or_else(|| format!("style note {} has no #genaiStyle label", hit["noteId"]))
    }

    /// The style's reference files on disk, fetched once per blob.
    pub async fn ref_files(&self, style: &Style) -> Result<Vec<PathBuf>, String> {
        let mut out = vec![];
        for r in &style.refs {
            let ext = Path::new(&r.title).extension().and_then(|e| e.to_str()).unwrap_or("bin");
            let path = self.cache.join(format!("{}.{ext}", r.blob));
            if !path.is_file() {
                let got = self.trilium("attachment_get", json!({"attachment_id": r.attachment})).await?;
                let bytes = base64::engine::general_purpose::STANDARD.decode(got["content_b64"].as_str().ok_or_else(|| format!("attachment {} came back with no content", r.title))?).map_err(|e| format!("attachment {}: {e}", r.title))?;
                std::fs::create_dir_all(&self.cache).map_err(|e| e.to_string())?;
                let part = path.with_extension("part");
                std::fs::write(&part, bytes).and_then(|_| std::fs::rename(&part, &path)).map_err(|e| e.to_string())?;
            }
            out.push(path);
        }
        Ok(out)
    }

    /// Create a style note under the `#genaiStyles` folder, with its reference files attached.
    pub async fn add(&self, name: &str, provider: &str, text: &str, size: Option<&str>, refs: &[(String, String)]) -> Result<String, String> {
        if !self.search(&format!("#genaiStyle=\"{name}\"")).await?.is_empty() {
            return Err(format!("a style named {name:?} exists; edit its note in Trilium"));
        }
        let folder = match self.search("#genaiStyles").await?.first().and_then(|n| n["noteId"].as_str().map(str::to_string)) {
            Some(id) => id,
            None => {
                self.trilium("note_create", json!({"note_id": FOLDER_ID, "title": "GenAI Styles", "type": "book", "parent": "root", "labels": {"genaiStyles": ""}})).await?;
                self.trilium("attr_set", json!({"note_id": FOLDER_ID, "name": "agentWritable", "inheritable": true})).await?;
                FOLDER_ID.to_string()
            }
        };
        let mut labels = json!({"genaiStyle": name, "provider": provider});
        if let Some(size) = size {
            labels["size"] = json!(size);
        }
        let made = self.trilium("note_create", json!({"title": name, "content": html(text), "parent": folder, "labels": labels})).await?;
        let id = made["noteId"].as_str().ok_or_else(|| format!("note_create answered {made}"))?.to_string();
        for (filename, b64) in refs {
            self.trilium("attachment_put", json!({"note_id": id, "content_b64": b64, "title": filename})).await?;
        }
        Ok(id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_style_note_reads_back_its_labels_text_and_image_refs() {
        let note = json!({
            "noteId": "n1",
            "attributes": [{"type": "label", "name": "genaiStyle", "value": "rt-sticker"}, {"type": "label", "name": "provider", "value": "muse-image"}, {"type": "label", "name": "size", "value": "1472x1408"}],
            "attachments": [{"attachmentId": "a1", "title": "ref.webp", "mime": "image/webp", "blobId": "b1"}, {"attachmentId": "a2", "title": "notes.txt", "mime": "text/plain", "blobId": "b2"}],
        });
        let s = style_of(&note, "<p>Pink background &amp; thin lines.</p><p>Change only what the prompt names.</p>").unwrap();
        assert_eq!((s.name.as_str(), s.provider.as_str(), s.size.as_deref()), ("rt-sticker", "muse-image", Some("1472x1408")));
        assert_eq!(s.text, "Pink background & thin lines.\nChange only what the prompt names.");
        assert_eq!(s.refs.iter().map(|r| r.blob.as_str()).collect::<Vec<_>>(), ["b1"]);
        assert_eq!(plain(&html(&s.text)), s.text);
    }
}
