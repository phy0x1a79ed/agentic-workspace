//! Which worktree's sandbox a call may drive, and running its script.

use std::path::{Component, Path, PathBuf};

use serde_json::{json, Value};

pub const ACTIONS: [&str; 5] = ["start", "stop", "restart", "seed", "status"];
pub const SANDBOX: &str = "core/plugins/dev/sandbox/run.sh";

/// Python's `repr` of a string, which the refusal texts quote.
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `path` with symlinks resolved as far as it exists, and the rest appended lexically, as
/// Python's `Path.resolve()` does for a path that need not exist.
pub fn resolve(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut rest: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(real) = existing.canonicalize() {
            let mut out = real;
            for part in rest.iter().rev() {
                match Path::new(part).components().next() {
                    Some(Component::ParentDir) => {
                        out.pop();
                    }
                    Some(Component::CurDir) | None => {}
                    _ => out.push(part),
                }
            }
            return out;
        }
        let Some(last) = existing.components().next_back() else { return path.to_path_buf() };
        rest.push(last.as_os_str().to_os_string());
        if !existing.pop() {
            return path.to_path_buf();
        }
    }
}

/// The workspace's `projects/`, from `AWM_WORKSPACE`, else `~/agentic_workspace`.
pub fn projects_dir() -> PathBuf {
    let ws = std::env::var_os("AWM_WORKSPACE")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join("agentic_workspace"));
    resolve(&ws.join("projects"))
}

/// The sandbox script of `worktree`, an absolute path under `projects`, or the refusal text.
pub fn sandbox_script(worktree: &str, projects: &Path) -> Result<PathBuf, String> {
    if worktree.is_empty() || !Path::new(worktree).is_absolute() {
        return Err(format!("worktree must be an absolute path, got {}", py_repr(worktree)));
    }
    let root = resolve(Path::new(worktree));
    if root == projects || !root.starts_with(projects) {
        return Err(format!("worktree {} is not under {}", root.display(), projects.display()));
    }
    let script = root.join(SANDBOX);
    if !script.is_file() {
        return Err(format!("no dev sandbox at {}", script.display()));
    }
    Ok(script)
}

pub type RunFuture = std::pin::Pin<Box<dyn std::future::Future<Output = (i32, String, String)> + Send>>;

/// What runs the script: `(argv, cwd) -> (rc, stdout, stderr)`.
pub trait Runner: Send + Sync {
    fn run(&self, argv: Vec<String>, cwd: PathBuf) -> RunFuture;
}

pub struct Bash;

impl Runner for Bash {
    fn run(&self, argv: Vec<String>, cwd: PathBuf) -> RunFuture {
        Box::pin(async move {
            let mut cmd = tokio::process::Command::new(&argv[0]);
            cmd.args(&argv[1..]).current_dir(cwd).stdin(std::process::Stdio::null());
            // The script finds this binary for `sandbox seed` and `sandbox services`.
            if let Ok(me) = std::env::current_exe() {
                cmd.env("DEV_BIN", me);
            }
            match cmd.output().await {
                Ok(o) => (
                    o.status.code().unwrap_or(-1),
                    String::from_utf8_lossy(&o.stdout).into_owned(),
                    String::from_utf8_lossy(&o.stderr).into_owned(),
                ),
                Err(e) => (-1, String::new(), format!("could not run bash: {e}")),
            }
        })
    }
}

/// Run `bash run.sh <action>` for `worktree`'s sandbox and answer `{action, rc, stdout, stderr}`,
/// or `{action, rc: 2, error}` for a call it refuses.
pub async fn run(action: &str, worktree: &str, projects: &Path, runner: &dyn Runner) -> Value {
    if !ACTIONS.contains(&action) {
        return json!({"action": action, "rc": 2, "error": format!("unknown dev action {}", py_repr(action))});
    }
    let script = match sandbox_script(worktree, projects) {
        Ok(s) => s,
        Err(error) => return json!({"action": action, "rc": 2, "error": error}),
    };
    let cwd = script.parent().expect("the script has a folder").to_path_buf();
    let (rc, stdout, stderr) = runner.run(vec!["bash".into(), script.display().to_string(), action.into()], cwd).await;
    json!({"action": action, "rc": rc, "stdout": stdout, "stderr": stderr})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    type Seen = Arc<Mutex<Option<(Vec<String>, PathBuf)>>>;

    struct Fake(Seen);

    impl Runner for Fake {
        fn run(&self, argv: Vec<String>, cwd: PathBuf) -> RunFuture {
            *self.0.lock().unwrap() = Some((argv, cwd));
            Box::pin(async { (0, "[dev] ok\n".to_string(), String::new()) })
        }
    }

    fn workspace() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let tree = tmp.path().join("projects/awm/web-ui");
        let script = tree.join(SANDBOX);
        std::fs::create_dir_all(script.parent().unwrap()).unwrap();
        std::fs::write(&script, "#!/usr/bin/env bash\n").unwrap();
        let projects = resolve(&tmp.path().join("projects"));
        (tmp, resolve(&tree), projects)
    }

    #[tokio::test]
    async fn runs_the_named_worktrees_sandbox() {
        let (_tmp, tree, projects) = workspace();
        let seen = Arc::new(Mutex::new(None));
        let out = run("restart", tree.to_str().unwrap(), &projects, &Fake(seen.clone())).await;
        assert_eq!(out, json!({"action": "restart", "rc": 0, "stdout": "[dev] ok\n", "stderr": ""}));
        let (argv, cwd) = seen.lock().unwrap().clone().unwrap();
        assert_eq!(argv, vec!["bash".to_string(), tree.join(SANDBOX).display().to_string(), "restart".into()]);
        assert_eq!(cwd, tree.join(SANDBOX).parent().unwrap());
    }

    #[tokio::test]
    async fn refuses_a_worktree_outside_the_workspace() {
        let (_tmp, _tree, projects) = workspace();
        for (w, why) in [("", "absolute"), ("projects/awm/web-ui", "absolute"), ("/etc", "not under")] {
            let seen = Arc::new(Mutex::new(None));
            let out = run("start", w, &projects, &Fake(seen.clone())).await;
            assert_eq!(out["rc"], 2);
            assert!(out["error"].as_str().unwrap().contains(why), "{out}");
            assert!(seen.lock().unwrap().is_none(), "ran a script");
        }
        let out = run("start", "projects/x", &projects, &Fake(Default::default())).await;
        assert_eq!(out["error"], "worktree must be an absolute path, got 'projects/x'");
    }

    #[tokio::test]
    async fn refuses_a_worktree_without_a_sandbox() {
        let (_tmp, tree, projects) = workspace();
        let bare = tree.parent().unwrap().join("bare");
        std::fs::create_dir_all(&bare).unwrap();
        let out = run("start", bare.to_str().unwrap(), &projects, &Fake(Default::default())).await;
        assert_eq!(out["rc"], 2);
        assert!(out["error"].as_str().unwrap().contains("no dev sandbox"));
        let missing = tree.parent().unwrap().join("nowhere/deeper");
        let out = run("start", missing.to_str().unwrap(), &projects, &Fake(Default::default())).await;
        assert!(out["error"].as_str().unwrap().contains("no dev sandbox"), "{out}");
    }

}
