//! The pre-redux snapshot of the state the auth port takes over, taken once before the first
//! Rust write.

use std::path::Path;

use awm_statecheck::decl::Decl;

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/auth"), awm_dir, |n| std::env::var(n).ok())
}

pub fn take(awm_dir: &Path) -> anyhow::Result<()> {
    let out = awm_statecheck::snapshot_once("auth", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("auth: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("auth: snapshot: {n}");
    }
    Ok(())
}
