//! Tony's Slack, Teams and Discord chats, read and staged through their signed-in tabs in
//! alcor's shared browser. `chat.js` makes every request with the page's own session.

use std::time::Duration;

use awm_chat::{value, Downloaded};
use awm_plugin_sdk::{Hub, VerbError};
use base64::Engine;
use serde_json::{json, Value};

use crate::tab::Tab;

const JS: &str = include_str!("chat.js");
const SITES: [&str; 3] = ["slack", "teams", "discord"];

type R = Result<Value, VerbError>;

/// One chat site's tab.
pub struct Chat {
    tab: Tab,
}

impl Chat {
    pub fn new(hub: &Hub, site: &str) -> Result<Self, VerbError> {
        if !SITES.contains(&site) {
            return Err(value(format!("site is one of {}, not {site:?}", SITES.join(", "))));
        }
        Ok(Chat { tab: Tab::new(hub.clone(), site) })
    }

    /// Await `window.__awmChat.<f>(args)` in the page.
    async fn run(&self, f: &str, args: &[Value]) -> R {
        self.tab.eval(JS, &format!("window.__awmChat.{f}"), args).await
    }

    fn site(&self) -> &str {
        &self.tab.site
    }

    pub async fn chats(&self) -> R {
        self.run(&format!("{}.list", self.site()), &[]).await
    }

    /// The chat Tony means by `want`: its id, else the one name that matches it.
    pub async fn chat(&self, want: &str) -> R {
        let chats = self.chats().await?;
        pick(chats.as_array().map(Vec::as_slice).unwrap_or_default(), want).cloned()
    }

    pub async fn messages(&self, id: &str, limit: usize) -> R {
        self.run(&format!("{}.messages", self.site()), &[json!(id), json!(limit)]).await
    }

    pub async fn download(&self, id: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>, VerbError> {
        let got = self.run(&format!("{}.download", self.site()), &[json!(id), json!(message_id), json!(idx)]).await?;
        got.as_array()
            .into_iter()
            .flatten()
            .map(|f| {
                let data = base64::engine::general_purpose::STANDARD.decode(f["b64"].as_str().unwrap_or_default()).map_err(|e| VerbError::runtime(e.to_string()))?;
                Ok(Downloaded { filename: f["filename"].as_str().unwrap_or("attachment").to_string(), mime: f["mime"].as_str().unwrap_or_default().to_string(), data })
            })
            .collect()
    }

    /// Open the chat in the tab and put `files` in its compose box. Returns how, and a
    /// screenshot of the tab as it was left.
    pub async fn stage(&self, id: &str, files: &[(String, Vec<u8>)]) -> Result<(Value, Downloaded), VerbError> {
        let url = self.run(&format!("{}.url", self.site()), &[json!(id)]).await?;
        self.tab.cdp("Page.navigate", json!({"url": url})).await?;
        self.tab.cdp("Page.bringToFront", json!({})).await?;
        let files: Vec<Value> = files
            .iter()
            .map(|(name, data)| json!({"name": name, "mime": mime_guess::from_path(name).first_or_octet_stream().to_string(), "b64": base64::engine::general_purpose::STANDARD.encode(data)}))
            .collect();
        let args = [json!(self.site()), json!(files)];
        // The navigation swaps the page out from under the first try now and then.
        let staged = match self.run("stage", &args).await {
            Err(e) if e.message.contains("context") => {
                tokio::time::sleep(Duration::from_secs(3)).await;
                self.run("stage", &args).await?
            }
            other => other?,
        };
        let shot = self.tab.cdp("Page.captureScreenshot", json!({"format": "png"})).await?;
        let png = base64::engine::general_purpose::STANDARD.decode(shot["data"].as_str().unwrap_or_default()).map_err(|e| VerbError::runtime(e.to_string()))?;
        Ok((staged, Downloaded { filename: format!("staged-{}.png", self.site()), mime: "image/png".into(), data: png }))
    }

    /// Discord opens only for the call that needs it.
    pub async fn done(&self) {
        if self.site() == "discord" {
            self.tab.close().await;
        }
    }
}

/// The one chat `want` names: an exact id, else the one exact name, else the one name that
/// contains it. Ambiguity is refused, never guessed.
pub fn pick<'c>(chats: &'c [Value], want: &str) -> Result<&'c Value, VerbError> {
    let name = |c: &Value| c["name"].as_str().unwrap_or_default().to_lowercase();
    let w = want.trim().to_lowercase();
    if let Some(c) = chats.iter().find(|c| c["id"] == want) {
        return Ok(c);
    }
    let exact: Vec<&Value> = chats.iter().filter(|c| name(c) == w).collect();
    let hits = if exact.is_empty() { chats.iter().filter(|c| !w.is_empty() && name(c).contains(&w)).collect() } else { exact };
    match hits.as_slice() {
        [one] => Ok(one),
        [] => Err(value(format!("no chat matches {want:?}; chat_read without `chat` lists them"))),
        many => {
            let named: Vec<String> = many.iter().take(10).map(|c| format!("{} ({})", c["name"].as_str().unwrap_or_default(), c["id"].as_str().unwrap_or_default())).collect();
            Err(value(format!("{want:?} matches {} chats: {}; pass the id", many.len(), named.join(", "))))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::pick;
    use serde_json::json;

    #[test]
    fn pick_takes_an_id_or_one_name_and_refuses_ambiguity() {
        let chats = [json!({"id": "D1", "name": "Ada Lovelace"}), json!({"id": "D2", "name": "Ada"}), json!({"id": "D3", "name": "Adam Smith"})];
        assert_eq!(pick(&chats, "D3").unwrap()["id"], "D3");
        assert_eq!(pick(&chats, "ada").unwrap()["id"], "D2");
        assert_eq!(pick(&chats, "smith").unwrap()["id"], "D3");
        assert!(pick(&chats, "lov").is_ok());
        assert!(pick(&chats, "ad").unwrap_err().message.contains("matches 3"));
        assert!(pick(&chats, "zed").is_err());
        assert!(pick(&chats, "").is_err());
    }
}
