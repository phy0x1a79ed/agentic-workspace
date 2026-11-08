//! The pre-redux snapshot of cch's state, taken once before the Rust cch first writes it.

use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};

use awm_statecheck::decl::Decl;

fn service_dir(awm_dir: &Path) -> PathBuf {
    awm_dir.join("services").join("cch")
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
    let out = awm_statecheck::snapshot_once("cch", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("cch: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("cch: snapshot: {n}");
    }
    Ok(())
}
