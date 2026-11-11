//! Tony's Slack, Teams and Discord chats, read and staged through their signed-in tabs in
//! alcor's shared browser. `chat.js` makes every request with the page's own session.

use awm_chat::{value, Downloaded};
use awm_plugin_sdk::{Hub, VerbError};
use serde_json::{json, Value};

use crate::tab::{self, Tab};

const JS: &str = include_str!("chat.js");
/// Each site, the host its signed-in tab is on, and whether its tab closes after a read.
/// Discord opens only for the call that needs it.
const SITES: [(&str, &str, bool); 3] = [("slack", "app.slack.com", false), ("teams", "teams.microsoft.com", false), ("discord", "discord.com", true)];

type R = Result<Value, VerbError>;

/// One chat site's tab.
pub struct Chat {
    tab: Tab,
    close_after: bool,
}

impl Chat {
    pub fn new(hub: &Hub, site: &str) -> Result<Self, VerbError> {
        let Some(&(_, host, close_after)) = SITES.iter().find(|(s, ..)| *s == site) else {
            let names: Vec<&str> = SITES.iter().map(|(s, ..)| *s).collect();
            return Err(value(format!("site is one of {}, not {site:?}", names.join(", "))));
        };
        Ok(Chat { tab: Tab::new(hub.clone(), site, host), close_after })
    }

    /// Await the site's `window.__awmChat.<site>.<f>(args)` in the page.
    async fn run(&self, f: &str, args: &[Value]) -> R {
        self.tab.eval(JS, &format!("window.__awmChat.{}.{f}", self.tab.site), args).await
    }

    pub async fn chats(&self) -> R {
        self.run("list", &[]).await
    }

    /// The chat Tony means by `want`: its id, else the one name that matches it.
    pub async fn chat(&self, want: &str) -> R {
        let chats = self.chats().await?;
        pick(chats.as_array().map(Vec::as_slice).unwrap_or_default(), want).cloned()
    }

    pub async fn messages(&self, id: &str, limit: usize) -> R {
        self.run("messages", &[json!(id), json!(limit)]).await
    }

    pub async fn download(&self, id: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>, VerbError> {
        let got = self.run("download", &[json!(id), json!(message_id), json!(idx)]).await?;
        got.as_array().into_iter().flatten().map(tab::file).collect()
    }

    /// Open the chat in the tab and put `files` in its compose box. Returns how, and a
    /// screenshot of the tab as it was left.
    pub async fn stage(&self, id: &str, files: &[(String, Vec<u8>)]) -> Result<(Value, Downloaded), VerbError> {
        let url = self.run("url", &[json!(id)]).await?;
        self.tab.cdp("Page.navigate", json!({"url": url})).await?;
        self.tab.cdp("Page.bringToFront", json!({})).await?;
        let files: Vec<Value> = files
            .iter()
            .map(|(name, data)| json!({"name": name, "mime": tab::mime_of(name), "b64": tab::b64(data)}))
            .collect();
        let args = [json!(self.tab.site), json!(files)];
        let staged = self.tab.eval(JS, "window.__awmChat.stage", &args).await?;
        let shot = self.tab.cdp("Page.captureScreenshot", json!({"format": "png"})).await?;
        Ok((staged, Downloaded { filename: format!("staged-{}.png", self.tab.site), mime: "image/png".into(), data: tab::unb64(&shot["data"])? }))
    }

    /// Run a read, then close the tab when the site says so. A staged tab stays open for Tony.
    pub async fn read<T>(&self, f: impl std::future::Future<Output = Result<T, VerbError>>) -> Result<T, VerbError> {
        let out = f.await;
        if self.close_after {
            self.tab.close().await;
        }
        out
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
