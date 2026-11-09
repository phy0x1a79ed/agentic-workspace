//! What the pre-redux state check reads of cch's state.

use std::path::{Path, PathBuf};

use awm_statecheck::decl::Decl;

fn service_dir(awm_dir: &Path) -> PathBuf {
    awm_dir.join("services").join("cch")
}

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &service_dir(awm_dir), awm_dir, |n| std::env::var(n).ok())
}
