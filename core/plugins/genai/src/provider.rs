//! The provider seam: one module per provider, registered in [`registry`].

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;

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
pub fn registry(hub: &awm_plugin_sdk::hub::Hub, store: &crate::store::Store) -> Vec<Arc<dyn Provider>> {
    vec![Arc::new(crate::muse::Muse::new(hub.clone(), store.clone())), Arc::new(ElevenLabsVoice)]
}
