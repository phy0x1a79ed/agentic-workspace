//! MIGRATION(session-ids): the one-time mapping of a job's eight-hex short id to its session id.
//! Delete with every `MIGRATION(session-ids)` block once no store holds a short id.

use awm_plugin_sdk::ids::{Platform, SessionId};
use serde_json::Value;

use super::ClaudeHome;

/// The session id `jobs/<short>/state.json` records, or None when the job has no readable state.
// MIGRATION(session-ids): the only short-id to session-id map.
pub fn canonical_of_job(home: &ClaudeHome, short: &str) -> Option<SessionId> {
    let state: Value = serde_json::from_slice(&std::fs::read(home.job_state(short)).ok()?).ok()?;
    SessionId::from_harness_native(Platform::Claude, state.get("sessionId")?.as_str()?).ok()
}

/// Whether `jobs/` can be listed: a migration drops nothing when it cannot.
// MIGRATION(session-ids)
pub fn jobs_readable(home: &ClaudeHome) -> bool {
    std::fs::read_dir(home.jobs()).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_a_job_maps_to_the_session_its_state_names() {
        let dir = tempfile::TempDir::new().unwrap();
        let home = ClaudeHome::at(dir.path(), dir.path().join(".claude.json"));
        std::fs::create_dir_all(home.jobs().join("abababab")).unwrap();
        std::fs::write(home.job_state("abababab"), r#"{"sessionId":"abababab-0000-4000-8000-000000000000"}"#).unwrap();
        let want = SessionId::parse("ccabababab000040008000000000000000").ok();
        assert_eq!(canonical_of_job(&home, "abababab"), want);
        assert_eq!(canonical_of_job(&home, "cdcdcdcd"), None);
        assert!(jobs_readable(&home));
    }
}
