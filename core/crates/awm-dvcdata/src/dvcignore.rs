//! `.dvcignore`: the paths DVC leaves out of a directory output. A checkout and a status
//! must skip the same paths DVC skips, or they would delete or hash files DVC never saw.

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::Match;

pub const DVCIGNORE: &str = ".dvcignore";
/// DVC's own defaults, matched at the repo root.
const DEFAULTS: [&str; 4] = [".hg/", ".git/", ".git", ".dvc/"];

/// The `.dvcignore` files of one repo, read as a walk reaches their directories.
pub struct DvcIgnore {
    root: PathBuf,
    defaults: Gitignore,
    files: RefCell<HashMap<PathBuf, Option<Gitignore>>>,
}

impl DvcIgnore {
    pub fn new(root: &Path) -> Self {
        let mut b = GitignoreBuilder::new(root);
        for p in DEFAULTS {
            let _ = b.add_line(None, p);
        }
        DvcIgnore { root: root.to_path_buf(), defaults: b.build().unwrap_or_else(|_| Gitignore::empty()), files: RefCell::default() }
    }

    fn file_in(&self, dir: &Path) -> Option<Gitignore> {
        self.files
            .borrow_mut()
            .entry(dir.to_path_buf())
            .or_insert_with(|| {
                let f = dir.join(DVCIGNORE);
                f.is_file().then(|| {
                    let mut b = GitignoreBuilder::new(dir);
                    let _ = b.add(&f);
                    b.build().unwrap_or_else(|_| Gitignore::empty())
                })
            })
            .clone()
    }

    /// Whether DVC ignores `path`, a path under the repo root. The deepest file that
    /// decides wins, as in git.
    pub fn is_ignored(&self, path: &Path, is_dir: bool) -> bool {
        let Ok(rel) = path.strip_prefix(&self.root) else { return false };
        let mut dirs = vec![self.root.clone()];
        let mut cur = self.root.clone();
        let comps: Vec<_> = rel.components().collect();
        for c in &comps[..comps.len().saturating_sub(1)] {
            cur.push(c);
            dirs.push(cur.clone());
        }
        // A path inside an ignored directory is ignored with it.
        let mut prefix = self.root.clone();
        for (i, c) in comps.iter().enumerate() {
            prefix.push(c);
            let last = i + 1 == comps.len();
            if self.decide(&dirs[..=i.min(dirs.len() - 1)], &prefix, if last { is_dir } else { true }) {
                return true;
            }
        }
        false
    }

    /// Whether DVC ignores one entry of a walk whose parents the walk already let through.
    pub fn entry_ignored(&self, path: &Path, is_dir: bool) -> bool {
        let Ok(rel) = path.strip_prefix(&self.root) else { return false };
        let mut dirs = vec![self.root.clone()];
        if let Some(parent) = rel.parent() {
            let mut cur = self.root.clone();
            for c in parent.components() {
                cur.push(c);
                dirs.push(cur.clone());
            }
        }
        self.decide(&dirs, path, is_dir)
    }

    fn decide(&self, dirs: &[PathBuf], path: &Path, is_dir: bool) -> bool {
        for d in dirs.iter().rev() {
            if let Some(g) = self.file_in(d) {
                match g.matched(path, is_dir) {
                    Match::Ignore(_) => return true,
                    Match::Whitelist(_) => return false,
                    Match::None => {}
                }
            }
        }
        matches!(self.defaults.matched(path, is_dir), Match::Ignore(_))
    }
}
