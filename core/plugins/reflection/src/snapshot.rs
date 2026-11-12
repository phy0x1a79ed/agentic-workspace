//! What the pre-redux state check reads of reflection's state.

use std::path::{Path, PathBuf};

use awm_statecheck::decl::Decl;

/// `<AWM_DIR>/services/reflection`, where the Python reflection kept its state.
pub fn service_dir(awm_dir: &Path) -> PathBuf {
    awm_dir.join("services").join("reflection")
}

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &service_dir(awm_dir), awm_dir, |n| std::env::var(n).ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_declaration_covers_the_state_dir_and_spares_cchs_old_promises() {
        let d = decl(Path::new("/a")).unwrap();
        assert_eq!(d.dirs, [PathBuf::from("/a/services/reflection")]);
        for gone in ["tickets/t-1.json", "tickets/t-1.closed", "waits.tmp", "pending/rec.json"] {
            assert!(d.is_transient_file(&Path::new("/a/services/reflection").join(gone)), "{gone}");
        }
        for kept in ["trust.json", "waits.json", "retired.json", "events.jsonl", "lost.jsonl"] {
            assert!(!d.is_transient_file(&Path::new("/a/services/reflection").join(kept)), "{kept}");
        }
    }
}
