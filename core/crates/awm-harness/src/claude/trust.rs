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
//! rubber-stamped. A repository inside an awm project, such as a seat's folder, is one too. A shape of `~/.claude.json` this module does not recognise reads as untrusted.
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
    workspace_key_in(path.as_ref(), std::env::vars_os().collect::<Vec<_>>()).await
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

/// The trust entries a directory awm created needs: a worktree's `.bare` repository and itself,
/// or a repository inside a project and the directory itself. None for anything else.
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
    if key.file_name().is_none_or(|n| n != ".bare") {
        let project = key.strip_prefix(&root).ok().and_then(|rel| rel.components().next());
        if !project.is_some_and(|c| root.join(c).join(".bare").is_dir()) {
            return Vec::new();
        }
        return if key == p { vec![key] } else { vec![key, p] };
    }
    let Some(project) = key.parent() else {
        return Vec::new();
    };
    if project.parent() != Some(root.as_path()) {
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
    async fn trust_needs_an_accepted_entry_on_the_path() {
        let t = tmp();
        let h = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        assert!(trusted(&t.path, &h).await);
        assert!(trusted(t.path.join("a").join("b"), &h).await);
        let elsewhere = trust_file(&t.path, json!({ s(&t.path.join("other")): { ACCEPTED: true } }));
        assert!(!trusted(t.path.join("here"), &elsewhere).await);
        let declined = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: false } }));
        assert!(!trusted(&t.path, &declined).await);

        let missing = ClaudeHome::at(t.path.join(".claude"), t.path.join("missing.json"));
        assert!(!trusted(&t.path, &missing).await);
        let bad_file = t.path.join("bad.json");
        let bad = ClaudeHome::at(t.path.join(".claude"), &bad_file);
        for text in ["{", r#"{"projects": ["not", "a", "dict"]}"#, "[]"] {
            std::fs::write(&bad_file, text).unwrap();
            assert!(!trusted(&t.path, &bad).await, "{text}");
        }
    }

    /// The stricter rule for `claude --bg`: a git worktree is trusted through its repository's
    /// entry, never through an ancestor directory.
    #[tokio::test]
    async fn a_start_needs_the_repository_entry_not_an_ancestor() {
        let t = tmp();
        let r = repos(&t.path);
        let ancestor = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        assert!(!trusted(&r.wt, &ancestor).await);
        let why = start_refusal(&r.wt, &ancestor).await.expect("refused");
        assert!(why.contains(&s(&r.bare)) && why.contains("claude") && why.contains("accept"), "{why}");

        let own = trust_file(&t.path, json!({ s(&r.bare): { ACCEPTED: true } }));
        assert_eq!(start_refusal(&r.wt, &own).await, None);
        let declined = trust_file(&t.path, json!({ s(&r.bare): { ACCEPTED: false } }));
        assert!(start_refusal(&r.wt, &declined).await.is_some());
        let missing = ClaudeHome::at(t.path.join(".claude"), t.path.join("missing.json"));
        assert!(start_refusal(&r.wt, &missing).await.is_some());

        let sub = r.plain.join("sub");
        std::fs::create_dir(&sub).unwrap();
        let ancestor = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        let why = start_refusal(&sub, &ancestor).await.expect("refused");
        assert!(why.contains(&s(&r.plain)), "{why}");
        let plain = trust_file(&t.path, json!({ s(&r.plain): { ACCEPTED: true } }));
        assert_eq!(start_refusal(&r.plain, &plain).await, None);
        assert_eq!(start_refusal(&sub, &plain).await, None);

        assert_eq!(workspace_key(&r.wt).await, Some(r.bare.clone()));
        assert_eq!(workspace_key(&r.plain).await, Some(r.plain.clone()));
        // Inherited GIT_* variables must not redirect the lookup.
        let dot_git = r.plain.join(".git");
        let env = std::env::vars_os().chain([
            ("GIT_DIR".into(), dot_git.clone().into_os_string()),
            ("GIT_WORK_TREE".into(), r.plain.clone().into_os_string()),
            ("GIT_COMMON_DIR".into(), dot_git.clone().into_os_string()),
            ("GIT_INDEX_FILE".into(), dot_git.join("index").into_os_string()),
        ]);
        assert_eq!(workspace_key_in(&r.wt, env).await, Some(r.bare.clone()));

        let outside = t.path.join("notgit");
        std::fs::create_dir(&outside).unwrap();
        assert_eq!(workspace_key(&outside).await, None);
        let ancestor = trust_file(&t.path, json!({ s(&t.path): { ACCEPTED: true } }));
        assert_eq!(start_refusal(&outside, &ancestor).await, None);
        let empty = trust_file(&t.path, json!({}));
        let why = start_refusal(&outside, &empty).await.expect("refused");
        assert!(why.contains(&s(&outside)) && why.contains("claude"), "{why}");
    }

    #[tokio::test]
    async fn only_awm_worktrees_under_the_projects_root_are_granted() {
        let t = tmp();
        let r = repos(&t.path);
        let h = trust_file(&t.path, json!({ "/elsewhere": { ACCEPTED: true, "allowedTools": ["x"] } }));
        let added = grant_awm_worktree(&r.wt, &t.path, &h).await;
        assert_eq!(added, vec![s(&r.bare), s(&r.wt)]);
        assert_eq!(start_refusal(&r.wt, &h).await, None);
        assert_eq!(read(&h)["projects"]["/elsewhere"], json!({ ACCEPTED: true, "allowedTools": ["x"] }));
        assert!(grant_awm_worktree(&r.wt, &t.path, &h).await.is_empty(), "a second grant adds nothing");

        // A declined entry is flipped and keeps its other fields.
        let declined = trust_file(&t.path, json!({ s(&r.bare): { ACCEPTED: false, "mcpServers": { "a": 1 } } }));
        grant_awm_worktree(&r.wt, &t.path, &declined).await;
        assert_eq!(read(&declined)["projects"][s(&r.bare)], json!({ ACCEPTED: true, "mcpServers": { "a": 1 } }));

        // A repository inside a project, as a seat's folder is, is its own entry.
        let seat = r.bare.parent().unwrap().join("seats").join("ana");
        std::fs::create_dir_all(&seat).unwrap();
        git(&seat, &["init", "-q"]);
        let seated = trust_file(&t.path, json!({}));
        assert_eq!(grant_awm_worktree(&seat, &t.path, &seated).await, vec![s(&seat)]);
        assert_eq!(start_refusal(&seat, &seated).await, None);

        // Refused: outside the root, a plain repository outside any project, the project dir, the bare repo, a non-repo,
        // and a worktree of another project's repository.
        let h = trust_file(&t.path, json!({}));
        let stray = t.path.join("proj-b").join("scope");
        std::fs::create_dir(stray.parent().unwrap()).unwrap();
        git(&r.bare, &["worktree", "add", "-q", "-b", "feat/stray", &s(&stray)]);
        let outside = t.path.join("notgit");
        std::fs::create_dir(&outside).unwrap();
        assert!(grant_awm_worktree(&r.wt, t.path.join("other"), &h).await.is_empty());
        for dir in [&r.plain, &t.path.join("proj"), &r.bare, &outside, &stray] {
            assert!(grant_awm_worktree(dir, &t.path, &h).await.is_empty(), "{dir:?}");
        }
        assert_eq!(read(&h), json!({ "projects": {} }));
    }

    /// The rewrite keeps Python's bytes: key order, `json.dumps(indent=2)` escaping, file mode.
    #[tokio::test]
    async fn a_grant_rewrites_the_file_as_python_would() {
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
        assert_eq!(grant_awm_worktree(&r.wt, &t.path, &h).await, vec![s(&r.bare), s(&r.wt)]);
        let expected = format!(
            "{{\n  \"zeta\": 1,\n  \"projects\": {{\n    \"{bare}\": {{\n      \"allowedTools\": [],\n      \
             \"{ACCEPTED}\": true,\n      \"z\": {{}}\n    }},\n    \"/b\": {{\n      \"x\": true\n    }},\n    \
             \"{wt}\": {{\n      \"{ACCEPTED}\": true\n    }}\n  }},\n  \"alpha\": [\n    1,\n    2\n  ]\n}}",
            bare = s(&r.bare),
            wt = s(&r.wt)
        );
        assert_eq!(std::fs::read_to_string(&f).unwrap(), expected);
        assert_eq!(std::fs::metadata(&f).unwrap().permissions().mode() & 0o777, 0o640);
        let leftovers = std::fs::read_dir(&t.path)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".awm."))
            .count();
        assert_eq!(leftovers, 0);

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

        let lobby = t.path.join("lobby-\u{e9}");
        std::fs::create_dir(&lobby).unwrap();
        let h = trust_file(&t.path, json!({}));
        assert_eq!(grant_plain_dir(&lobby, &h).await, vec![s(&lobby)]);
        let text = std::fs::read_to_string(&h.trust_file).unwrap();
        assert!(text.is_ascii() && text.contains("lobby-\\u00e9"), "{text}");
        assert!(trusted(&lobby, &h).await);
        assert!(grant_plain_dir(&r.plain, &h).await.is_empty());
    }
}
