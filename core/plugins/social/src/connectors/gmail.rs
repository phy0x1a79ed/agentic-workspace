//! Stub: replaced by the real connector.

use std::sync::Arc;

use crate::config::AccountConfig;
use super::{Connector, Hooks};

pub struct GmailConnector;

impl GmailConnector {
    pub fn new(_cfg: AccountConfig, _hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
        anyhow::bail!("not built yet")
    }
}
