//! The pre-redux snapshot of routine's state, taken once before the first Rust write. The
//! service and `routine land` both take it, under one lock, so neither touches the state first.

use std::os::fd::AsRawFd;
use std::path::Path;

use awm_statecheck::decl::Decl;

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/routine"), awm_dir, |n| std::env::var(n).ok())
}

/// Take each state dir's snapshot that does not exist yet. A second process waits for the
/// first, then finds the snapshot taken.
pub fn take(awm_dir: &Path) -> anyhow::Result<()> {
    let dir = awm_dir.join("services/routine");
    std::fs::create_dir_all(&dir)?;
    let lock = std::fs::File::open(&dir)?;
    // SAFETY: flock on a descriptor we own; it is released when `lock` closes.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let out = awm_statecheck::snapshot_once("routine", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("routine: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("routine: snapshot: {n}");
    }
    Ok(())
}
