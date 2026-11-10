//! The pool ledger: which sessions the warm pool made, keyed by canonical session id, and where
//! each stands. A session's name carries none of this.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use awm_plugin_sdk::ids::{Platform, SessionId};
use serde::{Deserialize, Serialize};

/// Where a pool session stands: waiting to be handed out, handed out and not yet named by its
/// first request, or named and left alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    Spare,
    Claimed,
    Kept,
}

pub type Ledger = BTreeMap<String, Stage>;

/// The canonical id of a Claude session UUID.
pub fn key(uuid: &str) -> Option<String> {
    SessionId::canonical_of_native(Platform::Claude, uuid)
}

/// The ledger at `path`; a missing or unreadable file is an empty ledger.
pub fn load(path: &Path) -> Ledger {
    fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

fn save(path: &Path, ledger: &Ledger) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4().simple()));
    let written = fs::write(&tmp, serde_json::to_vec_pretty(ledger).map_err(std::io::Error::other)?)
        .and_then(|()| fs::rename(&tmp, path));
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    written
}

/// Record `id` at `stage`.
pub fn set(path: &Path, id: &str, stage: Stage) -> std::io::Result<()> {
    let mut ledger = load(path);
    ledger.insert(id.to_string(), stage);
    save(path, &ledger)
}

/// Drop `id`.
pub fn remove(path: &Path, id: &str) {
    retain(path, |k| k != id);
}

/// Keep only the entries `keep` accepts; writes nothing when nothing goes.
pub fn retain(path: &Path, keep: impl Fn(&str) -> bool) {
    let mut ledger = load(path);
    let before = ledger.len();
    ledger.retain(|k, _| keep(k));
    if ledger.len() != before {
        let _ = save(path, &ledger);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_the_ledger_round_trips_and_forgets() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("sub").join("pool.json");
        assert!(load(&path).is_empty());
        let id = key("00000000-0000-4000-8000-000000000001").unwrap();
        set(&path, &id, Stage::Spare).unwrap();
        set(&path, &id, Stage::Claimed).unwrap();
        assert_eq!(load(&path), Ledger::from([(id.clone(), Stage::Claimed)]));
        assert!(fs::read_to_string(&path).unwrap().contains("\"claimed\""));
        remove(&path, &id);
        assert!(load(&path).is_empty());
    }
}
