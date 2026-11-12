//! Provider settings: identity, capacity, routes to other providers and pricing defaults.

use baitan_proto::{PropSet, ProviderId};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ProviderConfig {
    pub id: ProviderId,
    /// Empty means `~/.baitan/<id>`.
    pub state_dir: PathBuf,
    /// Registered properties; an offer's place type must be a subset of them.
    pub props: PropSet,
    pub versions: [u32; 2],
    /// Concurrent jobs the provider prices its queue against.
    pub slots: u32,
    pub placement_only: bool,
    /// Provider id to `[user@]host` for rsync over ssh. Empty or `local` means the same filesystem.
    pub routes: BTreeMap<ProviderId, String>,
    /// Bytes per second over a non-local route, for pricing moves.
    pub bandwidth: f64,
    /// Fixed seconds per non-local transfer.
    pub route_overhead_s: f64,
    /// Seconds a step is expected to run when its spec has no `est`.
    pub default_secs: f64,
    /// Used instead of searching PATH and `~/.cargo/bin`; a missing file means no cargo.
    pub cargo: Option<PathBuf>,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        ProviderConfig {
            id: String::new(),
            state_dir: PathBuf::new(),
            props: PropSet::new(),
            versions: [baitan_proto::SUPPORTED[0], baitan_proto::SUPPORTED[1]],
            slots: 4,
            placement_only: false,
            routes: BTreeMap::new(),
            bandwidth: 100e6,
            route_overhead_s: 1.0,
            default_secs: 60.0,
            cargo: None,
        }
    }
}

impl ProviderConfig {
    pub fn new(id: &str) -> Self {
        ProviderConfig { id: id.into(), ..Default::default() }
    }

    pub fn resolved_state_dir(&self) -> PathBuf {
        if !self.state_dir.as_os_str().is_empty() {
            return self.state_dir.clone();
        }
        PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".baitan").join(&self.id)
    }
}
