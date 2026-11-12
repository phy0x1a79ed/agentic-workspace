//! A site's tab in alcor's shared browser, driven through the browser's productivity-only
//! `service_cdp`. Page scripts run there with the site's own signed-in session.

use std::time::Duration;

use awm_chat::Downloaded;
use awm_plugin_sdk::{CallOpts, Hub, VerbError};
use base64::Engine;
use serde_json::{json, Value};

type R = Result<Value, VerbError>;

#[derive(Clone)]
pub struct Tab {
    hub: Hub,
    pub site: String,
    /// The hosts the tab is on once signed in. Anywhere else, such as a sign-in page, the page
    /// scripts are not run.
    hosts: &'static [&'static str],
}

impl Tab {
    pub fn new(hub: Hub, site: &str, hosts: &'static [&'static str]) -> Tab {
        Tab { hub, site: site.to_string(), hosts }
    }

    pub async fn cdp(&self, method: &str, params: Value) -> R {
        let opts = CallOpts { timeout: Duration::from_secs(120), ..CallOpts::default() };
        let args = json!({"site": self.site, "method": method, "params": params});
        self.hub.call_with("browser", "service_cdp", args, &opts).await.map_err(|e| VerbError::runtime(format!("browser {method} on {}: {e}", self.site)))
    }

    /// Run the `prelude` script, then await `f(args)` in the page and return its result. A tab
    /// still loading, as on its first use after Chrome starts, gets one more try.
    pub async fn eval(&self, prelude: &str, f: &str, args: &[Value]) -> R {
        match self.eval_once(prelude, f, args).await {
            Err(e) if e.message.contains("about:blank") || e.message.contains("context") => {
                tokio::time::sleep(Duration::from_secs(4)).await;
                self.eval_once(prelude, f, args).await
            }
            other => other,
        }
    }

    async fn eval_once(&self, prelude: &str, f: &str, args: &[Value]) -> R {
        let args: Vec<String> = args.iter().map(Value::to_string).collect();
        let off_site = format!("the {} tab is at ' + location.href.split('?')[0] + '; sign in to it through `browser viewer`", self.site);
        let expression = format!(
            "if (!{hosts}.some(h => location.hostname.endsWith(h))) throw new Error('{off_site}');\n{prelude}\n(async () => JSON.stringify(await {f}({})))()",
            args.join(", "),
            hosts = json!(self.hosts)
        );
        let r = self.cdp("Runtime.evaluate", json!({"expression": expression, "awaitPromise": true, "returnByValue": true})).await?;
        if let Some(ex) = r.get("exceptionDetails") {
            let why = ex.pointer("/exception/description").or_else(|| ex.get("text")).and_then(Value::as_str).unwrap_or("an error");
            let why = why.lines().next().unwrap_or(why).trim_start_matches("Error: ");
            return Err(VerbError::runtime(format!("{} page: {why}", self.site)));
        }
        let out = r.pointer("/result/value").and_then(Value::as_str).unwrap_or("null");
        serde_json::from_str(out).map_err(|e| VerbError::runtime(format!("{} page gave {out:.200}: {e}", self.site)))
    }

    pub async fn close(&self) {
        let _ = self.hub.call("browser", "service_close", json!({"site": self.site}), None).await;
    }
}

pub fn b64(data: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(data)
}

pub fn unb64(v: &Value) -> Result<Vec<u8>, VerbError> {
    base64::engine::general_purpose::STANDARD.decode(v.as_str().unwrap_or_default()).map_err(|e| VerbError::runtime(format!("a page sent bad base64: {e}")))
}

/// A `{filename, mime, b64}` file a page script returned.
pub fn file(v: &Value) -> Result<Downloaded, VerbError> {
    Ok(Downloaded { filename: v["filename"].as_str().unwrap_or("file").to_string(), mime: v["mime"].as_str().unwrap_or_default().to_string(), data: unb64(&v["b64"])? })
}

pub fn mime_of(name: &str) -> String {
    mime_guess::from_path(name).first_or_octet_stream().to_string()
}
