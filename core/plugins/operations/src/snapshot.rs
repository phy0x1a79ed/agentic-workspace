//! What the pre-redux state check reads of operations's state.

use std::path::Path;

use awm_statecheck::decl::Decl;

pub fn decl(awm_dir: &Path) -> anyhow::Result<Decl> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    Decl::parse(include_str!("../state.toml"), &Path::new(&home).join(".awm/operations"), awm_dir, |n| std::env::var(n).ok())
}
