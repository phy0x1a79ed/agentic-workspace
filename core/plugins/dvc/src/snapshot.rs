//! The pre-redux snapshot of dvc's state, taken once before the first Rust write. The
//! service and `dvc run-job` both take it, under one lock, so neither writes first.

use std::os::fd::AsRawFd;
use std::path::Path;

use awm_statecheck::decl::Decl;

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/dvc"), awm_dir, |n| std::env::var(n).ok())
}

pub fn take(awm_dir: &Path) -> anyhow::Result<()> {
    let dir = awm_dir.join("services/dvc");
    std::fs::create_dir_all(&dir)?;
    let lock = std::fs::File::open(&dir)?;
    // SAFETY: flock on a descriptor we own; it is released when `lock` closes.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let out = awm_statecheck::snapshot_once("dvc", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("dvc: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("dvc: snapshot: {n}");
    }
    Ok(())
}
