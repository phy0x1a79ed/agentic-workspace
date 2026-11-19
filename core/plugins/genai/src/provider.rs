//! The provider seam: one module per provider, registered in [`registry`].

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use awm_plugin_sdk::hub::{CallOpts, Hub};
use serde_json::Value;

use crate::store::Job;
use crate::styles::Style;

/// One job as a provider gets it: the style resolved, its reference files on disk, and the
/// directory the results go in.
pub struct Work {
    pub job: Job,
    pub style: Style,
    pub refs: Vec<PathBuf>,
    pub dir: PathBuf,
}

#[async_trait]
pub trait Provider: Send + Sync {
    /// The name a style's `#provider` label gives.
    fn name(&self) -> &'static str;
    /// `image` or `voice`.
    fn kind(&self) -> &'static str;
    /// What `status` shows about the provider's link, if it has one.
    async fn status(&self) -> Value {
        Value::Null
    }
    /// Run one job to its result files, each under `work.dir`. An error fails the job with that text.
    async fn run(&self, work: &Work) -> Result<Vec<PathBuf>, String>;
}

/// A voice provider that is not set up yet: it proves a second kind fits the seam.
pub struct ElevenLabsVoice;

#[async_trait]
impl Provider for ElevenLabsVoice {
    fn name(&self) -> &'static str {
        "elevenlabs-voice"
    }
    fn kind(&self) -> &'static str {
        "voice"
    }
    async fn run(&self, _: &Work) -> Result<Vec<PathBuf>, String> {
        Err("the elevenlabs-voice provider is not set up yet".into())
    }
}

/// Every provider genai serves. A new provider is one module and one line here.
pub fn registry(hub: &Hub) -> Vec<Arc<dyn Provider>> {
    vec![Arc::new(crate::muse::Muse::new(hub.clone())), Arc::new(ElevenLabsVoice)]
}

/// One verb of another service, its failure as text naming the verb.
pub async fn call(hub: &Hub, tool: &str, verb: &str, args: Value, timeout: Duration) -> Result<Value, String> {
    hub.call_with(tool, verb, args, &CallOpts { timeout, ..CallOpts::default() }).await.map_err(|e| format!("{tool} {verb}: {e}"))
}
