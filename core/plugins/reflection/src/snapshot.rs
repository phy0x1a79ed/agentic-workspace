//! The pre-redux snapshot of reflection's state, taken once before the Rust reflection first
//! writes it.

use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};

use awm_statecheck::decl::Decl;

/// `<AWM_DIR>/services/reflection`, where the Python reflection kept its state.
pub fn service_dir(awm_dir: &Path) -> PathBuf {
    awm_dir.join("services").join("reflection")
}

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &service_dir(awm_dir), awm_dir, |n| std::env::var(n).ok())
}

/// Take each state dir's snapshot that does not exist yet. A second process waits on the
/// lock, then finds the snapshot taken.
pub fn take(awm_dir: &Path) -> anyhow::Result<()> {
    let dir = service_dir(awm_dir);
    std::fs::create_dir_all(&dir)?;
    let lock = std::fs::File::open(&dir)?;
    // SAFETY: flock on a descriptor we own; it is released when `lock` closes.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let out = awm_statecheck::snapshot_once("reflection", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("reflection: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("reflection: snapshot: {n}");
    }
    Ok(())
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
