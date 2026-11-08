//! What the routine service asks of other plugins, behind one interface. The service calls
//! them only through the gateway, as plain calls: neither is a need. Tests replace
//! [`Outbound`] with a fake.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Hub};
use serde_json::{json, Value};

pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = anyhow::Result<T>> + Send + 'a>>;

/// The request to start a background session. An empty scope starts it in the lobby.
#[derive(Debug, Clone, PartialEq)]
pub struct Start {
    pub peer: String,
    pub project: String,
    pub scope: String,
    pub prompt: String,
    pub started_by: String,
    pub name: String,
}

pub trait Outbound: Send + Sync {
    /// Start a background session. Returns its job.
    fn start_session(&self, start: Start) -> BoxFut<'_, String>;
    /// Send the operator a message on their notice channel.
    fn notify_operator(&self, text: String) -> BoxFut<'_, ()>;
}

pub const OPERATOR_DM_ACCOUNT_ENV: &str = "AWM_OPERATOR_DM_ACCOUNT";
pub const OPERATOR_DM_CHANNEL_ENV: &str = "AWM_OPERATOR_DM_CHANNEL";
pub const DEFAULT_OPERATOR_DM_ACCOUNT: &str = "discord-bot";
/// unimatrix0#notifications, the channel when no setting names one (`awm.config.operator`).
pub const DEFAULT_SWARM_OPERATOR_DM: &str = "1522674357762261112";

/// The social account and channel that reach this node's operator.
pub fn operator_dm() -> (String, String) {
    let var = |n: &str| std::env::var(n).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
    (
        var(OPERATOR_DM_ACCOUNT_ENV).unwrap_or_else(|| DEFAULT_OPERATOR_DM_ACCOUNT.into()),
        var(OPERATOR_DM_CHANNEL_ENV).unwrap_or_else(|| DEFAULT_SWARM_OPERATOR_DM.into()),
    )
}

/// The real outbound, through this node's gateway.
pub struct GatewayOutbound {
    pub hub: Hub,
}

impl GatewayOutbound {
    async fn invoke(&self, tool: &str, verb: &str, args: Value, peer: Option<String>, timeout: Duration) -> anyhow::Result<Value> {
        let opts = CallOpts { peer, timeout, ..CallOpts::default() };
        let res = self.hub.call_with(tool, verb, args, &opts).await?;
        if let Some(err) = res.get("error").filter(|e| !e.is_null() && *e != &json!(false) && *e != &json!("")) {
            anyhow::bail!("{tool} {verb}: {}", err.as_str().map(str::to_string).unwrap_or_else(|| err.to_string()));
        }
        Ok(res)
    }
}

impl Outbound for GatewayOutbound {
    fn start_session(&self, s: Start) -> BoxFut<'_, String> {
        Box::pin(async move {
            let mut args = json!({"prompt": s.prompt, "started_by": s.started_by, "name": s.name, "model": "opus", "effort": "high"});
            if !s.scope.is_empty() {
                args["project"] = json!(s.project);
                args["scope"] = json!(s.scope);
            }
            let peer = if s.peer.is_empty() { "local".to_string() } else { s.peer };
            let res = self.invoke("reflection", "start_other", args, Some(peer), Duration::from_secs(120)).await?;
            match res.get("job").filter(|j| !j.is_null() && *j != &json!("")) {
                Some(j) => Ok(j.as_str().map(str::to_string).unwrap_or_else(|| j.to_string())),
                None => {
                    let shown: String = format!("start_other returned no job: {res}").chars().take(400).collect();
                    anyhow::bail!(shown)
                }
            }
        })
    }

    fn notify_operator(&self, text: String) -> BoxFut<'_, ()> {
        Box::pin(async move {
            let (account, channel) = operator_dm();
            let peer = awm_plugin_sdk::config::peer_env("AWM_SOCIAL_PEER");
            self.invoke("social", "send", json!({"account": account, "channel": channel, "text": text}), peer, Duration::from_secs(60)).await?;
            Ok(())
        })
    }
}
