//! The Cache Directory Tagging marker (<https://bford.info/cachedir/>). A directory that
//! holds a valid `CACHEDIR.TAG` opts out of dvc's off-site mirror, and of tar, borg and restic.

use std::io::Write;
use std::path::Path;

pub const CACHEDIR_TAG: &str = "CACHEDIR.TAG";
pub const CACHEDIR_SIGNATURE: &[u8] = b"Signature: 8a477f597d28d172789f06886806bc55";

/// Whether the file at `path` opens with the tag signature.
pub fn is_cache_tag(path: &Path) -> bool {
    let mut head = [0u8; 43];
    std::fs::File::open(path).and_then(|mut f| std::io::Read::read_exact(&mut f, &mut head)).is_ok() && head == CACHEDIR_SIGNATURE
}

/// Whether `dir` holds a valid `CACHEDIR.TAG` of its own, not a symlink.
pub fn has_cache_tag(dir: &Path) -> bool {
    let tag = dir.join(CACHEDIR_TAG);
    tag.symlink_metadata().is_ok_and(|m| m.is_file()) && is_cache_tag(&tag)
}

/// Write a valid tag into `dir` and fsync it.
pub fn write_cache_tag(dir: &Path, by: &str) -> std::io::Result<()> {
    let mut f = std::fs::File::create(dir.join(CACHEDIR_TAG))?;
    f.write_all(CACHEDIR_SIGNATURE)?;
    writeln!(f, "\n# This directory is a copy made by {by}. Backups and mirrors skip it.\n# See https://bford.info/cachedir/")?;
    f.sync_all()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_and_reads_a_tag() {
        let d = tempfile::tempdir().unwrap();
        assert!(!has_cache_tag(d.path()));
        write_cache_tag(d.path(), "a test").unwrap();
        assert!(has_cache_tag(d.path()));
        assert_eq!(CACHEDIR_SIGNATURE.len(), 43);
    }
}
