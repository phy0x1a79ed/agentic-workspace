//! Installing and removing plugins: a source fetches a plugin folder, an approver passes the
//! change, and the pin record keeps what each installed plugin came from (install.md R116 item 2).
//!
//! A local folder is the only source today. A git source plugs in through [`Source`]; it
//! returns the commit it fetched, and [`Approver`] decides whether that commit may load.

use std::path::{Path, PathBuf};

use awm_proto::{Manifest, MANIFEST_FILE};
use serde::{Deserialize, Serialize};

pub const INSTALLED_DIR: &str = "installed";
pub const PINS_FILE: &str = "installed.toml";

/// What a source fetched into the staging folder.
#[derive(Debug, Clone)]
pub struct Fetched {
    pub name: String,
    /// The commit the files came from, when the source knows one.
    pub commit: Option<String>,
}

/// Where a plugin's files come from.
pub trait Source: Send + Sync {
    /// The pin's `source` value, such as `folder:/path` or `git:<url>`.
    fn spec(&self) -> String;
    /// Copy the plugin's files into `dest`, an empty folder.
    fn fetch(&self, dest: &Path) -> Result<Fetched, String>;
    /// Whether a change from this source needs Tony's approval before it loads.
    fn needs_approval(&self) -> bool;
}

/// One plugin of a change, as the approver sees it.
#[derive(Debug, Clone, Serialize)]
pub struct Pending {
    pub name: String,
    pub source: String,
    pub commit: Option<String>,
}

/// Passes or refuses one change. A change may hold several plugins, and one approval covers
/// them all (R116 item 2).
pub trait Approver: Send + Sync {
    fn approve(&self, change: &[Pending]) -> Result<(), String>;
}

/// The approver until the passkey flow exists (queue.md R64): a change that needs approval is
/// refused, so nothing but a local folder installs.
pub struct NoPasskeyYet;

impl Approver for NoPasskeyYet {
    fn approve(&self, change: &[Pending]) -> Result<(), String> {
        let names: Vec<&str> = change.iter().map(|p| p.name.as_str()).collect();
        Err(format!("{names:?} need passkey approval, which this node cannot take yet"))
    }
}

/// A plugin folder on this machine. The install copies it, so a later edit of the source
/// changes nothing until the next install.
pub struct LocalFolder(pub PathBuf);

impl Source for LocalFolder {
    fn spec(&self) -> String {
        format!("folder:{}", self.0.display())
    }

    fn fetch(&self, dest: &Path) -> Result<Fetched, String> {
        let src = self.0.canonicalize().map_err(|e| format!("{}: {e}", self.0.display()))?;
        let manifest = read_manifest(&src)?;
        copy_tree(&src, dest).map_err(|e| format!("copy {}: {e}", src.display()))?;
        Ok(Fetched { name: manifest.plugin.name, commit: git_head(&src) })
    }

    fn needs_approval(&self) -> bool {
        false
    }
}

/// Parse a source argument. `git:` and URLs wait on the fleet-deploy review topic.
pub fn source_of(raw: &str) -> Result<Box<dyn Source>, String> {
    let raw = raw.trim();
    if raw.starts_with("git:") || raw.contains("://") || raw.ends_with(".git") {
        return Err(format!("{raw:?}: a git source is not built yet; install from a folder"));
    }
    let path = raw.strip_prefix("folder:").unwrap_or(raw);
    Ok(Box::new(LocalFolder(PathBuf::from(path))))
}

/// One row of the pin record.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Pin {
    pub name: String,
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commit: Option<String>,
    /// The container image by digest, for a contained plugin (R119 item 3).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image: Option<String>,
    pub installed_at: String,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Pins {
    #[serde(default)]
    pub plugin: Vec<Pin>,
}

impl Pins {
    pub fn load(state: &Path) -> Self {
        std::fs::read_to_string(state.join(PINS_FILE)).ok().and_then(|t| toml::from_str(&t).ok()).unwrap_or_default()
    }

    pub fn save(&self, state: &Path) -> std::io::Result<()> {
        let text = toml::to_string_pretty(self).map_err(std::io::Error::other)?;
        let tmp = state.join(format!("{PINS_FILE}.tmp"));
        std::fs::write(&tmp, text)?;
        std::fs::rename(tmp, state.join(PINS_FILE))
    }

    pub fn set(&mut self, pin: Pin) {
        self.plugin.retain(|p| p.name != pin.name);
        self.plugin.push(pin);
        self.plugin.sort_by(|a, b| a.name.cmp(&b.name));
    }
}

pub fn installed_root(state: &Path) -> PathBuf {
    state.join(INSTALLED_DIR)
}

fn read_manifest(dir: &Path) -> Result<Manifest, String> {
    let text = std::fs::read_to_string(dir.join(MANIFEST_FILE))
        .map_err(|e| format!("{}: no {MANIFEST_FILE}: {e}", dir.display()))?;
    Manifest::parse(&text).map_err(|e| format!("{MANIFEST_FILE}: {e}"))
}

fn git_head(dir: &Path) -> Option<String> {
    let out = std::process::Command::new("git").arg("-C").arg(dir).args(["rev-parse", "HEAD"]).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Copy a plugin folder, leaving out VCS state and build caches. A symlink is copied as a link.
fn copy_tree(src: &Path, dest: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dest)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let name = entry.file_name();
        if name == ".git" || crate::loader::UNSTAMPED.iter().any(|u| name == *u) {
            continue;
        }
        let (from, to) = (entry.path(), dest.join(&name));
        let kind = entry.file_type()?;
        if kind.is_symlink() {
            std::os::unix::fs::symlink(std::fs::read_link(&from)?, &to)?;
        } else if kind.is_dir() {
            copy_tree(&from, &to)?;
        } else {
            std::fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_folder_installs_by_copy_and_git_waits() {
        let t = tempfile::tempdir().unwrap();
        let src = t.path().join("src");
        std::fs::create_dir_all(src.join(".git")).unwrap();
        std::fs::create_dir_all(src.join("node_modules/x")).unwrap();
        std::fs::write(src.join(MANIFEST_FILE), "[plugin]\nname = 'p'\n").unwrap();
        std::fs::write(src.join("run.sh"), "exit 0\n").unwrap();
        let dest = t.path().join("dest");
        let got = source_of(src.to_str().unwrap()).unwrap().fetch(&dest).unwrap();
        assert_eq!(got.name, "p");
        assert!(dest.join("run.sh").is_file() && !dest.join(".git").exists() && !dest.join("node_modules").exists());
        assert!(source_of("git:https://example.org/p.git").is_err());
        assert!(NoPasskeyYet.approve(&[Pending { name: "p".into(), source: "git:x".into(), commit: None }]).is_err());

        let mut pins = Pins::default();
        pins.set(Pin { name: "p".into(), source: "folder:/x".into(), commit: None, image: None, installed_at: "t".into() });
        pins.set(Pin { name: "p".into(), source: "folder:/y".into(), commit: None, image: None, installed_at: "t".into() });
        pins.save(t.path()).unwrap();
        assert_eq!(Pins::load(t.path()).plugin[0].source, "folder:/y");
    }
}
