//! The pre-redux snapshot of operations' state, taken once before the first Rust write.

use std::path::Path;

use awm_statecheck::decl::{Decl, Extra};

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    let mut d = Decl::parse(include_str!("../state.toml"), &Path::new(&home).join(".awm/operations"), awm_dir, |n| std::env::var(n).ok())?;
    d.extra.push(Extra { name: "arbutus-pending".into(), path: crate::arbutus::approval_pending_dir() });
    Ok(d)
}

pub fn take(awm_dir: &Path) -> anyhow::Result<()> {
    let out = awm_statecheck::snapshot_once("operations", &decl(awm_dir)?)?;
    for p in &out.taken {
        tracing::info!("operations: took the pre-redux snapshot {}", p.display());
    }
    for n in &out.notes {
        tracing::info!("operations: snapshot: {n}");
    }
    Ok(())
}
