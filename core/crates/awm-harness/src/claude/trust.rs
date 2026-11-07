//! Would a session working in a directory ask the user a question first?
//!
//! Claude Code applies two rules. [`trusted`] answers for `/cd` into a directory the session has
//! not worked in before: that opens a trust dialog whose default answer is "No, stay put", and the
//! next thing typed into the session answers the dialog instead of doing what it meant to.
//! [`start_refusal`] answers for `claude --bg` starting in a directory, with the same rule and a
//! message that says what to trust. A start that fails it does not error: the daemon refuses with
//! "Workspace not trusted", and the caller sees nothing until its wait for a new job times out.
//!
//! Trust is recorded per directory in `~/.claude.json` (`projects.<abs path>.hasTrustDialogAccepted`).
//! Inside a git repository the entry must be the repository's own, and an ancestor such as the
//! home directory does not stand in for it. For a linked worktree the entry is the main repository,
//! which for an awm project is its `.bare` directory; for a plain repository it is the repository
//! root. Only a directory outside git inherits trust from an ancestor.
//!
//! Outside awm the user answers the trust prompt themselves. [`grant_awm_worktree`] is the
//! exception: a worktree of an awm project under the workspace's projects directory is a directory
//! awm created, so the agent moving into it records the trust a person would only have
//! rubber-stamped. A shape of `~/.claude.json` this module does not recognise reads as untrusted.
//!
//! A grant rewrites the file the way Claude Code does, through a temporary file and a rename, and
//! byte for byte as Python's `json.dump(data, fh, indent=2)` would: key order kept, two-space
//! indent, non-ASCII escaped as `\uXXXX`, no trailing newline.

use std::ffi::OsString;
use std::fmt::Write as _;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use serde_json::{Map, Value};

use super::ClaudeHome;

const GIT_TIMEOUT: Duration = Duration::from_secs(5);
const ACCEPTED: &str = "hasTrustDialogAccepted";

/// True if `/cd path` would move without opening the trust dialog.
pub async fn trusted(path: impl AsRef<Path>, home: &ClaudeHome) -> bool {
    start_refusal(path, home).await.is_none()
}

/// Why `claude --bg` would refuse to start in `path`, naming the entry that needs trust, or None.
pub async fn start_refusal(path: impl AsRef<Path>, home: &ClaudeHome) -> Option<String> {
    let path = path.as_ref();
    let accepted = accepted(&home.trust_file).await;
    let shown = path.display();
    if let Some(key) = workspace_key(path).await {
        let key_str = key.to_string_lossy();
        if accepted
            .as_ref()
            .is_some_and(|a| a.contains_key(key_str.as_ref()))
        {
            return None;
        }
        let where_ = if resolve(path) != key {
            format!(
                "{shown} is a git worktree of {key_str}, and Claude Code keys its trust on that \
                 repository, not on a parent directory"
            )
        } else {
            format!(
                "{key_str} is a git repository, and Claude Code requires an entry for the \
                 repository itself, not for a parent directory"
            )
        };
        return Some(format!(
            "{where_}; {key_str} is not trusted. Open `claude` once in {shown} and accept the \
             trust dialog, then retry."
        ));
    }
    if accepted.as_ref().is_some_and(|a| inherited(path, a)) {
        return None;
    }
    Some(format!(
        "{shown} is not trusted. Open `claude` once in {shown} and accept the trust dialog, then \
         retry."
    ))
}

/// The git repository Claude Code keys workspace trust on: the repository root for an ordinary
/// repository, the bare directory for worktrees of a bare clone, or None outside git.
pub async fn workspace_key(path: impl AsRef<Path>) -> Option<PathBuf> {
    workspace_key_in(path.as_ref(), std::env::vars_os()).await
}

/// [`workspace_key`] with `env` standing in for this process's environment.
async fn workspace_key_in(
    path: &Path,
    env: impl IntoIterator<Item = (OsString, OsString)>,
) -> Option<PathBuf> {
    // An inherited GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR or GIT_INDEX_FILE overrides `-C` and
    // would answer for the caller's repository, not `path`.
    let env = env
        .into_iter()
        .filter(|(k, _)| !k.to_string_lossy().starts_with("GIT_"));
    let child = tokio::process::Command::new("git")
        .arg("-C")
        .arg(path)
        .args(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        .env_clear()
        .envs(env)
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output();
    let out = tokio::time::timeout(GIT_TIMEOUT, child).await.ok()?.ok()?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    let line = stdout.trim();
    if !out.status.success() || line.is_empty() {
        return None;
    }
    let common = resolve(Path::new(line));
    if common.file_name().is_some_and(|n| n == ".git") {
        common.parent().map(Path::to_path_buf)
    } else {
        Some(common)
    }
}

/// The trust entries an awm worktree needs — its project's `.bare` repository, then itself — or
/// none for a directory that is not a worktree under `<projects_root>/<project>/`.
pub async fn awm_worktree_keys(
    path: impl AsRef<Path>,
    projects_root: impl AsRef<Path>,
) -> Vec<PathBuf> {
    let root = resolve(projects_root.as_ref());
    let p = resolve(path.as_ref());
    if !p.starts_with(&root) || p == root {
        return Vec::new();
    }
    let Some(key) = workspace_key(&p).await else {
        return Vec::new();
    };
    let Some(project) = key.parent() else {
        return Vec::new();
    };
    if key.file_name().is_none_or(|n| n != ".bare") || project.parent() != Some(root.as_path()) {
        return Vec::new();
    }
    if !p.starts_with(project) || p.starts_with(&key) {
        return Vec::new();
    }
    vec![key, p]
}

/// Record trust for an awm worktree under `projects_root`; returns the entries it added.
pub async fn grant_awm_worktree(
    path: impl AsRef<Path>,
    projects_root: impl AsRef<Path>,
    home: &ClaudeHome,
) -> Vec<String> {
    let keys = awm_worktree_keys(path, projects_root).await;
    if keys.is_empty() {
        return Vec::new();
    }
    record_trust(&keys, &home.trust_file).await
}

/// Record trust for a directory outside every git repository; adds nothing inside one.
pub async fn grant_plain_dir(path: impl AsRef<Path>, home: &ClaudeHome) -> Vec<String> {
    let path = path.as_ref();
    if workspace_key(path).await.is_some() {
        return Vec::new();
    }
    record_trust(&[resolve(path)], &home.trust_file).await
}

async fn accepted(trust_file: &Path) -> Option<Map<String, Value>> {
    let bytes = tokio::fs::read(trust_file).await.ok()?;
    let Value::Object(mut data) = serde_json::from_slice(&bytes).ok()? else {
        return None;
    };
    let Some(Value::Object(projects)) = data.remove("projects") else {
        return None;
    };
    Some(
        projects
            .into_iter()
            .filter(|(_, v)| v.get(ACCEPTED).is_some_and(truthy))
            .collect(),
    )
}

fn inherited(path: &Path, accepted: &Map<String, Value>) -> bool {
    resolve(path)
        .ancestors()
        .any(|cand| accepted.contains_key(cand.to_string_lossy().as_ref()))
}

async fn record_trust(keys: &[PathBuf], target: &Path) -> Vec<String> {
    let Ok(text) = tokio::fs::read_to_string(target).await else {
        return Vec::new();
    };
    let Ok(mut data) = serde_json::from_str::<Value>(&text) else {
        return Vec::new();
    };
    let Some(Value::Object(projects)) = data.as_object_mut().and_then(|d| d.get_mut("projects"))
    else {
        return Vec::new();
    };
    let mut added = Vec::new();
    for key in keys.iter().map(|k| k.to_string_lossy().into_owned()) {
        match projects.get_mut(&key) {
            Some(Value::Object(entry)) => {
                if entry.get(ACCEPTED).is_some_and(truthy) {
                    continue;
                }
                entry.insert(ACCEPTED.to_owned(), Value::Bool(true));
            }
            _ => {
                let mut entry = Map::new();
                entry.insert(ACCEPTED.to_owned(), Value::Bool(true));
                projects.insert(key.clone(), Value::Object(entry));
            }
        }
        added.push(key);
    }
    if added.is_empty() {
        return Vec::new();
    }
    match replace_file(target, python_json(&data).as_bytes()).await {
        Ok(()) => added,
        Err(err) => {
            tracing::warn!(file = %target.display(), %err, "could not record trust");
            Vec::new()
        }
    }
}

/// Write `bytes` to a temporary file beside `target`, give it `target`'s mode, rename it over.
async fn replace_file(target: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let dir = target.parent().unwrap_or(Path::new(""));
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (tmp, mut file) = create_temp(dir, &name).await?;
    let result = async {
        tokio::io::AsyncWriteExt::write_all(&mut file, bytes).await?;
        drop(file);
        let mode = tokio::fs::metadata(target).await?.permissions().mode() & 0o777;
        tokio::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode)).await?;
        tokio::fs::rename(&tmp, target).await
    }
    .await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(&tmp).await;
    }
    result
}

async fn create_temp(dir: &Path, name: &str) -> std::io::Result<(PathBuf, tokio::fs::File)> {
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let mut last = None;
    for attempt in 0..16u128 {
        let tag =
            (seed ^ (u128::from(std::process::id()) << 64)).wrapping_add(attempt * 0x9e37_79b9);
        let tmp = dir.join(format!("{name}.awm.{:08x}", tag as u32));
        match tokio::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .await
        {
            Ok(file) => return Ok((tmp, file)),
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => last = Some(err),
            Err(err) => return Err(err),
        }
    }
    Err(last.unwrap_or_else(|| std::io::Error::other("no free temporary name")))
}

/// Python's `json.dumps(value, indent=2)`: serde's pretty form with every character outside
/// printable ASCII escaped as `\uXXXX`, using surrogate pairs above the BMP.
fn python_json(value: &Value) -> String {
    let pretty = serde_json::to_string_pretty(value).expect("a Value always serializes");
    // Outside string literals serde emits only ASCII, so escaping the whole text is safe.
    let mut out = String::with_capacity(pretty.len());
    for c in pretty.chars() {
        if (c as u32) < 0x7f {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                let _ = write!(out, "\\u{unit:04x}");
            }
        }
    }
    out
}

/// Python truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_none_or(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `Path.resolve()`: absolute, symlinks followed where the path exists, the rest kept as written.
fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut out = PathBuf::from("/");
    for component in absolute.components() {
        match component {
            Component::RootDir | Component::Prefix(_) | Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(part) => {
                out.push(part);
                if out
                    .symlink_metadata()
                    .is_ok_and(|m| m.file_type().is_symlink())
                {
                    if let Ok(real) = std::fs::canonicalize(&out) {
                        out = real;
                    }
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::process::Command;
    use tempfile::TempDir;

    struct Tmp {
        _dir: TempDir,
        path: PathBuf,
    }

    fn tmp() -> Tmp {
        let dir = TempDir::new().unwrap();
        let path = std::fs::canonicalize(dir.path()).unwrap();
        Tmp { _dir: dir, path }
    }

    fn s(p: &Path) -> String {
        p.to_string_lossy().into_owned()
    }

    /// A fake `~/.claude.json` holding `projects`, inside `dir`.
    fn trust_file(dir: &Path, projects: Value) -> ClaudeHome {
        let f = dir.join("claude.json");
        std::fs::write(
            &f,
            serde_json::to_string(&json!({ "projects": projects })).unwrap(),
        )
        .unwrap();
        ClaudeHome::at(dir.join(".claude"), f)
    }

    fn read(home: &ClaudeHome) -> Value {
        serde_json::from_slice(&std::fs::read(&home.trust_file).unwrap()).unwrap()
    }

    fn git(cwd: &Path, args: &[&str]) {
        let mut cmd = Command::new("git");
        cmd.args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "protocol.file.allow=always",
        ])
        .args(args)
        .current_dir(cwd);
        for (k, _) in std::env::vars_os() {
            if k.to_string_lossy().starts_with("GIT_") {
                cmd.env_remove(k);
            }
        }
        let out = cmd.output().expect("git runs");
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    struct Repos {
        plain: PathBuf,
        bare: PathBuf,
        wt: PathBuf,
    }

    /// A plain repo, and a bare clone of it with one linked worktree.
    fn repos(tmp: &Path) -> Repos {
        let plain = tmp.join("plain");
        std::fs::create_dir(&plain).unwrap();
        git(&plain, &["init", "-q"]);
        git(&plain, &["commit", "-q", "--allow-empty", "-m", "x"]);
        let bare = tmp.join("proj").join(".bare");
        std::fs::create_dir(bare.parent().unwrap()).unwrap();
        git(tmp, &["clone", "-q", "--bare", &s(&plain), &s(&bare)]);
        let wt = tmp.join("proj").join("scope");
        git(
            &bare,
            &["worktree", "add", "-q", "-b", "feat/scope", &s(&wt)],
        );
        Repos { plain, bare, wt }
    }

    #[tokio::test]
    async fn trust_is_inherited_from_an_ancestor() {
        let t = tmp();
        let h = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        assert!(trusted(&t.path, &h).await);
        assert!(trusted(t.path.join("a").join("b"), &h).await);
    }

    #[tokio::test]
    async fn a_directory_with_no_trusted_ancestor_is_untrusted() {
        let t = tmp();
        let h = trust_file(
            &t.path,
            json!({ s(&t.path.join("other")): { ACCEPTED: true } }),
        );
        assert!(!trusted(t.path.join("here"), &h).await);
    }

    #[tokio::test]
    async fn a_declined_entry_is_not_trust() {
        let t = tmp();
        let h = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: false } }));
        assert!(!trusted(&t.path, &h).await);
    }

    #[tokio::test]
    async fn an_unreadable_or_reshaped_file_reads_as_untrusted() {
        let t = tmp();
        let missing = ClaudeHome::at(t.path.join(".claude"), t.path.join("missing.json"));
        assert!(!trusted(&t.path, &missing).await);
        let bad_file = t.path.join("bad.json");
        let bad = ClaudeHome::at(t.path.join(".claude"), &bad_file);
        for text in ["{", r#"{"projects": ["not", "a", "dict"]}"#, "[]"] {
            std::fs::write(&bad_file, text).unwrap();
            assert!(!trusted(&t.path, &bad).await, "{text}");
        }
    }

    // --- the stricter rule for `claude --bg` ---

    #[tokio::test]
    async fn an_ancestor_does_not_trust_a_git_worktree() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        // Measured: `/cd` into such a worktree opens "This directory is part of the repository
        // at …/.bare" under a trusted home.
        assert!(!trusted(&r.wt, &h).await);
        let why = start_refusal(&r.wt, &h).await.expect("refused");
        assert!(
            why.contains(&s(&r.bare)) && why.contains("claude") && why.contains("accept"),
            "{why}"
        );
    }

    #[tokio::test]
    async fn the_main_repository_entry_trusts_every_worktree() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(&t.path, json!({ s(&r.bare): { ACCEPTED: true } }));
        assert_eq!(start_refusal(&r.wt, &h).await, None);
    }

    #[tokio::test]
    async fn a_declined_repository_entry_is_not_trust() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(&t.path, json!({ s(&r.bare): { ACCEPTED: false } }));
        assert!(start_refusal(&r.wt, &h).await.is_some());
    }

    #[tokio::test]
    async fn a_plain_repository_needs_its_own_entry() {
        let t = tmp();
        let r = repos(&t.path);
        let sub = r.plain.join("sub");
        std::fs::create_dir(&sub).unwrap();
        let ancestor = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        let why = start_refusal(&sub, &ancestor).await.expect("refused");
        assert!(why.contains(&s(&r.plain)), "{why}");
        let own = trust_file(&t.path, json!({ s(&r.plain): { ACCEPTED: true } }));
        assert_eq!(start_refusal(&r.plain, &own).await, None);
        assert_eq!(start_refusal(&sub, &own).await, None);
    }

    #[tokio::test]
    async fn the_workspace_key_is_the_bare_dir_or_the_repo_root() {
        let t = tmp();
        let r = repos(&t.path);
        assert_eq!(workspace_key(&r.wt).await, Some(r.bare.clone()));
        assert_eq!(workspace_key(&r.plain).await, Some(r.plain.clone()));
    }

    #[tokio::test]
    async fn an_inherited_git_environment_does_not_redirect_the_lookup() {
        // The GIT_* variables go into the environment handed to the lookup, not this process's,
        // so parallel tests are unaffected.
        let t = tmp();
        let r = repos(&t.path);
        let dot_git = r.plain.join(".git");
        let env = std::env::vars_os().chain([
            ("GIT_DIR".into(), dot_git.clone().into_os_string()),
            ("GIT_WORK_TREE".into(), r.plain.clone().into_os_string()),
            ("GIT_COMMON_DIR".into(), dot_git.clone().into_os_string()),
            (
                "GIT_INDEX_FILE".into(),
                dot_git.join("index").into_os_string(),
            ),
        ]);
        assert_eq!(workspace_key_in(&r.wt, env).await, Some(r.bare.clone()));
    }

    #[tokio::test]
    async fn a_directory_outside_git_inherits_from_an_ancestor() {
        let t = tmp();
        let outside = t.path.join("notgit");
        std::fs::create_dir(&outside).unwrap();
        let h = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        assert_eq!(workspace_key(&outside).await, None);
        assert_eq!(start_refusal(&outside, &h).await, None);
        let empty = trust_file(&t.path, json!({}));
        let why = start_refusal(&outside, &empty).await.expect("refused");
        assert!(
            why.contains(&s(&outside)) && why.contains("claude"),
            "{why}"
        );
    }

    #[tokio::test]
    async fn an_unreadable_trust_file_refuses_a_start() {
        let t = tmp();
        let r = repos(&t.path);
        let missing = ClaudeHome::at(t.path.join(".claude"), t.path.join("missing.json"));
        assert!(start_refusal(&r.wt, &missing).await.is_some());
    }

    // --- granting trust to awm's own worktrees ---

    #[tokio::test]
    async fn an_awm_worktree_is_granted_its_repository_and_itself() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(
            &t.path,
            json!({ "/elsewhere": { ACCEPTED: true, "allowedTools": ["x"] } }),
        );
        let added = grant_awm_worktree(&r.wt, &t.path, &h).await;
        assert_eq!(added, vec![s(&r.bare), s(&r.wt)]);
        assert!(trusted(&r.wt, &h).await);
        assert_eq!(start_refusal(&r.wt, &h).await, None);
        assert_eq!(
            read(&h)["projects"]["/elsewhere"],
            json!({ ACCEPTED: true, "allowedTools": ["x"] })
        );
        assert!(grant_awm_worktree(&r.wt, &t.path, &h).await.is_empty());
    }

    #[tokio::test]
    async fn an_existing_entry_keeps_its_other_fields() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(
            &t.path,
            json!({ s(&r.bare): { ACCEPTED: false, "mcpServers": { "a": 1 } } }),
        );
        grant_awm_worktree(&r.wt, &t.path, &h).await;
        assert_eq!(
            read(&h)["projects"][s(&r.bare)],
            json!({ ACCEPTED: true, "mcpServers": { "a": 1 } })
        );
    }

    #[tokio::test]
    async fn nothing_outside_the_projects_root_is_granted() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(&t.path, json!({}));
        assert!(grant_awm_worktree(&r.wt, t.path.join("other"), &h)
            .await
            .is_empty());
        // A plain repository is not an awm project, even under the root.
        assert!(grant_awm_worktree(&r.plain, &t.path, &h).await.is_empty());
        // Nor is the project directory, or the bare repository itself.
        assert!(grant_awm_worktree(t.path.join("proj"), &t.path, &h)
            .await
            .is_empty());
        assert!(grant_awm_worktree(&r.bare, &t.path, &h).await.is_empty());
        let outside = t.path.join("notgit");
        std::fs::create_dir(&outside).unwrap();
        assert!(grant_awm_worktree(&outside, &t.path, &h).await.is_empty());
        assert_eq!(read(&h), json!({ "projects": {} }));
    }

    #[tokio::test]
    async fn a_worktree_of_another_projects_repository_is_not_granted() {
        // A worktree under proj-b/ whose repository is proj/.bare is not proj-b's.
        let t = tmp();
        let r = repos(&t.path);
        let stray = t.path.join("proj-b").join("scope");
        std::fs::create_dir(stray.parent().unwrap()).unwrap();
        git(
            &r.bare,
            &["worktree", "add", "-q", "-b", "feat/stray", &s(&stray)],
        );
        let h = trust_file(&t.path, json!({}));
        assert!(grant_awm_worktree(&stray, &t.path, &h).await.is_empty());
    }

    // --- beyond the Python suite: the rewrite matches Python's bytes ---

    #[tokio::test]
    async fn a_grant_keeps_key_order_fields_and_mode() {
        let t = tmp();
        let r = repos(&t.path);
        let f = t.path.join("claude.json");
        let before = format!(
            r#"{{"zeta": 1, "projects": {{"{bare}": {{"allowedTools": [], "{ACCEPTED}": false, "z": {{}}}}, "/b": {{"x": true}}}}, "alpha": [1, 2]}}"#,
            bare = s(&r.bare)
        );
        std::fs::write(&f, before).unwrap();
        std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o640)).unwrap();
        let h = ClaudeHome::at(t.path.join(".claude"), &f);
        assert_eq!(
            grant_awm_worktree(&r.wt, &t.path, &h).await,
            vec![s(&r.bare), s(&r.wt)]
        );
        let expected = format!(
            "{{\n  \"zeta\": 1,\n  \"projects\": {{\n    \"{bare}\": {{\n      \"allowedTools\": [],\n      \
             \"{ACCEPTED}\": true,\n      \"z\": {{}}\n    }},\n    \"/b\": {{\n      \"x\": true\n    }},\n    \
             \"{wt}\": {{\n      \"{ACCEPTED}\": true\n    }}\n  }},\n  \"alpha\": [\n    1,\n    2\n  ]\n}}",
            bare = s(&r.bare),
            wt = s(&r.wt)
        );
        assert_eq!(std::fs::read_to_string(&f).unwrap(), expected);
        assert_eq!(
            std::fs::metadata(&f).unwrap().permissions().mode() & 0o777,
            0o640
        );
        let leftovers: Vec<_> = std::fs::read_dir(&t.path)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".awm."))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn the_output_matches_python_json_dump_byte_for_byte() {
        // Expected text is Python's `json.dumps(value, indent=2)` of the same value.
        let value: Value = serde_json::from_str(
            "{\"projects\": {\"/home/\u{e9}/\u{65e5}\u{672c}\": {\"hasTrustDialogAccepted\": true, \
             \"x\": [], \"y\": {}, \"z\": [1, {\"a\": null}], \
             \"e\": \"\u{1f600}\u{7f}\\u0001\\t\\\"\\\\/\"}}, \"numStartups\": 3}",
        )
        .unwrap();
        let python = r#"{
  "projects": {
    "/home/\u00e9/\u65e5\u672c": {
      "hasTrustDialogAccepted": true,
      "x": [],
      "y": {},
      "z": [
        1,
        {
          "a": null
        }
      ],
      "e": "\ud83d\ude00\u007f\u0001\t\"\\/"
    }
  },
  "numStartups": 3
}"#;
        assert_eq!(python_json(&value), python);
    }

    #[tokio::test]
    async fn a_plain_dir_with_a_non_ascii_name_is_granted_and_escaped() {
        let t = tmp();
        let lobby = t.path.join("lobby-\u{e9}");
        std::fs::create_dir(&lobby).unwrap();
        let h = trust_file(&t.path, json!({}));
        assert_eq!(grant_plain_dir(&lobby, &h).await, vec![s(&lobby)]);
        let text = std::fs::read_to_string(&h.trust_file).unwrap();
        assert!(text.is_ascii() && text.contains("lobby-\\u00e9"), "{text}");
        assert!(trusted(&lobby, &h).await);
        let r = repos(&t.path);
        assert!(grant_plain_dir(&r.plain, &h).await.is_empty());
    }
}
