//! Installing and removing plugins: a source fetches a plugin folder, Tony's passkey approves
//! the commits of a change, and the pin record keeps what each installed plugin came from
//! (install.md R115, R116 item 2).

use std::path::{Path, PathBuf};

use awm_proto::{Manifest, MANIFEST_FILE};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const INSTALLED_DIR: &str = "installed";
pub const PINS_FILE: &str = "installed.toml";
/// The tool that takes Tony's passkey approval, and the request kind for plugin commits.
pub const APPROVER_TOOL: &str = "operations";
pub const COMMITS_KIND: &str = "plugin_commits";

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

/// One plugin of a change, as the approval request names it.
#[derive(Debug, Clone, Serialize)]
pub struct Pending {
    pub plugin: String,
    pub source: String,
    pub commit: String,
    /// The commit the plugin moves from, for an update.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from: Option<String>,
}

/// The `name@commit` list a grant must name, sorted as the approver sorts it.
pub fn commits_of(change: &[Pending]) -> Vec<String> {
    let mut out: Vec<String> = change.iter().map(|p| format!("{}@{}", p.plugin, p.commit)).collect();
    out.sort();
    out
}

/// The `plugin_commits` payload for one change. One request approves all of it.
pub fn request_payload(change: &[Pending]) -> Value {
    json!({"change": commits_of(change).join(","), "plugins": change})
}

/// Whether an `approval_get` answer approves exactly this change, now. The grant must name
/// every commit of the change and no other, so an approval of one change never passes another.
pub fn check_grant(change: &[Pending], answer: &Value, now: i64) -> Result<(), String> {
    let status = answer.get("status").and_then(Value::as_str).unwrap_or("unknown");
    if status != "approved" {
        return Err(format!("the approval request is {status}, not approved"));
    }
    let grant = answer.pointer("/grant/grant").ok_or("the approval carries no grant")?;
    if grant.get("kind").and_then(Value::as_str) != Some(COMMITS_KIND) {
        return Err(format!("the grant is not a {COMMITS_KIND} grant"));
    }
    let named: Vec<String> = grant
        .get("commits")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default();
    if named != commits_of(change) {
        return Err(format!("the grant approves {named:?}, not this change {:?}", commits_of(change)));
    }
    let window = (grant.get("not_before").and_then(Value::as_i64), grant.get("not_after").and_then(Value::as_i64));
    match window {
        (Some(from), Some(to)) if (from..=to).contains(&now) => Ok(()),
        _ => Err("the grant has expired; ask for a new approval".into()),
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

/// A plugin's own git repository at one pinned commit (R115). The manifest sits at the top
/// of the repository.
pub struct GitRepo {
    pub url: String,
    pub commit: String,
}

impl Source for GitRepo {
    fn spec(&self) -> String {
        format!("git:{}", self.url)
    }

    fn fetch(&self, dest: &Path) -> Result<Fetched, String> {
        let parent = dest.parent().ok_or("no staging folder")?;
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        let clone = tempfile_dir(parent, "clone")?;
        let result = (|| {
            git(&clone, &["init", "-q"])?;
            git(&clone, &["remote", "add", "origin", &self.url])?;
            // A server that refuses a fetch by commit still serves its branches.
            if git(&clone, &["fetch", "-q", "--depth", "1", "origin", &self.commit]).is_err() {
                git(&clone, &["fetch", "-q", "origin"])?;
            }
            git(&clone, &["-c", "advice.detachedHead=false", "checkout", "-q", &self.commit])?;
            let head = git_head(&clone).unwrap_or_default();
            if head != self.commit {
                return Err(format!("{}: checked out {head}, not the pinned {}", self.url, self.commit));
            }
            let manifest = read_manifest(&clone)?;
            copy_tree(&clone, dest).map_err(|e| format!("copy {}: {e}", self.url))?;
            Ok(Fetched { name: manifest.plugin.name, commit: Some(head) })
        })();
        let _ = std::fs::remove_dir_all(&clone);
        result
    }

    fn needs_approval(&self) -> bool {
        true
    }
}

/// Parse a source argument: `git:<url>@<commit>` for a repository, else a folder.
pub fn source_of(raw: &str) -> Result<Box<dyn Source>, String> {
    let raw = raw.trim();
    if let Some(rest) = raw.strip_prefix("git:") {
        let pinned = rest.rsplit_once('@').filter(|(url, c)| !url.is_empty() && is_commit(c));
        let Some((url, commit)) = pinned else {
            return Err(format!("{raw:?}: a git source pins a full commit, as git:<url>@<40-hex commit>"));
        };
        return Ok(Box::new(GitRepo { url: url.into(), commit: commit.into() }));
    }
    if raw.contains("://") || raw.ends_with(".git") {
        return Err(format!("{raw:?}: name a git source as git:<url>@<40-hex commit>"));
    }
    let path = raw.strip_prefix("folder:").unwrap_or(raw);
    Ok(Box::new(LocalFolder(PathBuf::from(path))))
}

fn is_commit(c: &str) -> bool {
    c.len() == 40 && c.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn git(dir: &Path, args: &[&str]) -> Result<(), String> {
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .env("GIT_TERMINAL_PROMPT", "0")
        .output()
        .map_err(|e| format!("git: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    Err(format!("git {}: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim()))
}

fn tempfile_dir(parent: &Path, tag: &str) -> Result<PathBuf, String> {
    let dir = parent.join(format!(".{tag}-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    Ok(dir)
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

    fn sh(dir: &Path, args: &[&str]) {
        assert!(std::process::Command::new("git").arg("-C").arg(dir).args(args).status().unwrap().success());
    }

    #[test]
    fn a_folder_installs_by_copy() {
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

        let mut pins = Pins::default();
        pins.set(Pin { name: "p".into(), source: "folder:/x".into(), commit: None, image: None, installed_at: "t".into() });
        pins.set(Pin { name: "p".into(), source: "folder:/y".into(), commit: None, image: None, installed_at: "t".into() });
        pins.save(t.path()).unwrap();
        assert_eq!(Pins::load(t.path()).plugin[0].source, "folder:/y");
    }

    #[test]
    fn a_git_source_installs_the_pinned_commit() {
        let t = tempfile::tempdir().unwrap();
        let repo = t.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        sh(&repo, &["init", "-q"]);
        let commit = |msg: &str| {
            sh(&repo, &["add", "-A"]);
            sh(&repo, &["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg]);
            let out = std::process::Command::new("git").arg("-C").arg(&repo).args(["rev-parse", "HEAD"]).output().unwrap();
            String::from_utf8(out.stdout).unwrap().trim().to_string()
        };
        std::fs::write(repo.join(MANIFEST_FILE), "[plugin]\nname = 'g'\n").unwrap();
        std::fs::write(repo.join("run.sh"), "echo one\n").unwrap();
        let first = commit("one");
        std::fs::write(repo.join("run.sh"), "echo two\n").unwrap();
        commit("two");

        let source = source_of(&format!("git:{}@{first}", repo.display())).unwrap();
        assert!(source.needs_approval());
        assert_eq!(source.spec(), format!("git:{}", repo.display()));
        let dest = t.path().join("stage/0");
        let got = source.fetch(&dest).unwrap();
        assert_eq!((got.name.as_str(), got.commit.as_deref()), ("g", Some(first.as_str())));
        assert_eq!(std::fs::read_to_string(dest.join("run.sh")).unwrap(), "echo one\n");
        assert!(!dest.join(".git").exists());
        assert_eq!(std::fs::read_dir(t.path().join("stage")).unwrap().count(), 1, "the clone is cleaned up");

        assert!(source_of(&format!("git:{}", repo.display())).is_err(), "a git source pins a commit");
        assert!(source_of(&format!("git:{}@abc123", repo.display())).is_err(), "a short commit is no pin");
        assert!(source_of("https://example.org/p.git").is_err());
        let missing = source_of(&format!("git:{}@{}", repo.display(), "0".repeat(40))).unwrap();
        assert!(missing.fetch(&t.path().join("stage/1")).is_err());
    }

    #[test]
    fn a_grant_approves_exactly_its_change() {
        let (a, b) = ("a".repeat(40), "b".repeat(40));
        let change = vec![
            Pending { plugin: "zotero".into(), source: "git:x".into(), commit: b.clone(), from: Some(a.clone()) },
            Pending { plugin: "engcyc".into(), source: "git:y".into(), commit: a.clone(), from: None },
        ];
        let payload = request_payload(&change);
        assert_eq!(payload["plugins"][0], json!({"plugin": "zotero", "source": "git:x", "commit": b, "from": a}));
        assert!(payload["plugins"][1].get("from").is_none());
        let answer = |status: &str, commits: Vec<String>| {
            json!({"status": status, "grant": {"grant": {"kind": "plugin_commits", "commits": commits,
                                                          "not_before": 100, "not_after": 200}}})
        };
        let both = commits_of(&change);
        assert_eq!(both, vec![format!("engcyc@{a}"), format!("zotero@{b}")]);
        assert!(check_grant(&change, &answer("approved", both.clone()), 150).is_ok());
        assert!(check_grant(&change, &answer("pending", both.clone()), 150).unwrap_err().contains("pending"));
        assert!(check_grant(&change, &answer("approved", both[..1].to_vec()), 150).is_err());
        assert!(check_grant(&change, &answer("approved", both), 201).unwrap_err().contains("expired"));
    }
}
