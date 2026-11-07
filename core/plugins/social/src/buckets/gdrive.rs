//! Stub: replaced by the real backend.

use crate::config::BucketConfig;

pub struct GoogleDrive;

impl GoogleDrive {
    pub fn new(_cfg: BucketConfig) -> anyhow::Result<std::sync::Arc<dyn super::Bucket>> {
        anyhow::bail!("not built yet")
    }
}
