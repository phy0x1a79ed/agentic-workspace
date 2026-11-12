//! What the pre-redux state check reads of auth's state.

use std::path::Path;

use awm_statecheck::decl::Decl;

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/auth"), awm_dir, |n| std::env::var(n).ok())
}
